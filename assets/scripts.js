const PANEL_WIDTH = 800; // native pixel width every panel shares (see styles.css --panel-width)
const PULL_THRESHOLD = 120;
const WHEEL_ARM_DELAY = 240;
const WHEEL_FINISH_DELAY = 180;
const GLOW_CROSSFADE_MS = 700;
const GLOW_CENTER_BAND = 72;

class ArchiveRequestError extends Error {
  constructor(message, status = null) {
    super(message);
    this.name = "ArchiveRequestError";
    this.status = status;
  }
}

class EpisodeFormatError extends Error {
  constructor(message) {
    super(message);
    this.name = "EpisodeFormatError";
  }
}

const query = new URLSearchParams(location.search);
const epParam = query.get("ep");
const isLatestRequested = epParam === "latest";
const requestedEpisode = Number.parseInt(epParam || "1", 10);
// When "latest" is requested this starts as a placeholder; initializeViewer()
// resolves it to the real newest episode number before it's used for any
// fetch or image path, so nothing downstream needs to know about "latest".
let episodeNumber =
  Number.isInteger(requestedEpisode) && requestedEpisode > 0
    ? requestedEpisode
    : 1;

const reader = document.querySelector(".reader");
const strip = document.getElementById("strip");
const episodeTitle = document.getElementById("episode-title");
const ambientLayers = [...document.querySelectorAll(".ambient__image")];
const viewerState = document.getElementById("viewer-state");
const viewerStateTitle = document.getElementById("viewer-state-title");
const viewerStateDetail = document.getElementById("viewer-state-detail");
const viewerStateRetry = document.getElementById("viewer-state-retry");
const episodeEnd = document.getElementById("episode-end");
const episodeEndTitle = document.getElementById("episode-end-title");
const episodeEndDetail = document.getElementById("episode-end-detail");
const continueLink = document.getElementById("episode-end-continue");
const nextEpisodeTitle = document.getElementById("next-episode-title");
const nextEpisodeIndicator = document.getElementById("next-episode");
const nextEpisodeLabel = nextEpisodeIndicator.querySelector(
  ".next-episode__label",
);

let nextEpisodeNumber = null;
let pullDistance = 0;
let pullInput = null;
let touchY = null;
let wheelArmTimer = null;
let wheelFinishTimer = null;
let isWheelPullArmed = false;
let isNavigating = false;
let ambientObserver = null;
let centeredPanel = null;
let activeAmbientLayerIndex = -1;
let visibleGlowSource = "";
let pendingGlowSource = "";
let glowRequestId = 0;
let resizeFrame = null;

function updateScale() {
  if (reader.hidden) return;
  if (!reader.clientWidth) return; // not laid out yet — avoid zoom: 0
  const scale = Math.min(reader.clientWidth / PANEL_WIDTH, 1);
  // CSS zoom participates in layout, so the browser resolves pixel snapping
  // in the zoomed coordinate system. This eliminates the subpixel seams that
  // appear with transform: scale(), which composites images out-of-flow and
  // rounds each panel boundary independently. reader.style.height also no
  // longer needs a manual override — zoom drives the layout height directly.
  strip.style.zoom = scale < 1 ? scale : "";
  reader.style.height = "";
}

function setViewerState(title, detail, { canRetry = false } = {}) {
  viewerState.hidden = false;
  viewerState.classList.remove("is-hidden");
  viewerState.classList.toggle("is-error", canRetry);
  viewerState.setAttribute("role", canRetry ? "alert" : "status");
  viewerStateTitle.textContent = title;
  viewerStateDetail.textContent = detail;
  viewerStateRetry.hidden = !canRetry;
}

function dismissViewerState() {
  viewerState.classList.add("is-hidden");
  window.setTimeout(() => {
    if (viewerState.classList.contains("is-hidden")) viewerState.hidden = true;
  }, 260);
}

function showViewerError(error) {
  reader.hidden = true;
  episodeEnd.hidden = true;
  hideAmbientGlow();

  if (!navigator.onLine) {
    setViewerState(
      "You're offline",
      "Reconnect to the internet, then try opening the episode again.",
      { canRetry: true },
    );
    return;
  }

  if (error instanceof ArchiveRequestError && error.status === 404) {
    setViewerState(
      "Episode unavailable",
      "This episode isn't in the archive yet, or the link may be incorrect.",
      { canRetry: true },
    );
    return;
  }

  if (error instanceof EpisodeFormatError) {
    setViewerState(
      "Episode temporarily unavailable",
      "The archived episode is incomplete. Please try again after the next update.",
      { canRetry: true },
    );
    return;
  }

  setViewerState(
    "Couldn't open this episode",
    "A temporary problem interrupted the archive. Please try again.",
    { canRetry: true },
  );
}

function validateEpisodeMetadata(metadata) {
  if (!metadata || typeof metadata !== "object") {
    throw new EpisodeFormatError("Episode metadata is not an object");
  }
  if (metadata.episode !== episodeNumber) {
    throw new EpisodeFormatError("Episode number does not match the request");
  }
  if (typeof metadata.title !== "string" || !metadata.title.trim()) {
    throw new EpisodeFormatError("Episode title is missing");
  }
  if (!Array.isArray(metadata.panels) || metadata.panels.length === 0) {
    throw new EpisodeFormatError("Episode has no panels");
  }
  if (metadata.panelCount !== metadata.panels.length) {
    throw new EpisodeFormatError("Panel count does not match the panel map");
  }

  metadata.panels.forEach((panel, panelIndex) => {
    const expectedFilename = `${String(panelIndex + 1).padStart(3, "0")}.webp`;
    if (
      !panel ||
      panel.file !== expectedFilename ||
      panel.width !== PANEL_WIDTH ||
      !Number.isInteger(panel.height) ||
      panel.height <= 0
    ) {
      throw new EpisodeFormatError(`Panel ${panelIndex + 1} is invalid`);
    }
  });
}

function createPanelUnavailable(panel, panelIndex) {
  const placeholder = document.createElement("div");
  placeholder.className = "panel-unavailable";
  placeholder.style.height = `${panel.height}px`;
  placeholder.setAttribute("role", "group");
  placeholder.setAttribute("aria-label", `Panel ${panelIndex + 1} unavailable`);

  const content = document.createElement("div");
  content.className = "panel-unavailable__content";

  const title = document.createElement("strong");
  title.textContent = `Panel ${panelIndex + 1} couldn't load`;

  const detail = document.createElement("p");
  detail.textContent =
    "The space is preserved so you can continue reading without losing your place.";

  const retry = document.createElement("button");
  retry.type = "button";
  retry.textContent = "Retry panel";
  retry.addEventListener("click", () => {
    const replacement = createPanelImage(panel, panelIndex);
    placeholder.replaceWith(replacement);
    ambientObserver?.observe(replacement);
  });

  content.append(title, detail, retry);
  placeholder.append(content);
  return placeholder;
}

function createPanelImage(panel, panelIndex) {
  const image = document.createElement("img");
  image.src = `episodes/${episodeNumber}/${panel.file}`;
  image.width = panel.width;
  image.height = panel.height;
  image.alt = "";
  image.loading = panelIndex === 0 ? "eager" : "lazy";
  image.decoding = "async";
  if (panelIndex === 0) image.fetchPriority = "high";
  image.addEventListener(
    "error",
    () => image.replaceWith(createPanelUnavailable(panel, panelIndex)),
    { once: true },
  );
  return image;
}

function renderPanels(metadata) {
  const panels = document.createDocumentFragment();
  let firstPanel = null;

  metadata.panels.forEach((panel, panelIndex) => {
    const image = createPanelImage(panel, panelIndex);
    if (panelIndex === 0) firstPanel = image;
    panels.append(image);
  });

  episodeTitle.textContent = metadata.title;
  strip.setAttribute(
    "aria-label",
    `${metadata.title}, ${metadata.panelCount} visual panels`,
  );
  strip.setAttribute("role", "group");
  strip.replaceChildren(panels);
  return firstPanel;
}

async function showPanelGlow(panel) {
  if (document.hidden || !panel.isConnected) return;
  const panelSource = panel.currentSrc || panel.src;
  if (!panelSource || panelSource === pendingGlowSource) return;

  if (panelSource === visibleGlowSource) {
    pendingGlowSource = panelSource;
    glowRequestId += 1;
    return;
  }

  pendingGlowSource = panelSource;
  const requestId = ++glowRequestId;
  const incomingLayerIndex = activeAmbientLayerIndex === 0 ? 1 : 0;
  const incomingGlow = ambientLayers[incomingLayerIndex];

  incomingGlow.classList.remove("is-active");
  incomingGlow.src = panelSource;

  try {
    await incomingGlow.decode();
  } catch {
    if (requestId === glowRequestId) {
      pendingGlowSource = "";
      hideAmbientGlow();
    }
    return;
  }

  if (requestId !== glowRequestId) {
    if (
      !incomingGlow.classList.contains("is-active") &&
      incomingGlow.src === panelSource
    ) {
      incomingGlow.removeAttribute("src");
    }
    return;
  }

  requestAnimationFrame(() => {
    if (requestId !== glowRequestId) return;

    const outgoingGlow =
      activeAmbientLayerIndex === -1
        ? null
        : ambientLayers[activeAmbientLayerIndex];
    const outgoingSource = outgoingGlow?.src;

    incomingGlow.classList.add("is-active");
    outgoingGlow?.classList.remove("is-active");
    activeAmbientLayerIndex = incomingLayerIndex;
    visibleGlowSource = panelSource;

    if (outgoingGlow) {
      window.setTimeout(() => {
        if (
          !outgoingGlow.classList.contains("is-active") &&
          outgoingGlow.src === outgoingSource
        ) {
          outgoingGlow.removeAttribute("src");
        }
      }, GLOW_CROSSFADE_MS);
    }
  });
}

function hideAmbientGlow() {
  glowRequestId += 1;
  pendingGlowSource = "";
  visibleGlowSource = "";
  activeAmbientLayerIndex = -1;
  ambientLayers.forEach((layer) => layer.classList.remove("is-active"));
}

function initializeAmbientGlow() {
  ambientObserver?.disconnect();
  centeredPanel = null;
  const centeredPanels = new Set();
  const verticalMargin = Math.max(
    0,
    Math.floor((window.innerHeight - GLOW_CENTER_BAND) / 2),
  );

  ambientObserver = new IntersectionObserver(
    (observations) => {
      observations.forEach((observation) => {
        if (observation.isIntersecting) centeredPanels.add(observation.target);
        else centeredPanels.delete(observation.target);
      });
      if (centeredPanels.size === 0) return;

      const viewportCenter = window.innerHeight / 2;
      centeredPanel = [...centeredPanels].reduce(
        (nearestPanel, panel) => {
          const bounds = panel.getBoundingClientRect();
          const distance = Math.abs(
            bounds.top + bounds.height / 2 - viewportCenter,
          );
          return distance < nearestPanel.distance
            ? { panel, distance }
            : nearestPanel;
        },
        { panel: null, distance: Number.POSITIVE_INFINITY },
      ).panel;

      if (centeredPanel) showPanelGlow(centeredPanel);
    },
    {
      rootMargin: `-${verticalMargin}px 0px`,
      threshold: 0,
    },
  );

  strip
    .querySelectorAll("img")
    .forEach((panel) => ambientObserver.observe(panel));
}

function isAtBottom() {
  return (
    window.scrollY + window.innerHeight >=
    document.documentElement.scrollHeight - 2
  );
}

function setPullLabel(label) {
  if (nextEpisodeLabel.textContent !== label) {
    nextEpisodeLabel.textContent = label;
  }
}

function setPullDistance(distance, input) {
  pullDistance = Math.max(0, Math.min(distance, PULL_THRESHOLD));
  pullInput = pullDistance > 0 ? input : null;
  const progress = pullDistance / PULL_THRESHOLD;

  nextEpisodeIndicator.style.setProperty("--pull-progress", progress);
  nextEpisodeIndicator.classList.toggle("is-ready", progress === 1);

  if (progress === 1) {
    setPullLabel(
      input === "touch" ? "Release for next episode" : "Next episode ready",
    );
  } else {
    setPullLabel(
      input === "wheel"
        ? "Keep scrolling for next episode"
        : "Pull for next episode",
    );
  }
}

function resetPull() {
  setPullDistance(0, null);
}

function nextEpisodeUrl() {
  const url = new URL(location.href);
  url.searchParams.set("ep", String(nextEpisodeNumber));
  return url;
}

function navigateToNextEpisode() {
  if (isNavigating || nextEpisodeNumber === null) return;

  isNavigating = true;
  nextEpisodeIndicator.classList.add("is-loading");
  setPullLabel("Loading next episode…");
  window.setTimeout(() => location.assign(nextEpisodeUrl()), 120);
}

function finishPull() {
  if (pullDistance >= PULL_THRESHOLD) navigateToNextEpisode();
  else resetPull();
}

function normalizeWheelDelta(event) {
  if (event.deltaMode === 1) return event.deltaY * 16;
  if (event.deltaMode === 2) return event.deltaY * window.innerHeight;
  return event.deltaY;
}

function handleWheel(event) {
  if (nextEpisodeNumber === null || isNavigating) return;

  if (!isAtBottom() || event.deltaY <= 0) {
    isWheelPullArmed = false;
    window.clearTimeout(wheelArmTimer);
    resetPull();
    return;
  }

  if (!isWheelPullArmed) {
    window.clearTimeout(wheelArmTimer);
    wheelArmTimer = window.setTimeout(() => {
      isWheelPullArmed = isAtBottom();
    }, WHEEL_ARM_DELAY);
    return;
  }

  event.preventDefault();
  setPullDistance(pullDistance + normalizeWheelDelta(event) * 0.35, "wheel");
  window.clearTimeout(wheelFinishTimer);
  wheelFinishTimer = window.setTimeout(finishPull, WHEEL_FINISH_DELAY);
}

function handleTouchStart(event) {
  if (pullInput === "wheel") resetPull();
  if (event.touches.length !== 1) {
    touchY = null;
    resetPull();
    return;
  }
  touchY = event.touches[0].clientY;
}

function handleTouchMove(event) {
  if (
    touchY === null ||
    event.touches.length !== 1 ||
    nextEpisodeNumber === null ||
    isNavigating
  ) {
    return;
  }

  const currentY = event.touches[0].clientY;
  const delta = touchY - currentY;
  touchY = currentY;

  if (isAtBottom() && delta > 0) {
    event.preventDefault();
    setPullDistance(pullDistance + delta * 0.55, "touch");
  } else if (pullDistance > 0) {
    event.preventDefault();
    setPullDistance(pullDistance + delta, "touch");
  }
}

function handleTouchEnd() {
  if (touchY === null) return;
  touchY = null;
  finishPull();
}

function handleTouchCancel() {
  touchY = null;
  resetPull();
}

function handleScroll() {
  if (!isAtBottom()) {
    isWheelPullArmed = false;
    window.clearTimeout(wheelArmTimer);
    resetPull();
  }
}

function handleResize() {
  window.cancelAnimationFrame(resizeFrame);
  resizeFrame = window.requestAnimationFrame(() => {
    updateScale();
    initializeAmbientGlow();
  });
}

function handleKeydown(event) {
  if (isNavigating) return;
  if (event.ctrlKey || event.altKey || event.metaKey || event.shiftKey) return;

  const tag = event.target.tagName;
  if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
  if (event.target.isContentEditable) return;

  if (event.key === "ArrowRight" && nextEpisodeNumber !== null) {
    event.preventDefault();
    navigateToNextEpisode();
  } else if (event.key === "ArrowLeft" && episodeNumber > 1) {
    event.preventDefault();
    isNavigating = true;
    const url = new URL(location.href);
    url.searchParams.set("ep", String(episodeNumber - 1));
    location.assign(url);
  }
}

function configureEpisodeEnd(manifest, metadata) {
  episodeEnd.hidden = false;
  continueLink.hidden = true;

  if (!manifest || !Array.isArray(manifest.episodes)) {
    episodeEndTitle.textContent = "Episode complete";
    episodeEndDetail.textContent = "You've reached the end of this episode.";
    return;
  }

  const currentEpisodeIndex = manifest.episodes.findIndex(
    (episode) => episode.episode === episodeNumber,
  );
  const followingEpisode = manifest.episodes[currentEpisodeIndex + 1];
  const hasFollowingEpisode =
    followingEpisode &&
    Number.isInteger(followingEpisode.episode) &&
    typeof followingEpisode.title === "string" &&
    followingEpisode.title.trim();

  if (currentEpisodeIndex === -1 || !hasFollowingEpisode) {
    episodeEndTitle.textContent = "You're all caught up";
    episodeEndDetail.textContent =
      "This is the latest episode currently in the archive.";
    return;
  }

  nextEpisodeNumber = followingEpisode.episode;
  episodeEndTitle.remove();
  episodeEndDetail.textContent = `You finished ${metadata.title}`;
  nextEpisodeTitle.textContent = followingEpisode.title;
  continueLink.href = nextEpisodeUrl();
  continueLink.hidden = false;
  nextEpisodeIndicator.hidden = false;
}

async function fetchMapFile(mapPath) {
  let response;
  try {
    response = await fetch(mapPath);
  } catch (error) {
    const requestError = new ArchiveRequestError("Archive request failed");
    requestError.cause = error;
    throw requestError;
  }
  if (!response.ok) {
    throw new ArchiveRequestError(
      `Archive request failed with status ${response.status}`,
      response.status,
    );
  }
  try {
    return await response.json();
  } catch (error) {
    const formatError = new EpisodeFormatError("Archive map is not valid JSON");
    formatError.cause = error;
    throw formatError;
  }
}

function resolveLatestEpisodeNumber(manifest) {
  // archive.py writes totalEpisodes as the authoritative episode count/number
  // in map/manifest.json — trust it directly instead of re-deriving it.
  if (
    !manifest ||
    !Number.isInteger(manifest.totalEpisodes) ||
    manifest.totalEpisodes <= 0
  ) {
    return null;
  }
  return manifest.totalEpisodes;
}

function attachViewerEvents() {
  window.addEventListener("resize", handleResize, { passive: true });
  window.addEventListener("scroll", handleScroll, { passive: true });
  window.addEventListener("wheel", handleWheel, { passive: false });
  window.addEventListener("touchstart", handleTouchStart, { passive: true });
  window.addEventListener("touchmove", handleTouchMove, { passive: false });
  window.addEventListener("touchend", handleTouchEnd, { passive: true });
  window.addEventListener("touchcancel", handleTouchCancel, { passive: true });
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && centeredPanel) showPanelGlow(centeredPanel);
  });
  window.addEventListener("keydown", handleKeydown);
}

async function initializeViewer() {
  setViewerState("Opening episode", "Preparing the panels for you.");

  let manifest = null;
  if (isLatestRequested) {
    try {
      manifest = await fetchMapFile("map/manifest.json");
      const latestEpisode = resolveLatestEpisodeNumber(manifest);
      if (latestEpisode === null) {
        throw new ArchiveRequestError(
          "Archive manifest has no episodes listed",
        );
      }
      episodeNumber = latestEpisode;
    } catch (error) {
      console.error(error);
      showViewerError(error);
      return;
    }
  }

  const manifestRequest = manifest
    ? Promise.resolve(manifest)
    : fetchMapFile("map/manifest.json").catch((error) => {
        console.warn("Episode navigation is unavailable:", error);
        return null;
      });

  try {
    const metadata = await fetchMapFile(`map/${episodeNumber}.json`);
    validateEpisodeMetadata(metadata);
    document.title = metadata.title;

    const firstPanel = renderPanels(metadata);
    reader.hidden = false;
    updateScale();
    initializeAmbientGlow();
    attachViewerEvents();

    if (firstPanel) {
      await Promise.race([
        firstPanel.decode().catch(() => undefined),
        new Promise((resolve) => window.setTimeout(resolve, 2500)),
      ]);
    }
    dismissViewerState();

    const resolvedManifest = await manifestRequest;
    configureEpisodeEnd(resolvedManifest, metadata);
  } catch (error) {
    console.error(error);
    showViewerError(error);
  }
}

viewerStateRetry.addEventListener("click", () => location.reload());
initializeViewer();
