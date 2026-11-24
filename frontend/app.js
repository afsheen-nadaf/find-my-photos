"use strict";

const API = String((window.CONFIG && window.CONFIG.API_URL) || "").replace(/\/$/, "");
const REGION = (API.match(/execute-api\.([a-z0-9-]+)\.amazonaws/) || [])[1];
const ACCEPTED = ["image/jpeg", "image/png"];
const POLL_MS = 2500;
const POLL_WINDOW_MS = 4 * 60 * 1000;
const UPLOAD_PARALLEL = 3;
const FINDER_DEFAULT = "Click the frame or drop a selfie into it.";
const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

const $ = (id) => document.getElementById(id);

const state = {
  photos: [],            // from GET /photos
  pending: new Map(),    // photoId -> { name, preview, pct, uploaded } for photos this tab is uploading
  names: new Map(),      // photoId -> original file name
  known: new Map(),      // photoId -> { thumb } used to log new events
  loaded: false,
  gaveUp: false,
  pollTimer: null,
  pollUntil: Date.now() + 60 * 1000,
};

/* ---------- Small helpers ---------- */
function el(tag, attrs = {}, ...kids) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === false || value == null) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : value);
  }
  node.append(...kids.filter(Boolean));
  return node;
}

const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;
const nameOf = (id) => state.names.get(id) || `Photo ${id.slice(0, 6)}`;

function log(message, fn, isError = false) {
  const feed = $("feed");
  const empty = $("feedEmpty");
  if (empty) empty.remove();
  feed.prepend(
    el("li", { class: "entry" },
      el("time", { text: new Date().toLocaleTimeString("en-GB") }),
      fn ? el("span", { class: "fn" + (isError ? " error" : ""), text: fn }) : null,
      el("span", { class: "msg", text: message }))
  );
  while (feed.children.length > 80) feed.lastChild.remove();
}

function setConn(ok) {
  const pill = $("conn");
  pill.dataset.state = ok ? "ok" : "error";
  pill.textContent = ok ? (REGION ? `Connected to AWS (${REGION})` : "Connected to AWS") : "Can’t reach AWS";
}

async function api(path, options) {
  let res;
  try {
    res = await fetch(API + path, options);
  } catch (err) {
    setConn(false);
    throw new Error("Can’t reach the server. Check your internet connection and try again.");
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || data.message || `Something went wrong (error ${res.status}).`);
  setConn(true);
  return data;
}

const postJson = (path, body) =>
  api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

function putFile(url, file, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("PUT", url);
    xhr.setRequestHeader("Content-Type", file.type);
    xhr.upload.onprogress = (e) => { if (e.lengthComputable) onProgress(e.loaded / e.total); };
    xhr.onload = () => (xhr.status >= 200 && xhr.status < 300
      ? resolve()
      : reject(new Error(`S3 rejected the upload (error ${xhr.status}).`)));
    xhr.onerror = () => reject(new Error("The upload was interrupted. Check your connection and try again."));
    xhr.send(file);
  });
}

/* ---------- Album ---------- */
const tileCache = new Map(); // photoId -> { sig, node }

function stageText(item) {
  if (item.stage === "uploading") return item.pct >= 100 ? "Saving to S3…" : `Uploading ${item.pct || 0}%`;
  if (state.gaveUp) return "Taking longer than expected";
  return item.stage === "faces" ? "Finding faces…" : "Creating thumbnail…";
}

function facesChip(faces, extra = "") {
  return el("span", {
    class: "chip" + (faces === 0 ? " warn" : "") + extra,
    text: faces === 0 ? "No faces found" : plural(faces, "face", "faces"),
  });
}

function tileEl(item) {
  const ready = item.stage === "ready";
  const node = el("figure", { class: "tile", "data-id": item.id, "data-stage": item.stage });
  const src = ready ? item.thumbUrl : item.preview;
  if (src) node.append(el("img", { src, alt: ready ? "Event photo" : "Photo being processed", loading: "lazy" }));

  if (ready) {
    node.append(facesChip(item.faces));
  } else {
    const active = { uploading: 0, faces: 1, thumb: 2 }[item.stage];
    if (item.stage === "thumb") node.append(facesChip(item.faces, " top"));
    node.append(
      el("div", { class: "work" },
        el("div", { class: "stage-label" },
          el("span", { class: "spin", "aria-hidden": "true" }),
          el("span", { class: "stage-text", text: stageText(item) })),
        el("ol", { class: "track" },
          ...["Upload", "Faces", "Thumbnail"].map((label, i) =>
            el("li", { class: i < active ? "done" : i === active ? "active" : "", text: label }))))
    );
  }
  return node;
}

function tileSignature(item) {
  // Rebuild a tile when its stage changes, or every 10 minutes so signed links never go stale.
  return [item.stage, item.faces ?? "", Math.floor(Date.now() / 600000), state.gaveUp && item.stage !== "ready"].join("|");
}

function renderAlbum() {
  const items = [];
  const onServer = new Set(state.photos.map((p) => p.photoId));

  for (const [id, pend] of [...state.pending].reverse()) {
    if (!onServer.has(id)) items.push({ id, preview: pend.preview, pct: pend.pct, stage: pend.uploaded ? "faces" : "uploading" });
  }
  for (const p of state.photos) {
    const pend = state.pending.get(p.photoId);
    items.push({
      id: p.photoId, faces: p.faceCount, thumbUrl: p.thumbUrl,
      preview: pend && pend.preview, stage: p.thumbUrl ? "ready" : "thumb",
    });
  }

  const nodes = items.map((item) => {
    const sig = tileSignature(item);
    const hit = tileCache.get(item.id);
    if (hit && hit.sig === sig) return hit.node;
    const node = tileEl(item);
    tileCache.set(item.id, { sig, node });
    return node;
  });
  const live = new Set(items.map((i) => i.id));
  for (const id of [...tileCache.keys()]) if (!live.has(id)) tileCache.delete(id);
  $("gallery").replaceChildren(...nodes);

  const faces = state.photos.reduce((sum, p) => sum + p.faceCount, 0);
  $("albumStats").textContent = state.photos.length
    // Faces are counted once per photo they appear in, so one person in 10 photos adds 10.
    ? `${plural(state.photos.length, "photo", "photos")}, ${plural(faces, "face appearance", "face appearances")} detected`
    : "";
  $("albumLoading").hidden = state.loaded;
  $("empty").hidden = nodes.length > 0 || !state.loaded;
}

function updateUploadingTile(id) {
  const pend = state.pending.get(id);
  const label = document.querySelector(`[data-id="${id}"] .stage-text`);
  if (pend && label) label.textContent = stageText({ stage: "uploading", pct: pend.pct });
}

function logChanges(photos) {
  if (!state.loaded) {
    for (const p of photos) state.known.set(p.photoId, { thumb: !!p.thumbUrl });
    return;
  }
  for (const p of photos) {
    const prev = state.known.get(p.photoId);
    if (!prev) log(`${nameOf(p.photoId)}: ${plural(p.faceCount, "face", "faces")} found and saved`, "IndexPhoto");
    if ((!prev || !prev.thumb) && p.thumbUrl) log(`${nameOf(p.photoId)}: thumbnail ready`, "Thumbnail");
    state.known.set(p.photoId, { thumb: !!p.thumbUrl });
  }
}

function schedulePoll() {
  const working = state.pending.size > 0 || state.photos.some((p) => !p.thumbUrl);
  if (!working) return;
  if (Date.now() < state.pollUntil) {
    state.gaveUp = false;
    state.pollTimer = setTimeout(loadGallery, POLL_MS);
  } else if (!state.gaveUp) {
    state.gaveUp = true;
    renderAlbum();
  }
}

async function loadGallery(manual = false) {
  clearTimeout(state.pollTimer);
  if (manual === true) state.pollUntil = Date.now() + POLL_WINDOW_MS;
  try {
    const { photos } = await api("/photos");
    logChanges(photos);
    state.photos = photos;
    state.loaded = true;
    for (const p of photos) {
      const pend = state.pending.get(p.photoId);
      if (pend && p.thumbUrl) {
        state.pending.delete(p.photoId);
        setTimeout(() => URL.revokeObjectURL(pend.preview), 4000);
      }
    }
    renderAlbum();
  } catch (err) {
    if (!state.loaded) {
      $("albumLoading").hidden = false;
      $("albumLoading").textContent = `Couldn’t load the album. ${err.message}`;
    }
  }
  schedulePoll();
}

/* ---------- Uploading event photos ---------- */
function setUploadNote(text, isError = false) {
  const note = $("uploadNote");
  note.textContent = text;
  note.classList.toggle("error", isError);
}

async function uploadFiles(fileList) {
  const all = [...fileList];
  const files = all.filter((f) => ACCEPTED.includes(f.type));
  const skipped = all.length - files.length;
  if (!files.length) {
    if (all.length) setUploadNote("Only JPEG and PNG photos work. Photos in HEIC format need to be converted first.", true);
    return;
  }
  setUploadNote("");
  state.pollUntil = Date.now() + POLL_WINDOW_MS;
  state.gaveUp = false;

  const total = files.length;
  const progress = new Array(total).fill(0);
  let finished = 0;
  let failed = 0;
  const box = $("uploadProgress");
  box.hidden = false;

  const refreshBar = () => {
    const pct = Math.round((progress.reduce((a, b) => a + b, 0) / total) * 100);
    $("uploadBar").style.width = `${pct}%`;
    $("uploadBarBox").setAttribute("aria-valuenow", String(pct));
    $("uploadProgressText").textContent = `Uploaded ${finished} of ${total}`;
  };
  refreshBar();

  const queue = files.map((file, index) => ({ file, index }));
  async function worker() {
    while (queue.length) {
      const { file, index } = queue.shift();
      let photoId = null;
      try {
        const link = await postJson("/upload-url", { contentType: file.type, filename: file.name });
        photoId = link.photoId;
        state.names.set(photoId, file.name);
        state.pending.set(photoId, { name: file.name, preview: URL.createObjectURL(file), pct: 0, uploaded: false });
        log(`Signed upload link issued for ${file.name}`, "GetUploadUrl");
        renderAlbum();
        await putFile(link.uploadUrl, file, (fraction) => {
          progress[index] = fraction;
          const pend = state.pending.get(photoId);
          if (pend) pend.pct = Math.round(fraction * 100);
          updateUploadingTile(photoId);
          refreshBar();
        });
        progress[index] = 1;
        state.pending.get(photoId).uploaded = true;
        finished++;
        log(`${file.name} saved to the private bucket`, "S3");
        refreshBar();
        renderAlbum();
        clearTimeout(state.pollTimer);
        state.pollTimer = setTimeout(loadGallery, 1200);
      } catch (err) {
        failed++;
        progress[index] = 1;
        if (photoId) {
          const pend = state.pending.get(photoId);
          if (pend) URL.revokeObjectURL(pend.preview);
          state.pending.delete(photoId);
        }
        log(`${file.name}: ${err.message}`, "Error", true);
        refreshBar();
        renderAlbum();
      }
    }
  }
  await Promise.all(Array.from({ length: Math.min(UPLOAD_PARALLEL, total) }, worker));

  const parts = [`Uploaded ${plural(finished, "photo", "photos")}.`];
  if (finished) parts.push("Faces and thumbnails appear in the album below as the functions finish.");
  if (failed) parts.push(`${failed} failed, see Live activity.`);
  if (skipped) parts.push(`${skipped} skipped because only JPEG and PNG work.`);
  setUploadNote(parts.join(" "), failed > 0 && finished === 0);
  setTimeout(() => { box.hidden = true; }, 1200);
}

/* ---------- Selfie search ---------- */
function toJpegDataUrl(file, max = 800) {
  return new Promise((resolve, reject) => {
    const url = URL.createObjectURL(file);
    const img = new Image();
    img.onload = () => {
      const scale = Math.min(1, max / Math.max(img.width, img.height));
      const canvas = document.createElement("canvas");
      canvas.width = Math.round(img.width * scale);
      canvas.height = Math.round(img.height * scale);
      canvas.getContext("2d").drawImage(img, 0, 0, canvas.width, canvas.height);
      URL.revokeObjectURL(url);
      resolve(canvas.toDataURL("image/jpeg", 0.85));
    };
    img.onerror = () => {
      URL.revokeObjectURL(url);
      reject(new Error("Couldn’t read that image. Try a JPEG or PNG selfie."));
    };
    img.src = url;
  });
}

function setFinder(stateName, text) {
  $("vf").dataset.state = stateName;
  const status = $("finderStatus");
  status.dataset.state = stateName;
  status.textContent = text;
}

function openLightbox(url) {
  $("lightboxImg").src = url;
  $("lightbox").showModal();
}

// Face matches carry `similarity` (0-100). Text matches carry `score` (cosine), shown relative to the best hit.
function matchEl(match, topScore) {
  const isText = match.score !== undefined;
  const pct = isText ? Math.round((match.score / topScore) * 100) : Math.round(match.similarity * 10) / 10;
  const label = isText ? `Relevance ${match.score.toFixed(2)}` : `${pct}% match`;
  return el("article", { class: "match" },
    el("button", { class: "match-img", type: "button", "aria-label": "View this photo larger", onclick: () => openLightbox(match.downloadUrl) },
      el("img", { src: match.thumbUrl, alt: isText ? "A photo matching your description" : "A photo you appear in", loading: "lazy" })),
    el("div", { class: "match-meta" },
      el("div", {},
        el("span", { class: "sim-label", text: label }),
        el("span", { class: "sim-bar" }, el("i", { style: `width:${Math.max(0, Math.min(100, pct))}%` })),
        isText && match.labels && match.labels.length
          ? el("span", { class: "tags", text: match.labels.slice(0, 5).join(" · ") })
          : null),
      el("a", { class: "btn small", href: match.downloadUrl, rel: "noopener", text: "Download photo" })));
}

async function searchSelfie(file) {
  if (!file) return;
  $("results").hidden = true;

  let image;
  try {
    image = await toJpegDataUrl(file);
  } catch (err) {
    setFinder("error", err.message);
    return;
  }
  const vf = $("vf");
  vf.style.backgroundImage = `url(${image})`;
  vf.classList.add("has-photo");

  if (state.loaded && state.photos.length === 0) {
    setFinder("none", "The album is empty. Add event photos in step 1, then try again.");
    return;
  }

  setFinder("searching", `Comparing your face with ${state.photos.length ? plural(state.photos.length, "photo", "photos") : "the album"}…`);
  log("Selfie received. Matching it against every indexed face.", "FindMyPhotos");
  const started = performance.now();
  try {
    const { matches, message } = await postJson("/search", { image });
    const seconds = ((performance.now() - started) / 1000).toFixed(1);
    if (!matches.length) {
      setFinder("none", message || "No matches found. Try a clearer, front-facing selfie.");
      log(`No matching faces found (${seconds}s)`, "FindMyPhotos");
      return;
    }
    setFinder("found", `Found you in ${plural(matches.length, "photo", "photos")}.`);
    log(`${plural(matches.length, "match", "matches")} found in ${seconds}s`, "FindMyPhotos");
    $("resultsTitle").textContent = matches.length === 1 ? "You’re in 1 photo" : `You’re in ${matches.length} photos`;
    $("resultsMeta").textContent = `Searched the whole album in ${seconds} seconds. Best matches first.`;
    $("resultsGrid").replaceChildren(...matches.map(matchEl));
    $("results").hidden = false;
    $("results").scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth", block: "start" });
  } catch (err) {
    setFinder("error", err.message);
    log(err.message, "Error", true);
  }
}

/* ---------- Describe-a-photo search ---------- */
async function searchText(query) {
  const q = query.trim();
  const note = $("describeNote");
  const setNote = (text, isError = false) => { note.textContent = text; note.classList.toggle("error", isError); };
  if (!q) { setNote("Type what you are looking for first.", true); return; }

  $("describeBtn").disabled = true;
  $("results").hidden = true;
  setNote(`Searching for “${q}”…`);
  log(`Embedding “${q}” and comparing it with every photo.`, "TextSearch");
  const started = performance.now();
  try {
    const { matches, message } = await postJson("/search-text", { query: q, k: 12, mode: "semantic" });
    const seconds = ((performance.now() - started) / 1000).toFixed(1);
    if (!matches.length) {
      setNote(message || "Nothing found. Try different words.", true);
      log(`No results for “${q}” (${seconds}s)`, "TextSearch");
      return;
    }
    setNote(`Top ${plural(matches.length, "photo", "photos")} for “${q}” in ${seconds}s.`);
    log(`${plural(matches.length, "result", "results")} for “${q}” in ${seconds}s`, "TextSearch");
    $("resultsTitle").textContent = `Closest to “${q}”`;
    $("resultsMeta").textContent = "Ranked by embedding similarity. The bar is relative to the best match.";
    const top = matches[0].score || 1;
    $("resultsGrid").replaceChildren(...matches.map((m) => matchEl(m, top)));
    $("results").hidden = false;
    $("results").scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth", block: "start" });
  } catch (err) {
    setNote(err.message, true);
    log(err.message, "Error", true);
  } finally {
    $("describeBtn").disabled = false;
  }
}

/* ---------- Wiring ---------- */
function wireDrop(zone, onFiles) {
  for (const type of ["dragenter", "dragover"]) {
    zone.addEventListener(type, (e) => { e.preventDefault(); zone.classList.add("over"); });
  }
  for (const type of ["dragleave", "drop"]) {
    zone.addEventListener(type, (e) => { e.preventDefault(); zone.classList.remove("over"); });
  }
  zone.addEventListener("drop", (e) => onFiles(e.dataTransfer.files));
}

// A photo dropped outside a drop zone should not make the browser leave the page mid-demo.
window.addEventListener("dragover", (e) => e.preventDefault());
window.addEventListener("drop", (e) => e.preventDefault());

$("upload").addEventListener("change", (e) => {
  const files = [...e.target.files];
  e.target.value = "";
  uploadFiles(files);
});
$("selfie").addEventListener("change", (e) => {
  const file = e.target.files[0];
  e.target.value = "";
  searchSelfie(file);
});
$("describeForm").addEventListener("submit", (e) => {
  e.preventDefault();
  searchText($("describeInput").value);
});
wireDrop($("drop"), uploadFiles);
wireDrop($("vf"), (files) => searchSelfie(files[0]));
$("refresh").addEventListener("click", () => loadGallery(true));

const lightbox = $("lightbox");
$("lightboxClose").addEventListener("click", () => lightbox.close());
lightbox.addEventListener("click", (e) => { if (e.target === lightbox) lightbox.close(); });
lightbox.addEventListener("close", () => { $("lightboxImg").removeAttribute("src"); });

if (!API) {
  setConn(false);
  $("albumLoading").textContent = "The API address is missing. Check config.js.";
} else {
  loadGallery();
}