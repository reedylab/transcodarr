(function () {
  const API = (window.API_BASE || "/api").replace(/\/+$/, "");
  const $  = (sel) => document.querySelector(sel);
  const $$ = (sel) => Array.from(document.querySelectorAll(sel));

  // ----- SSE helpers (?nostream=1 forces the polling fallback) -----
  const sseEnabled = !new URLSearchParams(location.search).has("nostream") && typeof EventSource !== "undefined";
  const STREAM_OPEN_TIMEOUT_MS = 5000;

  function openStream(path, handlers, pollFallback) {
    if (!sseEnabled) { pollFallback && pollFallback(); return null; }
    let es;
    try { es = new EventSource(API + path); }
    catch { pollFallback && pollFallback(); return null; }
    let opened = false;
    const watchdog = setTimeout(() => {
      if (!opened) {
        console.warn(`[SSE] ${path} never opened — falling back to polling`);
        try { es.close(); } catch {}
        pollFallback && pollFallback();
      }
    }, STREAM_OPEN_TIMEOUT_MS);
    es.addEventListener("open", () => { opened = true; clearTimeout(watchdog); });
    for (const [name, fn] of Object.entries(handlers)) {
      es.addEventListener(name, (e) => {
        try { fn(JSON.parse(e.data)); } catch (err) { console.error(`[SSE/${name}]`, err); }
      });
    }
    return es;
  }

  // ----- Keyed table-row diffing (no full innerHTML rebuild) -----
  // Sigs live in a WeakMap so we don't have to HTML-escape arbitrary content into a DOM attribute.
  // items: array; opts: { keyFn(item)->str, sigFn(item)->str, buildRowFn(item)->html, wireRowFn(tr,item) }
  const _rowSigs = new WeakMap();
  function patchTableRows(tbody, items, opts) {
    const { keyFn, sigFn, buildRowFn, wireRowFn } = opts;
    // Drop placeholder/skeleton children (no data-row-key) — works for tr OR div tiles
    Array.from(tbody.children).forEach(tr => { if (!tr.dataset.rowKey) tr.remove(); });
    const existing = new Map();
    tbody.querySelectorAll(":scope > [data-row-key]").forEach(tr => existing.set(tr.dataset.rowKey, tr));
    let cursor = tbody.firstElementChild;
    const seen = new Set();
    for (const item of items) {
      const key = keyFn(item);
      seen.add(key);
      const sig = sigFn(item);
      let tr = existing.get(key);
      if (!tr) {
        const tmp = document.createElement("tbody");
        tmp.innerHTML = buildRowFn(item);
        tr = tmp.firstElementChild;
        tbody.insertBefore(tr, cursor);
        _rowSigs.set(tr, sig);
        wireRowFn(tr, item);
      } else if (_rowSigs.get(tr) !== sig) {
        const tmp = document.createElement("tbody");
        tmp.innerHTML = buildRowFn(item);
        const fresh = tmp.firstElementChild;
        tr.parentNode.replaceChild(fresh, tr);
        _rowSigs.set(fresh, sig);
        wireRowFn(fresh, item);
        tr = fresh;
      } else if (tr !== cursor) {
        tbody.insertBefore(tr, cursor);
      }
      cursor = tr.nextElementSibling;
    }
    for (const [key, tr] of existing) if (!seen.has(key)) tr.remove();
  }

  // Elements
  const statusBadge = $("#status-badge");
  const btnStart    = $("#start-btn");
  const btnStop     = $("#stop-btn");
  const logOut      = $("#log-output");
  const logBox      = $(".log-container");

  // Boot values
  if (window.UI_BOOT) {
    // Path info moved to Settings > General > Paths
  }

  // ----- View & Tabs -----
  const settingsSubnav = $("#settings-subnav");
  const settingsNavItem = $(".nav-item[data-view='settings']");

  $$(".nav-item").forEach(b => b.addEventListener("click", () => {
    const view = b.dataset.view;

    // Handle settings toggle
    if (view === "settings") {
      // Toggle subnav visibility
      const isExpanded = settingsSubnav.classList.contains("expanded");
      if (isExpanded) {
        settingsSubnav.classList.remove("expanded");
        settingsNavItem.classList.remove("expanded");
      } else {
        settingsSubnav.classList.add("expanded");
        settingsNavItem.classList.add("expanded");
        // Show settings view
        $$(".nav-item").forEach(x => x.classList.remove("active"));
        b.classList.add("active");
        $$(".view").forEach(v => v.classList.toggle("visible", v.id === "view-settings"));
        // Select first section if none selected
        if (!currentSection && Object.keys(settingsSchema).length > 0) {
          showSettingsSection(Object.keys(settingsSchema)[0]);
        }
      }
      return;
    }

    // Normal nav handling for other views
    $$(".nav-item").forEach(x => x.classList.remove("active"));
    b.classList.add("active");
    $$(".view").forEach(v => v.classList.toggle("visible", v.id === `view-${view}`));
    // Collapse settings subnav when switching to other views
    settingsSubnav.classList.remove("expanded");
    settingsNavItem.classList.remove("expanded");
  }));

  $$(".tab").forEach(t => t.addEventListener("click", () => {
    $$(".tab").forEach(x => x.classList.remove("active"));
    t.classList.add("active");
    const tab = t.dataset.tab;
    $$(".tabpane").forEach(p => p.classList.toggle("visible", p.id === `tab-${tab}`));
  }));

  // ----- Status -----
  function setRunningUI(running) {
    if (running) {
      statusBadge.textContent = "Running";
      statusBadge.className = "badge badge-running";
      btnStart.disabled = true; btnStop.disabled = false;
    } else {
      statusBadge.textContent = "Stopped";
      statusBadge.className = "badge badge-stopped";
      btnStart.disabled = false; btnStop.disabled = true;
    }
  }

  async function updateStatus() {
    try {
      const r = await fetch(`${API}/status`, {headers:{Accept:"application/json"}, cache:"no-store"});
      const d = r.ok ? await r.json() : {};
      const running = (d.status || d.running) === "running" || d.running === true;
      setRunningUI(running);
      // Path display handled in Settings > General > Paths
    } catch {
      setRunningUI(false);
    }
  }

  // ----- Logs (byte-offset tail) -----
  let tailPos   = 0;
  let tailInode = null;

  function atBottom(){
    return logBox.scrollTop + logBox.clientHeight >= logBox.scrollHeight - 8;
  }
  function scrollToBottom(){ logBox.scrollTop = logBox.scrollHeight; }
  function appendText(txt){
    if (!txt) return;
    const stick = atBottom();
    logOut.textContent += txt.replace(/\r\n/g, "\n");
    // Hard cap
    const lines = logOut.textContent.split("\n");
    if (lines.length > 5000) logOut.textContent = lines.slice(-5000).join("\n");
    if (stick) scrollToBottom();
  }

  async function pollLogs(){
    try{
      const url = new URL(`${API}/logs/tail`, window.location.origin);
      url.searchParams.set("pos", String(tailPos));
      if (tailInode) url.searchParams.set("inode", tailInode);
      const r = await fetch(url, {headers:{Accept:"application/json"}, cache:"no-store"});
      if (!r.ok) return;
      const d = await r.json(); // {text,pos,inode,reset}
      if (d.reset || (tailInode && d.inode && d.inode !== tailInode)) {
        logOut.textContent = "";
      }
      if (typeof d.text === "string" && d.text.length) appendText(d.text);
      if (typeof d.pos === "number") tailPos = d.pos;
      if (d.inode) tailInode = d.inode;
    } catch {}
  }

  $("#clear-log").addEventListener("click", () => { logOut.textContent = ""; });

// Skeleton loaders
function moviesSkeleton(n = 6) {
  return Array(n).fill(`
    <tr class="skeleton-row">
      <td class="select-cell"></td>
      <td class="poster-cell"><div class="skeleton skeleton-poster"></div></td>
      <td><div class="skeleton skeleton-text"></div><div class="skeleton skeleton-text-sm"></div></td>
      <td><div class="skeleton skeleton-text-sm"></div></td>
      <td><div class="skeleton skeleton-text-sm"></div></td>
      <td><div class="skeleton skeleton-text-sm"></div></td>
      <td><div class="skeleton skeleton-text-sm"></div></td>
      <td><div class="skeleton skeleton-text-sm"></div></td>
      <td><div class="skeleton skeleton-text-sm"></div></td>
    </tr>
  `).join("");
}

function tvSkeleton(n = 6) {
  return Array(n).fill(`
    <tr class="skeleton-row">
      <td class="select-cell"></td>
      <td class="poster-cell"><div class="skeleton skeleton-poster"></div></td>
      <td><div class="skeleton skeleton-text"></div><div class="skeleton skeleton-text-sm"></div></td>
      <td><div class="skeleton skeleton-text-sm"></div></td>
      <td><div class="skeleton skeleton-text-sm"></div></td>
      <td><div class="skeleton skeleton-text-sm"></div></td>
      <td><div class="skeleton skeleton-text-sm"></div></td>
      <td><div class="skeleton skeleton-text-sm"></div></td>
      <td><div class="skeleton skeleton-text-sm"></div></td>
    </tr>
  `).join("");
}

// Worker pool status
let workerStatus = { active_manual_jobs: 0, active_auto_jobs: 0, manual_workers: 0, auto_workers: 2, can_accept: false };

async function updateWorkerStatus() {
  try {
    const r = await fetch(`${API}/workers/status`, {headers:{Accept:"application/json"}, cache:"no-store"});
    if (r.ok) {
      workerStatus = await r.json();
      const mw = workerStatus.manual_workers || 0;
      const aw = workerStatus.auto_workers || 0;
      const am = workerStatus.active_manual_jobs || 0;
      const aa = workerStatus.active_auto_jobs || 0;

      // Auto pill
      const autoCount = $("#auto-worker-count");
      const autoPill = $("#auto-workers");
      if (autoCount) autoCount.textContent = aw > 0 ? `${aa}/${aw}` : "off";
      if (autoPill) {
        autoPill.classList.toggle("busy", aw > 0 && aa >= aw);
        autoPill.classList.toggle("off", aw === 0);
      }
      const autoGroup = $("#auto-group");
      if (autoGroup) autoGroup.title = aw > 0 ? `Auto: ${aa} active / ${aw} workers` : "Auto: disabled";

      // Manual pill
      const manualCount = $("#manual-worker-count");
      const manualPill = $("#manual-workers");
      if (manualCount) manualCount.textContent = mw > 0 ? `${am}/${mw}` : "off";
      if (manualPill) {
        manualPill.classList.toggle("busy", mw > 0 && am >= mw);
        manualPill.classList.toggle("off", mw === 0);
      }

      // Manual badge
      const manualBadge = $("#manual-badge");
      if (manualBadge) {
        if (mw === 0) {
          manualBadge.textContent = "Off";
          manualBadge.className = "badge badge-off";
        } else if (am >= mw) {
          manualBadge.textContent = "Busy";
          manualBadge.className = "badge badge-busy";
        } else {
          manualBadge.textContent = "Ready";
          manualBadge.className = "badge badge-ready";
        }
      }
      const manualGroup = $("#manual-group");
      if (manualGroup) manualGroup.title = mw > 0 ? `Manual: ${am} active / ${mw} workers` : "Manual: disabled";
    }
  } catch {}
}

// Action buttons for items
function actionHtml(item, type, idx) {
  const isIgnored = item.ignored === true;
  const isProcessing = item.status === "processing" || item.status === "queued";

  // Subs button available for non-ignored, non-processing items
  const subsBtn = (isIgnored || isProcessing)
    ? ""
    : `<button class="btn btn-sm btn-subs" data-type="${type}" data-idx="${idx}" title="Manual subtitle search">&#128269;</button>`;

  // Show stop button for processing/queued items
  if (isProcessing) {
    return `<div class="action-btns"><button class="btn btn-sm btn-stop" data-type="${type}" data-idx="${idx}" title="Stop transcode">Stop</button></div>`;
  }

  // Show stop button for re-encoding items
  if (item.status === "re-encoding") {
    return `<div class="action-btns"><button class="btn btn-sm btn-stop" data-type="${type}" data-idx="${idx}" title="Stop re-encode">Stop</button></div>`;
  }

  // Ready items get a transcode button + get meta
  if (item.status === "ready") {
    const transcodeBtn = `<button class="btn btn-sm btn-transcode" data-type="${type}" data-idx="${idx}" title="Transcode with current settings">Transcode</button>`;
    const enrichBtn = `<button class="btn btn-sm btn-enrich" data-type="${type}" data-idx="${idx}" title="Fetch metadata, NFO, poster">Meta</button>`;
    return `<div class="action-btns">${enrichBtn}${transcodeBtn}</div>`;
  }

  // Only show transcode/ignore for pending items
  if (item.status !== "pending") {
    return subsBtn ? `<div class="action-btns">${subsBtn}</div>` : "";
  }

  const ignoreClass = isIgnored ? "btn-ignore active" : "btn-ignore";
  const ignoreTitle = isIgnored ? "Remove from ignore list" : "Add to ignore list (skip auto-transcode)";

  // Don't show transcode button if ignored
  const transcodeBtn = isIgnored
    ? ""
    : `<button class="btn btn-sm btn-transcode" data-type="${type}" data-idx="${idx}" title="Queue manual transcode">Transcode</button>`;

  // Delete subs button for pending non-ignored items
  const deleteSubsBtn = isIgnored
    ? ""
    : `<button class="btn btn-sm btn-delete-subs" data-type="${type}" data-idx="${idx}" title="Delete existing subtitles">&#128465;</button>`;

  // Get metadata button
  const enrichBtn = isIgnored
    ? ""
    : `<button class="btn btn-sm btn-enrich" data-type="${type}" data-idx="${idx}" title="Fetch metadata, NFO, poster">Meta</button>`;

  return `
    <div class="action-btns">
      ${subsBtn}
      ${deleteSubsBtn}
      ${enrichBtn}
      ${transcodeBtn}
      <button class="${ignoreClass}" data-type="${type}" data-idx="${idx}" title="${ignoreTitle}">
        ${isIgnored ? "&#10003;" : "&#8709;"}
      </button>
    </div>
  `;
}

async function handleEnrichClick(item, type) {
  const btn = event ? event.target : null;
  const origText = btn ? btn.textContent : "";
  if (btn) { btn.textContent = "..."; btn.disabled = true; }

  try {
    const r = await fetch(`${API}/media/enrich`, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({ path: item.path, force: true })
    });
    const result = await r.json();
    if (r.ok) {
      const parts = [];
      if (result.nfo_written) parts.push("NFO");
      if (result.poster_downloaded) parts.push("Poster");
      const msg = parts.length > 0 ? parts.join(" + ") + " saved" : "No new metadata found";
      showSubsToast({ _custom: msg });
    } else {
      showSubsToast({ error: result.error || "Enrichment failed" });
    }
  } catch (e) {
    showSubsToast({ error: e.message });
  } finally {
    if (btn) { btn.textContent = origText; btn.disabled = false; }
  }
}

async function handleTranscodeClick(item, type) {
  if ((workerStatus.manual_workers || 0) <= 0) {
    alert("Manual transcoding is disabled (MANUAL_WORKERS=0). Enable it in Settings > Advanced.");
    return;
  }
  if (!workerStatus.can_accept) {
    alert("All manual workers are busy. Please wait.");
    return;
  }

  const data = {
    file_path: item.path,
    media_type: type,
  };

  if (type === "movie") {
    data.title = item.title;
    data.year = item.year;
  } else {
    data.show = item.show;
    data.season = item.season;
    data.episode = item.episode;
    data.title = item.title;
  }

  try {
    const r = await fetch(`${API}/transcode/manual`, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(data)
    });
    const result = await r.json();

    if (r.ok) {
      // Refresh tables to show processing status
      if (type === "movie") loadMovies(false);
      else loadTV(false);
      updateWorkerStatus();
    } else {
      alert(`Failed to queue: ${result.error || "Unknown error"}`);
    }
  } catch (e) {
    alert(`Failed to queue: ${e.message}`);
  }
}

async function handleStopClick(item, type) {
  try {
    const r = await fetch(`${API}/transcode/stop`, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({ file_path: item.source_path || item.path })
    });
    const result = await r.json();
    if (r.ok) {
      if (type === "movie") loadMovies(false);
      else loadTV(false);
      updateWorkerStatus();
    } else {
      alert(`Failed to stop: ${result.error || "Unknown error"}`);
    }
  } catch (e) {
    alert(`Failed to stop: ${e.message}`);
  }
}

async function handleIgnoreClick(item, type) {
  try {
    const r = await fetch(`${API}/media/ignore`, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        file_path: item.path,
        action: "toggle"
      })
    });
    const result = await r.json();

    if (r.ok) {
      // Refresh tables to show updated ignore status
      if (type === "movie") loadMovies(false);
      else loadTV(false);
    } else {
      alert(`Failed to toggle ignore: ${result.error || "Unknown error"}`);
    }
  } catch (e) {
    alert(`Failed to toggle ignore: ${e.message}`);
  }
}

// ----- Manual Subtitle Search -----
function showSubtitleSearchModal(item, type) {
  // Pre-fill values based on item type
  let defaultQuery = "";
  let defaultSeason = "";
  let defaultEpisodes = "";

  if (type === "movie") {
    defaultQuery = item.title || "";
    if (item.year) defaultQuery += ` ${item.year}`;
  } else {
    defaultQuery = item.show || "";
    defaultSeason = item.season != null ? String(item.season) : "";
    if (item.episodes && item.episodes.length > 0) {
      defaultEpisodes = item.episodes.join(", ");
    } else if (item.episode != null) {
      defaultEpisodes = String(item.episode);
    }
  }

  const modal = document.createElement("div");
  modal.className = "modal-overlay";
  modal.innerHTML = `
    <div class="modal-content subs-search-modal">
      <h3>Manual Subtitle Search</h3>
      <p class="modal-hint">Override search parameters for: <strong>${type === "movie" ? (item.title || "Unknown") : (item.show || "Unknown")}</strong></p>
      <div class="modal-field">
        <label for="subs-query">Search Query</label>
        <input type="text" id="subs-query" value="${escapeHtml(defaultQuery)}" placeholder="Movie or show title" autocomplete="off">
      </div>
      <div class="modal-field-row">
        <div class="modal-field">
          <label for="subs-season">Season <span class="field-optional">(optional)</span></label>
          <input type="number" id="subs-season" value="${escapeHtml(defaultSeason)}" placeholder="e.g. 4" min="1" autocomplete="off">
        </div>
        <div class="modal-field">
          <label for="subs-episodes">Episodes <span class="field-optional">(optional)</span></label>
          <input type="text" id="subs-episodes" value="${escapeHtml(defaultEpisodes)}" placeholder="e.g. 28, 29, 30" autocomplete="off">
        </div>
      </div>
      <div class="modal-field">
        <label>Max Results</label>
        <div class="toggle-group" id="subs-max-results">
          <button type="button" class="toggle-btn" data-value="1">1</button>
          <button type="button" class="toggle-btn active" data-value="3">3</button>
          <button type="button" class="toggle-btn" data-value="5">5</button>
          <button type="button" class="toggle-btn" data-value="8">8</button>
        </div>
      </div>
      <div class="modal-actions">
        <button class="btn btn-ghost modal-cancel">Cancel</button>
        <button class="btn btn-primary modal-confirm">Search</button>
      </div>
    </div>
  `;

  document.body.appendChild(modal);

  const queryInput = modal.querySelector("#subs-query");
  const seasonInput = modal.querySelector("#subs-season");
  const episodesInput = modal.querySelector("#subs-episodes");
  const confirmBtn = modal.querySelector(".modal-confirm");
  const toggleGroup = modal.querySelector("#subs-max-results");
  let maxResults = 3;

  // Toggle group handlers
  toggleGroup.querySelectorAll(".toggle-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      toggleGroup.querySelectorAll(".toggle-btn").forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      maxResults = parseInt(btn.dataset.value, 10);
    });
  });

  modal.querySelector(".modal-cancel").addEventListener("click", () => modal.remove());

  confirmBtn.addEventListener("click", () => {
    const query = queryInput.value.trim();
    if (!query) {
      alert("Search query is required");
      return;
    }

    // Close modal immediately
    modal.remove();

    // Show searching toast and run search
    showSubsToast({ searching: true });
    submitSubtitleSearch(item, query, seasonInput.value, episodesInput.value, maxResults);
  });

  // Close on overlay click
  modal.addEventListener("click", (e) => {
    if (e.target === modal) modal.remove();
  });

  // Close on Escape
  const handleEscape = (e) => {
    if (e.key === "Escape") {
      modal.remove();
      document.removeEventListener("keydown", handleEscape);
    }
  };
  document.addEventListener("keydown", handleEscape);

  queryInput.focus();
  queryInput.select();
}

function submitSubtitleSearch(item, query, seasonStr, episodesStr, maxResults) {
  const data = {
    file_path: item.path,
    search_query: query,
    max_downloads: maxResults || 3
  };

  // Parse season (optional)
  const season = parseInt(seasonStr, 10);
  if (!isNaN(season) && season > 0) {
    data.season = season;
  }

  // Parse episodes (optional, comma-separated)
  if (episodesStr && episodesStr.trim()) {
    const episodes = episodesStr.split(/[,\s]+/)
      .map(s => parseInt(s.trim(), 10))
      .filter(n => !isNaN(n) && n > 0);
    if (episodes.length > 0) {
      data.episodes = episodes;
    }
  }

  fetch(`${API}/subtitles/search`, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(data)
  })
    .then(r => r.json().then(result => ({ ok: r.ok, result })))
    .then(({ ok, result }) => {
      if (!ok) {
        showSubsToast({ error: result.error || "Unknown error" });
      } else {
        showSubsToast(result);
      }
    })
    .catch(e => {
      showSubsToast({ error: e.message });
    });
}

function showSubsToast(result) {
  // Remove existing toast if any
  const existing = document.querySelector(".subs-toast");
  if (existing) existing.remove();

  const toast = document.createElement("div");

  if (result.searching) {
    toast.className = "subs-toast searching";
    toast.innerHTML = `<span class="toast-icon spin">&#128269;</span> Searching...`;
    document.body.appendChild(toast);
    return; // Don't auto-remove searching toast
  }

  if (result.deleting) {
    toast.className = "subs-toast searching";
    toast.innerHTML = `<span class="toast-icon spin">&#128465;</span> Deleting...`;
    document.body.appendChild(toast);
    return; // Don't auto-remove deleting toast
  }

  if (result.error) {
    toast.className = "subs-toast error";
    toast.innerHTML = `<span class="toast-icon">&#10007;</span> ${result.error}`;
  } else if (result.deleted !== undefined) {
    // Delete result
    if (result.count > 0) {
      toast.className = "subs-toast success";
      toast.innerHTML = `<span class="toast-icon">&#10003;</span> Deleted ${result.count} subtitle(s)`;
    } else {
      toast.className = "subs-toast warning";
      toast.innerHTML = `<span class="toast-icon">&#8709;</span> No subtitles found`;
    }
  } else if (result.saved && result.saved.length > 0) {
    toast.className = "subs-toast success";
    toast.innerHTML = `<span class="toast-icon">&#10003;</span> Saved ${result.saved.length} subtitle(s)`;
  } else if (result.found > 0) {
    toast.className = "subs-toast warning";
    toast.innerHTML = `<span class="toast-icon">!</span> Found ${result.found} but none matched`;
  } else {
    toast.className = "subs-toast warning";
    toast.innerHTML = `<span class="toast-icon">&#10007;</span> No subtitles found`;
  }

  // Override with custom message if provided
  if (result._custom) {
    toast.className = "subs-toast success";
    toast.innerHTML = `<span class="toast-icon">&#10003;</span> ${result._custom}`;
  }

  // Custom searching message
  if (result._searching && result._custom_searching) {
    toast.className = "subs-toast searching";
    toast.innerHTML = `<span class="toast-icon spin">&#9881;</span> ${result._custom_searching}`;
    document.body.appendChild(toast);
    return;
  }

  document.body.appendChild(toast);

  // Auto-remove after 4 seconds
  setTimeout(() => {
    toast.classList.add("fade-out");
    setTimeout(() => toast.remove(), 300);
  }, 4000);
}

async function handleDeleteSubsClick(item, type) {
  if (!confirm("Delete all subtitle files for this media?")) return;

  showSubsToast({ deleting: true });

  try {
    const r = await fetch(`${API}/subtitles`, {
      method: "DELETE",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({ file_path: item.path })
    });
    const result = await r.json();

    if (!r.ok) {
      showSubsToast({ error: result.error || "Failed to delete" });
    } else {
      showSubsToast(result);
    }
  } catch (e) {
    showSubsToast({ error: e.message });
  }
}

// Modal handling
let currentMediaItems = { movies: [], tv: [] };

// Sort, filter, selection state
let movieSort  = { col: "mtime", dir: "desc" };
let tvSort     = { col: "mtime", dir: "desc" };
let movieFilter = { text: "", status: "all" };
let tvFilter    = { text: "", status: "all" };
let displayedMovies = [];
let displayedTV     = [];
let movieSelection = new Set();
let tvSelection    = new Set();
let lastCheckedIdx = { movie: null, tv: null };

function showMediaModal(item, type) {
  const modal = $("#media-modal");
  const posterImg = $("#modal-poster-img");

  // Set poster
  if (item.poster) {
    posterImg.src = item.poster;
    posterImg.style.display = "block";
  } else {
    posterImg.style.display = "none";
  }

  // Set title
  if (type === "movie") {
    $("#modal-title").textContent = item.title || "Unknown";
    $("#modal-subtitle").textContent = item.year ? `(${item.year})` : "";
  } else {
    let ep = "";
    if (item.season != null && item.episode != null) {
      const s = String(item.season).padStart(2, "0");
      if (item.episodes && item.episodes.length > 1) {
        const first = String(item.episodes[0]).padStart(2, "0");
        const last = String(item.episodes[item.episodes.length - 1]).padStart(2, "0");
        ep = `S${s}E${first}-E${last}`;
      } else {
        ep = `S${s}E${String(item.episode).padStart(2, "0")}`;
      }
    }
    // Strip any leading episode codes from title (e.g., "E11 - Title" or "S03E10 - Title")
    let cleanTitle = (item.title || "").replace(/^[sS]?\d{0,2}[eE]\d{1,3}(?:[eE-]\d{1,3})*\s*[-–—]?\s*/g, "").trim();
    $("#modal-title").textContent = item.show || "Unknown";
    $("#modal-subtitle").textContent = ep ? `${ep} - ${cleanTitle}` : cleanTitle;
  }

  // Reset and hide description section initially
  const descGroup = $("#modal-description-group");
  descGroup.style.display = "none";
  $("#modal-description").textContent = "";
  $("#modal-genres").textContent = "";
  $("#modal-rating").textContent = "";

  // Fetch metadata (description, genres, rating) asynchronously
  fetchMediaMetadata(item, type);

  // File info
  $("#modal-path").textContent = item.path || "-";
  $("#modal-path").title = item.path || "";
  $("#modal-size").textContent = item.size_gb != null ? `${item.size_gb.toFixed(2)} GB` : "-";
  $("#modal-container").textContent = item.container ? item.container.toUpperCase() : "-";
  $("#modal-mtime").textContent = item.mtime_fmt || "-";

  // Video info
  $("#modal-vcodec").textContent = item.vcodec || "-";
  $("#modal-resolution").textContent = fmtRes(item.resolution);
  $("#modal-fps").textContent = item.frame_rate ? `${item.frame_rate} fps` : "-";
  $("#modal-vbitrate").textContent = item.video_bitrate_fmt || item.total_bitrate_fmt || "-";

  // Audio info
  $("#modal-acodec").textContent = item.acodec || "-";
  $("#modal-channels").textContent = item.audio_channels_fmt || (item.audio_channels ? `${item.audio_channels} channels` : "-");
  $("#modal-abitrate").textContent = item.audio_bitrate_fmt || "-";

  // Transcode info (only show if we have data)
  const transcodeGroup = $("#modal-transcode-group");
  if (item.processed_at || item.processing_duration) {
    transcodeGroup.style.display = "block";
    $("#modal-processed").textContent = item.processed_at_fmt || "-";
    $("#modal-duration").textContent = item.processing_duration_fmt || "-";
    $("#modal-source-size").textContent = item.source_size_gb != null ? `${item.source_size_gb.toFixed(2)} GB` : "-";
    $("#modal-compression").textContent = item.compression_ratio ? `${item.compression_ratio}x` : "-";
  } else {
    transcodeGroup.style.display = "none";
  }

  modal.classList.remove("hidden");
}

async function fetchMediaMetadata(item, type) {
  try {
    let url;
    if (type === "movie") {
      // Try to fetch by title and year
      const params = new URLSearchParams();
      if (item.title) params.append("title", item.title);
      if (item.year) params.append("year", item.year);
      url = `${API_BASE}/media/metadata/movie?${params}`;
    } else {
      // TV - fetch series metadata by show name
      const params = new URLSearchParams();
      if (item.show) params.append("title", item.show);
      url = `${API_BASE}/media/metadata/series?${params}`;
    }

    const resp = await fetch(url);
    if (!resp.ok) return;

    const metadata = await resp.json();
    if (metadata && metadata.description) {
      const descGroup = $("#modal-description-group");
      $("#modal-description").textContent = metadata.description;
      descGroup.style.display = "block";

      // Show genres if available
      if (metadata.genres) {
        $("#modal-genres").textContent = metadata.genres;
      }

      // Show rating if available
      if (metadata.rating) {
        $("#modal-rating").textContent = `Rating: ${metadata.rating}`;
      }
    }
  } catch (e) {
    console.debug("Failed to fetch metadata:", e);
  }
}

function hideMediaModal() {
  $("#media-modal").classList.add("hidden");
}

// Modal event listeners
document.addEventListener("DOMContentLoaded", () => {
  const modal = $("#media-modal");
  if (modal) {
    modal.querySelector(".modal-backdrop").addEventListener("click", hideMediaModal);
    modal.querySelector(".modal-close").addEventListener("click", hideMediaModal);
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && !modal.classList.contains("hidden")) {
        hideMediaModal();
      }
    });
  }
});

function posterHtml(url) {
  if (url) {
    return `<img class="poster-thumb" src="${url}" loading="lazy" onerror="this.outerHTML='<div class=\\'poster-placeholder\\'>🎬</div>'">`;
  }
  return `<div class="poster-placeholder">🎬</div>`;
}

function statusHtml(status, progress, ignored, reencodeProgress) {
  if (status === "processing") {
    const pct = progress != null ? Math.round(progress) : 0;
    return `<span class="status-badge status-processing">${pct}%</span>`;
  }
  if (status === "queued") {
    return `<span class="status-badge status-queued">Queued</span>`;
  }
  if (status === "re-encoding") {
    const pct = reencodeProgress != null ? Math.round(reencodeProgress) : 0;
    return `<span class="status-badge status-processing">${pct}%</span>`;
  }
  if (status === "pending") {
    if (ignored) {
      return `<span class="status-badge status-ignored">Ignored</span>`;
    }
    return `<span class="status-badge status-pending">Pending</span>`;
  }
  return `<span class="status-badge status-ready">Ready</span>`;
}

// Track scanning state for auto-refresh
let moviesScanning = false;
let tvScanning = false;
let hasProcessingItems = false;

// Track totals for stats display
let movieStats = { count: 0, sizeGb: 0 };
let tvStats = { count: 0, sizeGb: 0 };

function updateMediaStats() {
  const totalCount = movieStats.count + tvStats.count;
  const totalSizeGb = movieStats.sizeGb + tvStats.sizeGb;

  let sizeStr;
  if (totalSizeGb >= 1000) {
    sizeStr = `${(totalSizeGb / 1000).toFixed(2)} TB`;
  } else {
    sizeStr = `${totalSizeGb.toFixed(1)} GB`;
  }

  const statsEl = $("#stats-total");
  if (statsEl) {
    statsEl.innerHTML = `<span class="stat-value">${totalCount}</span> items · <span class="stat-value">${sizeStr}</span> total`;
  }
}

// ----- Filter & Sort Pipeline -----
function _statusKey(item) {
  if (item.status === "processing" || item.status === "queued" || item.status === "re-encoding") return "processing";
  if (item.status === "pending" && item.ignored) return "ignored";
  if (item.status === "pending") return "pending";
  return "ready";
}

function applyFilter(items, filter) {
  let out = items;
  if (filter.text) {
    const q = filter.text.toLowerCase();
    out = out.filter(d => {
      const blob = (d.title || "") + " " + (d.show || "") + " " + (d.year || "");
      return blob.toLowerCase().includes(q);
    });
  }
  if (filter.status !== "all") {
    out = out.filter(d => _statusKey(d) === filter.status);
  }
  return out;
}

function _resolutionRank(res) {
  if (!res) return 0;
  const m = res.match(/\d+x(\d+)/);
  return m ? parseInt(m[1], 10) : 0;
}

function _statusRank(status, ignored) {
  if (status === "processing") return 5;
  if (status === "re-encoding") return 4.5;
  if (status === "queued") return 4;
  if (status === "pending" && !ignored) return 3;
  if (status === "pending" && ignored) return 1;
  return 0; // ready
}

function applySort(items, sort) {
  const dir = sort.dir === "asc" ? 1 : -1;
  return [...items].sort((a, b) => {
    let cmp = 0;
    switch (sort.col) {
      case "title":
      case "show":
        cmp = (a[sort.col] || "").localeCompare(b[sort.col] || "");
        break;
      case "year":
      case "size_gb":
      case "mtime":
        cmp = (a[sort.col] || 0) - (b[sort.col] || 0);
        break;
      case "episode": {
        const aVal = (a.season || 0) * 10000 + (a.episode || 0);
        const bVal = (b.season || 0) * 10000 + (b.episode || 0);
        cmp = aVal - bVal;
        break;
      }
      case "resolution":
        cmp = _resolutionRank(a.resolution) - _resolutionRank(b.resolution);
        break;
      case "status":
        cmp = _statusRank(a.status, a.ignored) - _statusRank(b.status, b.ignored);
        break;
    }
    return cmp * dir;
  });
}

function getDisplayedMovies() {
  const filtered = applyFilter(currentMediaItems.movies, movieFilter);
  displayedMovies = applySort(filtered, movieSort);
  return displayedMovies;
}

function getDisplayedTV() {
  const filtered = applyFilter(currentMediaItems.tv, tvFilter);
  displayedTV = applySort(filtered, tvSort);
  return displayedTV;
}

// ----- Thead Builders -----
function _sortArrow(sort, col) {
  if (sort.col !== col) return "";
  return `<span class="sort-arrow">${sort.dir === "asc" ? "▲" : "▼"}</span>`;
}

function movieTheadHtml() {
  const s = movieSort;
  const cols = [
    { key: null, label: '<input type="checkbox" class="select-all" data-type="movie">', cls: "select-cell", sortable: false },
    { key: null, label: "", cls: "poster-cell", sortable: false },
    { key: "title", label: "Title" },
    { key: "year", label: "Year" },
    { key: "resolution", label: "Resolution" },
    { key: "size_gb", label: "Size" },
    { key: "mtime", label: "Changed" },
    { key: "status", label: "Status" },
    { key: null, label: "Action", sortable: false },
  ];
  return "<tr>" + cols.map(c => {
    if (c.sortable === false) return `<th class="${c.cls || ""}">${c.label}</th>`;
    const active = s.col === c.key ? " sort-active" : "";
    return `<th class="sortable${active}" data-sort="${c.key}">${c.label}${_sortArrow(s, c.key)}</th>`;
  }).join("") + "</tr>";
}

function tvTheadHtml() {
  const s = tvSort;
  const cols = [
    { key: null, label: '<input type="checkbox" class="select-all" data-type="tv">', cls: "select-cell", sortable: false },
    { key: null, label: "", cls: "poster-cell", sortable: false },
    { key: "show", label: "Series" },
    { key: "episode", label: "Episode" },
    { key: "resolution", label: "Resolution" },
    { key: "size_gb", label: "Size" },
    { key: "mtime", label: "Changed" },
    { key: "status", label: "Status" },
    { key: null, label: "Action", sortable: false },
  ];
  return "<tr>" + cols.map(c => {
    if (c.sortable === false) return `<th class="${c.cls || ""}">${c.label}</th>`;
    const active = s.col === c.key ? " sort-active" : "";
    return `<th class="sortable${active}" data-sort="${c.key}">${c.label}${_sortArrow(s, c.key)}</th>`;
  }).join("") + "</tr>";
}

// Cache last thead html so we don't re-bind sort/select-all listeners every render
let _moviesTheadHtml = "";
let _tvTheadHtml = "";

function _movieRowSig(m) {
  return JSON.stringify([
    m.status, m.progress, m.ignored, m.mtime_fmt, m.size_gb, m.elapsed_fmt,
    m.reencode_progress, m.title, m.year, m.resolution, m.runtime_min,
    m.vcodec, m.poster, movieSelection.has(m.path),
  ]);
}

function _buildMovieRowHtml(m) {
  const runtime = m.runtime_min ? `${m.runtime_min} min` : "";
  const codec = m.vcodec || "";
  const meta = [runtime, codec].filter(Boolean).join(" · ");
  const rowClass = m.status === "processing" ? "processing-row" : m.status === "queued" ? "queued-row" : m.status === "pending" ? "pending-row" : "";
  const ignoredClass = m.ignored ? " ignored-row" : "";
  const changed = m.status === "processing" ? (m.elapsed_fmt || "...") : (m.mtime_fmt || "-");
  const checked = movieSelection.has(m.path) ? "checked" : "";
  const key = encodeURIComponent(m.path);
  return `
    <tr class="${rowClass}${ignoredClass}" data-row-key="${key}">
      <td class="select-cell"><input type="checkbox" class="row-select" data-type="movie" ${checked}></td>
      <td class="poster-cell">${posterHtml(m.poster)}</td>
      <td>
        <div class="title-cell">
          <span class="title-main title-clickable" data-type="movie">${m.title || "-"}</span>
          ${meta ? `<span class="title-meta">${meta}</span>` : ""}
        </div>
      </td>
      <td>${m.year ?? "-"}</td>
      <td>${fmtRes(m.resolution)}</td>
      <td>${m.size_gb != null ? `${m.size_gb.toFixed(2)} GB` : "-"}</td>
      <td class="changed-cell">${changed}</td>
      <td>${statusHtml(m.status, m.progress, m.ignored, m.reencode_progress)}</td>
      <td class="action-cell">${actionHtml(m, "movie", "")}</td>
    </tr>
  `;
}

function _wireMovieRow(tr, item) {
  const path = item.path;
  const cb = tr.querySelector(".row-select");
  if (cb) {
    cb.addEventListener("click", (e) => {
      const idx = displayedMovies.findIndex(d => d.path === path);
      if (idx === -1) return;
      if (e.shiftKey && lastCheckedIdx.movie !== null) {
        const start = Math.min(lastCheckedIdx.movie, idx);
        const end = Math.max(lastCheckedIdx.movie, idx);
        for (let i = start; i <= end; i++) {
          const it = displayedMovies[i];
          if (!it) continue;
          if (cb.checked) movieSelection.add(it.path);
          else movieSelection.delete(it.path);
        }
      } else {
        if (cb.checked) movieSelection.add(path);
        else movieSelection.delete(path);
      }
      lastCheckedIdx.movie = idx;
      // Re-render — keyed diff will only repaint rows whose selection sig changed
      renderMoviesTable(currentMediaItems.movies);
    });
  }
  const titleEl = tr.querySelector(".title-clickable");
  if (titleEl) {
    titleEl.addEventListener("click", () => {
      const live = currentMediaItems.movies.find(m => m.path === path) || item;
      showMediaModal(live, "movie");
    });
  }
  const handlers = [
    [".btn-stop", handleStopClick],
    [".btn-transcode", handleTranscodeClick],
    [".btn-ignore", handleIgnoreClick],
    [".btn-subs", showSubtitleSearchModal],
    [".btn-delete-subs", handleDeleteSubsClick],
    [".btn-enrich", handleEnrichClick],
  ];
  for (const [sel, fn] of handlers) {
    tr.querySelectorAll(sel).forEach(el => {
      el.addEventListener("click", (e) => {
        e.stopPropagation();
        const live = currentMediaItems.movies.find(m => m.path === path) || item;
        fn(live, "movie");
      });
    });
  }
}

// ----- Tile renderers (movie + TV) — share state, sigs, wireRow with the table renderer -----
function _movieTileSig(m) {
  // Same axes as table sig; tile layout reuses the same data
  return _movieRowSig(m);
}

function _buildMovieTileHtml(m) {
  const rowClass = m.status === "processing" ? "processing-row" : m.status === "queued" ? "queued-row" : m.status === "pending" ? "pending-row" : "";
  const ignoredClass = m.ignored ? " ignored-row" : "";
  const checked = movieSelection.has(m.path) ? "checked" : "";
  const key = encodeURIComponent(m.path);
  const titleAttr = (m.title || "").replace(/"/g, "&quot;");

  let statusPill = "";
  if (m.status === "processing") statusPill = `<span class="tile-status-pill" style="background:rgba(90,169,255,.85)">${Math.round(m.progress || 0)}%</span>`;
  else if (m.status === "queued") statusPill = `<span class="tile-status-pill" style="background:rgba(139,92,246,.85)">Queued</span>`;
  else if (m.status === "pending") statusPill = `<span class="tile-status-pill" style="background:rgba(245,158,11,.85)">Pending</span>`;
  else if (m.status === "re-encoding") statusPill = `<span class="tile-status-pill" style="background:rgba(90,169,255,.85)">Re-enc ${Math.round(m.reencode_progress || 0)}%</span>`;
  else if (m.ignored) statusPill = `<span class="tile-status-pill" style="background:rgba(120,120,120,.85)">Ignored</span>`;

  let progressOverlay = "";
  if (m.status === "processing") {
    const pct = Math.round(m.progress || 0);
    progressOverlay = `<div class="tile-progress-overlay"><div class="tile-progress-bar"><div style="width:${pct}%"></div></div><div class="tile-progress-text"><span>${pct}%</span><span>${m.elapsed_fmt || ""}</span></div></div>`;
  } else if (m.status === "re-encoding") {
    const pct = Math.round(m.reencode_progress || 0);
    progressOverlay = `<div class="tile-progress-overlay"><div class="tile-progress-bar"><div style="width:${pct}%"></div></div><div class="tile-progress-text"><span>Re-encode ${pct}%</span><span>${m.reencode_elapsed_fmt || ""}</span></div></div>`;
  }

  const posterContent = m.poster
    ? `<img src="${m.poster}" alt="" loading="lazy" onerror="this.style.display='none'">`
    : `<div class="tile-no-poster">${m.title || "No poster"}</div>`;

  const subtitle = [m.year, fmtRes(m.resolution), m.size_gb != null ? `${m.size_gb.toFixed(1)} GB` : null].filter(Boolean).join(" · ");

  return `
    <div class="media-tile ${rowClass}${ignoredClass}" data-row-key="${key}">
      <div class="tile-poster-area">
        <input type="checkbox" class="tile-checkbox row-select" ${checked} title="Select">
        ${statusPill}
        ${posterContent}
        ${progressOverlay}
      </div>
      <div class="tile-body">
        <div class="tile-title title-clickable" title="${titleAttr}">${m.title || "-"}</div>
        ${subtitle ? `<div class="tile-meta">${subtitle}</div>` : ""}
        <div class="tile-actions">${actionHtml(m, "movie", "")}</div>
      </div>
    </div>
  `;
}

function _renderMoviesTiles(displayed, items) {
  const grid = $("#movies-tile-grid");
  if (!grid) return;
  if (displayed.length === 0) {
    grid.innerHTML = `<div class="empty-state" style="grid-column:1/-1;color:var(--text-muted);text-align:center;padding:40px">${items.length === 0 ? "No movies found" : "No matches"}</div>`;
    return;
  }
  patchTableRows(grid, displayed, {
    keyFn: m => m.path,
    sigFn: _movieTileSig,
    buildRowFn: _buildMovieTileHtml,
    wireRowFn: _wireMovieRow,  // identical click behavior — looks up by path
  });
}

function renderMoviesTable(items) {
  const body = $("#movies-body");
  const thead = $("#movies-thead");
  currentMediaItems.movies = items;

  // Stats on raw (unfiltered) items
  const readyItems = items.filter(m => m.status === "ready");
  movieStats.count = readyItems.length;
  movieStats.sizeGb = readyItems.reduce((sum, m) => sum + (m.size_gb || 0), 0);
  updateMediaStats();

  // Build displayed array through filter → sort pipeline
  const displayed = getDisplayedMovies();

  // Update thead only if content changed (reflects current sort indicators)
  const newTheadHtml = movieTheadHtml();
  if (_moviesTheadHtml !== newTheadHtml) {
    thead.innerHTML = newTheadHtml;
    _moviesTheadHtml = newTheadHtml;
    thead.querySelectorAll("th.sortable").forEach(th => {
      th.addEventListener("click", () => {
        const col = th.dataset.sort;
        if (movieSort.col === col) movieSort.dir = movieSort.dir === "asc" ? "desc" : "asc";
        else { movieSort.col = col; movieSort.dir = "asc"; }
        _moviesTheadHtml = "";  // force thead rebuild on next render so sort arrow updates
        fetchPage("movie", { reset: true });
      });
    });
    const selectAll = thead.querySelector(".select-all");
    if (selectAll) {
      selectAll.addEventListener("change", () => {
        const dsp = getDisplayedMovies();
        if (selectAll.checked) dsp.forEach(d => movieSelection.add(d.path));
        else dsp.forEach(d => movieSelection.delete(d.path));
        renderMoviesTable(currentMediaItems.movies);
      });
    }
  }

  if (displayed.length === 0) {
    body.innerHTML = `<tr><td colspan="9" style="text-align:center;color:var(--muted);padding:40px">${items.length === 0 ? "No movies found" : "No matches"}</td></tr>`;
    _renderMoviesTiles(displayed, items);
    updateBulkActionBar("movie");
    updateSelectAllState("movie");
    return;
  }

  hasProcessingItems = items.some(m => m.status === "processing" || m.status === "queued" || m.status === "pending" || m.status === "re-encoding") || hasProcessingItems;

  patchTableRows(body, displayed, {
    keyFn: m => m.path,
    sigFn: _movieRowSig,
    buildRowFn: _buildMovieRowHtml,
    wireRowFn: _wireMovieRow,
  });

  // Mirror to tile grid — both views stay in sync regardless of which is visible
  _renderMoviesTiles(displayed, items);

  updateBulkActionBar("movie");
  updateSelectAllState("movie");
}

// ----- Bulk Actions -----
function updateBulkActionBar(type) {
  const sel = type === "movie" ? movieSelection : tvSelection;
  const displayed = type === "movie" ? displayedMovies : displayedTV;
  const bar = $(`#${type}-bulk-actions`);
  const countEl = $(`#${type}-bulk-count`);
  if (!bar) return;

  // Only count selected items that are currently displayed
  const selectedDisplayed = displayed.filter(d => sel.has(d.path));
  const count = selectedDisplayed.length;

  if (count === 0) {
    bar.classList.add("hidden");
    return;
  }

  bar.classList.remove("hidden");
  countEl.textContent = `${count} selected`;

  // Enable/disable buttons based on selected statuses
  const hasEncodable = selectedDisplayed.some(d => (d.status === "pending" || d.status === "ready") && !d.ignored);
  const hasIgnorable = selectedDisplayed.some(d => d.status === "pending" && !d.ignored);
  const hasReady = selectedDisplayed.some(d => d.status === "ready");

  const transcodeBtn = bar.querySelector(".bulk-transcode");
  const ignoreBtn = bar.querySelector(".bulk-ignore");
  const deleteBtn = bar.querySelector(".bulk-delete");
  if (transcodeBtn) transcodeBtn.disabled = !hasEncodable;
  if (ignoreBtn) ignoreBtn.disabled = !hasIgnorable;
  if (deleteBtn) deleteBtn.disabled = !hasReady;

  _updateSelectAllBanner(type);
}

function updateSelectAllState(type) {
  const sel = type === "movie" ? movieSelection : tvSelection;
  const displayed = type === "movie" ? displayedMovies : displayedTV;
  const thead = type === "movie" ? $("#movies-thead") : $("#tv-thead");
  if (!thead) return;
  const cb = thead.querySelector(".select-all");
  if (!cb || displayed.length === 0) {
    if (cb) { cb.checked = false; cb.indeterminate = false; }
    return;
  }
  const selectedCount = displayed.filter(d => sel.has(d.path)).length;
  cb.checked = selectedCount === displayed.length;
  cb.indeterminate = selectedCount > 0 && selectedCount < displayed.length;
}

async function handleBulkTranscode(type) {
  const store = type === "movie" ? movieStore : tvStore;
  const flt = type === "movie" ? movieFilter : tvFilter;

  // Select-all-matching path: use server-side bulk-by-filter endpoint
  if (store.selectAllMatching) {
    if (!confirm(`Queue ALL ${store.totalCount} matching items for transcoding?`)) return;
    try {
      const r = await fetch(`${API}/transcode/batch-by-filter`, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({ media_type: type, filter: { q: flt.text, status: flt.status } }),
      });
      const result = await r.json();
      if (r.ok) {
        store.selectAllMatching = false;
        if (type === "movie") loadMovies(false); else loadTV(false);
        updateWorkerStatus();
      } else {
        alert(`Failed to queue batch: ${result.error || "Unknown error"}`);
      }
    } catch (e) { alert(`Error: ${e.message}`); }
    return;
  }

  // Per-path path: existing behavior over loaded selection
  const sel = type === "movie" ? movieSelection : tvSelection;
  const displayed = type === "movie" ? displayedMovies : displayedTV;
  const eligible = displayed.filter(d => sel.has(d.path) && (d.status === "pending" || d.status === "ready") && !d.ignored);

  if (eligible.length === 0) { alert("No eligible items selected."); return; }
  if (!confirm(`Queue ${eligible.length} item(s) for transcoding? They will be processed sequentially by a single worker.`)) return;

  const items = eligible.map(item => {
    const entry = { file_path: item.path, media_type: type };
    if (type === "movie") { entry.title = item.title; entry.year = item.year; }
    else { entry.show = item.show; entry.season = item.season; entry.episode = item.episode; entry.title = item.title; }
    return entry;
  });

  try {
    const r = await fetch(`${API}/transcode/batch`, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({ items })
    });
    const result = await r.json();
    if (r.ok) {
      if (type === "movie") loadMovies(false); else loadTV(false);
      updateWorkerStatus();
    } else {
      alert(`Failed to queue batch: ${result.error || "Unknown error"}`);
    }
  } catch (e) {
    alert(`Error: ${e.message}`);
  }
}

async function handleBulkIgnore(type) {
  const store = type === "movie" ? movieStore : tvStore;
  const flt = type === "movie" ? movieFilter : tvFilter;

  if (store.selectAllMatching) {
    if (!confirm(`Ignore ALL ${store.totalCount} matching pending items?`)) return;
    try {
      const r = await fetch(`${API}/media/ignore-by-filter`, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({ media_type: type, filter: { q: flt.text, status: flt.status } }),
      });
      const result = await r.json();
      if (r.ok) {
        store.selectAllMatching = false;
        if (type === "movie") loadMovies(false); else loadTV(false);
      } else { alert(`Failed: ${result.error || "Unknown error"}`); }
    } catch (e) { alert(`Error: ${e.message}`); }
    return;
  }

  const sel = type === "movie" ? movieSelection : tvSelection;
  const displayed = type === "movie" ? displayedMovies : displayedTV;
  const eligible = displayed.filter(d => sel.has(d.path) && d.status === "pending" && !d.ignored);

  if (eligible.length === 0) { alert("No eligible items selected."); return; }
  if (!confirm(`Ignore ${eligible.length} item(s)? They will be skipped by auto-transcode.`)) return;

  try {
    await Promise.all(eligible.map(item =>
      fetch(`${API}/media/ignore`, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({ file_path: item.path, action: "add" })
      })
    ));
    if (type === "movie") loadMovies(false); else loadTV(false);
  } catch (e) {
    alert(`Error: ${e.message}`);
  }
}

async function handleBulkDelete(type) {
  const store = type === "movie" ? movieStore : tvStore;
  const flt = type === "movie" ? movieFilter : tvFilter;

  if (store.selectAllMatching) {
    if (!confirm(`Delete output files for ALL ${store.totalCount} matching ready items?\n\nCompanion files (.nfo, .srt, etc) will also be deleted. Source files are NOT affected.`)) return;
    try {
      const r = await fetch(`${API}/media/output-by-filter`, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({ media_type: type, filter: { q: flt.text, status: flt.status } }),
      });
      const result = await r.json();
      if (r.ok) {
        store.selectAllMatching = false;
        if (type === "movie") loadMovies(true); else loadTV(true);
      } else { alert(`Failed: ${result.error || "Unknown error"}`); }
    } catch (e) { alert(`Error: ${e.message}`); }
    return;
  }

  const sel = type === "movie" ? movieSelection : tvSelection;
  const displayed = type === "movie" ? displayedMovies : displayedTV;
  const eligible = displayed.filter(d => sel.has(d.path) && d.status === "ready");

  if (eligible.length === 0) { alert("No eligible ready items selected."); return; }
  showDeleteConfirmModal(eligible, type);
}

function showDeleteConfirmModal(items, type) {
  const modal = document.createElement("div");
  modal.className = "modal-overlay";
  modal.innerHTML = `
    <div class="modal-content delete-confirm-modal">
      <h3>Delete Output Files</h3>
      <p class="modal-hint">This will delete <strong>${items.length}</strong> output file(s) and their companion files (.nfo, .srt, etc). Source files are not affected.</p>
      <ul class="delete-list">
        ${items.map(d => `<li>${escapeHtml(d.title || d.show || "Unknown")}<span class="delete-path">${escapeHtml(d.path)}</span></li>`).join("")}
      </ul>
      <div class="modal-actions">
        <button class="btn btn-ghost modal-cancel">Cancel</button>
        <button class="btn btn-danger modal-confirm">Delete ${items.length} file(s)</button>
      </div>
    </div>
  `;

  document.body.appendChild(modal);

  modal.querySelector(".modal-cancel").addEventListener("click", () => modal.remove());
  modal.querySelector(".modal-confirm").addEventListener("click", async () => {
    const confirmBtn = modal.querySelector(".modal-confirm");
    confirmBtn.disabled = true;
    confirmBtn.textContent = "Deleting...";

    try {
      const r = await fetch(API + "/media/output", {
        method: "DELETE",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({ paths: items.map(d => d.path) })
      });
      const result = await r.json();
      if (r.ok) {
        // Clear selection for deleted items
        const sel = type === "movie" ? movieSelection : tvSelection;
        items.forEach(d => sel.delete(d.path));
        modal.remove();
        // Force refresh
        if (type === "movie") loadMovies(true);
        else loadTV(true);
      } else {
        alert("Delete failed: " + (result.error || "Unknown error"));
        confirmBtn.disabled = false;
        confirmBtn.textContent = "Delete " + items.length + " file(s)";
      }
    } catch (e) {
      alert("Delete failed: " + e.message);
      confirmBtn.disabled = false;
      confirmBtn.textContent = "Delete " + items.length + " file(s)";
    }
  });

  modal.addEventListener("click", (e) => { if (e.target === modal) modal.remove(); });
  const handleEscape = (e) => {
    if (e.key === "Escape") { modal.remove(); document.removeEventListener("keydown", handleEscape); }
  };
  document.addEventListener("keydown", handleEscape);
}

async function handleBulkEnrich(type) {
  const sel = type === "movie" ? movieSelection : tvSelection;
  const displayed = type === "movie" ? displayedMovies : displayedTV;
  const eligible = displayed.filter(d => sel.has(d.path) && !d.ignored);

  if (eligible.length === 0) { alert("No eligible items selected."); return; }
  if (!confirm(`Fetch metadata for ${eligible.length} item(s)?`)) return;

  let done = 0, nfos = 0, posters = 0;
  showSubsToast({ _searching: true, _custom_searching: `Enriching 0/${eligible.length}...` });

  for (const item of eligible) {
    try {
      const r = await fetch(`${API}/media/enrich`, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({ path: item.path, force: true })
      });
      const result = await r.json();
      if (result.nfo_written) nfos++;
      if (result.poster_downloaded) posters++;
    } catch (e) { /* continue */ }
    done++;
    const existing = document.querySelector(".subs-toast");
    if (existing) existing.innerHTML = `<span class="toast-icon spin">&#9881;</span> Enriching ${done}/${eligible.length}...`;
  }

  showSubsToast({ _custom: `Done: ${nfos} NFOs, ${posters} posters` });
  if (type === "movie") loadMovies(false); else loadTV(false);
}

async function handleEnrichAll() {
  const enrichBtn = event ? event.target : null;
  if (enrichBtn) { enrichBtn.disabled = true; enrichBtn.textContent = "Starting..."; }

  try {
    const r = await fetch(`${API}/media/enrich-all`, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({ force: true }),
    });
    const result = await r.json();
    if (!r.ok) {
      alert(result.error || "Failed to start enrichment");
      if (enrichBtn) { enrichBtn.disabled = false; enrichBtn.textContent = "Enrich All"; }
      return;
    }

    // Poll progress
    const pollInterval = setInterval(async () => {
      try {
        const sr = await fetch(`${API}/media/enrich-status`);
        const status = await sr.json();

        if (enrichBtn) {
          enrichBtn.textContent = `${status.processed}/${status.total} (${status.nfo_written} NFOs)`;
        }

        if (!status.running) {
          clearInterval(pollInterval);
          if (enrichBtn) { enrichBtn.disabled = false; enrichBtn.textContent = "Enrich All"; }
          showSubsToast({ _custom: `Enrichment complete: ${status.nfo_written} NFOs, ${status.posters_downloaded} posters` });
          loadMovies(false);
          loadTV(false);
        }
      } catch (e) {
        clearInterval(pollInterval);
        if (enrichBtn) { enrichBtn.disabled = false; enrichBtn.textContent = "Enrich All"; }
      }
    }, 2000);
  } catch (e) {
    alert("Error: " + e.message);
    if (enrichBtn) { enrichBtn.disabled = false; enrichBtn.textContent = "Enrich All"; }
  }
}

// ----- Pagination engine (Phase 2: server-side filter/sort + infinite scroll) -----
const movieStore = { page: 0, pageSize: 50, totalCount: 0, hasMore: false, inflight: null, selectAllMatching: false, didReset: false };
const tvStore    = { page: 0, pageSize: 50, totalCount: 0, hasMore: false, inflight: null, selectAllMatching: false, didReset: false };

function _storeFor(type) { return type === "movie" ? movieStore : tvStore; }
function _filterFor(type) { return type === "movie" ? movieFilter : tvFilter; }
function _sortFor(type)   { return type === "movie" ? movieSort   : tvSort; }

async function fetchPage(type, { reset = false, force = false } = {}) {
  const store = _storeFor(type);
  const f = _filterFor(type);
  const s = _sortFor(type);
  if (store.inflight) try { store.inflight.abort(); } catch {}

  if (reset) {
    store.page = 1;
    store.didReset = true;
    currentMediaItems[type === "movie" ? "movies" : "tv"] = [];
    if (type === "movie") movieSelection.clear();
    else tvSelection.clear();
    store.selectAllMatching = false;
    _updateSelectAllBanner(type);
  } else {
    if (!store.hasMore) return;
    store.page = (store.page || 0) + 1;
  }

  const params = new URLSearchParams({
    page: String(store.page),
    page_size: String(store.pageSize),
    status: f.status || "all",
    sort: s.col || "mtime",
    sort_order: s.dir || "desc",
  });
  if (f.text) params.set("q", f.text);
  if (force) params.set("refresh", "1");

  const url = `${API}/media/${type === "movie" ? "movies" : "tv"}?${params.toString()}`;
  const ctrl = new AbortController();
  store.inflight = ctrl;
  const skelTarget = type === "movie" ? "#movies-body" : "#tv-body";
  if (reset) {
    const body = $(skelTarget);
    if (body) body.innerHTML = type === "movie" ? moviesSkeleton() : tvSkeleton();
  }

  try {
    const r = await fetch(url, { headers: { Accept: "application/json" }, cache: "no-store", signal: ctrl.signal });
    if (!r.ok) return;
    const data = await r.json();
    if (ctrl !== store.inflight) return; // a newer fetch superseded us
    const items = Array.isArray(data.items) ? data.items : [];

    // Append (or set on reset)
    const arrKey = type === "movie" ? "movies" : "tv";
    if (reset) currentMediaItems[arrKey] = items;
    else {
      // Dedup against existing loaded keys
      const existing = new Set(currentMediaItems[arrKey].map(x => x.path));
      for (const it of items) if (!existing.has(it.path)) currentMediaItems[arrKey].push(it);
    }

    store.totalCount = data.total_count || 0;
    store.hasMore = data.has_more === true;

    if (type === "movie") {
      moviesScanning = data.scanning === true;
      renderMoviesTable(currentMediaItems.movies);
    } else {
      tvScanning = data.scanning === true;
      renderTVTable(currentMediaItems.tv);
    }
    _updateSelectAllBanner(type);
  } catch (e) {
    if (e.name !== "AbortError") console.error(`[fetchPage/${type}]`, e);
  } finally {
    if (ctrl === store.inflight) store.inflight = null;
  }
}

async function loadMovies(forceRefresh = false){
  return fetchPage("movie", { reset: true, force: !!forceRefresh });
}

function _tvRowSig(e) {
  return JSON.stringify([
    e.status, e.progress, e.ignored, e.mtime_fmt, e.size_gb, e.elapsed_fmt,
    e.reencode_progress, e.show, e.title, e.season, e.episode,
    JSON.stringify(e.episodes || null), e.resolution, e.runtime_min, e.vcodec,
    e.poster, tvSelection.has(e.path),
  ]);
}

function _buildTVRowHtml(e) {
  let epLabel = "-";
  if (e.season != null && e.episode != null) {
    const s = String(e.season).padStart(2, "0");
    if (e.episodes && e.episodes.length > 1) {
      const first = String(e.episodes[0]).padStart(2, "0");
      const last = String(e.episodes[e.episodes.length - 1]).padStart(2, "0");
      epLabel = `S${s}E${first}-E${last}`;
    } else {
      epLabel = `S${s}E${String(e.episode).padStart(2, "0")}`;
    }
  }
  const runtime = e.runtime_min ? `${e.runtime_min} min` : "";
  const codec = e.vcodec || "";
  const meta = [runtime, codec].filter(Boolean).join(" · ");
  const rowClass = e.status === "processing" ? "processing-row" : e.status === "queued" ? "queued-row" : e.status === "pending" ? "pending-row" : "";
  const ignoredClass = e.ignored ? " ignored-row" : "";
  const changed = e.status === "processing" ? (e.elapsed_fmt || "...") : (e.mtime_fmt || "-");
  const checked = tvSelection.has(e.path) ? "checked" : "";
  const key = encodeURIComponent(e.path);
  return `
    <tr class="${rowClass}${ignoredClass}" data-row-key="${key}">
      <td class="select-cell"><input type="checkbox" class="row-select" data-type="tv" ${checked}></td>
      <td class="poster-cell">${posterHtml(e.poster)}</td>
      <td>
        <div class="title-cell">
          <span class="title-main title-clickable" data-type="tv">${e.show || "-"}</span>
          ${meta ? `<span class="title-meta">${meta}</span>` : ""}
        </div>
      </td>
      <td>${epLabel}</td>
      <td>${fmtRes(e.resolution)}</td>
      <td>${e.size_gb != null ? `${e.size_gb.toFixed(2)} GB` : "-"}</td>
      <td class="changed-cell">${changed}</td>
      <td>${statusHtml(e.status, e.progress, e.ignored, e.reencode_progress)}</td>
      <td class="action-cell">${actionHtml(e, "tv", "")}</td>
    </tr>
  `;
}

function _wireTVRow(tr, item) {
  const path = item.path;
  const cb = tr.querySelector(".row-select");
  if (cb) {
    cb.addEventListener("click", (e) => {
      const idx = displayedTV.findIndex(d => d.path === path);
      if (idx === -1) return;
      if (e.shiftKey && lastCheckedIdx.tv !== null) {
        const start = Math.min(lastCheckedIdx.tv, idx);
        const end = Math.max(lastCheckedIdx.tv, idx);
        for (let i = start; i <= end; i++) {
          const it = displayedTV[i];
          if (!it) continue;
          if (cb.checked) tvSelection.add(it.path);
          else tvSelection.delete(it.path);
        }
      } else {
        if (cb.checked) tvSelection.add(path);
        else tvSelection.delete(path);
      }
      lastCheckedIdx.tv = idx;
      renderTVTable(currentMediaItems.tv);
    });
  }
  const titleEl = tr.querySelector(".title-clickable");
  if (titleEl) {
    titleEl.addEventListener("click", () => {
      const live = currentMediaItems.tv.find(m => m.path === path) || item;
      showMediaModal(live, "tv");
    });
  }
  const handlers = [
    [".btn-stop", handleStopClick],
    [".btn-transcode", handleTranscodeClick],
    [".btn-ignore", handleIgnoreClick],
    [".btn-subs", showSubtitleSearchModal],
    [".btn-delete-subs", handleDeleteSubsClick],
    [".btn-enrich", handleEnrichClick],
  ];
  for (const [sel, fn] of handlers) {
    tr.querySelectorAll(sel).forEach(el => {
      el.addEventListener("click", (e) => {
        e.stopPropagation();
        const live = currentMediaItems.tv.find(m => m.path === path) || item;
        fn(live, "tv");
      });
    });
  }
}

function _tvTileSig(e) { return _tvRowSig(e); }

function _buildTVTileHtml(e) {
  const rowClass = e.status === "processing" ? "processing-row" : e.status === "queued" ? "queued-row" : e.status === "pending" ? "pending-row" : "";
  const ignoredClass = e.ignored ? " ignored-row" : "";
  const checked = tvSelection.has(e.path) ? "checked" : "";
  const key = encodeURIComponent(e.path);

  let epLabel = "";
  if (e.season != null && e.episode != null) {
    const s = String(e.season).padStart(2, "0");
    if (e.episodes && e.episodes.length > 1) {
      const first = String(e.episodes[0]).padStart(2, "0");
      const last = String(e.episodes[e.episodes.length - 1]).padStart(2, "0");
      epLabel = `S${s}E${first}-E${last}`;
    } else {
      epLabel = `S${s}E${String(e.episode).padStart(2, "0")}`;
    }
  }

  let statusPill = "";
  if (e.status === "processing") statusPill = `<span class="tile-status-pill" style="background:rgba(90,169,255,.85)">${Math.round(e.progress || 0)}%</span>`;
  else if (e.status === "queued") statusPill = `<span class="tile-status-pill" style="background:rgba(139,92,246,.85)">Queued</span>`;
  else if (e.status === "pending") statusPill = `<span class="tile-status-pill" style="background:rgba(245,158,11,.85)">Pending</span>`;
  else if (e.status === "re-encoding") statusPill = `<span class="tile-status-pill" style="background:rgba(90,169,255,.85)">Re-enc ${Math.round(e.reencode_progress || 0)}%</span>`;
  else if (e.ignored) statusPill = `<span class="tile-status-pill" style="background:rgba(120,120,120,.85)">Ignored</span>`;

  let progressOverlay = "";
  if (e.status === "processing") {
    const pct = Math.round(e.progress || 0);
    progressOverlay = `<div class="tile-progress-overlay"><div class="tile-progress-bar"><div style="width:${pct}%"></div></div><div class="tile-progress-text"><span>${pct}%</span><span>${e.elapsed_fmt || ""}</span></div></div>`;
  } else if (e.status === "re-encoding") {
    const pct = Math.round(e.reencode_progress || 0);
    progressOverlay = `<div class="tile-progress-overlay"><div class="tile-progress-bar"><div style="width:${pct}%"></div></div><div class="tile-progress-text"><span>Re-encode ${pct}%</span><span>${e.reencode_elapsed_fmt || ""}</span></div></div>`;
  }

  const posterContent = e.poster
    ? `<img src="${e.poster}" alt="" loading="lazy" onerror="this.style.display='none'">`
    : `<div class="tile-no-poster">${e.show || "No poster"}</div>`;

  const showTitle = e.show || "-";
  const epTitle = e.title || "";
  const subtitle = [epLabel, fmtRes(e.resolution), e.size_gb != null ? `${e.size_gb.toFixed(1)} GB` : null].filter(Boolean).join(" · ");

  return `
    <div class="media-tile ${rowClass}${ignoredClass}" data-row-key="${key}">
      <div class="tile-poster-area">
        <input type="checkbox" class="tile-checkbox row-select" ${checked} title="Select">
        ${statusPill}
        ${posterContent}
        ${progressOverlay}
      </div>
      <div class="tile-body">
        <div class="tile-title title-clickable" title="${showTitle.replace(/"/g, "&quot;")}">${showTitle}</div>
        ${epTitle ? `<div class="tile-meta" style="color:var(--text)">${epTitle}</div>` : ""}
        ${subtitle ? `<div class="tile-meta">${subtitle}</div>` : ""}
        <div class="tile-actions">${actionHtml(e, "tv", "")}</div>
      </div>
    </div>
  `;
}

function _renderTVTiles(displayed, items) {
  const grid = $("#tv-tile-grid");
  if (!grid) return;
  if (displayed.length === 0) {
    grid.innerHTML = `<div class="empty-state" style="grid-column:1/-1;color:var(--text-muted);text-align:center;padding:40px">${items.length === 0 ? "No TV shows found" : "No matches"}</div>`;
    return;
  }
  patchTableRows(grid, displayed, {
    keyFn: e => e.path,
    sigFn: _tvTileSig,
    buildRowFn: _buildTVTileHtml,
    wireRowFn: _wireTVRow,
  });
}

function renderTVTable(items) {
  const body = $("#tv-body");
  const thead = $("#tv-thead");
  currentMediaItems.tv = items;

  const readyItems = items.filter(e => e.status === "ready");
  tvStats.count = readyItems.length;
  tvStats.sizeGb = readyItems.reduce((sum, e) => sum + (e.size_gb || 0), 0);
  updateMediaStats();

  const displayed = getDisplayedTV();

  const newTheadHtml = tvTheadHtml();
  if (_tvTheadHtml !== newTheadHtml) {
    thead.innerHTML = newTheadHtml;
    _tvTheadHtml = newTheadHtml;
    thead.querySelectorAll("th.sortable").forEach(th => {
      th.addEventListener("click", () => {
        const col = th.dataset.sort;
        if (tvSort.col === col) tvSort.dir = tvSort.dir === "asc" ? "desc" : "asc";
        else { tvSort.col = col; tvSort.dir = "asc"; }
        _tvTheadHtml = "";
        fetchPage("tv", { reset: true });
      });
    });
    const selectAll = thead.querySelector(".select-all");
    if (selectAll) {
      selectAll.addEventListener("change", () => {
        const dsp = getDisplayedTV();
        if (selectAll.checked) dsp.forEach(d => tvSelection.add(d.path));
        else dsp.forEach(d => tvSelection.delete(d.path));
        renderTVTable(currentMediaItems.tv);
      });
    }
  }

  if (displayed.length === 0) {
    body.innerHTML = `<tr><td colspan="9" style="text-align:center;color:var(--muted);padding:40px">${items.length === 0 ? "No TV shows found" : "No matches"}</td></tr>`;
    _renderTVTiles(displayed, items);
    updateBulkActionBar("tv");
    updateSelectAllState("tv");
    return;
  }

  hasProcessingItems = items.some(e => e.status === "processing" || e.status === "queued" || e.status === "pending" || e.status === "re-encoding") || hasProcessingItems;

  patchTableRows(body, displayed, {
    keyFn: e => e.path,
    sigFn: _tvRowSig,
    buildRowFn: _buildTVRowHtml,
    wireRowFn: _wireTVRow,
  });

  _renderTVTiles(displayed, items);

  updateBulkActionBar("tv");
  updateSelectAllState("tv");
}

async function loadTV(forceRefresh = false){
  return fetchPage("tv", { reset: true, force: !!forceRefresh });
}

// Stub — real implementation assigned later in the IIFE kickoff. Declared as a
// function declaration so it's hoisted and safe to call before the kickoff runs.
function _updateSelectAllBanner(type) { /* no-op until kickoff installs real impl */ }
  $("#refresh-movies").addEventListener("click", () => loadMovies(true));
  $("#refresh-tv").addEventListener("click", () => loadTV(true));
  const _enrichAllMovies = $("#enrich-all-movies");
  if (_enrichAllMovies) _enrichAllMovies.addEventListener("click", () => handleEnrichAll());
  const _enrichAllTV = $("#enrich-all-tv");
  if (_enrichAllTV) _enrichAllTV.addEventListener("click", () => handleEnrichAll());

  // ----- Filter event listeners (debounced server-side fetch) -----
  let _movieSearchTimer = null;
  $("#movie-search").addEventListener("input", (e) => {
    clearTimeout(_movieSearchTimer);
    _movieSearchTimer = setTimeout(() => {
      movieFilter.text = e.target.value.trim();
      fetchPage("movie", { reset: true });
    }, 250);
  });
  $("#movie-status-filter").addEventListener("change", (e) => {
    movieFilter.status = e.target.value;
    fetchPage("movie", { reset: true });
  });

  let _tvSearchTimer = null;
  $("#tv-search").addEventListener("input", (e) => {
    clearTimeout(_tvSearchTimer);
    _tvSearchTimer = setTimeout(() => {
      tvFilter.text = e.target.value.trim();
      fetchPage("tv", { reset: true });
    }, 250);
  });
  $("#tv-status-filter").addEventListener("change", (e) => {
    tvFilter.status = e.target.value;
    fetchPage("tv", { reset: true });
  });

  // Tile-mode sort dropdowns
  $$(".tile-sort-select").forEach(sel => {
    sel.addEventListener("change", (e) => {
      const target = sel.dataset.sortTarget;
      const [col, dir] = e.target.value.split(":");
      const sortObj = target === "movie" ? movieSort : tvSort;
      sortObj.col = col;
      sortObj.dir = dir;
      _moviesTheadHtml = ""; _tvTheadHtml = "";  // force thead re-render so arrows update on next table view
      fetchPage(target, { reset: true });
    });
  });

  // ----- Bulk action button listeners -----
  $$(".bulk-transcode").forEach(btn => {
    btn.addEventListener("click", () => handleBulkTranscode(btn.dataset.type));
  });
  $$(".bulk-ignore").forEach(btn => {
    btn.addEventListener("click", () => handleBulkIgnore(btn.dataset.type));
  });
  $$(".bulk-delete").forEach(btn => {
    btn.addEventListener("click", () => handleBulkDelete(btn.dataset.type));
  });
  $$(".bulk-enrich").forEach(btn => {
    btn.addEventListener("click", () => handleBulkEnrich(btn.dataset.type));
  });

  // ----- Settings -----
  let settingsSchema = {};
  let settingsOriginal = {};
  let settingsModified = {};
  let currentSection = null;
  let encodingPresets = [];
  let activePresetId = null;

  async function loadSettings() {
    const container = $("#settings-container");
    const navContainer = $("#settings-subnav");
    container.innerHTML = `<div style="padding:40px;text-align:center;color:var(--muted)">Loading...</div>`;
    navContainer.innerHTML = "";

    try {
      const r = await fetch(`${API}/settings`, {headers:{Accept:"application/json"}});
      const data = await r.json();

      if (!r.ok || data.error) {
        throw new Error(data.error || `HTTP ${r.status}`);
      }

      settingsSchema = data.schema;
      settingsOriginal = {...data.values};
      settingsModified = {...data.values};
      encodingPresets = data.encoding_presets || [];
      activePresetId = data.active_preset_id || null;

      renderSettingsNav();
      // Show first section by default
      const firstSection = Object.keys(settingsSchema)[0];
      if (firstSection) showSettingsSection(firstSection);
    } catch (e) {
      container.innerHTML = `<div style="padding:40px;text-align:center;color:var(--danger)">Failed to load: ${e.message}</div>`;
      console.error("Settings load error:", e);
    }
  }

  function renderSettingsNav() {
    const navContainer = $("#settings-subnav");
    navContainer.innerHTML = "";

    for (const [sectionKey, section] of Object.entries(settingsSchema)) {
      const btn = document.createElement("button");
      btn.className = "nav-subitem";
      btn.dataset.section = sectionKey;
      btn.textContent = section.label;
      btn.addEventListener("click", () => {
        // Show settings view
        $$(".nav-item").forEach(x => x.classList.remove("active"));
        settingsNavItem.classList.add("active");
        $$(".view").forEach(v => v.classList.toggle("visible", v.id === "view-settings"));
        // Show selected section
        showSettingsSection(sectionKey);
      });
      navContainer.appendChild(btn);
    }
  }

  function showSettingsSection(sectionKey) {
    currentSection = sectionKey;
    const section = settingsSchema[sectionKey];
    if (!section) return;

    // Update nav active state in sidebar subnav
    $$(".nav-subitem").forEach(btn => {
      btn.classList.toggle("active", btn.dataset.section === sectionKey);
    });

    // Update title
    $("#settings-section-title").textContent = section.label;

    // Render fields
    const container = $("#settings-container");
    container.innerHTML = "";

    // Special handling for integrations section
    if (section.type === "integrations") {
      renderIntegrationsSection(container, section);
      return;
    }

    // Special handling for general grouped section
    if (section.type === "general_grouped") {
      renderGeneralSection(container, section);
      return;
    }

    // Special handling for subtitle providers section
    if (section.type === "subtitle_providers") {
      renderSubtitleProvidersSection(container, section);
      return;
    }

    // Special handling for encoding section — presets + two-column layout
    if (sectionKey === "encoding") {
      renderEncodingSection(container, section);
      return;
    }

    renderGenericFields(container, section);
  }

  function renderGenericFields(container, section) {
    const fieldsDiv = document.createElement("div");
    fieldsDiv.className = "settings-fields";

    for (const [fieldKey, field] of Object.entries(section.fields)) {
      if (fieldApplies(field)) renderSettingField(fieldsDiv, fieldKey, field);
    }

    container.appendChild(fieldsDiv);
  }

  // ----- General Grouped Section -----
  function renderGeneralSection(container, section) {
    const groups = section.groups || {};
    const grid = document.createElement("div");
    grid.className = "general-grid";
    const groupKeys = Object.keys(groups);

    for (const groupKey of groupKeys) {
      const groupDef = groups[groupKey];
      const fieldset = document.createElement("fieldset");
      fieldset.className = "general-fieldset";
      // Database spans full width below the top row
      if (groupKey === "database") fieldset.classList.add("general-grid-full");

      const legend = document.createElement("legend");
      legend.className = "general-group-title";
      legend.textContent = groupDef.label;
      fieldset.appendChild(legend);

      if (groupDef.hint) {
        const hint = document.createElement("p");
        hint.className = "general-group-hint";
        hint.textContent = groupDef.hint;
        fieldset.appendChild(hint);
      }

      const fieldsDiv = document.createElement("div");
      fieldsDiv.className = "settings-fields";
      for (const [fieldKey, field] of Object.entries(section.fields)) {
        if (field.group === groupKey && fieldApplies(field)) {
          renderSettingField(fieldsDiv, fieldKey, field);
        }
      }
      fieldset.appendChild(fieldsDiv);
      grid.appendChild(fieldset);
    }
    container.appendChild(grid);
  }

  // ----- Integrations Section -----
  const INTEGRATION_ICONS = {
    radarr:   '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M20 4H4c-1.1 0-2 .9-2 2v12c0 1.1.9 2 2 2h16c1.1 0 2-.9 2-2V6c0-1.1-.9-2-2-2zm-8 12.5L6 13V11l6 3.5L18 11v2l-6 3.5zM18 9l-6 3.5L6 9V7l6 3.5L18 7v2z"/></svg>',
    sonarr:   '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M21 3H3c-1.1 0-2 .9-2 2v12c0 1.1.9 2 2 2h5v2h8v-2h5c1.1 0 2-.9 2-2V5c0-1.1-.9-2-2-2zm0 14H3V5h18v12z"/><path d="M8 10l5 3-5 3z"/></svg>',
    jellyfin: '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-2 14.5v-9l7 4.5-7 4.5z"/></svg>',
    tvdb:     '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M4 4h16v2H4zm0 4h16v12H4zm4 3v6h8v-6zm2 2h4v2h-4z"/></svg>',
    tmdb:     '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M18 4v1h-2V4c0-.55-.45-1-1-1H9c-.55 0-1 .45-1 1v1H6V4c0-.55-.45-1-1-1s-1 .45-1 1v16c0 .55.45 1 1 1s1-.45 1-1v-1h2v1c0 .55.45 1 1 1h6c.55 0 1-.45 1-1v-1h2v1c0 .55.45 1 1 1s1-.45 1-1V4c0-.55-.45-1-1-1s-1 .45-1 1zM8 17H6v-2h2zm0-4H6v-2h2zm0-4H6V7h2zm10 8h-2v-2h2zm0-4h-2v-2h2zm0-4h-2V7h2z"/></svg>',
    omdb:     '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 2l3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01z"/></svg>',
  };

  function getIntegrationStatus(cardKey, card, connData) {
    if (card.has_webhook && connData && connData[cardKey]) {
      const conn = connData[cardKey];
      if (conn.connected) return {cls: "conn-connected", text: "Connected"};
      if (conn.configured) return {cls: "conn-disconnected", text: "Not Connected"};
    }
    // Check if fields have values
    const hasValues = card.fields.some(fk => settingsModified[fk] && settingsModified[fk] !== "");
    if (hasValues) return {cls: "conn-configured", text: "Configured"};
    return {cls: "conn-not-configured", text: "Not Configured"};
  }

  let _integrationsConnData = null;

  async function renderIntegrationsSection(container, section) {
    container.innerHTML = `<div style="padding:40px;text-align:center;color:var(--text-muted)">Loading integrations...</div>`;

    try {
      const r = await fetch(`${API}/connections`, {headers:{Accept:"application/json"}, cache:"no-store"});
      _integrationsConnData = r.ok ? await r.json() : {};
    } catch (e) {
      _integrationsConnData = {};
    }

    container.innerHTML = "";
    const grid = document.createElement("div");
    grid.className = "integration-cards";

    for (const [cardKey, card] of Object.entries(section.cards)) {
      const status = getIntegrationStatus(cardKey, card, _integrationsConnData);
      const cardEl = document.createElement("div");
      cardEl.className = "integration-card";
      cardEl.dataset.card = cardKey;
      cardEl.innerHTML = `
        <div class="integration-card-icon">${INTEGRATION_ICONS[cardKey] || ""}</div>
        <div class="integration-card-name">${card.label}</div>
        <div class="integration-card-desc">${card.desc}</div>
        <span class="conn-badge ${status.cls}">${status.text}</span>
      `;
      cardEl.addEventListener("click", () => {
        showIntegrationModal(cardKey, card, section);
      });
      grid.appendChild(cardEl);
    }

    container.appendChild(grid);
  }

  function refreshIntegrationCards() {
    const section = settingsSchema.integrations;
    if (!section) return;
    const cards = document.querySelectorAll(".integration-card");
    cards.forEach(cardEl => {
      const cardKey = cardEl.dataset.card;
      const card = section.cards[cardKey];
      if (!card) return;
      const status = getIntegrationStatus(cardKey, card, _integrationsConnData);
      const badge = cardEl.querySelector(".conn-badge");
      if (badge) {
        badge.className = `conn-badge ${status.cls}`;
        badge.textContent = status.text;
      }
    });
  }

  function showIntegrationModal(cardKey, card, section) {
    const modal = document.createElement("div");
    modal.className = "modal-overlay";
    modal.innerHTML = `
      <div class="modal-content integration-modal">
        <div class="integration-modal-header">
          <div class="integration-modal-icon">${INTEGRATION_ICONS[cardKey] || ""}</div>
          <h3>${card.label}</h3>
        </div>
        <div class="integration-modal-fields"></div>
        ${card.has_webhook ? '<div class="integration-modal-webhook"></div>' : ''}
        ${["radarr","sonarr"].includes(cardKey) ? '<div class="integration-modal-extras"></div>' : ''}
        <div class="integration-modal-footer">
          <button class="btn btn-ghost modal-close-btn">Close</button>
        </div>
      </div>
    `;

    // Render settings fields
    const fieldsContainer = modal.querySelector(".integration-modal-fields");
    for (const fieldKey of card.fields) {
      const field = section.fields[fieldKey];
      if (field && fieldApplies(field)) renderSettingField(fieldsContainer, fieldKey, field);
    }

    // Render webhook controls if applicable
    if (card.has_webhook) {
      const webhookContainer = modal.querySelector(".integration-modal-webhook");
      const conn = _integrationsConnData ? _integrationsConnData[cardKey] : null;
      webhookContainer.innerHTML = `
        <h4 class="integration-webhook-title">Webhook</h4>
        <div class="connection-status">${renderConnectionStatus(conn)}</div>
        <div class="connection-actions">${renderConnectionActions(cardKey, conn)}</div>
      `;

      webhookContainer.querySelectorAll(".btn-connect").forEach(btn => {
        btn.addEventListener("click", async () => {
          btn.disabled = true; btn.textContent = "Connecting...";
          try {
            const r = await fetch(`${API}/connections/${btn.dataset.service}`, {method: "POST"});
            if (r.ok) {
              const cr = await fetch(`${API}/connections`, {headers:{Accept:"application/json"}, cache:"no-store"});
              _integrationsConnData = cr.ok ? await cr.json() : {};
              const conn2 = _integrationsConnData[cardKey];
              webhookContainer.querySelector(".connection-status").innerHTML = renderConnectionStatus(conn2);
              webhookContainer.querySelector(".connection-actions").innerHTML = renderConnectionActions(cardKey, conn2);
              attachWebhookListeners(webhookContainer, cardKey, card, section);
              refreshIntegrationCards();
            } else {
              const d = await r.json(); alert(`Failed: ${d.error || "Unknown error"}`);
              btn.disabled = false; btn.textContent = "Connect";
            }
          } catch (e) { alert(`Failed: ${e.message}`); btn.disabled = false; btn.textContent = "Connect"; }
        });
      });
      webhookContainer.querySelectorAll(".btn-disconnect").forEach(btn => {
        btn.addEventListener("click", async () => {
          if (!confirm(`Disconnect ${cardKey}? The webhook will be removed.`)) return;
          btn.disabled = true; btn.textContent = "Disconnecting...";
          try {
            const r = await fetch(`${API}/connections/${btn.dataset.service}`, {method: "DELETE"});
            if (r.ok) {
              const cr = await fetch(`${API}/connections`, {headers:{Accept:"application/json"}, cache:"no-store"});
              _integrationsConnData = cr.ok ? await cr.json() : {};
              const conn2 = _integrationsConnData[cardKey];
              webhookContainer.querySelector(".connection-status").innerHTML = renderConnectionStatus(conn2);
              webhookContainer.querySelector(".connection-actions").innerHTML = renderConnectionActions(cardKey, conn2);
              attachWebhookListeners(webhookContainer, cardKey, card, section);
              refreshIntegrationCards();
            } else {
              const d = await r.json(); alert(`Failed: ${d.error || "Unknown error"}`);
              btn.disabled = false; btn.textContent = "Disconnect";
            }
          } catch (e) { alert(`Failed: ${e.message}`); btn.disabled = false; btn.textContent = "Disconnect"; }
        });
      });
      webhookContainer.querySelectorAll(".btn-test").forEach(btn => {
        btn.addEventListener("click", async () => {
          btn.disabled = true; btn.textContent = "Testing...";
          try {
            const r = await fetch(`${API}/connections/${btn.dataset.service}/test`, {method: "POST"});
            const d = await r.json();
            alert(r.ok ? `${cardKey} test successful!` : `Test failed: ${d.error || "Unknown error"}`);
          } catch (e) { alert(`Test failed: ${e.message}`); }
          finally { btn.disabled = false; btn.textContent = "Test"; }
        });
      });
    }

    // Extra file extensions (Radarr/Sonarr only)
    if (["radarr","sonarr"].includes(cardKey)) {
      const extrasContainer = modal.querySelector(".integration-modal-extras");
      const conn = _integrationsConnData ? _integrationsConnData[cardKey] : null;
      extrasContainer.innerHTML = `
        <h4 class="integration-webhook-title">Preserved File Extensions</h4>
        ${renderExtraExtensionsBlock(cardKey, conn || {configured: true})}
      `;
      const cardEl = extrasContainer.querySelector("[data-ext-service]");
      if (cardEl) loadExtraExtensions(cardKey, cardEl);
      extrasContainer.querySelectorAll(".btn-ext-apply").forEach(btn => {
        btn.addEventListener("click", () => applyExtraExtensions(btn.dataset.service));
      });
      extrasContainer.querySelectorAll(".btn-ext-recommended").forEach(btn => {
        btn.addEventListener("click", () => {
          const input = extrasContainer.querySelector(`#ext-input-${btn.dataset.service}`);
          if (input) input.value = input.dataset.recommended || ".srt,.nfo,.jpg";
        });
      });
    }

    // Close handlers
    const closeModal = () => { modal.remove(); refreshIntegrationCards(); updateSettingsUI(); };
    modal.querySelector(".modal-close-btn").addEventListener("click", closeModal);
    modal.addEventListener("click", (e) => { if (e.target === modal) closeModal(); });
    const escHandler = (e) => { if (e.key === "Escape") { closeModal(); document.removeEventListener("keydown", escHandler); } };
    document.addEventListener("keydown", escHandler);

    document.body.appendChild(modal);
  }

  function attachWebhookListeners(webhookContainer, cardKey, card, section) {
    // Re-attach listeners after innerHTML replacement — handled inline above
  }

  // A field may declare the values of another field it's relevant for, e.g. encoder
  // threads do nothing once the GPU is encoding, and VAAPI has no preset knob.
  // Hiding them keeps a hardware preset from showing software-only settings that
  // would silently do nothing.
  function fieldApplies(field) {
    if (!field || !field.show_if) return true;
    return Object.entries(field.show_if).every(([key, allowed]) => {
      const cur = (settingsModified[key] ?? settingsOriginal[key] ?? "");
      return Array.isArray(allowed) ? allowed.includes(cur) : allowed === cur;
    });
  }

  // Keys that other fields' visibility depends on — changing one re-renders the
  // section so the form reshapes immediately.
  function settingsDependencyKeys() {
    const keys = new Set();
    const scan = fields => Object.values(fields || {}).forEach(f => {
      if (f && f.show_if) Object.keys(f.show_if).forEach(k => keys.add(k));
    });
    for (const section of Object.values(settingsSchema || {})) {
      scan(section.fields);
      Object.values(section.groups || {}).forEach(g => scan(g.fields));
    }
    return keys;
  }

  function renderSettingField(parent, fieldKey, field) {
    const value = settingsModified[fieldKey] || "";
    const isModified = settingsModified[fieldKey] !== settingsOriginal[fieldKey];
    const isPassword = field.type === "password";
    const isSelect = field.type === "select";

    const fieldEl = document.createElement("div");
    fieldEl.className = `setting-field${isModified ? " modified" : ""}`;
    fieldEl.dataset.key = fieldKey;

    if (isSelect) {
      const optionsHtml = (field.options || []).map(opt =>
        `<option value="${escapeHtml(opt.value)}"${opt.value === value ? " selected" : ""}${opt.disabled && opt.value !== value ? " disabled" : ""}>${escapeHtml(opt.label)}</option>`
      ).join("");

      fieldEl.innerHTML = `
        <label for="set-${fieldKey}">${field.label}</label>
        <div class="input-wrap">
          <select id="set-${fieldKey}" data-key="${fieldKey}">${optionsHtml}</select>
        </div>
      `;
      parent.appendChild(fieldEl);

      const select = fieldEl.querySelector("select");
      select.addEventListener("change", (e) => {
        settingsModified[fieldKey] = e.target.value;
        // Reshape the form when this field gates others (e.g. Encode Backend).
        if (settingsDependencyKeys().has(fieldKey)) {
          showSettingsSection(currentSection);
          return;
        }
        updateSettingsUI();
      });
    } else {
      fieldEl.innerHTML = `
        <label for="set-${fieldKey}">${field.label}</label>
        <div class="input-wrap">
          <input type="${isPassword ? "password" : "text"}"
                 id="set-${fieldKey}"
                 data-key="${fieldKey}"
                 placeholder="${field.placeholder || ""}"
                 value="${escapeHtml(value)}"
                 autocomplete="off"
                 ${field.readonly ? "disabled" : ""}>
          ${isPassword ? `<button type="button" class="btn-reveal" data-for="set-${fieldKey}">Show</button>` : ""}
        </div>
      `;
      parent.appendChild(fieldEl);

      const input = fieldEl.querySelector("input");
      input.addEventListener("input", (e) => {
        settingsModified[fieldKey] = e.target.value;
        updateSettingsUI();
      });

      if (isPassword) {
        const revealBtn = fieldEl.querySelector(".btn-reveal");
        revealBtn.addEventListener("click", () => {
          const isHidden = input.type === "password";
          input.type = isHidden ? "text" : "password";
          revealBtn.textContent = isHidden ? "Hide" : "Show";
        });
      }
    }
  }

  // ----- Encoding Section with Presets -----

  function renderEncodingSection(container, section) {
    // 1. Preset strip
    const strip = document.createElement("div");
    strip.className = "preset-strip";
    strip.innerHTML = `
      <div class="preset-strip-header">
        <h3>Presets</h3>
        <div style="display:flex;gap:8px">
          <button type="button" class="btn btn-ghost" id="btn-restore-presets" style="font-size:12px">Restore Defaults</button>
          <button type="button" class="btn btn-ghost" id="btn-new-preset">+ New Preset</button>
        </div>
      </div>
      <div class="preset-cards" id="preset-cards"></div>
      <div class="new-preset-form" id="new-preset-form" style="display:none">
        <input type="text" class="new-preset-input" id="new-preset-name" placeholder="Preset name..." maxlength="40">
        <button type="button" class="btn btn-primary" id="btn-save-preset">Save</button>
        <button type="button" class="btn btn-ghost" id="btn-cancel-preset">Cancel</button>
      </div>
    `;
    container.appendChild(strip);

    renderPresetCards();

    // Wire preset form
    $("#btn-new-preset").addEventListener("click", () => {
      $("#new-preset-form").style.display = "flex";
      $("#new-preset-name").focus();
    });
    $("#btn-cancel-preset").addEventListener("click", () => {
      $("#new-preset-form").style.display = "none";
      $("#new-preset-name").value = "";
    });
    $("#btn-save-preset").addEventListener("click", () => createPreset());
    $("#new-preset-name").addEventListener("keydown", (e) => {
      if (e.key === "Enter") createPreset();
    });
    $("#btn-restore-presets").addEventListener("click", () => restoreDefaultPresets());

    // 2. Content area — Auto rules editor OR encoding fields
    const contentArea = document.createElement("div");
    contentArea.id = "encoding-content-area";
    container.appendChild(contentArea);

    const isAutoActive = _isAutoPresetActive();
    if (isAutoActive) {
      renderAutoRulesPanel(contentArea);
    } else {
      renderEncodingFields(contentArea, section);
    }
  }

  function _isAutoPresetActive() {
    const autoPreset = encodingPresets.find(p => p.auto_rules);
    return autoPreset && autoPreset.id === activePresetId;
  }

  function renderEncodingFields(container, section) {
    const groups = {video: [], audio: [], advanced: []};
    for (const [fieldKey, field] of Object.entries(section.fields)) {
      // Skip settings that don't apply to this preset's backend — a hardware
      // preset showing "Encoder Threads" implies a knob that does nothing.
      if (!fieldApplies(field)) continue;
      const group = field.group || "advanced";
      if (groups[group]) groups[group].push([fieldKey, field]);
    }

    const columns = document.createElement("div");
    columns.className = "encoding-columns";

    const videoGroup = document.createElement("div");
    videoGroup.className = "encoding-group";
    videoGroup.innerHTML = `<div class="encoding-group-title">Video</div>`;
    const videoFields = document.createElement("div");
    videoFields.className = "settings-fields";
    for (const [key, field] of groups.video) renderSettingField(videoFields, key, field);
    videoGroup.appendChild(videoFields);
    columns.appendChild(videoGroup);

    const audioGroup = document.createElement("div");
    audioGroup.className = "encoding-group";
    audioGroup.innerHTML = `<div class="encoding-group-title">Audio</div>`;
    const audioFields = document.createElement("div");
    audioFields.className = "settings-fields";
    for (const [key, field] of groups.audio) renderSettingField(audioFields, key, field);
    audioGroup.appendChild(audioFields);
    columns.appendChild(audioGroup);

    if (groups.advanced.length > 0) {
      const advGroup = document.createElement("div");
      advGroup.className = "encoding-group encoding-advanced";
      advGroup.innerHTML = `<div class="encoding-group-title">Advanced</div>`;
      const advFields = document.createElement("div");
      advFields.className = "settings-fields";
      for (const [key, field] of groups.advanced) renderSettingField(advFields, key, field);
      advGroup.appendChild(advFields);
      columns.appendChild(advGroup);
    }

    container.appendChild(columns);
  }

  function renderPresetCards() {
    const cardsDiv = $("#preset-cards");
    if (!cardsDiv) return;
    cardsDiv.innerHTML = "";

    // Sort: Auto first, then other defaults, then custom
    const sorted = [...encodingPresets].sort((a, b) => {
      if (a.auto_rules && !b.auto_rules) return -1;
      if (!a.auto_rules && b.auto_rules) return 1;
      if (a.is_default && !b.is_default) return -1;
      if (!a.is_default && b.is_default) return 1;
      return a.name.localeCompare(b.name);
    });

    for (const preset of sorted) {
      const isAuto = !!preset.auto_rules;
      const card = document.createElement("div");
      card.className = `preset-card${preset.id === activePresetId ? " active" : ""}${isAuto ? " preset-auto" : ""}`;
      card.dataset.presetId = preset.id;

      let badge = preset.is_default ? "Built-in" : "Custom";
      if (isAuto) badge = "Dynamic";

      let html = `
        <div class="preset-card-name">${escapeHtml(preset.name)}</div>
        <div class="preset-card-badge">${badge}</div>
      `;
      if (preset.id === activePresetId) {
        html += `<div class="preset-card-active">● Active</div>`;
      }
      if (!preset.is_default) {
        html += `<button type="button" class="preset-card-delete" title="Delete preset">&times;</button>`;
      }
      card.innerHTML = html;

      card.addEventListener("click", (e) => {
        if (e.target.classList.contains("preset-card-delete")) return;
        activatePreset(preset);
      });

      const delBtn = card.querySelector(".preset-card-delete");
      if (delBtn) {
        delBtn.addEventListener("click", (e) => {
          e.stopPropagation();
          deletePreset(preset.id, preset.name);
        });
      }

      cardsDiv.appendChild(card);
    }
  }

  function detectActivePreset() {
    // Update card active states from activePresetId (set by backend)
    $$(".preset-card").forEach(card => {
      const id = parseInt(card.dataset.presetId);
      const isActive = id === activePresetId;
      card.classList.toggle("active", isActive);
      const dot = card.querySelector(".preset-card-active");
      if (isActive && !dot) {
        const d = document.createElement("div");
        d.className = "preset-card-active";
        d.textContent = "● Active";
        card.appendChild(d);
      } else if (!isActive && dot) {
        dot.remove();
      }
    });
  }

  async function activatePreset(preset) {
    try {
      const r = await fetch(`${API}/presets/activate`, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({preset_id: preset.id}),
      });
      if (!r.ok) return;
      activePresetId = preset.id;

      // For non-Auto presets, update form fields
      if (!preset.auto_rules && preset.settings) {
        for (const [key, val] of Object.entries(preset.settings)) {
          settingsModified[key] = val;
          settingsOriginal[key] = val;
          const el = document.querySelector(`#set-${key}`);
          if (el) el.value = val;
        }
      }

      // Re-render encoding section to swap between rules editor and fields
      if (currentSection === "encoding") {
        showSettingsSection("encoding");
      }
    } catch (e) {
      alert("Failed to activate preset: " + e.message);
    }
  }

  async function createPreset() {
    const nameInput = $("#new-preset-name");
    const name = nameInput.value.trim();
    if (!name) return;

    // Collect current encoding values
    const encodingFields = settingsSchema.encoding ? Object.keys(settingsSchema.encoding.fields) : [];
    const settings = {};
    for (const key of encodingFields) {
      settings[key] = settingsModified[key] || "";
    }

    try {
      const r = await fetch(`${API}/encoding-presets`, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({name, settings}),
      });
      const data = await r.json();
      if (!r.ok) {
        alert(data.error || "Failed to create preset");
        return;
      }
      encodingPresets.push(data.preset);
      renderPresetCards();
      detectActivePreset();
      $("#new-preset-form").style.display = "none";
      nameInput.value = "";
    } catch (e) {
      alert("Failed to create preset: " + e.message);
    }
  }

  async function deletePreset(id, name) {
    if (!confirm(`Delete preset "${name}"?`)) return;
    try {
      const r = await fetch(`${API}/encoding-presets/${id}`, {method: "DELETE"});
      if (r.ok) {
        encodingPresets = encodingPresets.filter(p => p.id !== id);
        renderPresetCards();
        detectActivePreset();
      }
    } catch (e) {
      alert("Failed to delete preset: " + e.message);
    }
  }

  async function restoreDefaultPresets() {
    try {
      const r = await fetch(`${API}/encoding-presets/restore`, {method: "POST"});
      const data = await r.json();
      if (r.ok) {
        // Refresh full preset list
        const pr = await fetch(`${API}/encoding-presets`);
        const pd = await pr.json();
        encodingPresets = pd.presets || [];
        renderPresetCards();
        detectActivePreset();
      }
    } catch (e) {
      alert("Failed to restore presets: " + e.message);
    }
  }

  // ----- Auto Rules Editor -----
  async function renderAutoRulesPanel(container) {
    container.innerHTML = `<div style="padding:20px;text-align:center;color:var(--muted)">Loading rules...</div>`;

    let rulesData;
    try {
      const r = await fetch(`${API}/auto-rules`, {headers:{Accept:"application/json"}, cache:"no-store"});
      rulesData = r.ok ? await r.json() : {rules:[], fallback_preset_id: null, target_presets:[]};
    } catch(e) {
      rulesData = {rules:[], fallback_preset_id: null, target_presets:[]};
    }

    let rules = rulesData.rules || [];
    let fallbackId = rulesData.fallback_preset_id;
    const targetPresets = rulesData.target_presets || [];

    const RESOLUTION_OPTIONS = [
      {value: "", label: "Any"},
      {value: "sd_below", label: "SD and below (\u2264480p)"},
      {value: "720p_below", label: "720p and below"},
      {value: "1080p_below", label: "1080p and below"},
      {value: "above_1080p", label: "Above 1080p"},
      {value: "4k_above", label: "4K+ (\u22652160p)"},
    ];

    const CODEC_OPTIONS = [
      {value: "h264", label: "H.264"},
      {value: "hevc", label: "H.265 (HEVC)"},
      {value: "mpeg2video", label: "MPEG-2"},
      {value: "mpeg4", label: "MPEG-4"},
      {value: "wmv3", label: "WMV3"},
      {value: "vc1", label: "VC-1"},
      {value: "vp9", label: "VP9"},
      {value: "av1", label: "AV1"},
    ];

    function presetSelect(selected, idx, field) {
      let html = `<select class="rule-select" data-idx="${idx}" data-field="${field}">`;
      for (const p of targetPresets) {
        html += `<option value="${p.id}"${p.id === selected ? " selected" : ""}>${escapeHtml(p.name)}</option>`;
      }
      html += `</select>`;
      return html;
    }

    function codecCheckboxes(selected, idx) {
      const sel = new Set(selected || []);
      return CODEC_OPTIONS.map(c =>
        `<label class="codec-label"><input type="checkbox" class="rule-codec" data-idx="${idx}" value="${c.value}"${sel.has(c.value) ? " checked" : ""}> ${c.label}</label>`
      ).join("");
    }

    function renderRules() {
      let html = `
        <div class="auto-rules-header">
          <h4>Auto Rules</h4>
          <p class="auto-rules-desc">Rules are evaluated top-to-bottom. The first matching rule determines which preset to use. All conditions within a rule must match (AND logic).</p>
        </div>
      `;

      if (rules.length === 0) {
        html += `<div class="auto-rules-empty">No rules configured. All files will use the fallback preset.</div>`;
      }

      rules.forEach((rule, i) => {
        const c = rule.conditions || {};
        html += `
          <div class="auto-rule-card" data-idx="${i}">
            <div class="auto-rule-header">
              <span class="auto-rule-number">${i + 1}</span>
              <input type="text" class="auto-rule-name" data-idx="${i}" value="${escapeHtml(rule.name || "")}" placeholder="Rule name...">
              <div class="auto-rule-actions">
                ${i > 0 ? `<button class="btn-rule-move" data-idx="${i}" data-dir="up" title="Move up">&#9650;</button>` : ""}
                ${i < rules.length - 1 ? `<button class="btn-rule-move" data-idx="${i}" data-dir="down" title="Move down">&#9660;</button>` : ""}
                <button class="btn-rule-delete" data-idx="${i}" title="Delete rule">&times;</button>
              </div>
            </div>
            <div class="auto-rule-body">
              <div class="auto-rule-conditions">
                <div class="auto-rule-condition">
                  <label>Resolution</label>
                  <select class="rule-select" data-idx="${i}" data-field="resolution">
                    ${RESOLUTION_OPTIONS.map(o => `<option value="${o.value}"${(c.resolution || "") === o.value ? " selected" : ""}>${o.label}</option>`).join("")}
                  </select>
                </div>
                <div class="auto-rule-condition">
                  <label>Media Type</label>
                  <select class="rule-select" data-idx="${i}" data-field="media_type">
                    <option value=""${!c.media_type ? " selected" : ""}>Any</option>
                    <option value="movie"${c.media_type === "movie" ? " selected" : ""}>Movie</option>
                    <option value="tv"${c.media_type === "tv" ? " selected" : ""}>TV</option>
                  </select>
                </div>
                <div class="auto-rule-condition auto-rule-codecs">
                  <label>Video Codec</label>
                  <div class="codec-checkboxes">${codecCheckboxes(c.video_codec, i)}</div>
                </div>
              </div>
              <div class="auto-rule-target">
                <label>Use Preset</label>
                ${presetSelect(rule.target_preset_id, i, "target_preset_id")}
              </div>
            </div>
          </div>
        `;
      });

      html += `
        <div class="auto-rules-fallback">
          <label>Fallback Preset</label>
          <span class="auto-rules-fallback-hint">(used when no rule matches)</span>
          ${presetSelect(fallbackId, -1, "fallback")}
        </div>
        <div class="auto-rules-actions">
          <button class="btn btn-ghost" id="btn-add-rule">+ Add Rule</button>
          <button class="btn btn-primary" id="btn-save-rules">Save Rules</button>
          <span class="auto-rules-status" id="auto-rules-status"></span>
        </div>
      `;

      container.innerHTML = html;

      // Wire name inputs
      container.querySelectorAll(".auto-rule-name").forEach(el => {
        el.addEventListener("input", () => {
          rules[parseInt(el.dataset.idx)].name = el.value;
        });
      });

      // Wire condition selects
      container.querySelectorAll(".rule-select").forEach(el => {
        el.addEventListener("change", () => {
          const idx = parseInt(el.dataset.idx);
          const field = el.dataset.field;
          if (field === "fallback") {
            fallbackId = parseInt(el.value);
          } else if (field === "target_preset_id") {
            rules[idx].target_preset_id = parseInt(el.value);
          } else if (field === "media_type") {
            if (!rules[idx].conditions) rules[idx].conditions = {};
            rules[idx].conditions.media_type = el.value || null;
          } else if (field === "resolution") {
            if (!rules[idx].conditions) rules[idx].conditions = {};
            rules[idx].conditions.resolution = el.value || null;
          }
        });
      });

      // Wire codec checkboxes
      container.querySelectorAll(".rule-codec").forEach(el => {
        el.addEventListener("change", () => {
          const idx = parseInt(el.dataset.idx);
          if (!rules[idx].conditions) rules[idx].conditions = {};
          const checked = [];
          container.querySelectorAll(`.rule-codec[data-idx="${idx}"]:checked`).forEach(c => checked.push(c.value));
          rules[idx].conditions.video_codec = checked.length > 0 ? checked : null;
        });
      });

      // Wire move buttons
      container.querySelectorAll(".btn-rule-move").forEach(btn => {
        btn.addEventListener("click", () => {
          const idx = parseInt(btn.dataset.idx);
          const dir = btn.dataset.dir;
          const newIdx = dir === "up" ? idx - 1 : idx + 1;
          if (newIdx < 0 || newIdx >= rules.length) return;
          [rules[idx], rules[newIdx]] = [rules[newIdx], rules[idx]];
          renderRules();
        });
      });

      // Wire delete buttons
      container.querySelectorAll(".btn-rule-delete").forEach(btn => {
        btn.addEventListener("click", () => {
          rules.splice(parseInt(btn.dataset.idx), 1);
          renderRules();
        });
      });

      // Add rule
      const addBtn = container.querySelector("#btn-add-rule");
      if (addBtn) {
        addBtn.addEventListener("click", () => {
          rules.push({
            name: "",
            conditions: {resolution: null, video_codec: null, media_type: null},
            target_preset_id: targetPresets.length > 0 ? targetPresets[0].id : null,
          });
          renderRules();
        });
      }

      // Save rules
      const saveBtn = container.querySelector("#btn-save-rules");
      if (saveBtn) {
        saveBtn.addEventListener("click", async () => {
          const statusEl = container.querySelector("#auto-rules-status");
          statusEl.textContent = "Saving...";
          statusEl.className = "auto-rules-status";
          try {
            const r = await fetch(`${API}/auto-rules`, {
              method: "POST",
              headers: {"Content-Type": "application/json"},
              body: JSON.stringify({rules, fallback_preset_id: fallbackId}),
            });
            const result = await r.json();
            if (r.ok) {
              statusEl.textContent = "Saved!";
              statusEl.className = "auto-rules-status success";
            } else {
              statusEl.textContent = result.error || "Save failed";
              statusEl.className = "auto-rules-status error";
            }
          } catch(e) {
            statusEl.textContent = "Network error";
            statusEl.className = "auto-rules-status error";
          }
          setTimeout(() => {
            const s = container.querySelector("#auto-rules-status");
            if (s) s.textContent = "";
          }, 3000);
        });
      }
    }

    renderRules();
  }

  // ----- Connections Section -----
  async function renderConnectionsSection(container) {
    container.innerHTML = `<div style="padding:20px;text-align:center;color:var(--muted)">Loading connections...</div>`;

    try {
      const r = await fetch(`${API}/connections`, {headers:{Accept:"application/json"}, cache:"no-store"});
      const data = r.ok ? await r.json() : {};

      container.innerHTML = `
        <div class="connections-grid">
          <div class="connection-card" id="conn-radarr">
            <div class="connection-header">
              <span class="connection-icon">🎬</span>
              <h3>Radarr</h3>
            </div>
            <div class="connection-status" id="radarr-status">
              ${renderConnectionStatus(data.radarr)}
            </div>
            <div class="connection-actions">
              ${renderConnectionActions("radarr", data.radarr)}
            </div>
            ${renderExtraExtensionsBlock("radarr", data.radarr)}
          </div>

          <div class="connection-card" id="conn-sonarr">
            <div class="connection-header">
              <span class="connection-icon">📺</span>
              <h3>Sonarr</h3>
            </div>
            <div class="connection-status" id="sonarr-status">
              ${renderConnectionStatus(data.sonarr)}
            </div>
            <div class="connection-actions">
              ${renderConnectionActions("sonarr", data.sonarr)}
            </div>
            ${renderExtraExtensionsBlock("sonarr", data.sonarr)}
          </div>
        </div>

        <div class="connections-info">
          <p>Webhooks allow Radarr/Sonarr to notify Transcodarr when new media is imported, eliminating the need for external post-processing scripts.</p>
          <p>Make sure the Radarr/Sonarr URL and API Key are configured in their respective settings sections.</p>
        </div>
      `;

      // Add event listeners
      container.querySelectorAll(".btn-connect").forEach(btn => {
        btn.addEventListener("click", () => connectService(btn.dataset.service));
      });
      container.querySelectorAll(".btn-disconnect").forEach(btn => {
        btn.addEventListener("click", () => disconnectService(btn.dataset.service));
      });
      container.querySelectorAll(".btn-test").forEach(btn => {
        btn.addEventListener("click", () => testConnection(btn.dataset.service));
      });
      container.querySelectorAll("[data-ext-service]").forEach(card => {
        loadExtraExtensions(card.dataset.extService, card);
      });
      container.querySelectorAll(".btn-ext-apply").forEach(btn => {
        btn.addEventListener("click", () => applyExtraExtensions(btn.dataset.service));
      });
      container.querySelectorAll(".btn-ext-recommended").forEach(btn => {
        btn.addEventListener("click", () => {
          const input = container.querySelector(`#ext-input-${btn.dataset.service}`);
          if (input) input.value = input.dataset.recommended || ".srt,.nfo,.jpg";
        });
      });

    } catch (e) {
      container.innerHTML = `<div style="padding:20px;color:var(--danger)">Failed to load connections: ${e.message}</div>`;
    }
  }

  function renderConnectionStatus(conn) {
    if (!conn) return `<span class="conn-badge conn-unknown">Unknown</span>`;
    if (!conn.configured) {
      return `<span class="conn-badge conn-not-configured">Not Configured</span><p class="conn-hint">Configure URL and API key in settings first</p>`;
    }
    if (conn.error) {
      return `<span class="conn-badge conn-error">Error</span><p class="conn-hint">${conn.error}</p>`;
    }
    if (conn.connected) {
      return `<span class="conn-badge conn-connected">Connected</span><p class="conn-hint">Webhook registered</p>`;
    }
    return `<span class="conn-badge conn-disconnected">Not Connected</span><p class="conn-hint">Webhook not registered</p>`;
  }

  function renderConnectionActions(service, conn) {
    if (!conn || !conn.configured) {
      return `<button class="btn" disabled>Connect</button>`;
    }
    if (conn.connected) {
      return `
        <button class="btn btn-ghost btn-test" data-service="${service}">Test</button>
        <button class="btn btn-disconnect" data-service="${service}">Disconnect</button>
      `;
    }
    return `<button class="btn btn-primary btn-connect" data-service="${service}">Connect</button>`;
  }

  function renderExtraExtensionsBlock(service, conn) {
    if (!conn || !conn.configured) return "";
    return `
      <div class="connection-extras" data-ext-service="${service}">
        <label class="connection-extras-label">Preserve file extensions</label>
        <div class="connection-extras-row">
          <input type="text" class="connection-extras-input" id="ext-input-${service}"
                 placeholder="loading..." disabled>
          <button class="btn btn-ghost btn-ext-recommended" data-service="${service}"
                  title="Fill with recommended value" disabled>Recommended</button>
          <button class="btn btn-primary btn-ext-apply" data-service="${service}"
                  title="Push to ${service}" disabled>Apply</button>
        </div>
        <p class="conn-hint" id="ext-hint-${service}">
          Extensions ${service} treats as managed extras; others get deleted on disk scan.
        </p>
      </div>
    `;
  }

  async function loadExtraExtensions(service, card) {
    const input = card.querySelector(`#ext-input-${service}`);
    const applyBtn = card.querySelector(`.btn-ext-apply[data-service="${service}"]`);
    const recBtn = card.querySelector(`.btn-ext-recommended[data-service="${service}"]`);
    try {
      const r = await fetch(`${API}/connections/${service}/extra-extensions`);
      const data = await r.json();
      if (!r.ok) throw new Error(data.error || "Load failed");
      input.value = data.extraFileExtensions || "";
      input.dataset.recommended = data.recommended || ".srt,.nfo,.jpg";
      input.placeholder = data.recommended || ".srt,.nfo,.jpg";
      input.disabled = false;
      applyBtn.disabled = false;
      recBtn.disabled = false;
    } catch (e) {
      input.placeholder = `Error: ${e.message}`;
    }
  }

  async function applyExtraExtensions(service) {
    const input = document.querySelector(`#ext-input-${service}`);
    const btn = document.querySelector(`.btn-ext-apply[data-service="${service}"]`);
    const hint = document.querySelector(`#ext-hint-${service}`);
    if (!input || !btn) return;
    const value = input.value.trim();
    btn.disabled = true;
    const oldText = btn.textContent;
    btn.textContent = "Applying...";
    try {
      const r = await fetch(`${API}/connections/${service}/extra-extensions`, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({extraFileExtensions: value}),
      });
      const data = await r.json();
      if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
      input.value = data.extraFileExtensions || value;
      if (hint) hint.textContent = `Applied: ${data.extraFileExtensions}`;
    } catch (e) {
      alert(`Failed to apply: ${e.message}`);
    } finally {
      btn.disabled = false;
      btn.textContent = oldText;
    }
  }

  async function connectService(service) {
    const btn = document.querySelector(`.btn-connect[data-service="${service}"]`);
    if (btn) { btn.disabled = true; btn.textContent = "Connecting..."; }

    try {
      const r = await fetch(`${API}/connections/${service}`, {method: "POST"});
      const data = await r.json();
      if (r.ok) {
        // Refresh the connections view
        renderConnectionsSection($("#settings-container"));
      } else {
        alert(`Failed to connect: ${data.error || "Unknown error"}`);
        if (btn) { btn.disabled = false; btn.textContent = "Connect"; }
      }
    } catch (e) {
      alert(`Failed to connect: ${e.message}`);
      if (btn) { btn.disabled = false; btn.textContent = "Connect"; }
    }
  }

  async function disconnectService(service) {
    if (!confirm(`Disconnect ${service}? The webhook will be removed.`)) return;

    const btn = document.querySelector(`.btn-disconnect[data-service="${service}"]`);
    if (btn) { btn.disabled = true; btn.textContent = "Disconnecting..."; }

    try {
      const r = await fetch(`${API}/connections/${service}`, {method: "DELETE"});
      const data = await r.json();
      if (r.ok) {
        renderConnectionsSection($("#settings-container"));
      } else {
        alert(`Failed to disconnect: ${data.error || "Unknown error"}`);
        if (btn) { btn.disabled = false; btn.textContent = "Disconnect"; }
      }
    } catch (e) {
      alert(`Failed to disconnect: ${e.message}`);
      if (btn) { btn.disabled = false; btn.textContent = "Disconnect"; }
    }
  }

  async function testConnection(service) {
    const btn = document.querySelector(`.btn-test[data-service="${service}"]`);
    if (btn) { btn.disabled = true; btn.textContent = "Testing..."; }

    try {
      const r = await fetch(`${API}/connections/${service}/test`, {method: "POST"});
      const data = await r.json();
      if (r.ok) {
        alert(`${service} test successful!`);
      } else {
        alert(`Test failed: ${data.error || "Unknown error"}`);
      }
    } catch (e) {
      alert(`Test failed: ${e.message}`);
    } finally {
      if (btn) { btn.disabled = false; btn.textContent = "Test"; }
    }
  }

  // ----- Subtitle Providers Section -----
  async function renderSubtitleProvidersSection(container, section) {
    container.innerHTML = `<div style="padding:20px;text-align:center;color:var(--muted)">Loading providers...</div>`;

    try {
      const r = await fetch(`${API}/subtitle-providers`, {headers:{Accept:"application/json"}, cache:"no-store"});
      const data = r.ok ? await r.json() : {providers: {}};

      let providersHtml = "";
      for (const [providerId, provider] of Object.entries(data.providers)) {
        // All providers get a toggle; auth providers also get account management
        const toggleHtml = renderProviderToggle(providerId, provider);
        const accountsHtml = provider.requires_auth && provider.supports_multiple_accounts
          ? renderProviderAccounts(providerId, provider)
          : "";

        providersHtml += `
          <div class="provider-card" data-provider="${providerId}">
            <div class="provider-header">
              <h3>${escapeHtml(provider.name)}</h3>
              <span class="provider-status ${provider.enabled ? 'enabled' : 'disabled'}">
                ${provider.enabled ? 'Enabled' : 'Disabled'}
              </span>
            </div>
            <div class="provider-content">
              ${toggleHtml}
              ${accountsHtml}
            </div>
          </div>
        `;
      }

      // Also render the regular fields (like FFSUBSYNC_MAX_OFFSET)
      let fieldsHtml = "";
      if (section.fields && Object.keys(section.fields).length > 0) {
        fieldsHtml = `<div class="settings-fields subtitle-settings-fields">`;
        for (const [fieldKey, field] of Object.entries(section.fields)) {
          const value = settingsModified[fieldKey] || "";
          const isModified = settingsModified[fieldKey] !== settingsOriginal[fieldKey];
          const isPassword = field.type === "password";

          fieldsHtml += `
            <div class="setting-field${isModified ? " modified" : ""}" data-key="${fieldKey}">
              <label for="set-${fieldKey}">${field.label}</label>
              <div class="input-wrap">
                <input type="${isPassword ? "password" : "text"}"
                       id="set-${fieldKey}"
                       data-key="${fieldKey}"
                       placeholder="${field.placeholder || ""}"
                       value="${escapeHtml(value)}"
                       autocomplete="off">
                ${isPassword ? `<button type="button" class="btn-reveal" data-for="set-${fieldKey}">Show</button>` : ""}
              </div>
            </div>
          `;
        }
        fieldsHtml += `</div>`;
      }

      container.innerHTML = `
        <div class="subtitle-providers-section">
          <div class="providers-grid">
            ${providersHtml}
          </div>
          ${fieldsHtml}
          <div class="providers-info">
            <p>Add multiple accounts for OpenSubtitles.com to rotate through when download limits are reached.</p>
            <p>Enable Podnapisi as a fallback provider (no account required).</p>
          </div>
        </div>
      `;

      // Add event listeners for the regular settings fields
      container.querySelectorAll(".subtitle-settings-fields input").forEach(input => {
        input.addEventListener("input", (e) => {
          settingsModified[e.target.dataset.key] = e.target.value;
          updateSettingsUI();
        });
      });

      // Add event listeners for provider actions
      container.querySelectorAll(".btn-add-account").forEach(btn => {
        btn.addEventListener("click", () => showAddAccountModal(btn.dataset.provider));
      });
      container.querySelectorAll(".btn-remove-account").forEach(btn => {
        btn.addEventListener("click", () => removeProviderAccount(btn.dataset.provider, btn.dataset.username));
      });
      container.querySelectorAll(".provider-toggle").forEach(toggle => {
        toggle.addEventListener("change", (e) => toggleProvider(e.target.dataset.provider, e.target.checked));
      });

    } catch (e) {
      container.innerHTML = `<div style="padding:20px;color:var(--danger)">Failed to load providers: ${e.message}</div>`;
    }
  }

  function renderProviderAccounts(providerId, provider) {
    let accountsListHtml = "";
    if (provider.accounts && provider.accounts.length > 0) {
      for (const acc of provider.accounts) {
        accountsListHtml += `
          <div class="account-item">
            <span class="account-user">${escapeHtml(acc.user)}</span>
            <span class="account-status">${acc.has_pass ? '●' : '○'}</span>
            <button class="btn btn-sm btn-ghost btn-remove-account" data-provider="${providerId}" data-username="${escapeHtml(acc.user)}" title="Remove account">✕</button>
          </div>
        `;
      }
    } else {
      accountsListHtml = `<div class="no-accounts">No accounts configured</div>`;
    }

    return `
      <div class="provider-accounts">
        <div class="accounts-list">
          ${accountsListHtml}
        </div>
        <button class="btn btn-sm btn-add-account" data-provider="${providerId}">+ Add Account</button>
      </div>
    `;
  }

  function renderProviderToggle(providerId, provider) {
    return `
      <div class="provider-toggle-wrap">
        <label class="toggle-label">
          <input type="checkbox" class="provider-toggle" data-provider="${providerId}" ${provider.enabled ? 'checked' : ''}>
          <span class="toggle-text">${provider.enabled ? 'Enabled' : 'Disabled'}</span>
        </label>
      </div>
    `;
  }

  function showAddAccountModal(providerId) {
    // Create modal
    const modal = document.createElement("div");
    modal.className = "modal-overlay";
    modal.innerHTML = `
      <div class="modal-content add-account-modal">
        <h3>Add Account</h3>
        <div class="modal-field">
          <label>Username</label>
          <input type="text" id="new-account-user" placeholder="username" autocomplete="off">
        </div>
        <div class="modal-field">
          <label>Password</label>
          <input type="password" id="new-account-pass" placeholder="password" autocomplete="off">
        </div>
        <div class="modal-actions">
          <button class="btn btn-ghost modal-cancel">Cancel</button>
          <button class="btn btn-primary modal-confirm">Add</button>
        </div>
      </div>
    `;

    document.body.appendChild(modal);

    const userInput = modal.querySelector("#new-account-user");
    const passInput = modal.querySelector("#new-account-pass");

    modal.querySelector(".modal-cancel").addEventListener("click", () => modal.remove());
    modal.querySelector(".modal-confirm").addEventListener("click", async () => {
      const user = userInput.value.trim();
      const pass = passInput.value.trim();

      if (!user || !pass) {
        alert("Username and password are required");
        return;
      }

      try {
        const r = await fetch(`${API}/subtitle-providers/${providerId}/accounts`, {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({user, pass})
        });
        const data = await r.json();

        if (r.ok) {
          modal.remove();
          // Refresh the section
          renderSubtitleProvidersSection($("#settings-container"), settingsSchema["subtitles"]);
        } else {
          alert(data.error || "Failed to add account");
        }
      } catch (e) {
        alert("Failed to add account: " + e.message);
      }
    });

    // Close on overlay click
    modal.addEventListener("click", (e) => {
      if (e.target === modal) modal.remove();
    });

    userInput.focus();
  }

  async function removeProviderAccount(providerId, username) {
    if (!confirm(`Remove account "${username}"?`)) return;

    try {
      const r = await fetch(`${API}/subtitle-providers/${providerId}/accounts/${encodeURIComponent(username)}`, {
        method: "DELETE"
      });
      const data = await r.json();

      if (r.ok) {
        // Refresh the section
        renderSubtitleProvidersSection($("#settings-container"), settingsSchema["subtitles"]);
      } else {
        alert(data.error || "Failed to remove account");
      }
    } catch (e) {
      alert("Failed to remove account: " + e.message);
    }
  }

  async function toggleProvider(providerId, enabled) {
    try {
      const r = await fetch(`${API}/subtitle-providers/${providerId}/toggle`, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({enabled})
      });
      const data = await r.json();

      if (r.ok) {
        // Refresh the section
        renderSubtitleProvidersSection($("#settings-container"), settingsSchema["subtitles"]);
      } else {
        alert(data.error || "Failed to toggle provider");
      }
    } catch (e) {
      alert("Failed to toggle provider: " + e.message);
    }
  }

  function fmtRes(res) {
    if (!res) return "-";
    const map = {"3840x2160":"4K","2560x1440":"1440p","1920x1080":"1080p","1280x720":"720p","720x480":"480p","640x480":"480p"};
    if (map[res]) return map[res];
    const m = res.match(/\d+x(\d+)/);
    return m ? m[1] + "p" : res;
  }

  function escapeHtml(val) {
    if (val === null || val === undefined) return "";
    const str = String(val);
    return str.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  function updateSettingsUI() {
    // Update modified indicators for visible fields
    $$(".setting-field[data-key]").forEach(fieldEl => {
      const key = fieldEl.dataset.key;
      const isModified = settingsModified[key] !== settingsOriginal[key];
      fieldEl.classList.toggle("modified", isModified);
    });

    // Update status text
    const modifiedCount = Object.keys(settingsModified).filter(k => settingsModified[k] !== settingsOriginal[k]).length;
    const status = $("#settings-status");
    if (modifiedCount > 0) {
      status.textContent = `${modifiedCount} unsaved`;
      status.className = "settings-status";
    } else {
      status.textContent = "";
    }

    // Update active preset indicator
    if (currentSection === "encoding") detectActivePreset();
  }

  async function saveSettings() {
    const btn = $("#save-settings");
    const status = $("#settings-status");

    btn.disabled = true;
    btn.textContent = "Saving...";

    try {
      const r = await fetch(`${API}/settings`, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(settingsModified)
      });
      const data = await r.json();

      if (data.status === "ok" || data.status === "partial") {
        for (const key of data.updated) {
          settingsOriginal[key] = settingsModified[key];
        }
        status.textContent = "Saved!";
        status.className = "settings-status success";
        updateSettingsUI();
        updateStatus();

        setTimeout(() => {
          if (status.classList.contains("success")) {
            status.textContent = "";
            status.className = "settings-status";
          }
        }, 3000);
      } else {
        status.textContent = "Failed to save";
        status.className = "settings-status error";
      }
    } catch (e) {
      status.textContent = "Error saving";
      status.className = "settings-status error";
    } finally {
      btn.disabled = false;
      btn.textContent = "Save";
    }
  }

  $("#save-settings").addEventListener("click", saveSettings);

  // ----- Start/Stop -----
  btnStart.addEventListener("click", async () => { btnStart.disabled = true; try{ await fetch(`${API}/start`, {method:"POST"});}catch{} updateStatus();});
  btnStop .addEventListener("click", async () => { btnStop .disabled = true; try{ await fetch(`${API}/stop`,  {method:"POST"});}catch{} updateStatus();});

  // ----- Poll scanning status -----
  async function pollScanStatus() {
    // Only poll if we're actively scanning
    if (moviesScanning || tvScanning) {
      if (moviesScanning) loadMovies(false);
      if (tvScanning) loadTV(false);
    }
  }

  // ----- Poll processing items -----
  async function pollProcessing() {
    // Refresh media tables to update processing progress
    if (hasProcessingItems) {
      hasProcessingItems = false; // Reset, will be set by render if still processing
      loadMovies(false);
      loadTV(false);
    }
  }

  // ----- System Stats -----
  let statsData = null;
  let storageHistory = null;
  let statsViewActive = false;

  async function updateSystemStats() {
    if (!statsViewActive) return;
    try {
      const r = await fetch(`${API}/system/stats`, {cache:"no-store"});
      if (!r.ok) return;
      statsData = await r.json();
      renderStatGauges();
      renderLineChart("cpu-chart", statsData.history.timestamps, statsData.history.cpu, {
        color: "var(--accent)", maxY: 100, suffix: "%", label: "CPU"
      });
      renderLineChart("ram-chart", statsData.history.timestamps, statsData.history.ram, {
        color: "var(--accent-2)", maxY: 100, suffix: "%", label: "RAM"
      });
      renderDiskMiniChart();
      // Auto-update modal chart if open
      if (_chartModalOpen && (_chartModalOpen.type === "cpu" || _chartModalOpen.type === "ram")) {
        renderModalChart(_chartModalOpen.type, _chartModalOpen.range);
      }
    } catch {}
  }

  // ----- Transcoding hardware capabilities -----
  let hwCaps = null;

  async function loadHwCapabilities(refresh = false) {
    const body = $("#hw-body");
    if (!body) return;
    try {
      const r = await fetch(`${API}/system/capabilities${refresh ? "?refresh=1" : ""}`, {cache:"no-store"});
      if (!r.ok) return;
      hwCaps = await r.json();
      renderHwCapabilities();
    } catch {}
  }

  function renderHwCapabilities() {
    const body = $("#hw-body");
    if (!body || !hwCaps) return;

    const note = $("#hw-cap-note");
    if (note) {
      note.textContent = hwCaps.hardware_available
        ? `node: ${hwCaps.node_id}`
        : `node: ${hwCaps.node_id} — no hardware detected, encoding on CPU`;
    }

    body.innerHTML = (hwCaps.backends || []).map(b => {
      const ok = b.available;
      // An unavailable backend's reason is the actionable part — surface it rather
      // than just greying the row out.
      const detail = ok
        ? [b.device, b.driver].filter(Boolean).join(" · ")
        : (b.reason || "unavailable");
      const codecs = ok && b.codecs && b.codecs.length
        ? b.codecs.map(c => `<span class="hw-codec">${escapeHtml(c)}</span>`).join("")
        : "";
      const sessions = ok && b.max_sessions ? `max ${b.max_sessions} concurrent` : "";
      return `
        <div class="hw-row${ok ? " hw-on" : " hw-off"}">
          <div class="hw-status">${ok ? "●" : "○"}</div>
          <div class="hw-main">
            <div class="hw-name">${escapeHtml(b.label || b.id)}</div>
            <div class="hw-detail">${escapeHtml(detail)}</div>
          </div>
          <div class="hw-codecs">${codecs}</div>
          <div class="hw-sessions">${escapeHtml(sessions)}</div>
        </div>`;
    }).join("");
  }

  async function loadStorageHistory() {
    if (!statsViewActive) return;
    try {
      const r = await fetch(`${API}/system/stats/storage`, {cache:"no-store"});
      if (!r.ok) return;
      const d = await r.json();
      storageHistory = d.history || [];
      renderStorageChart();
    } catch {}
  }

  function renderStatGauges() {
    if (!statsData) return;
    const c = statsData.current;
    const cpuEl = $("#cpu-live");
    const ramEl = $("#ram-live");
    const diskEl = $("#disk-live");
    if (cpuEl) cpuEl.textContent = `${Math.round(c.cpu_percent)}%`;
    if (ramEl) ramEl.textContent = `${Math.round(c.ram_percent)}%`;
    if (diskEl && c.disk) {
      const used = (c.disk.used / 1e12).toFixed(2);
      const total = (c.disk.total / 1e12).toFixed(2);
      // Use GB if < 1 TB
      if (c.disk.total < 1e12) {
        const usedG = (c.disk.used / 1e9).toFixed(1);
        const totalG = (c.disk.total / 1e9).toFixed(1);
        diskEl.textContent = `${usedG} / ${totalG} GB`;
      } else {
        diskEl.textContent = `${used} / ${total} TB`;
      }
    }
  }

  function renderDiskMiniChart() {
    if (!statsData || !statsData.current.disk) return;
    const d = statsData.current.disk;
    const pct = d.percent;
    const container = $("#disk-chart");
    if (!container) return;
    // Simple usage bar for disk
    container.innerHTML = `
      <div style="display:flex;flex-direction:column;justify-content:center;height:100%;gap:8px">
        <div style="font-size:13px;color:var(--muted)">${Math.round(pct)}% used</div>
        <div style="background:var(--bg-soft);border-radius:6px;height:24px;overflow:hidden">
          <div style="height:100%;width:${pct}%;background:${pct > 90 ? 'var(--danger)' : 'var(--ok)'};border-radius:6px;transition:width 0.5s"></div>
        </div>
        <div style="display:flex;justify-content:space-between;font-size:11px;color:var(--muted)">
          <span>Free: ${_fmtBytes(d.free)}</span>
          <span>Total: ${_fmtBytes(d.total)}</span>
        </div>
      </div>`;
  }

  function _fmtBytes(b) {
    if (b >= 1e12) return (b / 1e12).toFixed(2) + " TB";
    if (b >= 1e9) return (b / 1e9).toFixed(1) + " GB";
    if (b >= 1e6) return (b / 1e6).toFixed(0) + " MB";
    return b + " B";
  }

  function renderLineChart(containerId, timestamps, values, opts) {
    const container = $(`#${containerId}`);
    if (!container || !timestamps || timestamps.length < 2) {
      if (container && (!timestamps || timestamps.length < 2)) {
        container.innerHTML = `<div style="display:flex;align-items:center;justify-content:center;height:100%;color:var(--muted);font-size:13px">Collecting data...</div>`;
      }
      return;
    }

    const W = opts.W || 600, H = opts.H || 160, PAD_L = 36, PAD_R = 10, PAD_T = 10, PAD_B = 24;
    const plotW = W - PAD_L - PAD_R;
    const plotH = H - PAD_T - PAD_B;
    const maxY = opts.maxY || Math.max(...values, 1);

    const tMin = timestamps[0], tMax = timestamps[timestamps.length - 1];
    const tRange = tMax - tMin || 1;

    function x(i) { return PAD_L + ((timestamps[i] - tMin) / tRange) * plotW; }
    function y(v) { return PAD_T + plotH - (v / maxY) * plotH; }

    // Grid lines
    let gridLines = "";
    for (let pct of [25, 50, 75, 100]) {
      const gy = y(pct * maxY / 100);
      gridLines += `<line x1="${PAD_L}" y1="${gy}" x2="${W - PAD_R}" y2="${gy}" class="chart-grid"/>`;
      gridLines += `<text x="${PAD_L - 4}" y="${gy + 3}" text-anchor="end" class="chart-label">${pct}${opts.suffix || ""}</text>`;
    }

    // Data polyline
    const points = values.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
    // Area polygon
    const areaPoints = `${x(0).toFixed(1)},${y(0).toFixed(1)} ${points} ${x(values.length - 1).toFixed(1)},${(PAD_T + plotH).toFixed(1)} ${x(0).toFixed(1)},${(PAD_T + plotH).toFixed(1)}`;

    // Time labels (show ~4-6 labels)
    let timeLabels = "";
    const labelCount = Math.min(6, timestamps.length);
    const step = Math.max(1, Math.floor(timestamps.length / labelCount));
    for (let i = 0; i < timestamps.length; i += step) {
      const d = new Date(timestamps[i] * 1000);
      const lbl = `${d.getHours().toString().padStart(2, "0")}:${d.getMinutes().toString().padStart(2, "0")}`;
      timeLabels += `<text x="${x(i).toFixed(1)}" y="${H - 2}" text-anchor="middle" class="chart-label">${lbl}</text>`;
    }

    container.innerHTML = `
      <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" style="width:100%;height:100%">
        ${gridLines}
        <polygon points="${areaPoints}" fill="${opts.color}" class="chart-area"/>
        <polyline points="${points}" stroke="${opts.color}" class="chart-line"/>
        ${timeLabels}
      </svg>`;

    // Tooltip on hover
    const svg = container.querySelector("svg");
    let tooltip = container.querySelector(".chart-tooltip");
    if (!tooltip) {
      tooltip = document.createElement("div");
      tooltip.className = "chart-tooltip";
      tooltip.style.display = "none";
      container.appendChild(tooltip);
    }

    svg.addEventListener("mousemove", (e) => {
      const rect = svg.getBoundingClientRect();
      const mx = (e.clientX - rect.left) / rect.width * W;
      // Find nearest point
      let closest = 0, minDist = Infinity;
      for (let i = 0; i < timestamps.length; i++) {
        const dist = Math.abs(x(i) - mx);
        if (dist < minDist) { minDist = dist; closest = i; }
      }
      const d = new Date(timestamps[closest] * 1000);
      const timeStr = `${d.getHours().toString().padStart(2, "0")}:${d.getMinutes().toString().padStart(2, "0")}:${d.getSeconds().toString().padStart(2, "0")}`;
      tooltip.textContent = `${opts.label}: ${values[closest].toFixed(1)}${opts.suffix || ""} at ${timeStr}`;
      tooltip.style.display = "block";
      // Position tooltip near cursor
      const pxX = (e.clientX - rect.left);
      const pxY = (e.clientY - rect.top);
      tooltip.style.left = `${Math.min(pxX + 10, rect.width - 160)}px`;
      tooltip.style.top = `${Math.max(pxY - 30, 0)}px`;
    });

    svg.addEventListener("mouseleave", () => { tooltip.style.display = "none"; });
  }

  function renderStorageChart() {
    const container = $("#storage-chart");
    if (!container || !storageHistory || storageHistory.length < 2) {
      if (container && (!storageHistory || storageHistory.length < 2)) {
        container.innerHTML = `<div style="display:flex;align-items:center;justify-content:center;height:100%;color:var(--muted);font-size:13px">No storage history yet</div>`;
      }
      return;
    }

    // Filter by range
    const rangeEl = $("#storage-range");
    const range = rangeEl ? rangeEl.value : "30d";
    let data = storageHistory;
    if (range !== "all") {
      const days = parseInt(range) || 30;
      const cutoff = Date.now() / 1000 - days * 86400;
      data = storageHistory.filter(r => r.recorded_at >= cutoff);
    }
    if (data.length < 2) {
      container.innerHTML = `<div style="display:flex;align-items:center;justify-content:center;height:100%;color:var(--muted);font-size:13px">Not enough data for this range</div>`;
      return;
    }

    const rect = container.getBoundingClientRect();
    const W = Math.round(rect.width) || 900, H = Math.round(rect.height) || 200, PAD_L = 50, PAD_R = 10, PAD_T = 10, PAD_B = 28;
    const plotW = W - PAD_L - PAD_R;
    const plotH = H - PAD_T - PAD_B;

    const timestamps = data.map(r => r.recorded_at);
    const usedVals = data.map(r => r.used_bytes);
    const maxTotal = Math.max(...data.map(r => r.total_bytes), 1);
    const tMin = timestamps[0], tMax = timestamps[timestamps.length - 1];
    const tRange = tMax - tMin || 1;

    function x(i) { return PAD_L + ((timestamps[i] - tMin) / tRange) * plotW; }
    function y(v) { return PAD_T + plotH - (v / maxTotal) * plotH; }

    // Grid lines
    let gridLines = "";
    for (let pct of [25, 50, 75, 100]) {
      const val = pct / 100 * maxTotal;
      const gy = y(val);
      gridLines += `<line x1="${PAD_L}" y1="${gy}" x2="${W - PAD_R}" y2="${gy}" class="chart-grid"/>`;
      gridLines += `<text x="${PAD_L - 4}" y="${gy + 3}" text-anchor="end" class="chart-label">${_fmtBytes(val)}</text>`;
    }

    // Data polyline
    const points = usedVals.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
    const areaPoints = `${x(0).toFixed(1)},${y(0).toFixed(1)} ${points} ${x(usedVals.length - 1).toFixed(1)},${(PAD_T + plotH).toFixed(1)} ${x(0).toFixed(1)},${(PAD_T + plotH).toFixed(1)}`;

    // Date labels
    let dateLabels = "";
    const labelCount = Math.min(8, timestamps.length);
    const step = Math.max(1, Math.floor(timestamps.length / labelCount));
    for (let i = 0; i < timestamps.length; i += step) {
      const d = new Date(timestamps[i] * 1000);
      const lbl = `${d.getMonth() + 1}/${d.getDate()}`;
      dateLabels += `<text x="${x(i).toFixed(1)}" y="${H - 2}" text-anchor="middle" class="chart-label">${lbl}</text>`;
    }

    container.innerHTML = `
      <svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" style="display:block">
        ${gridLines}
        <polygon points="${areaPoints}" fill="var(--ok)" class="chart-area"/>
        <polyline points="${points}" stroke="var(--ok)" class="chart-line"/>
        ${dateLabels}
      </svg>`;

    // Tooltip
    const svg = container.querySelector("svg");
    let tooltip = container.querySelector(".chart-tooltip");
    if (!tooltip) {
      tooltip = document.createElement("div");
      tooltip.className = "chart-tooltip";
      tooltip.style.display = "none";
      container.appendChild(tooltip);
    }

    svg.addEventListener("mousemove", (e) => {
      const rect = svg.getBoundingClientRect();
      const mx = (e.clientX - rect.left) / rect.width * W;
      let closest = 0, minDist = Infinity;
      for (let i = 0; i < timestamps.length; i++) {
        const dist = Math.abs(x(i) - mx);
        if (dist < minDist) { minDist = dist; closest = i; }
      }
      const d = new Date(timestamps[closest] * 1000);
      const dateStr = d.toLocaleDateString() + " " + d.toLocaleTimeString();
      tooltip.textContent = `Used: ${_fmtBytes(usedVals[closest])} / ${_fmtBytes(data[closest].total_bytes)} — ${dateStr}`;
      tooltip.style.display = "block";
      const pxX = (e.clientX - rect.left);
      const pxY = (e.clientY - rect.top);
      tooltip.style.left = `${Math.min(pxX + 10, rect.width - 240)}px`;
      tooltip.style.top = `${Math.max(pxY - 30, 0)}px`;
    });

    svg.addEventListener("mouseleave", () => { tooltip.style.display = "none"; });
  }

  // ----- Chart Modal -----
  let _chartModalOpen = null; // null or { type, range }

  function showChartModal(type) {
    // Remove existing modal if any
    const existing = $(".modal-overlay.chart-modal-overlay");
    if (existing) existing.remove();

    const isCpuRam = (type === "cpu" || type === "ram");
    const title = type === "cpu" ? "CPU Usage" : type === "ram" ? "Memory Usage" : "Storage Over Time";
    const ranges = isCpuRam
      ? [{ label: "1h", sec: 3600 }, { label: "2h", sec: 7200 }, { label: "6h", sec: 21600 }, { label: "12h", sec: 43200 }, { label: "24h", sec: 86400 }]
      : [{ label: "7d", sec: 604800 }, { label: "30d", sec: 2592000 }, { label: "90d", sec: 7776000 }, { label: "All", sec: 0 }];
    const defaultSec = isCpuRam ? 21600 : 2592000;

    const overlay = document.createElement("div");
    overlay.className = "modal-overlay chart-modal-overlay";
    overlay.innerHTML = `
      <div class="chart-modal">
        <div class="chart-modal-header">
          <h3>${title}</h3>
          <button class="modal-close chart-modal-close">&times;</button>
        </div>
        <div class="chart-modal-body">
          <div class="toggle-group chart-range-group">
            ${ranges.map(r => `<button class="toggle-btn${r.sec === defaultSec ? ' active' : ''}" data-sec="${r.sec}">${r.label}</button>`).join("")}
          </div>
          <div class="chart-modal-chart" id="chart-modal-canvas"></div>
        </div>
      </div>`;
    document.body.appendChild(overlay);

    _chartModalOpen = { type, range: defaultSec };

    // Render initial chart
    renderModalChart(type, defaultSec);

    // Range button clicks
    overlay.querySelectorAll(".chart-range-group .toggle-btn").forEach(btn => {
      btn.addEventListener("click", () => {
        overlay.querySelectorAll(".chart-range-group .toggle-btn").forEach(b => b.classList.remove("active"));
        btn.classList.add("active");
        const sec = parseInt(btn.dataset.sec, 10);
        _chartModalOpen = { type, range: sec };
        renderModalChart(type, sec);
      });
    });

    // Close handlers
    function closeModal() {
      _chartModalOpen = null;
      overlay.remove();
    }
    overlay.querySelector(".chart-modal-close").addEventListener("click", closeModal);
    overlay.addEventListener("click", (e) => { if (e.target === overlay) closeModal(); });
    const escHandler = (e) => { if (e.key === "Escape") { closeModal(); document.removeEventListener("keydown", escHandler); } };
    document.addEventListener("keydown", escHandler);
  }

  function renderModalChart(type, rangeSec) {
    const container = $("#chart-modal-canvas");
    if (!container) return;

    if (type === "cpu" || type === "ram") {
      if (!statsData || !statsData.history) {
        container.innerHTML = `<div style="display:flex;align-items:center;justify-content:center;height:100%;color:var(--muted);font-size:13px">Collecting data...</div>`;
        return;
      }
      const allTs = statsData.history.timestamps;
      const allVals = type === "cpu" ? statsData.history.cpu : statsData.history.ram;
      const color = type === "cpu" ? "var(--accent)" : "var(--accent-2)";
      const label = type === "cpu" ? "CPU" : "RAM";

      // Filter by range
      let ts, vals;
      if (rangeSec > 0) {
        const cutoff = Date.now() / 1000 - rangeSec;
        const startIdx = allTs.findIndex(t => t >= cutoff);
        if (startIdx < 0 || startIdx >= allTs.length - 1) {
          ts = allTs; vals = allVals; // show all if range exceeds data
        } else {
          ts = allTs.slice(startIdx); vals = allVals.slice(startIdx);
        }
      } else {
        ts = allTs; vals = allVals;
      }

      renderLineChart("chart-modal-canvas", ts, vals, {
        color, maxY: 100, suffix: "%", label, W: 900, H: 380
      });
    } else {
      // Storage
      if (!storageHistory || storageHistory.length < 2) {
        container.innerHTML = `<div style="display:flex;align-items:center;justify-content:center;height:100%;color:var(--muted);font-size:13px">No storage history yet</div>`;
        return;
      }
      let data = storageHistory;
      if (rangeSec > 0) {
        const cutoff = Date.now() / 1000 - rangeSec;
        data = storageHistory.filter(r => r.recorded_at >= cutoff);
      }
      if (data.length < 2) {
        container.innerHTML = `<div style="display:flex;align-items:center;justify-content:center;height:100%;color:var(--muted);font-size:13px">Not enough data for this range</div>`;
        return;
      }
      renderStorageChartTo("chart-modal-canvas", data, 900, 380);
    }
  }

  function renderStorageChartTo(containerId, data, W, H) {
    const container = $(`#${containerId}`);
    if (!container) return;
    const PAD_L = 50, PAD_R = 10, PAD_T = 10, PAD_B = 28;
    const plotW = W - PAD_L - PAD_R;
    const plotH = H - PAD_T - PAD_B;
    const timestamps = data.map(r => r.recorded_at);
    const usedVals = data.map(r => r.used_bytes);
    const maxTotal = Math.max(...data.map(r => r.total_bytes), 1);
    const tMin = timestamps[0], tMax = timestamps[timestamps.length - 1];
    const tRange = tMax - tMin || 1;

    function x(i) { return PAD_L + ((timestamps[i] - tMin) / tRange) * plotW; }
    function y(v) { return PAD_T + plotH - (v / maxTotal) * plotH; }

    let gridLines = "";
    for (let pct of [25, 50, 75, 100]) {
      const val = pct / 100 * maxTotal;
      const gy = y(val);
      gridLines += `<line x1="${PAD_L}" y1="${gy}" x2="${W - PAD_R}" y2="${gy}" class="chart-grid"/>`;
      gridLines += `<text x="${PAD_L - 4}" y="${gy + 3}" text-anchor="end" class="chart-label">${_fmtBytes(val)}</text>`;
    }

    const points = usedVals.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
    const areaPoints = `${x(0).toFixed(1)},${y(0).toFixed(1)} ${points} ${x(usedVals.length - 1).toFixed(1)},${(PAD_T + plotH).toFixed(1)} ${x(0).toFixed(1)},${(PAD_T + plotH).toFixed(1)}`;

    let dateLabels = "";
    const labelCount = Math.min(8, timestamps.length);
    const step = Math.max(1, Math.floor(timestamps.length / labelCount));
    for (let i = 0; i < timestamps.length; i += step) {
      const d = new Date(timestamps[i] * 1000);
      const lbl = `${d.getMonth() + 1}/${d.getDate()}`;
      dateLabels += `<text x="${x(i).toFixed(1)}" y="${H - 2}" text-anchor="middle" class="chart-label">${lbl}</text>`;
    }

    container.innerHTML = `
      <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" style="width:100%;height:100%">
        ${gridLines}
        <polygon points="${areaPoints}" fill="var(--ok)" class="chart-area"/>
        <polyline points="${points}" stroke="var(--ok)" class="chart-line"/>
        ${dateLabels}
      </svg>`;

    const svg = container.querySelector("svg");
    let tooltip = container.querySelector(".chart-tooltip");
    if (!tooltip) {
      tooltip = document.createElement("div");
      tooltip.className = "chart-tooltip";
      tooltip.style.display = "none";
      container.appendChild(tooltip);
    }
    svg.addEventListener("mousemove", (e) => {
      const rect = svg.getBoundingClientRect();
      const mx = (e.clientX - rect.left) / rect.width * W;
      let closest = 0, minDist = Infinity;
      for (let i = 0; i < timestamps.length; i++) {
        const dist = Math.abs(x(i) - mx);
        if (dist < minDist) { minDist = dist; closest = i; }
      }
      const d = new Date(timestamps[closest] * 1000);
      const dateStr = d.toLocaleDateString() + " " + d.toLocaleTimeString();
      tooltip.textContent = `Used: ${_fmtBytes(usedVals[closest])} / ${_fmtBytes(data[closest].total_bytes)} — ${dateStr}`;
      tooltip.style.display = "block";
      const pxX = (e.clientX - rect.left);
      const pxY = (e.clientY - rect.top);
      tooltip.style.left = `${Math.min(pxX + 10, rect.width - 240)}px`;
      tooltip.style.top = `${Math.max(pxY - 30, 0)}px`;
    });
    svg.addEventListener("mouseleave", () => { tooltip.style.display = "none"; });
  }

  // Watch for stats view becoming visible
  const _origNavClick = null;
  // Detect view changes to start/stop stats polling
  function checkStatsView() {
    const statsView = $("#view-stats");
    const isVisible = statsView && statsView.classList.contains("visible");
    if (isVisible && !statsViewActive) {
      statsViewActive = true;
      updateSystemStats();
      loadStorageHistory();
      // Capabilities are a cached probe server-side, so fetch once per view entry
      // rather than on the stats poll interval.
      if (!hwCaps) loadHwCapabilities();
    } else if (!isVisible) {
      statsViewActive = false;
    }
  }

  // Re-detect is the entry point after attaching a GPU without restarting.
  document.addEventListener("click", (e) => {
    const btn = e.target.closest("#hw-refresh");
    if (!btn) return;
    btn.disabled = true;
    const prev = btn.textContent;
    btn.textContent = "Detecting…";
    loadHwCapabilities(true).finally(() => {
      btn.disabled = false;
      btn.textContent = prev;
    });
  });

  // Hook into nav clicks to detect stats view
  $$(".nav-item").forEach(b => {
    b.addEventListener("click", () => setTimeout(checkStatsView, 50));
  });

  // Wire storage range selector
  const storageRange = $("#storage-range");
  if (storageRange) {
    storageRange.addEventListener("change", () => renderStorageChart());
  }

  // Redraw storage chart on resize
  let resizeTimer;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => renderStorageChart(), 150);
  });

  // Clickable stat cards → chart modal
  const statCards = $$(".stat-card");
  const cardTypes = ["cpu", "ram", "disk"];
  statCards.forEach((card, i) => {
    if (cardTypes[i] === "disk") return; // disk card has no modal
    card.addEventListener("click", () => showChartModal(cardTypes[i]));
  });

  // Clickable storage panel header → storage chart modal
  const storagePanelHead = document.querySelector("#storage-panel .panel-head");
  if (storagePanelHead) {
    storagePanelHead.addEventListener("click", (e) => {
      // Don't open modal if clicking on the range dropdown
      if (e.target.closest("select")) return;
      showChartModal("storage");
    });
  }

  // ----- SSE delta appliers -----
  function _overlayDelta(arrKey, d) {
    // Phase 4: SSE only carries in-flight items. Overlay onto loaded REST pages.
    // - If item.path is in loadedKeys → update in place (live progress)
    // - If item is in-flight AND not yet loaded → prepend (newly queued)
    // - Otherwise → ignore (outside loaded pages or already-completed)
    const arr = currentMediaItems[arrKey];
    const loadedKeys = new Set(arr.map(x => x.path));
    let mutated = false;
    for (const item of (d.changed || [])) {
      const idx = arr.findIndex(x => x.path === item.path);
      if (idx !== -1) {
        arr[idx] = item;
        mutated = true;
      } else if (item.status === "pending" || item.status === "queued" ||
                 item.status === "processing" || item.status === "re-encoding") {
        arr.unshift(item);
        loadedKeys.add(item.path);
        mutated = true;
      }
    }
    for (const path of (d.removed || [])) {
      const idx = arr.findIndex(x => x.path === path);
      if (idx !== -1) {
        arr.splice(idx, 1);
        mutated = true;
      }
    }
    return mutated;
  }

  function _applyMoviesDelta(d) {
    if (_overlayDelta("movies", d)) renderMoviesTable(currentMediaItems.movies);
  }

  function _applyTVDelta(d) {
    if (_overlayDelta("tv", d)) renderTVTable(currentMediaItems.tv);
  }

  function _applyCacheProgress(d) {
    moviesScanning = d.movies_scanning === true;
    tvScanning = d.tv_scanning === true;
    const refreshM = $("#refresh-movies");
    if (refreshM) {
      refreshM.textContent = moviesScanning ? `Scanning (${d.movies_count || 0})…` : "Refresh";
      refreshM.disabled = !!moviesScanning;
    }
    const refreshT = $("#refresh-tv");
    if (refreshT) {
      refreshT.textContent = tvScanning ? `Scanning (${d.tv_count || 0})…` : "Refresh";
      refreshT.disabled = !!tvScanning;
    }
    // When a scan transitions from scanning → done, refetch the current view so newly cached items appear
    if (d._wasMoviesScanning === true && !moviesScanning) fetchPage("movie", { reset: true });
    if (d._wasTVScanning     === true && !tvScanning)     fetchPage("tv",    { reset: true });
  }
  // Track scanning state across cache_progress events to detect "scan finished" transitions
  let _lastMoviesScanning = null, _lastTVScanning = null;
  const _origApplyCacheProgress = _applyCacheProgress;
  _applyCacheProgress = function (d) {
    d._wasMoviesScanning = _lastMoviesScanning;
    d._wasTVScanning = _lastTVScanning;
    _lastMoviesScanning = d.movies_scanning === true;
    _lastTVScanning = d.tv_scanning === true;
    _origApplyCacheProgress(d);
  };

  function _applyStatus(d) {
    const running = (d.status || d.running) === "running" || d.running === true;
    setRunningUI(running);
  }

  function _applyWorkers(payload) {
    workerStatus = payload || workerStatus;
    const mw = workerStatus.manual_workers || 0;
    const aw = workerStatus.auto_workers || 0;
    const am = workerStatus.active_manual_jobs || 0;
    const aa = workerStatus.active_auto_jobs || 0;
    const autoCount = $("#auto-worker-count"); const autoPill = $("#auto-workers");
    if (autoCount) autoCount.textContent = aw > 0 ? `${aa}/${aw}` : "off";
    if (autoPill) { autoPill.classList.toggle("busy", aw > 0 && aa >= aw); autoPill.classList.toggle("off", aw === 0); }
    const autoGroup = $("#auto-group");
    if (autoGroup) autoGroup.title = aw > 0 ? `Auto: ${aa} active / ${aw} workers` : "Auto: disabled";
    const manualCount = $("#manual-worker-count"); const manualPill = $("#manual-workers");
    if (manualCount) manualCount.textContent = mw > 0 ? `${am}/${mw}` : "off";
    if (manualPill) { manualPill.classList.toggle("busy", mw > 0 && am >= mw); manualPill.classList.toggle("off", mw === 0); }
    const manualBadge = $("#manual-badge");
    if (manualBadge) {
      if (mw === 0) { manualBadge.textContent = "Off"; manualBadge.className = "badge badge-off"; }
      else if (am >= mw) { manualBadge.textContent = "Busy"; manualBadge.className = "badge badge-busy"; }
      else { manualBadge.textContent = "Ready"; manualBadge.className = "badge badge-ready"; }
    }
    const manualGroup = $("#manual-group");
    if (manualGroup) manualGroup.title = mw > 0 ? `Manual: ${am} active / ${mw} workers` : "Manual: disabled";
  }

  function _applyLogChunk(d) {
    if (d.reset || (tailInode && d.inode && d.inode !== tailInode)) {
      logOut.textContent = "";
    }
    if (typeof d.text === "string" && d.text.length) appendText(d.text);
    if (typeof d.pos === "number") tailPos = d.pos;
    if (d.inode) tailInode = d.inode;
  }

  function _applyStats(payload) {
    statsData = payload;
    if (!statsViewActive) return;
    renderStatGauges();
    renderLineChart("cpu-chart", statsData.history.timestamps, statsData.history.cpu, {
      color: "var(--accent)", maxY: 100, suffix: "%", label: "CPU"
    });
    renderLineChart("ram-chart", statsData.history.timestamps, statsData.history.ram, {
      color: "var(--accent-2)", maxY: 100, suffix: "%", label: "RAM"
    });
    renderDiskMiniChart();
    if (_chartModalOpen && (_chartModalOpen.type === "cpu" || _chartModalOpen.type === "ram")) {
      renderModalChart(_chartModalOpen.type, _chartModalOpen.range);
    }
  }

  // ----- Kickoff -----
  // First-paint: synchronous loads keep tables/status populated for ~500ms before SSE catches up.
  // Logs are NOT pre-populated synchronously — the SSE log stream sends a reset on connect,
  // which would otherwise clear any pre-painted text and cause a flash.
  updateStatus();
  updateWorkerStatus();
  loadMovies(); loadTV();
  loadSettings();

  // Open SSE streams. Each one falls back to its old polling timer if it can't connect.
  openStream("/events/status", {
    status: _applyStatus,
    workers: _applyWorkers,
  }, () => {
    setInterval(updateStatus, 2000);
    setInterval(updateWorkerStatus, 2000);
  });

  openStream("/events/media", {
    movies_delta: _applyMoviesDelta,
    tv_delta: _applyTVDelta,
    cache_progress: _applyCacheProgress,
  }, () => {
    setInterval(pollScanStatus, 2000);
    setInterval(pollProcessing, 3000);
  });

  openStream("/events/logs", {
    log_chunk: _applyLogChunk,
  }, () => {
    pollLogs();
    setInterval(pollLogs, 1500);
  });

  // System stats stream is opened lazily when the stats view becomes active (see checkStatsView)
  let _systemStream = null;
  const _origCheckStatsView = checkStatsView;
  checkStatsView = function () {
    const wasActive = statsViewActive;
    _origCheckStatsView();
    if (statsViewActive && !wasActive && !_systemStream) {
      _systemStream = openStream("/events/system", { stats: _applyStats }, () => {
        setInterval(updateSystemStats, 5000);
      });
    } else if (!statsViewActive && _systemStream) {
      try { _systemStream.close(); } catch {}
      _systemStream = null;
    }
  };

  setInterval(loadStorageHistory, 300000); // Storage history every 5 min — left as polling, changes too rarely to bother with SSE

  // ----- View toggle (table ↔ tile) with localStorage persistence -----
  function _applyViewMode(target, mode) {
    const tableWrap = $(`#${target === "movie" ? "movies" : "tv"}-table-wrap`);
    const tileGrid  = $(`#${target === "movie" ? "movies" : "tv"}-tile-grid`);
    if (!tableWrap || !tileGrid) return;
    const showTiles = mode === "tile";
    tableWrap.classList.toggle("hidden", showTiles);
    tileGrid.classList.toggle("hidden", !showTiles);
    // Update toggle button active state
    $$(`.view-toggle-group[data-view-target="${target}"] .view-toggle`).forEach(b => {
      b.classList.toggle("active", b.dataset.view === mode);
    });
  }

  function _setViewMode(target, mode) {
    try { localStorage.setItem(`transcodarr_${target}_view`, mode); } catch {}
    _applyViewMode(target, mode);
  }

  function _initViewMode(target) {
    let mode = "table";
    try {
      const saved = localStorage.getItem(`transcodarr_${target}_view`);
      if (saved) mode = saved;
      else if (window.innerWidth < 768) mode = "tile";   // phones get tiles by default
    } catch {}
    _applyViewMode(target, mode);
  }

  $$(".view-toggle-group").forEach(group => {
    const target = group.dataset.viewTarget;
    group.querySelectorAll(".view-toggle").forEach(btn => {
      btn.addEventListener("click", () => _setViewMode(target, btn.dataset.view));
    });
  });

  _initViewMode("movie");
  _initViewMode("tv");

  // ----- Show/hide tile sort dropdown based on active view -----
  function _syncTileSortVisibility(target, mode) {
    const sel = $(`.tile-sort-select[data-sort-target="${target}"]`);
    if (sel) sel.classList.toggle("hidden", mode !== "tile");
    if (sel && mode === "tile") {
      const sortObj = target === "movie" ? movieSort : tvSort;
      sel.value = `${sortObj.col}:${sortObj.dir}`;
    }
  }
  // Hook view-toggle clicks (additive — runs alongside _setViewMode)
  $$(".view-toggle-group").forEach(group => {
    const target = group.dataset.viewTarget;
    group.querySelectorAll(".view-toggle").forEach(btn => {
      btn.addEventListener("click", () => _syncTileSortVisibility(target, btn.dataset.view));
    });
  });
  // Apply on init — must match the same default logic as _initViewMode (auto-tile on phones)
  ["movie", "tv"].forEach(t => {
    let mode = "table";
    try {
      const saved = localStorage.getItem(`transcodarr_${t}_view`);
      if (saved) mode = saved;
      else if (window.innerWidth < 768) mode = "tile";
    } catch {}
    _syncTileSortVisibility(t, mode);
  });

  // ----- Select-all-matching banner -----
  _updateSelectAllBanner = function (type) {
    const store = _storeFor(type);
    const sel = type === "movie" ? movieSelection : tvSelection;
    const banner = $(`#${type}-select-all-banner`);
    if (!banner) return;
    const totalLoaded = (type === "movie" ? currentMediaItems.movies : currentMediaItems.tv).length;
    const allLoadedSelected = totalLoaded > 0 && sel.size >= totalLoaded;
    const hasMoreThanLoaded = store.totalCount > totalLoaded;
    const showBanner = allLoadedSelected && hasMoreThanLoaded && !store.selectAllMatching;
    const showActive = store.selectAllMatching;
    banner.classList.toggle("hidden", !showBanner && !showActive);
    const textEl = banner.querySelector(".banner-text");
    const selBtn = banner.querySelector('[data-action="select-all-matching"]');
    const clrBtn = banner.querySelector('[data-action="clear-select-matching"]');
    if (showActive) {
      textEl.textContent = `All ${store.totalCount} matching items will be acted on by bulk operations.`;
      selBtn.classList.add("hidden");
      clrBtn.classList.remove("hidden");
    } else if (showBanner) {
      textEl.textContent = `${sel.size} selected. ${store.totalCount - totalLoaded} more match this filter.`;
      selBtn.classList.remove("hidden");
      clrBtn.classList.add("hidden");
      selBtn.textContent = `Select all ${store.totalCount} matching`;
    }
  };
  $$(".select-all-matching-banner").forEach(banner => {
    banner.addEventListener("click", (e) => {
      const btn = e.target.closest("[data-action]");
      if (!btn) return;
      const target = btn.dataset.target;
      const store = _storeFor(target);
      if (btn.dataset.action === "select-all-matching") {
        store.selectAllMatching = true;
      } else if (btn.dataset.action === "clear-select-matching") {
        store.selectAllMatching = false;
        if (target === "movie") movieSelection.clear();
        else tvSelection.clear();
      }
      if (target === "movie") renderMoviesTable(currentMediaItems.movies);
      else renderTVTable(currentMediaItems.tv);
      _updateSelectAllBanner(target);
    });
  });

  // ----- IntersectionObserver for infinite scroll -----
  function _wireSentinel(target) {
    const el = $(`.paging-sentinel[data-paging-target="${target}"]`);
    if (!el) return;
    const observer = new IntersectionObserver((entries) => {
      if (entries[0].isIntersecting) {
        const store = _storeFor(target);
        if (store.hasMore && !store.inflight) fetchPage(target, { reset: false });
      }
    }, { rootMargin: "400px" });
    observer.observe(el);
  }
  _wireSentinel("movie");
  _wireSentinel("tv");

  // ----- Scroll-to-top floating button -----
  const _scrollTopBtn = $("#scroll-top-btn");
  if (_scrollTopBtn) {
    let _scrollTopRaf = null;
    const _updateScrollTopBtn = () => {
      _scrollTopRaf = null;
      _scrollTopBtn.classList.toggle("hidden", window.scrollY < 300);
    };
    window.addEventListener("scroll", () => {
      if (!_scrollTopRaf) _scrollTopRaf = requestAnimationFrame(_updateScrollTopBtn);
    }, { passive: true });
    _scrollTopBtn.addEventListener("click", () => {
      window.scrollTo({ top: 0, behavior: "smooth" });
    });
  }
})();