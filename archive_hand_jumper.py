"""
Hand Jumper Webtoon Archiver
============================
Downloads all free episodes of Hand Jumper (title_no=2702) from Webtoons,
extracts the panel images from each episode, and saves each panel as a
high-quality WebP file (quality 92).

Architecture (two-phase pipeline for maximum throughput):
    Phase 1 — Fetch all episode viewer pages concurrently, extract panel URLs.
    Phase 2 — Download ALL panels concurrently across ALL episodes.
    Phase 3 — Retry any failures with exponential backoff.

New episodes are auto-discovered: the archiver probes beyond the last known
episode until it hits consecutive 404s, so no manual update is ever needed.

Usage:
    python archive_hand_jumper.py

Requirements:
    pip install requests beautifulsoup4 pillow

Output:
    episodes/{episode_no}/{panel_number_3_digits}.webp
    e.g. episodes/24/007.webp
"""

import io
import json
import logging
import os
import re
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup
from PIL import Image
from requests.adapters import HTTPAdapter

Image.MAX_IMAGE_PIXELS = None  # Disable decompression bomb limit

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

TITLE_NO = 2702
MAX_CONSECUTIVE_MISSES = 3  # stop probing after this many 404s in a row
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "episodes")

VIEWER_URL = (
    "https://www.webtoons.com/en/thriller/hand-jumper/"
    "episode/viewer?title_no={title_no}&episode_no={episode_no}"
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": "https://www.webtoons.com/",
}

IMAGE_HEADERS = {
    "User-Agent": HEADERS["User-Agent"],
    "Accept-Language": HEADERS["Accept-Language"],
    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
}

# --- Concurrency tuning ---
CONCURRENT_PAGES = 5       # Phase 1: concurrent episode page fetches
CONCURRENT_PANELS = 20     # Phase 2: concurrent panel downloads (global)

# --- Connection pool (per-thread session, sized to match concurrency) ---
POOL_CONNECTIONS = 25
POOL_MAXSIZE = 25

# --- Retry ---
MAX_RETRIES = 5
RETRY_DELAY = 3            # base seconds before retry (multiplied by attempt)
RETRY_PASSES = 2           # additional full passes over failed panels

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Thread-safe HTTP sessions
# ---------------------------------------------------------------------------

_thread_local = threading.local()


def _get_session():
    """Return a per-thread requests.Session with a large connection pool."""
    if not hasattr(_thread_local, "session"):
        s = requests.Session()
        s.headers.update(HEADERS)
        adapter = HTTPAdapter(
            pool_connections=POOL_CONNECTIONS,
            pool_maxsize=POOL_MAXSIZE,
        )
        s.mount("https://", adapter)
        s.mount("http://", adapter)
        _thread_local.session = s
    return _thread_local.session


def fetch_with_retry(url, extra_headers=None):
    """GET a URL with retries and exponential backoff."""
    headers = dict(HEADERS)
    if extra_headers:
        headers.update(extra_headers)

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = _get_session().get(url, headers=headers, timeout=30)
            resp.raise_for_status()
            return resp
        except requests.RequestException as exc:
            if attempt < MAX_RETRIES:
                wait = RETRY_DELAY * attempt
                log.warning(
                    "Attempt %d/%d failed for %s: %s  (retry in %ds)",
                    attempt, MAX_RETRIES, url[:90], exc, wait,
                )
                time.sleep(wait)
            else:
                log.error("All %d retries exhausted for: %s", MAX_RETRIES, url[:90])
    raise RuntimeError("All %d retries failed for: %s" % (MAX_RETRIES, url))


# ---------------------------------------------------------------------------
# Quality helpers
# ---------------------------------------------------------------------------


def maximize_image_url(url):
    """Strip CDN quality-reduction query params to fetch the raw original."""
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    params.pop("type", None)
    new_query = urlencode(params, doseq=True)
    return urlunparse(parsed._replace(query=new_query))


# ---------------------------------------------------------------------------
# Thread-safe progress tracker
# ---------------------------------------------------------------------------


class Progress:
    """Atomic progress counter shared across all download threads."""

    def __init__(self, total):
        self.total = total
        self.done = 0
        self.ok = 0
        self.skipped = 0
        self.failed = 0
        self._lock = threading.Lock()

    def record(self, status):
        with self._lock:
            self.done += 1
            if status == "ok":
                self.ok += 1
            elif status == "skipped":
                self.skipped += 1
            else:
                self.failed += 1
            if self.done % 100 == 0 or self.done == self.total:
                pct = 100.0 * self.done / self.total
                log.info(
                    "    ⏩ %d/%d (%.0f%%)  |  %d saved  %d skipped  %d failed",
                    self.done, self.total, pct,
                    self.ok, self.skipped, self.failed,
                )


# ---------------------------------------------------------------------------
# Phase 1: Episode metadata extraction
# ---------------------------------------------------------------------------


def _extract_episode_tasks(episode_no):
    """
    Fetch one episode's viewer page and return a list of panel download tasks.

    Each task is a tuple:
        (episode_no, panel_index, total_panels, image_url, page_url)
    """
    try:
        page_url = VIEWER_URL.format(title_no=TITLE_NO, episode_no=episode_no)
        resp = fetch_with_retry(
            page_url,
            extra_headers={
                "Referer": (
                    "https://www.webtoons.com/en/thriller/hand-jumper/"
                    "list?title_no=%d" % TITLE_NO
                ),
            },
        )

        soup = BeautifulSoup(resp.text, "html.parser")

        container = soup.find(id="_imageList")
        if container is None:
            log.warning("Ep %03d: #_imageList not found; global search.", episode_no)
            img_tags = soup.find_all("img", class_="_images")
        else:
            img_tags = container.find_all("img", class_="_images")

        urls = []
        for tag in img_tags:
            data_url = tag.get("data-url", "").strip()
            if data_url and "pstatic.net" in data_url:
                urls.append(maximize_image_url(data_url))

        if not urls:
            log.error("Ep %03d: zero panel URLs found.", episode_no)
            return []

        total = len(urls)
        log.info("Ep %03d: %d panels found.", episode_no, total)
        return [
            (episode_no, i, total, url, page_url)
            for i, url in enumerate(urls, 1)
        ]

    except Exception as exc:
        log.error("Ep %03d: metadata fetch failed: %s", episode_no, exc)
        return []


def _discover_total_episodes():
    """
    Auto-discover the total number of available episodes by probing.
    Starts from the number of existing episode folders on disk (so nightly
    runs only probe a few new episodes instead of re-checking all 126+),
    then probes forward until MAX_CONSECUTIVE_MISSES consecutive misses.
    """
    # Count existing episode folders to avoid re-probing from 1 every time
    known = 0
    if os.path.isdir(OUTPUT_DIR):
        known = len([
            d for d in os.listdir(OUTPUT_DIR)
            if os.path.isdir(os.path.join(OUTPUT_DIR, d)) and d.isdigit()
        ])

    start = max(1, known)  # start at last known (or 1 if empty)
    log.info(
        "Discovering episodes (known on disk: %d, probing from ep %d)...",
        known, start,
    )
    ep = start
    consecutive_misses = 0
    last_valid = known  # assume all existing folders are valid

    while consecutive_misses < MAX_CONSECUTIVE_MISSES:
        try:
            page_url = VIEWER_URL.format(title_no=TITLE_NO, episode_no=ep)
            resp = _get_session().get(page_url, headers=HEADERS, timeout=15)
            if resp.status_code == 200 and "_imageList" in resp.text:
                last_valid = ep
                consecutive_misses = 0
            else:
                consecutive_misses += 1
        except requests.RequestException:
            consecutive_misses += 1
        ep += 1

    log.info("Discovered %d total episodes.", last_valid)
    return last_valid


def _load_known_completion():
    """
    Reads data.js to find how many panels each episode is supposed to have.
    Returns a dict mapping episode_no (int) -> expected_panels (int).
    """
    data_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.js")
    if not os.path.exists(data_path):
        return {}
    try:
        with open(data_path, "r", encoding="utf-8") as f:
            content = f.read()
        # Extract the JSON object from `const comicData = {...};`
        match = re.search(r'const comicData\s*=\s*({.*?});', content, re.DOTALL)
        if match:
            data = json.loads(match.group(1))
            return {int(k): int(v) for k, v in data.items()}
    except Exception as e:
        log.warning("Could not parse data.js for known panel counts: %s", e)
    return {}


def fetch_all_episode_tasks():
    """
    Phase 1: Concurrently fetch all episode viewer pages and return the
    complete flat list of panel download tasks.
    """
    total_episodes = _discover_total_episodes()
    if total_episodes == 0:
        log.error("Could not discover any episodes.")
        return [], []

    log.info(
        "PHASE 1 ▸ Checking %d episodes ...",
        total_episodes,
    )
    t0 = time.monotonic()

    all_tasks = []
    failed_episodes = []
    known_counts = _load_known_completion()
    episodes_to_fetch = []

    for ep in range(1, total_episodes + 1):
        ep_dir = os.path.join(OUTPUT_DIR, str(ep))
        expected = known_counts.get(ep, -1)
        # If the directory exists, count valid webp files
        if expected > 0 and os.path.isdir(ep_dir):
            actual = len([f for f in os.listdir(ep_dir) if f.endswith(".webp") and os.path.getsize(os.path.join(ep_dir, f)) > 0])
            if actual == expected:
                log.debug("Ep %03d: completely downloaded (%d panels). Skipping HTML fetch.", ep, actual)
                continue
        episodes_to_fetch.append(ep)

    log.info("Need to fetch HTML for %d incomplete/new episodes.", len(episodes_to_fetch))

    with ThreadPoolExecutor(max_workers=CONCURRENT_PAGES) as pool:
        future_to_ep = {
            pool.submit(_extract_episode_tasks, ep): ep
            for ep in episodes_to_fetch
        }
        for future in as_completed(future_to_ep):
            ep = future_to_ep[future]
            try:
                tasks = future.result()
                if tasks:
                    all_tasks.extend(tasks)
                else:
                    failed_episodes.append(ep)
            except Exception as exc:
                log.error("Ep %03d: unexpected error: %s", ep, exc)
                failed_episodes.append(ep)

    elapsed = time.monotonic() - t0
    log.info(
        "PHASE 1 ▸ Done in %.1fs — %d panels across %d episodes (%d episodes failed).",
        elapsed, len(all_tasks),
        total_episodes - len(failed_episodes), len(failed_episodes),
    )
    return all_tasks, sorted(failed_episodes)


# ---------------------------------------------------------------------------
# Phase 2: Panel downloading
# ---------------------------------------------------------------------------


def _download_and_save_panel(task, progress=None):
    """
    Download and save a single panel with atomic file write.

    Writes to a temp file first, then atomically renames to the final path.
    This guarantees no corrupt/partial files on disk from interrupted runs.

    Returns (task, 'ok' | 'skipped' | 'failed').
    """
    episode_no, panel_index, total_panels, url, page_url = task
    ep_dir = os.path.join(OUTPUT_DIR, str(episode_no))
    panel_path = os.path.join(ep_dir, "%03d.webp" % panel_index)

    # Skip panels that already exist and are non-empty
    if os.path.exists(panel_path) and os.path.getsize(panel_path) > 0:
        if progress:
            progress.record("skipped")
        return (task, "skipped")

    try:
        os.makedirs(ep_dir, exist_ok=True)

        # Download
        resp = fetch_with_retry(
            url,
            extra_headers={**IMAGE_HEADERS, "Referer": page_url},
        )
        img = Image.open(io.BytesIO(resp.content))
        img.load()

        # Preserve transparency if present; otherwise RGB
        if img.mode in ("RGBA", "LA", "PA") or (
            img.mode == "P" and "transparency" in img.info
        ):
            img = img.convert("RGBA")
        else:
            img = img.convert("RGB")

        # Capture dimensions before close
        w, h = img.size

        # Atomic save: temp file → rename
        fd, tmp_path = tempfile.mkstemp(suffix=".tmp.webp", dir=ep_dir)
        try:
            os.close(fd)
            img.save(tmp_path, format="WEBP", quality=92, method=4)
            img.close()
            os.replace(tmp_path, panel_path)  # Atomic on Windows and Unix
        except BaseException:
            # Clean up temp file on ANY error (including KeyboardInterrupt)
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

        log.debug(
            "  Ep %03d panel %03d/%03d: saved (%d×%d px)",
            episode_no, panel_index, total_panels, w, h,
        )

        if progress:
            progress.record("ok")
        return (task, "ok")

    except Exception as exc:
        log.error(
            "  Ep %03d panel %03d/%03d: FAILED — %s",
            episode_no, panel_index, total_panels, exc,
        )
        if progress:
            progress.record("failed")
        return (task, "failed")


def download_all_panels(tasks):
    """
    Phase 2: Download all panels concurrently.
    Returns list of tasks that failed.
    """
    if not tasks:
        return []

    log.info(
        "PHASE 2 ▸ Downloading %d panels (%d concurrent threads) ...",
        len(tasks), CONCURRENT_PANELS,
    )
    t0 = time.monotonic()
    progress = Progress(len(tasks))
    failed = []

    with ThreadPoolExecutor(max_workers=CONCURRENT_PANELS) as pool:
        future_to_task = {
            pool.submit(_download_and_save_panel, task, progress): task
            for task in tasks
        }
        for future in as_completed(future_to_task):
            try:
                task, status = future.result()
                if status == "failed":
                    failed.append(task)
            except Exception as exc:
                task = future_to_task[future]
                log.error("Ep %03d panel %03d: unexpected error: %s",
                          task[0], task[1], exc)
                failed.append(task)

    elapsed = time.monotonic() - t0
    rate = (len(tasks) - len(failed)) / elapsed if elapsed > 0 else 0
    log.info(
        "PHASE 2 ▸ Done in %.1fs (%.1f panels/sec). %d failed.",
        elapsed, rate, len(failed),
    )
    return failed


def retry_failed(failed_tasks):
    """
    Phase 3: Retry failed downloads with exponential backoff
    and reduced concurrency.
    """
    remaining = list(failed_tasks)

    for pass_num in range(1, RETRY_PASSES + 1):
        if not remaining:
            break

        backoff = RETRY_DELAY * (2 ** (pass_num - 1))
        workers = max(1, CONCURRENT_PANELS // (2 * pass_num))

        log.info(
            "RETRY %d ▸ %d panels to retry (backoff %ds, %d threads) ...",
            pass_num, len(remaining), backoff, workers,
        )
        time.sleep(backoff)

        progress = Progress(len(remaining))
        still_failing = []

        with ThreadPoolExecutor(max_workers=workers) as pool:
            future_to_task = {
                pool.submit(_download_and_save_panel, task, progress): task
                for task in remaining
            }
            for future in as_completed(future_to_task):
                try:
                    task, status = future.result()
                    if status == "failed":
                        still_failing.append(task)
                except Exception:
                    still_failing.append(future_to_task[future])

        remaining = still_failing
        log.info("RETRY %d ▸ %d still failing.", pass_num, len(remaining))

    return remaining


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    log.info("=" * 60)
    log.info("  Hand Jumper Archiver — MAXIMUM QUALITY & SPEED")
    log.info("=" * 60)
    log.info("Title:       Hand Jumper (title_no=%d)", TITLE_NO)
    log.info("Episodes:    auto-discovery (probe beyond known count)")
    log.info("Output:      %s", OUTPUT_DIR)
    log.info("Quality:     WebP q92, no CDN compression, original resolution")
    log.info("Concurrency: %d page fetches, %d panel downloads",
             CONCURRENT_PAGES, CONCURRENT_PANELS)
    log.info("-" * 60)

    t_start = time.monotonic()

    # Phase 1: Fetch all episode metadata
    all_tasks, failed_episodes = fetch_all_episode_tasks()

    if not all_tasks:
        log.error("No panels to download. Exiting.")
        return

    # Phase 2: Download all panels
    failed_tasks = download_all_panels(all_tasks)

    # Phase 3: Retry failures
    if failed_tasks:
        failed_tasks = retry_failed(failed_tasks)

    # Summary
    total_time = time.monotonic() - t_start
    total_panels = len(all_tasks)
    permanently_failed = len(failed_tasks)
    succeeded = total_panels - permanently_failed

    log.info("=" * 60)
    log.info("  COMPLETE — %.1f seconds total", total_time)
    log.info("=" * 60)
    log.info("Panels: %d/%d succeeded (%.1f%%)",
             succeeded, total_panels,
             100.0 * succeeded / total_panels if total_panels else 0)

    if failed_episodes:
        log.warning("Episodes with no panels fetched: %s", failed_episodes)

    if failed_tasks:
        log.warning(
            "Permanently failed panels (%d):", permanently_failed,
        )
        for t in sorted(failed_tasks):
            log.warning("  Episode %d, panel %03d", t[0], t[1])
        log.info("Re-run to retry — existing panels are skipped automatically.")
    else:
        log.info("🎉 All panels saved successfully!")

    if total_time > 0:
        log.info("Average speed: %.1f panels/sec", succeeded / total_time)


if __name__ == "__main__":
    main()
