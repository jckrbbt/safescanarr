/* ── Safe Scanarr v0.61 ───────────────────────────────────────── */

const TABS     = ["pending", "approved", "quarantined", "rejected"];
let currentTab = "pending";
let tabSheets  = {};
let selections = {};
let statsTimer    = null;
let scanPollTimer = null;
let scanRunning   = false;
let pageSize      = 20;
let tabPage       = {};  // { tabName: currentPage }

TABS.forEach(t => {
  tabSheets[t]  = [];
  selections[t] = new Set();
  tabPage[t]    = 1;
});

document.addEventListener("DOMContentLoaded", () => {
  loadVersion();
  loadStats();
  navigateTo("pending");
  statsTimer    = setInterval(loadStats, 60000);
  checkScanStatus();
  scanPollTimer = setInterval(checkScanStatus, 5000);

  document.querySelectorAll(".sidebar-link").forEach(link => {
    link.addEventListener("click", e => {
      e.preventDefault();
      navigateTo(link.dataset.page);
    });
  });
});

// ── Navigation ────────────────────────────────────────────────────
function navigateTo(page) {
  currentTab = page;
  document.querySelectorAll(".page").forEach(p => p.classList.remove("active"));
  document.querySelectorAll(".sidebar-link").forEach(l => l.classList.remove("active"));
  const pageEl = document.getElementById("page-" + page);
  const linkEl = document.querySelector(".sidebar-link[data-page='" + page + "']");
  if (pageEl) pageEl.classList.add("active");
  if (linkEl) linkEl.classList.add("active");

  if (TABS.includes(page)) loadPage(page);
  if (page === "config")   loadConfig();
  if (page === "logs")     loadLogs();
}

// ── Version ───────────────────────────────────────────────────────
async function loadVersion() {
  const res = await fetch("/api/version");
  const d   = await res.json();
  document.getElementById("nav-version").textContent = "v" + d.version;
}

// ── Stats ─────────────────────────────────────────────────────────
async function loadStats() {
  try {
    const res   = await fetch("/api/stats");
    const stats = await res.json();
    TABS.forEach(t => {
      const badge = document.getElementById("badge-" + t);
      const count = document.getElementById(t + "-count");
      const n = stats[t] || 0;
      if (badge) badge.textContent = n > 0 ? n : "";
      if (count) count.textContent = n + " item" + (n !== 1 ? "s" : "");
    });
  } catch(e) { console.warn("Stats load failed", e); }
}

// ── Load a tab ────────────────────────────────────────────────────
async function loadPage(tab, resetPage) {
  if (resetPage) tabPage[tab] = 1;
  const search = (document.getElementById("global-search") || {}).value || "";
  const res    = await fetch("/api/sheets?state=" + tab + "&search=" + encodeURIComponent(search.toLowerCase()));
  tabSheets[tab] = await res.json();
  selections[tab].clear();
  window._stems = window._stems || {};
  window._stems[tab] = {};
  renderTab(tab);
  updateBulkBar(tab);
}

function renderTab(tab) {
  const grid  = document.getElementById(tab + "-grid");
  const empty = document.getElementById(tab + "-empty");
  if (!grid) return;
  grid.innerHTML = "";

  const sheets = tabSheets[tab];
  if (sheets.length === 0) {
    if (empty) empty.style.display = "block";
    return;
  }
  if (empty) empty.style.display = "none";

  const page      = tabPage[tab] || 1;
  const totalPages = Math.ceil(sheets.length / pageSize);
  const start      = (page - 1) * pageSize;
  const pageSheets = sheets.slice(start, start + pageSize);

  // Render pagination controls
  renderPagination(tab, page, totalPages, sheets.length);

  pageSheets.forEach(function(sheet, localIdx) {
    const idx = start + localIdx;
    if (!window._stems[tab]) window._stems[tab] = {};
    window._stems[tab][idx] = sheet.stem;

    const isApproved = tab === "approved";
    const isRejected = tab === "rejected";
    const showNsfw   = sheet.flagged && !isApproved;
    const checked    = selections[tab].has(sheet.stem) ? "checked" : "";

    const card = document.createElement("div");
    card.className = "sheet-card" + (showNsfw ? " flagged" : "") +
                     (selections[tab].has(sheet.stem) ? " selected" : "");
    card.dataset.stem = sheet.stem;

    // Confidence badge with tooltip
    let confBadge = "";
    if (sheet.nsfw_confidence != null && !isApproved) {
      const pct  = Math.round(sheet.nsfw_confidence * 100);
      const tip  = sheet.flag_reason ? sheet.flag_reason.replace(/"/g, "&quot;") : "NSFW detected";
      confBadge  = '<span class="conf-badge" title="' + tip + '">' + pct + '%</span>';
    }

    // Label breakdown chips
    let labelBreakdown = "";
    if (showNsfw && sheet.flag_reason) {
      const chips = sheet.flag_reason.split(", ").map(function(l) {
        return '<span class="label-chip">' + l.replace(/_/g, " ") + '</span>';
      }).join("");
      labelBreakdown = '<div class="label-breakdown">' + chips + '</div>';
    }

    // Image
    let imgHtml = '<div class="sheet-img-placeholder">No sheet</div>';
    if (sheet.has_sheet) {
      const src = "/api/sheets/image/" + encodeURIComponent(sheet.filename);
      if (isRejected) {
        imgHtml = '<div class="sheet-img-hidden" data-src="' + src + '">' +
                  '<div class="sheet-img-hidden-label">⚠ Click to reveal</div></div>';
      } else {
        imgHtml = '<img class="sheet-img" src="' + src + '" alt="" loading="lazy">';
      }
    }

    const nsfwOverlay = showNsfw ? '<div class="flagged-overlay">⚠ NSFW</div>' : "";
    const srcPath = (sheet.source_path || "Unknown").replace(/"/g, "&quot;");

    card.innerHTML =
      '<div class="sheet-select">' +
        '<input type="checkbox" class="sheet-checkbox" data-tab="' + tab + '" data-idx="' + idx + '" ' + checked +
        ' onchange="toggleSelectByIdx(\'' + tab + '\', ' + idx + ', this.checked)">' +
      '</div>' +
      nsfwOverlay +
      imgHtml +
      '<div class="sheet-info">' +
        '<div class="sheet-name">' + confBadge + escapeHtml(sheet.stem) + '</div>' +
        labelBreakdown +
        '<div class="sheet-path" title="' + srcPath + '">' + escapeHtml(sheet.source_path || "Unknown") + '</div>' +
        renderActions(tab, idx, sheet) +
      '</div>';

    // Bind image events after innerHTML (avoids inline handler issues with special chars)
    const img = card.querySelector(".sheet-img");
    if (img) {
      const src = img.src;
      img.addEventListener("click", function() { openLightbox(src); });
    }
    const hidden = card.querySelector(".sheet-img-hidden");
    if (hidden) {
      const src = hidden.dataset.src;
      hidden.addEventListener("click", function() { revealImage(hidden, src); });
    }

    grid.appendChild(card);
  });
}

function renderPagination(tab, page, totalPages, total) {
  var existing = document.getElementById("pagination-" + tab);
  if (existing) existing.remove();
  if (totalPages <= 1) return;

  var pag = document.createElement("div");
  pag.id = "pagination-" + tab;
  pag.className = "pagination-bar";

  // Page size selector
  var sizeHtml = '<select class="select select-sm" onchange="changePageSize(' + "'" + tab + "'" + ', this.value)">';
  [20, 50, 100].forEach(function(n) {
    sizeHtml += '<option value="' + n + '"' + (n === pageSize ? ' selected' : '') + '>' + n + ' per page</option>';
  });
  sizeHtml += '</select>';

  // Page info
  var infoHtml = '<span class="pag-info">Page ' + page + ' of ' + totalPages + ' (' + total + ' total)</span>';

  // Prev/Next
  var prevHtml = page > 1
    ? '<button class="btn btn-secondary btn-sm" onclick="gotoPage(' + "'" + tab + "'" + ', ' + (page-1) + ')">← Prev</button>'
    : '';
  var nextHtml = page < totalPages
    ? '<button class="btn btn-secondary btn-sm" onclick="gotoPage(' + "'" + tab + "'" + ', ' + (page+1) + ')">Next →</button>'
    : '';

  pag.innerHTML = sizeHtml + prevHtml + infoHtml + nextHtml;

  // Insert before grid
  var grid = document.getElementById(tab + "-grid");
  if (grid) grid.parentNode.insertBefore(pag, grid);
}

function gotoPage(tab, page) {
  tabPage[tab] = page;
  window._stems = window._stems || {};
  window._stems[tab] = {};
  renderTab(tab);
  updateBulkBar(tab);
  // Scroll content to top
  var content = document.querySelector(".content");
  if (content) content.scrollTop = 0;
}

function changePageSize(tab, size) {
  pageSize    = parseInt(size);
  tabPage[tab] = 1;
  window._stems = window._stems || {};
  window._stems[tab] = {};
  renderTab(tab);
  updateBulkBar(tab);
}

function renderActions(tab, idx, sheet) {
  if (tab === "pending") {
    return '<div class="sheet-actions">' +
      '<button class="btn btn-success btn-sm" onclick="singleAction(\'pending\',\'approve\',' + idx + ')">✓ Approve</button>' +
      '<button class="btn btn-warn btn-sm" onclick="singleAction(\'pending\',\'quarantine-single\',' + idx + ')">⚠ Quarantine</button>' +
      '<button class="btn btn-danger btn-sm" onclick="confirmSingle(\'pending\',\'reject\',' + idx + ')">✗ Reject</button>' +
      '</div>';
  }
  if (tab === "approved") {
    return '<div class="sheet-actions">' +
      '<button class="btn btn-secondary btn-sm" onclick="singleAction(\'approved\',\'requeue\',' + idx + ')">↻ Re-queue</button>' +
      '</div>';
  }
  if (tab === "quarantined") {
    return '<div class="sheet-actions">' +
      '<button class="btn btn-success btn-sm" onclick="singleAction(\'quarantined\',\'approve\',' + idx + ')">✓ Restore</button>' +
      '<button class="btn btn-danger btn-sm" onclick="confirmSingle(\'quarantined\',\'reject\',' + idx + ')">✗ Delete</button>' +
      '</div>';
  }
  if (tab === "rejected") {
    const ts = sheet.state_updated_at ? new Date(sheet.state_updated_at).toLocaleString() : "";
    return '<div class="meta-time">' + ts + '</div>';
  }
  return "";
}

// ── Actions ───────────────────────────────────────────────────────
async function singleAction(tab, action, idx) {
  const stem = window._stems[tab][idx];
  await performAction(action, [stem]);
  await loadPage(tab);
  await loadStats();
}

function confirmSingle(tab, action, idx) {
  const stem = window._stems[tab][idx];
  showModal(
    "Reject & Delete?",
    "This will permanently delete the source video for \"" + stem + "\". This cannot be undone.",
    async function() { closeModal(); await singleAction(tab, action, idx); }
  );
}

async function bulkAction(tab, action) {
  const stems = [...selections[tab]];
  if (!stems.length) return;
  await performAction(action, stems);
  await loadPage(tab);
  await loadStats();
  clearSelection(tab);
}

function confirmBulkAction(tab, action) {
  const n = selections[tab].size;
  if (!n) return;
  showModal(
    "Reject " + n + " item" + (n > 1 ? "s" : "") + "?",
    "This will permanently delete " + n + " source video file" + (n > 1 ? "s" : "") + ". This cannot be undone.",
    async function() { closeModal(); await bulkAction(tab, action); }
  );
}

async function performAction(action, stems) {
  const endpoints = {
    "approve":           "/api/sheets/approve",
    "reject":            "/api/sheets/reject",
    "quarantine-single": "/api/sheets/quarantine",
    "requeue":           "/api/sheets/requeue",
    "remove-rejected":   "/api/sheets/remove-rejected",
  };
  const url = endpoints[action];
  if (!url) return;

  if (action === "quarantine-single") {
    const res  = await fetch(url, { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({stem: stems[0]}) });
    const data = await res.json();
    toast(res.ok ? "Moved to quarantine ⚠" : "Error: " + (data.message || "unknown"), !res.ok);
    return;
  }

  if (action === "requeue") {
    for (const stem of stems) {
      const res  = await fetch(url, { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({stem}) });
      const data = await res.json();
      if (!res.ok) { toast("Error: " + (data.message || "unknown"), true); return; }
    }
    toast("Re-queued ✓ — check Review shortly");
    return;
  }

  if (action === "remove-rejected") {
    const res  = await fetch(url, { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({stems}) });
    const data = await res.json();
    toast(res.ok ? "Removed " + (data.removed || []).length + " entr" + ((data.removed||[]).length > 1 ? "ies" : "y") + " ✓" : "Error removing", !res.ok);
    return;
  }

  const res  = await fetch(url, { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({stems}) });
  const data = await res.json();
  const labels = {approve: "Approved ✓", reject: "Rejected ✗"};
  toast(res.ok ? (labels[action] || "Done") + " (" + stems.length + ")" : "Error: " + (data.message || "unknown"), !res.ok);
}

async function triggerScan() {
  await fetch("/api/scan", {method: "POST"});
  toast("Scan started — check Logs for progress");
}

// ── Scan control ──────────────────────────────────────────────────
async function checkScanStatus() {
  try {
    const res  = await fetch("/api/scan/status");
    const data = await res.json();
    setScanRunning(data.running);
  } catch(e) {}
}

function setScanRunning(running) {
  scanRunning = running;
  const btn = document.getElementById("scan-btn");
  if (!btn) return;
  if (running) {
    btn.textContent = "■ Stop Scan";
    btn.className   = "btn btn-danger btn-sm";
  } else {
    btn.textContent = "▶ Run Scan Now";
    btn.className   = "btn btn-primary btn-sm";
  }
}

async function toggleScan() {
  if (scanRunning) {
    const res = await fetch("/api/scan/stop", {method: "POST"});
    if (res.ok) {
      setScanRunning(false);
      toast("Scan stopping — current file will finish before stopping");
    }
  } else {
    const res = await fetch("/api/scan", {method: "POST"});
    if (res.ok) {
      setScanRunning(true);
      toast("Scan started — check Logs for progress");
    }
  }
}

// ── Selection ─────────────────────────────────────────────────────
function toggleSelectByIdx(tab, idx, checked) {
  const stem = window._stems[tab][idx];
  if (checked) selections[tab].add(stem);
  else         selections[tab].delete(stem);
  const card = document.querySelector("#" + tab + "-grid .sheet-card[data-stem='" + CSS.escape(stem) + "']");
  if (card) card.classList.toggle("selected", checked);
  updateBulkBar(tab);
}

function toggleSelectAll(tab) {
  const cb = document.getElementById("select-all-" + tab);
  const checked = cb ? cb.checked : false;
  document.querySelectorAll("#" + tab + "-grid .sheet-checkbox").forEach(function(c) {
    c.checked = checked;
    toggleSelectByIdx(tab, parseInt(c.dataset.idx), checked);
  });
}

function selectAllVisible(tab) {
  document.querySelectorAll("#" + tab + "-grid .sheet-checkbox").forEach(function(c) {
    c.checked = true;
    toggleSelectByIdx(tab, parseInt(c.dataset.idx), true);
  });
  const sa = document.getElementById("select-all-" + tab);
  if (sa) sa.checked = true;
}

function clearSelection(tab) {
  selections[tab].clear();
  document.querySelectorAll("#" + tab + "-grid .sheet-checkbox").forEach(function(c) { c.checked = false; });
  const sa = document.getElementById("select-all-" + tab);
  if (sa) sa.checked = false;
  document.querySelectorAll("#" + tab + "-grid .sheet-card").forEach(function(c) { c.classList.remove("selected"); });
  updateBulkBar(tab);
}

function updateBulkBar(tab) {
  const bar   = document.getElementById("bulk-actions-" + tab);
  const count = document.getElementById("selected-count-" + tab);
  const saBtn = document.getElementById("bulk-select-all-" + tab);
  const total = tabSheets[tab].length;
  const n     = selections[tab].size;
  if (!bar) return;
  if (n > 0) {
    bar.style.display = "flex";
    if (count) count.textContent = n + " selected";
    if (saBtn) saBtn.style.display = n < total ? "inline-flex" : "none";
  } else {
    bar.style.display = "none";
  }
}

// ── Global search ─────────────────────────────────────────────────
async function onGlobalSearch() {
  const q       = ((document.getElementById("global-search") || {}).value || "").trim();
  const clearBtn = document.getElementById("search-clear");

  if (q.length === 0) {
    if (clearBtn) clearBtn.style.display = "none";
    // Return to previous tab
    var prev = currentTab === "search" ? "pending" : currentTab;
    navigateTo(prev);
    return;
  }

  if (clearBtn) clearBtn.style.display = "block";

  // Show search results page
  document.querySelectorAll(".page").forEach(function(p) { p.classList.remove("active"); });
  document.querySelectorAll(".sidebar-link").forEach(function(l) { l.classList.remove("active"); });
  var searchPage = document.getElementById("page-search");
  if (searchPage) searchPage.classList.add("active");
  currentTab = "search";

  var heading = document.getElementById("search-heading");
  if (heading) heading.textContent = "Search: " + q;

  var container = document.getElementById("search-results");
  if (container) container.innerHTML = '<p style="color:var(--text-dim);padding:20px 0">Searching…</p>';

  var tabLabels = {pending: "Review", approved: "Approved", quarantined: "Quarantine", rejected: "Rejected"};
  var sections  = [];

  for (var i = 0; i < TABS.length; i++) {
    var tab = TABS[i];
    var res = await fetch("/api/sheets?state=" + tab + "&search=" + encodeURIComponent(q.toLowerCase()));
    var sheets = await res.json();
    tabSheets[tab] = sheets;
    if (sheets.length > 0) sections.push({tab: tab, label: tabLabels[tab], sheets: sheets});
  }

  if (!container) return;
  if (sections.length === 0) {
    container.innerHTML = '<div class="search-empty">No results found for "' + escapeHtml(q) + '"</div>';
    return;
  }

  container.innerHTML = "";
  sections.forEach(function(sec) {
    var section = document.createElement("div");
    section.className = "search-section";

    var badge = '<span class="nav-badge">' + sec.sheets.length + '</span>';
    section.innerHTML = '<div class="search-section-title">' + sec.label + badge + '</div>';

    // Mini grid for results
    var grid = document.createElement("div");
    grid.className = "sheets-grid";
    grid.id = "search-grid-" + sec.tab;

    if (!window._stems) window._stems = {};
    window._stems[sec.tab] = {};

    sec.sheets.slice(0, 20).forEach(function(sheet, idx) {
      window._stems[sec.tab][idx] = sheet.stem;

      var isApproved = sec.tab === "approved";
      var isRejected = sec.tab === "rejected";
      var showNsfw   = sheet.flagged && !isApproved;

      var confBadge = "";
      if (sheet.nsfw_confidence != null && !isApproved) {
        var pct = Math.round(sheet.nsfw_confidence * 100);
        var tip = sheet.flag_reason ? sheet.flag_reason.replace(/"/g, "&quot;") : "NSFW detected";
        confBadge = '<span class="conf-badge" title="' + tip + '">' + pct + '%</span>';
      }

      var imgHtml = '<div class="sheet-img-placeholder">No sheet</div>';
      if (sheet.has_sheet) {
        var src = "/api/sheets/image/" + encodeURIComponent(sheet.filename);
        if (isRejected) {
          imgHtml = '<div class="sheet-img-hidden" data-src="' + src + '"><div class="sheet-img-hidden-label">⚠ Click to reveal</div></div>';
        } else {
          imgHtml = '<img class="sheet-img" src="' + src + '" alt="" loading="lazy">';
        }
      }

      var nsfwOverlay = showNsfw ? '<div class="flagged-overlay">⚠ NSFW</div>' : "";
      var srcPath = (sheet.source_path || "Unknown").replace(/"/g, "&quot;");

      var card = document.createElement("div");
      card.className = "sheet-card" + (showNsfw ? " flagged" : "");
      card.dataset.stem = sheet.stem;
      card.innerHTML =
        nsfwOverlay + imgHtml +
        '<div class="sheet-info">' +
          '<div class="sheet-name">' + confBadge + escapeHtml(sheet.stem) + '</div>' +
          '<div class="sheet-path" title="' + srcPath + '">' + escapeHtml(sheet.source_path || "Unknown") + '</div>' +
          renderActions(sec.tab, idx, sheet) +
        '</div>';

      var img = card.querySelector(".sheet-img");
      if (img) { var s = img.src; img.addEventListener("click", function() { openLightbox(s); }); }
      var hidden = card.querySelector(".sheet-img-hidden");
      if (hidden) { var s2 = hidden.dataset.src; hidden.addEventListener("click", function() { revealImage(hidden, s2); }); }

      grid.appendChild(card);
    });

    if (sec.sheets.length > 20) {
      var more = document.createElement("p");
      more.style.cssText = "color:var(--text-dim);font-size:12px;margin-top:8px";
      more.textContent = "+" + (sec.sheets.length - 20) + " more — go to " + sec.label + " tab to see all";
      section.appendChild(grid);
      section.appendChild(more);
    } else {
      section.appendChild(grid);
    }

    container.appendChild(section);
  });
}

function clearSearch() {
  var input  = document.getElementById("global-search");
  var clearBtn = document.getElementById("search-clear");
  if (input) input.value = "";
  if (clearBtn) clearBtn.style.display = "none";
  navigateTo("pending");
}

// ── Config ────────────────────────────────────────────────────────
async function loadConfig() {
  const res = await fetch("/api/config");
  const cfg = await res.json();
  document.getElementById("cfg-zone-approve").value        = cfg.zone_auto_approve  != null ? cfg.zone_auto_approve  : 0.2;
  document.getElementById("cfg-zone-quarantine").value     = cfg.zone_quarantine     != null ? cfg.zone_quarantine     : 0.5;
  document.getElementById("cfg-zone-reject").value         = cfg.zone_auto_reject    != null ? cfg.zone_auto_reject    : 0.9;
  document.getElementById("cfg-nudenet-frames").value      = cfg.nudenet_frames      || 10;
  document.getElementById("cfg-watch-folders").value       = (cfg.watch_folders || []).join("\n");
  document.getElementById("cfg-scan-schedule-enabled").checked = !!cfg.scan_schedule_enabled;
  document.getElementById("cfg-scan-schedule").value       = cfg.scan_schedule       || "daily";
  document.getElementById("cfg-quarantine-dir").value      = cfg.quarantine_dir      || "";
  document.getElementById("cfg-quarantine-days").value     = cfg.quarantine_auto_reject_days != null ? cfg.quarantine_auto_reject_days : 0;
  document.getElementById("cfg-polling-enabled").checked   = !!cfg.polling_enabled;
  document.getElementById("cfg-poll-interval").value       = cfg.poll_interval_seconds || 600;
  document.getElementById("cfg-sonarr-url").value          = cfg.sonarr_url          || "";
  document.getElementById("cfg-sonarr-key").value          = cfg.sonarr_api_key      || "";
  document.getElementById("cfg-radarr-url").value          = cfg.radarr_url          || "";
  document.getElementById("cfg-radarr-key").value          = cfg.radarr_api_key      || "";
  document.getElementById("cfg-webhook-url").value         = cfg.webhook_url         || "";
  document.getElementById("cfg-webhook-quarantine").checked = !!cfg.webhook_on_quarantine;
  document.getElementById("cfg-webhook-reject").checked    = !!cfg.webhook_on_reject;
  document.getElementById("cfg-vcs-grid").value            = cfg.vcs_grid            || "4x4";
  document.getElementById("cfg-vcsi-timeout").value        = cfg.vcsi_timeout_seconds || 300;
  checkArrKeys();
}

async function saveConfig() {
  const folders = document.getElementById("cfg-watch-folders").value
    .split("\n").map(function(s) { return s.trim(); }).filter(Boolean);

  const payload = {
    zone_auto_approve:           parseFloat(document.getElementById("cfg-zone-approve").value),
    zone_quarantine:             parseFloat(document.getElementById("cfg-zone-quarantine").value),
    zone_auto_reject:            parseFloat(document.getElementById("cfg-zone-reject").value),
    nudenet_frames:              parseInt(document.getElementById("cfg-nudenet-frames").value),
    watch_folders:               folders,
    scan_schedule_enabled:       document.getElementById("cfg-scan-schedule-enabled").checked,
    scan_schedule:               document.getElementById("cfg-scan-schedule").value,
    quarantine_dir:              document.getElementById("cfg-quarantine-dir").value.trim(),
    quarantine_auto_reject_days: parseInt(document.getElementById("cfg-quarantine-days").value),
    polling_enabled:             document.getElementById("cfg-polling-enabled").checked,
    poll_interval_seconds:       parseInt(document.getElementById("cfg-poll-interval").value),
    sonarr_url:                  document.getElementById("cfg-sonarr-url").value.trim(),
    sonarr_api_key:              document.getElementById("cfg-sonarr-key").value.trim(),
    radarr_url:                  document.getElementById("cfg-radarr-url").value.trim(),
    radarr_api_key:              document.getElementById("cfg-radarr-key").value.trim(),
    webhook_url:                 document.getElementById("cfg-webhook-url").value.trim(),
    webhook_on_quarantine:       document.getElementById("cfg-webhook-quarantine").checked,
    webhook_on_reject:           document.getElementById("cfg-webhook-reject").checked,
    vcs_grid:                    document.getElementById("cfg-vcs-grid").value.trim(),
    vcsi_timeout_seconds:        parseInt(document.getElementById("cfg-vcsi-timeout").value),
  };

  const res = await fetch("/api/config", {
    method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload)
  });
  const msg = document.getElementById("config-msg");
  if (res.ok) {
    msg.className   = "msg success";
    msg.textContent = "✓ Configuration saved.";
  } else {
    const d = await res.json().catch(function() { return {}; });
    msg.className   = "msg error";
    msg.textContent = "✗ " + (d.message || "Failed to save.");
  }
  msg.style.display = "block";
  setTimeout(function() { msg.style.display = "none"; }, 4000);
}

function checkArrKeys() {
  const hasKeys = (document.getElementById("cfg-sonarr-key") || {value:""}).value.trim().length > 0 ||
                  (document.getElementById("cfg-radarr-key") || {value:""}).value.trim().length > 0;
  const toggle  = document.getElementById("cfg-polling-enabled");
  const hint    = document.getElementById("polling-no-keys-hint");
  if (!toggle) return;
  if (!hasKeys) {
    toggle.disabled    = true;
    toggle.checked     = false;
    if (hint) hint.style.display = "block";
  } else {
    toggle.disabled    = false;
    if (hint) hint.style.display = "none";
  }
}

async function testConnection(service) {
  const btn    = document.getElementById("test-" + service + "-btn");
  const result = document.getElementById("test-" + service + "-result");
  btn.textContent = "…"; btn.disabled = true;
  await saveConfig();
  const res  = await fetch("/api/config/test-" + service, {method: "POST"});
  const data = await res.json();
  result.className   = res.ok ? "test-result success" : "test-result error";
  result.textContent = res.ok ? "✓ Connected — " + service + " v" + data.version : "✗ " + (data.message || "Connection failed");
  btn.textContent = "Test"; btn.disabled = false;
}

async function testWebhook() {
  const btn    = document.getElementById("test-webhook-btn");
  const result = document.getElementById("test-webhook-result");
  btn.textContent = "…"; btn.disabled = true;
  await saveConfig();
  const res  = await fetch("/api/config/test-webhook", {method: "POST"});
  const data = await res.json();
  result.className   = res.ok ? "test-result success" : "test-result error";
  result.textContent = res.ok ? "✓ Webhook delivered" : "✗ " + (data.message || "Failed");
  btn.textContent = "Test"; btn.disabled = false;
}

async function cleanDatabase() {
  const result = document.getElementById("clean-db-result");
  result.className   = "test-result";
  result.textContent = "Cleaning…";
  const res  = await fetch("/api/db/clean", {method: "POST"});
  const data = await res.json();
  result.className   = res.ok ? "test-result success" : "test-result error";
  result.textContent = res.ok
    ? (data.removed > 0 ? "✓ Removed " + data.removed + " stale record" + (data.removed > 1 ? "s" : "") : "✓ Database is clean")
    : "✗ Clean failed";
}

// ── Logs ──────────────────────────────────────────────────────────
async function loadLogs() {
  const lines  = document.getElementById("log-lines").value;
  const level  = document.getElementById("log-level").value;
  const res    = await fetch("/api/logs?lines=" + lines + "&level=" + level);
  const data   = await res.json();
  const out    = document.getElementById("log-output");
  out.innerHTML = data.lines.map(function(line) {
    let cls = "log-info";
    if (line.indexOf("[ERROR]") >= 0)   cls = "log-error";
    else if (line.indexOf("[WARNING]") >= 0) cls = "log-warn";
    else if (line.indexOf("[DEBUG]") >= 0)   cls = "log-debug";
    return '<div class="log-line ' + cls + '">' + escapeHtml(line) + '</div>';
  }).join("");
  out.scrollTop = out.scrollHeight;
}

// ── Modal ─────────────────────────────────────────────────────────
function showModal(title, body, onConfirm) {
  document.getElementById("modal-title").textContent = title;
  document.getElementById("modal-body").textContent  = body;
  document.getElementById("modal-confirm").onclick   = onConfirm;
  document.getElementById("modal-overlay").style.display = "flex";
}
function closeModal() {
  document.getElementById("modal-overlay").style.display = "none";
}

// ── Lightbox ──────────────────────────────────────────────────────
var _lightboxTab   = null;
var _lightboxIdx   = null;
var _lightboxTotal = 0;

function openLightbox(src, tab, idx) {
  // Remove existing
  var existing = document.querySelector(".lightbox");
  if (existing) existing.remove();

  _lightboxTab   = tab   || null;
  _lightboxIdx   = idx   != null ? idx : null;
  _lightboxTotal = tab   ? (tabSheets[tab] || []).length : 0;

  var lb = document.createElement("div");
  lb.className = "lightbox";
  lb.id = "lightbox";

  // Build content
  var sheet = (tab && idx != null) ? tabSheets[tab][idx] : null;
  var labelsHtml = "";
  if (sheet && sheet.flag_reason) {
    var chips = sheet.flag_reason.split(", ").map(function(l) {
      return '<span class="label-chip">' + l.replace(/_/g, " ") + '</span>';
    }).join("");
    var pct = sheet.nsfw_confidence ? Math.round(sheet.nsfw_confidence * 100) + "% confidence" : "";
    labelsHtml = '<div class="lightbox-labels">' + (pct ? '<span class="conf-badge">' + pct + '</span>' : '') + chips + '</div>';
  }

  // Actions
  var actionsHtml = "";
  if (tab === "pending" && idx != null) {
    actionsHtml = '<div class="lightbox-actions">' +
      '<button class="btn btn-success" onclick="lightboxAction(\'approve\')">✓ Approve</button>' +
      '<button class="btn btn-danger"  onclick="lightboxAction(\'reject\')">✗ Reject</button>' +
      '</div>';
  } else if (tab === "quarantined" && idx != null) {
    actionsHtml = '<div class="lightbox-actions">' +
      '<button class="btn btn-success" onclick="lightboxAction(\'approve\')">✓ Restore</button>' +
      '<button class="btn btn-danger"  onclick="lightboxAction(\'reject\')">✗ Delete</button>' +
      '</div>';
  }

  // Nav arrows
  var prevHtml = (idx != null && idx > 0)
    ? '<button class="lightbox-nav lightbox-prev" onclick="lightboxNav(-1)">‹</button>' : '';
  var nextHtml = (idx != null && idx < _lightboxTotal - 1)
    ? '<button class="lightbox-nav lightbox-next" onclick="lightboxNav(1)">›</button>' : '';

  var name = sheet ? '<div class="lightbox-name">' + escapeHtml(sheet.stem) + '</div>' : '';

  lb.innerHTML =
    '<div class="lightbox-inner" onclick="event.stopPropagation()">' +
      prevHtml +
      '<img src="' + src + '" alt="">' +
      nextHtml +
      name +
      labelsHtml +
      actionsHtml +
    '</div>' +
    '<button class="lightbox-close" onclick="closeLightbox()">✕</button>';

  lb.addEventListener("click", closeLightbox);
  document.body.appendChild(lb);
}

function closeLightbox() {
  var lb = document.getElementById("lightbox");
  if (lb) lb.remove();
  _lightboxTab = null;
  _lightboxIdx = null;
}

function lightboxNav(dir) {
  if (_lightboxTab == null || _lightboxIdx == null) return;
  var newIdx = _lightboxIdx + dir;
  if (newIdx < 0 || newIdx >= _lightboxTotal) return;
  var sheet = tabSheets[_lightboxTab][newIdx];
  if (!sheet || !sheet.has_sheet) return;
  var src = "/api/sheets/image/" + encodeURIComponent(sheet.filename);
  openLightbox(src, _lightboxTab, newIdx);
}

async function lightboxAction(action) {
  if (_lightboxTab == null || _lightboxIdx == null) return;
  var idx = _lightboxIdx;
  var tab = _lightboxTab;
  closeLightbox();
  if (action === "reject") {
    confirmSingle(tab, "reject", idx);
  } else {
    await singleAction(tab, "approve", idx);
  }
}

// Keyboard handler
document.addEventListener("keydown", function(e) {
  if (!document.getElementById("lightbox")) return;
  if (e.key === "Escape")      closeLightbox();
  if (e.key === "ArrowLeft")   lightboxNav(-1);
  if (e.key === "ArrowRight")  lightboxNav(1);
  if (e.key === "a" || e.key === "A") lightboxAction("approve");
  if (e.key === "r" || e.key === "R") lightboxAction("reject");
});

function revealImage(el, src) {
  const img     = document.createElement("img");
  img.className = "sheet-img";
  img.src       = src;
  img.alt       = "";
  img.loading   = "lazy";
  img.addEventListener("click", function() { openLightbox(src); });
  el.replaceWith(img);
}

// ── Sidebar toggle ───────────────────────────────────────────────
function toggleSidebar() {
  var sidebar   = document.querySelector(".sidebar");
  var backdrop  = document.getElementById("sidebar-backdrop");
  var isMobile  = window.innerWidth <= 768;

  if (isMobile) {
    var hidden = sidebar.classList.toggle("collapsed");
    if (backdrop) backdrop.classList.toggle("visible", !hidden);
  } else {
    sidebar.classList.toggle("collapsed");
  }
  // Persist preference
  try { localStorage.setItem("sidebarCollapsed", sidebar.classList.contains("collapsed")); } catch(e) {}
}

// Restore sidebar state on load
document.addEventListener("DOMContentLoaded", function() {
  try {
    var pref    = localStorage.getItem("sidebarCollapsed");
    var sidebar = document.querySelector(".sidebar");
    var isMobile = window.innerWidth <= 768;
    // Default: collapsed on mobile, expanded on desktop
    if (pref === null) {
      if (isMobile) sidebar.classList.add("collapsed");
    } else if (pref === "true") {
      sidebar.classList.add("collapsed");
    }
  } catch(e) {}
});

// ── Toast ─────────────────────────────────────────────────────────
function toast(msg, isError) {
  const el        = document.getElementById("toast");
  el.textContent  = msg;
  el.style.color  = isError ? "var(--danger)" : "var(--accent2)";
  el.style.display = "block";
  setTimeout(function() { el.style.display = "none"; }, 3000);
}

// ── Helpers ───────────────────────────────────────────────────────
function escapeHtml(s) {
  if (!s) return "";
  return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");
}
