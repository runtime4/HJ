"""Hand Jumper archiver.

Downloads every free episode of Hand Jumper from Webtoons and saves each
panel as a WebP image, converting the instant it's downloaded (never in a
separate pass). Also writes map/{episode_no}.json recording the episode's
real title and every panel's pixel dimensions, which the frontend depends
on for a seam-free layout, and rebuilds map/manifest.json — an aggregate
index of every archived episode's title and panel count.

Output:
    episodes/{episode_no}/{panel_index:03d}.webp
    map/{episode_no}.json   # title, panel count, per-panel pixel dimensions
    map/manifest.json       # title + panel count for every episode, in order

Usage:
    python build/archive.py

Requirements:
    pip install -r build/requirements.txt

Re-running is safe and cheap: episodes with a matching map/{n}.json and all
their panel files on disk are skipped. One episode-list request discovers
the latest public episode, then each incomplete episode page is fetched
exactly once.
"""

from __future__ import annotations

import io
import json
import logging
import os
import tempfile
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests
from bs4 import BeautifulSoup
from PIL import Image
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

Image.MAX_IMAGE_PIXELS = None  # panels are tall; disable the decompression-bomb guard

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

TITLE_NO = 2702
COMIC_PATH = "thriller/hand-jumper"
VIEWER_URL = (
    f"https://www.webtoons.com/en/{COMIC_PATH}/episode/viewer"
    f"?title_no={TITLE_NO}&episode_no={{episode_no}}"
)
LIST_URL = f"https://www.webtoons.com/en/{COMIC_PATH}/list?title_no={TITLE_NO}"

ROOT_DIR = Path(__file__).resolve().parent.parent
EPISODES_DIR = ROOT_DIR / "episodes"
MAP_DIR = ROOT_DIR / "map"
MANIFEST_PATH = MAP_DIR / "manifest.json"

WEBP_LOSSLESS = True   # TRUE lossless WebP — perfect quality, no generation loss
# Note: In lossless mode, quality/method affect compression effort, not visual quality --
# every setting below is bit-for-bit lossless. quality=100 and method=6 together squeeze
# the smallest possible file out of libwebp; quality alone (method defaults to 4 in Pillow)
# leaves real compression on the table. Slower to encode -- time budget is not a concern here.
WEBP_QUALITY = 100
WEBP_METHOD  = 6   # 0 (fast/large) .. 6 (slow/smallest) -- Pillow defaults to 4 if unset

# All panels share the same native width (800 px) — no rescaling needed.

CONCURRENT_PAGES = 8       # episode-page fetches — main site rate-limits aggressively
CONCURRENT_PANELS = 128    # panel downloads in flight at once, globally
RETRY_CONCURRENT_PANELS = 16
PANEL_RETRY_ROUNDS = 3     # app-level retry rounds for failed panels
PANEL_RETRY_BASE_DELAY = 3 # seconds — multiplied by round number for backoff
PAGE_REQUEST_TIMEOUT = (5, 20)   # (connect, read) seconds — main-site HTML pages
IMAGE_REQUEST_TIMEOUT = (5, 20)  # (connect, read) seconds — CDN panel images ~335 KB
HTTP_RETRIES = 4           # transport-level retries with backoff per request
PAGE_FETCH_ATTEMPTS = 3    # app-level attempts for each episode page fetch
LIST_FETCH_ATTEMPTS = 3    # app-level attempts for the episode-list request

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
PAGE_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml",
    "Referer": LIST_URL,
}
IMAGE_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "image/avif,image/webp,image/*,*/*;q=0.8",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("archive")


class ArchiveError(RuntimeError):
    """A failure that makes the archive run incomplete or unsafe to publish."""


@dataclass(frozen=True)
class Panel:
    episode_no: int
    index: int  # 1-based
    url: str
    referer: str


@dataclass
class ArchivedPanel:
    index: int
    width: int
    height: int


# ---------------------------------------------------------------------------
# HTTP session — one per thread, pooled, with transport-level retry/backoff
# ---------------------------------------------------------------------------

_thread_state = threading.local()


def http_session() -> requests.Session:
    """Return this thread's session.  Transport-level retries (with backoff,
    honouring the server's Retry-After on 429s) happen transparently so
    callers don't need their own retry loops for transient errors."""
    client = getattr(_thread_state, "session", None)
    if client is None:
        client = requests.Session()
        retry_policy = Retry(
            total=HTTP_RETRIES,
            backoff_factor=1.5,
            status_forcelist=(429, 500, 502, 503, 504),
            respect_retry_after_header=True,
            allowed_methods=("GET",),
            raise_on_status=False,
        )
        transport = HTTPAdapter(
            pool_connections=CONCURRENT_PANELS,
            pool_maxsize=CONCURRENT_PANELS,
            max_retries=retry_policy,
            pool_block=False,
        )
        client.mount("https://", transport)
        client.mount("http://", transport)
        _thread_state.session = client
    return client


# ---------------------------------------------------------------------------
# Startup cleanup — remove orphaned temp files from prior crashed runs
# ---------------------------------------------------------------------------


def cleanup_temp_files() -> None:
    """Remove stale temp files left by interrupted downloads or JSON writes."""
    cleaned = 0
    # Orphaned panel temp files in episode directories (tempfile.mkstemp
    # produces names like tmpXXXXXXXX.webp or similar platform-specific
    # patterns, so match any file that is NOT a valid 3-digit panel name).
    if EPISODES_DIR.is_dir():
        for webp_file in EPISODES_DIR.rglob("*.webp"):
            if not webp_file.stem.isdigit():
                try:
                    webp_file.unlink()
                    cleaned += 1
                except OSError:
                    pass
    # Orphaned JSON temp files in map directory
    if MAP_DIR.is_dir():
        for tmp_file in MAP_DIR.glob("*.tmp"):
            try:
                tmp_file.unlink()
                cleaned += 1
            except OSError:
                pass
    if cleaned:
        log.info("Cleaned %d orphaned temp files.", cleaned)


# ---------------------------------------------------------------------------
# Episode page parsing
# ---------------------------------------------------------------------------


def extract_episode_title(document: BeautifulSoup) -> str | None:
    """Pull the real episode title from the page.  Selector order is a
    best-effort chain; ``h1.subj_episode`` is confirmed against live markup,
    the rest are untested fallbacks in case that markup ever changes."""
    for selector in ("h1.subj_episode", 'meta[property="og:title"]', "title"):
        tag = document.select_one(selector)
        if tag is None:
            continue
        raw = tag.get("content") if tag.name == "meta" else tag.get_text()
        if not isinstance(raw, str):
            continue
        title = " ".join(raw.split())
        if title:
            return title
    return None


def parse_episode_page(episode_no: int) -> tuple[str, list[str]] | None:
    """Return (title, panel_urls) for *episode_no*, or ``None`` for a 404.

    Transport failures and unexpected markup raise :class:`ArchiveError` so
    they can never be mistaken for an episode that hasn't been published.
    Retries ``PAGE_FETCH_ATTEMPTS`` times with linear backoff.
    """
    viewer_url = VIEWER_URL.format(episode_no=episode_no)
    last_error: Exception | None = None

    for attempt in range(1, PAGE_FETCH_ATTEMPTS + 1):
        try:
            response = http_session().get(
                viewer_url, headers=PAGE_HEADERS, timeout=PAGE_REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            last_error = exc
            if attempt < PAGE_FETCH_ATTEMPTS:
                time.sleep(2 * attempt)
                continue
            raise ArchiveError(
                f"Ep {episode_no}: page request failed after "
                f"{PAGE_FETCH_ATTEMPTS} attempts: {exc}"
            ) from exc

        if response.status_code == 404:
            return None

        try:
            response.raise_for_status()
        except requests.RequestException as exc:
            last_error = exc
            if attempt < PAGE_FETCH_ATTEMPTS:
                time.sleep(2 * attempt)
                continue
            raise ArchiveError(
                f"Ep {episode_no}: HTTP {response.status_code} after "
                f"{PAGE_FETCH_ATTEMPTS} attempts: {exc}"
            ) from exc

        # Good response — parse it below.
        break
    else:
        # Shouldn't be reachable (the exception paths above always raise on
        # the final attempt), but guard against it anyway.
        raise ArchiveError(
            f"Ep {episode_no}: exhausted {PAGE_FETCH_ATTEMPTS} attempts: "
            f"{last_error}"
        )

    document = BeautifulSoup(response.text, "html.parser")

    episode_title = extract_episode_title(document)
    if episode_title is None:
        raise ArchiveError(f"Ep {episode_no}: episode title not found")

    image_list = document.find(id="_imageList")
    if image_list is None:
        raise ArchiveError(f"Ep {episode_no}: panel container not found")

    panel_urls: list[str] = []
    for img in image_list.find_all("img", class_="_images"):
        src = img.get("data-url")
        if isinstance(src, str) and src.strip():
            panel_urls.append(src.strip())

    if not panel_urls:
        raise ArchiveError(f"Ep {episode_no}: no panel URLs found")

    return episode_title, panel_urls


def discover_latest_episode() -> int:
    """Fetch the first page of the public episode list and return the highest
    episode number linked on it.  Retries ``LIST_FETCH_ATTEMPTS`` times."""
    last_error: Exception | None = None

    for attempt in range(1, LIST_FETCH_ATTEMPTS + 1):
        try:
            response = http_session().get(
                LIST_URL, headers=PAGE_HEADERS, timeout=PAGE_REQUEST_TIMEOUT,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            last_error = exc
            if attempt < LIST_FETCH_ATTEMPTS:
                time.sleep(2 * attempt)
                continue
            raise ArchiveError(
                f"Episode list request failed after {LIST_FETCH_ATTEMPTS} "
                f"attempts: {exc}"
            ) from exc

        # Good response.
        break
    else:
        raise ArchiveError(
            f"Episode list request exhausted {LIST_FETCH_ATTEMPTS} attempts: "
            f"{last_error}"
        )

    document = BeautifulSoup(response.text, "html.parser")
    episode_numbers: set[int] = set()
    for link in document.select('a[href*="episode_no="]'):
        href = link.get("href")
        if not isinstance(href, str):
            continue
        values = parse_qs(urlparse(href).query).get("episode_no")
        if values and values[0].isdigit():
            episode_numbers.add(int(values[0]))

    if not episode_numbers:
        raise ArchiveError("Episode list did not contain any viewer links")

    latest = max(episode_numbers)
    log.info("Discovered %d episodes (latest: %d).", len(episode_numbers), latest)
    return latest


# ---------------------------------------------------------------------------
# Metadata helpers
# ---------------------------------------------------------------------------


def load_episode_metadata(episode_no: int) -> dict | None:
    """Load and fully validate episode metadata.

    Returns the parsed dict only when:
    - the JSON schema matches expectations,
    - ``panelCount`` equals ``len(panels)``,
    - every referenced panel file exists on disk with size > 0.
    """
    metadata_path = MAP_DIR / f"{episode_no}.json"
    episode_dir   = EPISODES_DIR / str(episode_no)
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None

    if not isinstance(metadata, dict):
        return None
    if metadata.get("episode") != episode_no:
        return None
    if not isinstance(metadata.get("title"), str) or not metadata["title"].strip():
        return None

    panels      = metadata.get("panels")
    panel_count = metadata.get("panelCount")
    if not isinstance(panels, list) or not panels:
        return None
    if not isinstance(panel_count, int) or panel_count != len(panels):
        return None

    for panel_index, panel in enumerate(panels, start=1):
        if not isinstance(panel, dict):
            return None
        filename = panel.get("file")
        width    = panel.get("width")
        height   = panel.get("height")
        if not isinstance(filename, str) or filename != f"{panel_index:03d}.webp":
            return None
        if not isinstance(width, int) or isinstance(width, bool) or width <= 0:
            return None
        if not isinstance(height, int) or isinstance(height, bool) or height <= 0:
            return None
        panel_path = episode_dir / filename
        try:
            if not panel_path.is_file() or panel_path.stat().st_size == 0:
                return None
        except OSError:
            return None

    return metadata


def is_episode_complete(episode_no: int) -> bool:
    return load_episode_metadata(episode_no) is not None


# ---------------------------------------------------------------------------
# Panel download + WebP conversion
# ---------------------------------------------------------------------------


def download_panel(panel: Panel, episode_dir: Path) -> ArchivedPanel | None:
    """Download one panel and convert it to an atomic lossless WebP file.

    Returns ``ArchivedPanel`` on success, ``None`` on failure (logged).
    Existing valid files are returned immediately without re-downloading.
    """
    panel_path = episode_dir / f"{panel.index:03d}.webp"

    try:
        # --- Fast path: already on disk ----------------------------------
        if panel_path.is_file() and panel_path.stat().st_size > 0:
            try:
                with Image.open(panel_path) as img:
                    width, height = img.size
                return ArchivedPanel(panel.index, width, height)
            except OSError:
                log.warning(
                    "Ep %d panel %03d: corrupt file on disk — re-downloading.",
                    panel.episode_no,
                    panel.index,
                )
                panel_path.unlink(missing_ok=True)

        # --- Download ----------------------------------------------------
        response = http_session().get(
            panel.url,
            headers={**IMAGE_HEADERS, "Referer": panel.referer},
            timeout=IMAGE_REQUEST_TIMEOUT,
        )
        response.raise_for_status()

        # Guard: CDN edge errors sometimes arrive as 200 with an HTML body.
        content_type = response.headers.get("Content-Type", "")
        if content_type and not content_type.startswith("image/"):
            raise ArchiveError(
                f"unexpected Content-Type {content_type!r} "
                f"({len(response.content)} bytes)"
            )

        # Guard: detect truncated transfers.
        raw_cl = response.headers.get("Content-Length")
        if raw_cl is not None:
            try:
                expected = int(raw_cl)
            except (ValueError, OverflowError):
                expected = None
            if expected is not None and len(response.content) != expected:
                raise ArchiveError(
                    f"truncated: expected {expected} bytes, "
                    f"got {len(response.content)}"
                )

        # --- Decode & convert --------------------------------------------
        with Image.open(io.BytesIO(response.content)) as src:
            if src.mode in ("RGBA", "LA", "PA") or (
                src.mode == "P" and "transparency" in src.info
            ):
                converted = src.convert("RGBA")
            else:
                converted = src.convert("RGB")

            width, height = converted.size

            # --- Atomic write --------------------------------------------
            episode_dir.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(suffix=".webp", dir=episode_dir)
            tmp_path = Path(tmp_name)
            try:
                os.close(fd)
                converted.save(
                    tmp_path,
                    format="WEBP",
                    lossless=WEBP_LOSSLESS,
                    quality=WEBP_QUALITY,
                    method=WEBP_METHOD,
                )
                tmp_path.replace(panel_path)
            except BaseException:
                tmp_path.unlink(missing_ok=True)
                raise
            finally:
                converted.close()

        return ArchivedPanel(panel.index, width, height)

    except Exception as exc:  # noqa: BLE001 — isolate one panel's failure
        log.error(
            "Ep %d panel %03d: download failed: %s",
            panel.episode_no,
            panel.index,
            exc,
        )
        return None


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def fetch_episode_pages(
    episode_numbers: list[int],
) -> tuple[dict[int, tuple[str, list[str]]], list[int]]:
    """Concurrently fetch episode pages; report every failure."""
    episode_pages: dict[int, tuple[str, list[str]]] = {}
    failed: list[int] = []

    with ThreadPoolExecutor(max_workers=CONCURRENT_PAGES) as pool:
        futures = {
            pool.submit(parse_episode_page, n): n for n in episode_numbers
        }
        for future in as_completed(futures):
            ep = futures[future]
            try:
                result = future.result()
            except ArchiveError as exc:
                log.error("%s", exc)
                failed.append(ep)
                continue
            except Exception as exc:  # noqa: BLE001
                log.error("Ep %d: unexpected page error: %s", ep, exc)
                failed.append(ep)
                continue

            if result is None:
                log.error("Ep %d: linked episode returned 404", ep)
                failed.append(ep)
            else:
                episode_pages[ep] = result

    return episode_pages, sorted(failed)


def collect_panel_downloads(
    episode_pages: dict[int, tuple[str, list[str]]],
) -> list[Panel]:
    """Flatten pending episodes into one global panel download queue."""
    panels: list[Panel] = []
    for ep_no, (_, urls) in episode_pages.items():
        referer = VIEWER_URL.format(episode_no=ep_no)
        for idx, url in enumerate(urls, start=1):
            panels.append(Panel(ep_no, idx, url, referer))
    return panels


def download_panel_batch(
    panels: list[Panel], worker_count: int,
) -> tuple[dict[int, list[ArchivedPanel]], list[Panel]]:
    """Run one concurrent pass.  No single worker can abort the batch."""
    archived: dict[int, list[ArchivedPanel]] = defaultdict(list)
    failed: list[Panel] = []
    dirs      = {p.episode_no: EPISODES_DIR / str(p.episode_no) for p in panels}
    total     = len(panels)
    log_every = max(100, total // 20)  # ~5 % or every 100

    with ThreadPoolExecutor(max_workers=worker_count) as pool:
        futures = {
            pool.submit(download_panel, p, dirs[p.episode_no]): p for p in panels
        }
        for done_count, future in enumerate(as_completed(futures), start=1):
            panel = futures[future]
            try:
                result = future.result()
            except Exception as exc:  # noqa: BLE001
                log.error(
                    "Ep %d panel %03d: worker error: %s",
                    panel.episode_no, panel.index, exc,
                )
                result = None

            if result is None:
                failed.append(panel)
            else:
                archived[panel.episode_no].append(result)

            if done_count % log_every == 0 or done_count == total:
                log.info(
                    "Panels: %d/%d done (%d failed)",
                    done_count, total, len(failed),
                )

    return archived, failed


def download_panels(
    panels: list[Panel],
) -> tuple[dict[int, list[ArchivedPanel]], list[Panel]]:
    """Primary pass at full concurrency, then up to PANEL_RETRY_ROUNDS
    reduced-concurrency retry rounds with linear backoff."""
    archived, failed = download_panel_batch(panels, CONCURRENT_PANELS)

    for rnd in range(1, PANEL_RETRY_ROUNDS + 1):
        if not failed:
            break
        delay = PANEL_RETRY_BASE_DELAY * rnd
        log.warning(
            "Retry %d/%d: %d panels, waiting %ds, %d workers.",
            rnd, PANEL_RETRY_ROUNDS, len(failed), delay, RETRY_CONCURRENT_PANELS,
        )
        time.sleep(delay)
        recovered, failed = download_panel_batch(failed, RETRY_CONCURRENT_PANELS)
        for ep_no, ep_panels in recovered.items():
            archived[ep_no].extend(ep_panels)

    return archived, failed


# ---------------------------------------------------------------------------
# JSON / metadata writers
# ---------------------------------------------------------------------------


def write_json(path: Path, payload: object) -> None:
    """Serialize *payload* as JSON via a sibling temp file, then atomically
    replace *path*.  ``fsync`` guarantees durability."""
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent,
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        tmp_path.replace(path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def write_episode_metadata(
    episode_no: int,
    title: str,
    archived_panels: list[ArchivedPanel],
) -> None:
    ordered  = sorted(archived_panels, key=lambda p: p.index)
    metadata = {
        "episode":    episode_no,
        "title":      title,
        "panelCount": len(ordered),
        "panels": [
            {
                "file":   f"{p.index:03d}.webp",
                "width":  p.width,
                "height": p.height,
            }
            for p in ordered
        ],
    }
    write_json(MAP_DIR / f"{episode_no}.json", metadata)


def build_manifest() -> None:
    """Atomically rebuild the aggregate episode index."""
    summaries: list[dict] = []
    for meta_path in MAP_DIR.glob("*.json"):
        if meta_path.name == MANIFEST_PATH.name or not meta_path.stem.isdigit():
            continue
        ep_no = int(meta_path.stem)
        meta  = load_episode_metadata(ep_no)
        if meta is None:
            log.warning("Excluding incomplete metadata: %s", meta_path)
            continue
        summaries.append({
            "episode":    meta["episode"],
            "title":      meta["title"],
            "panelCount": meta["panelCount"],
        })
    summaries.sort(key=lambda e: e["episode"])

    write_json(MANIFEST_PATH, {
        "totalEpisodes": len(summaries),
        "episodes":      summaries,
    })
    log.info("Wrote manifest with %d episodes.", len(summaries))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> int:
    started_at = time.monotonic()
    EPISODES_DIR.mkdir(parents=True, exist_ok=True)
    MAP_DIR.mkdir(parents=True, exist_ok=True)

    cleanup_temp_files()

    try:
        latest_episode = discover_latest_episode()
    except ArchiveError as exc:
        log.error("%s", exc)
        return 1

    incomplete = [
        n for n in range(1, latest_episode + 1) if not is_episode_complete(n)
    ]
    log.info(
        "Episodes: %d published, %d complete, %d to process.",
        latest_episode,
        latest_episode - len(incomplete),
        len(incomplete),
    )

    if not incomplete:
        build_manifest()
        log.info("Archive is already up to date.")
        return 0

    episode_pages, failed_eps = fetch_episode_pages(incomplete)
    archived_count   = 0
    incomplete_count = len(failed_eps)

    if episode_pages:
        panel_queue = collect_panel_downloads(episode_pages)
        log.info(
            "Downloading %d panels across %d episodes (%d concurrent)…",
            len(panel_queue), len(episode_pages), CONCURRENT_PANELS,
        )
        panels_by_ep, still_failed = download_panels(panel_queue)

        for ep_no, (ep_title, ep_urls) in episode_pages.items():
            ep_panels = panels_by_ep.get(ep_no, [])
            if len(ep_panels) == len(ep_urls):
                write_episode_metadata(ep_no, ep_title, ep_panels)
                archived_count += 1
            else:
                incomplete_count += 1
                log.warning(
                    "Ep %d: %d/%d panels — metadata NOT written.",
                    ep_no, len(ep_panels), len(ep_urls),
                )

        if still_failed:
            log.error(
                "%d panels still failed after %d retry rounds.",
                len(still_failed), PANEL_RETRY_ROUNDS,
            )

    build_manifest()

    elapsed = time.monotonic() - started_at
    log.info(
        "Done in %.1fs — %d episodes archived, %d incomplete.",
        elapsed, archived_count, incomplete_count,
    )
    return 1 if incomplete_count else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        log.error("Interrupted.")
        raise SystemExit(1)
