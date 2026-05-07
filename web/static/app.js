/* ── Safe Scanarr v0.65 ───────────────────────────────────────── */

const TABS     = ["pending", "approved", "quarantined", "rejected"];
let currentTab = "pending";
let tabSheets  = {};
let selections = {};
let statsTimer    = null;
let scanPollTimer = null;
let scanRunning   = false;
let pageSize      = 20;
let tabPage       = {};

TABS.forEach(function(t) {
  tabSheets[t]  = [];
  selections[t] = new Set();
  tabPage[t]    = 1;
});

document.addEventListener("DOMContentLoaded", function() {
  loadVersion();
  loadStats();
  navigateTo("pending");
  statsTimer    = setInterval(loadStats, 60000);
  checkScanStatus();
  scanPollTimer = setInterval(checkScanStatus, 5000);

  document.querySelectorAll(".sidebar-link").forEach(function(link) {
    link.addEventListener("click", function(e) {
      e.preventDefault();
      navigateTo(link.dataset.page);
    });
  });

  // Restore sidebar state
  try {
    var pref     = localStorage.getItem("sidebarCollapsed");
    var sidebar  = document.querySelector(".sidebar");
    var isMobile = window.innerWidth <= 768;
    if (pref === null) { if (isMobile) sidebar.classList.add("collapsed"); }
    else if (pref === "true") sidebar.classList.add("collapsed");
  } catch(e) {}
});

// ── Navigation ────────────────────────────────────────────────────
function navigateTo(page) {
  currentTab = page;
  document.querySelectorAll(".page").forEach(function(p) { p.classList.remove("active"); });
  document.querySelectorAll(".sidebar-link").forEach(function(l) { l.classList.remove("active"); });
  var pageEl = document.getElementById("page-" + page);
  var linkEl = document.querySelector(".sidebar-link[data-page='" + page + "']");
  if (pageEl) pageEl.classList.add("active");
  if (linkEl) linkEl.classList.add("active");
  if (TABS.includes(page)) loadPage(page);
  if (page === "config")   loadConfig();
  if (page === "logs")     loadLogs();
}

// ── Version ───────────────────────────────────────────────────────
async function loadVersion() {
  try {
    var res = await fetch("/api/version");
    var d   = await res.json();
    document.getElementById("nav-version").textContent = "v" + d.version;
  } catch(e) {}
}

// ── Stats ─────────────────────────────────────────────────────────
async function loadStats() {
  try {
    var res   = await fetch("/api/stats");
    var stats = await res.json();
    TABS.forEach(function(t) {
      var badge = document.getElementById("badge-" + t);
      var count = document.getElementById(t + "-count");
      var n = stats[t] || 0;
      if (badge) badge.textContent = n > 0 ? n : "";
      if (count) count.textContent = n + " item" + (n !== 1 ? "s" : "");
    });
  } catch(e) {}
}

// ── Load a tab ────────────────────────────────────────────────────
async function loadPage(tab, resetPage) {
  if (resetPage) tabPage[tab] = 1;
  var search = (document.getElementById("global-search") || {value:""}).value || "";
  var res    = await fetch("/api/sheets?state=" + tab + "&search=" + encodeURIComponent(search.toLowerCase()));
  tabSheets[tab] = await res.json();
  selections[tab].clear();
  if (!window._stems) window._stems = {};
  window._stems[tab] = {};
  renderTab(tab);
  updateBulkBar(tab);
}

function renderTab(tab) {
  var grid  = document.getElementById(tab + "-grid");
  var empty = document.getElementById(tab + "-empty");
  if (!grid) return;
  grid.innerHTML = "";

  var sheets = tabSheets[tab];
  if (sheets.length === 0) {
    if (empty) empty.style.display = "block";
    return;
  }
  if (empty) empty.style.display = "none";

  var page      = tabPage[tab] || 1;
  var total     = sheets.length;
  var totalPages = Math.ceil(total / pageSize);
  var start     = (page - 1) * pageSize;
  var pageItems = sheets.slice(start, start + pageSize);

  renderPagination(tab, page, totalPages, total);

  pageItems.forEach(function(sheet, localIdx) {
    var idx = start + localIdx;
    if (!window._stems[tab]) window._stems[tab] = {};
    window._stems[tab][idx] = sheet.stem;

    var isApproved = tab === "approved";
    var isRejected = tab === "rejected";
    var showNsfw   = sheet.flagged && !isApproved;
    var checked    = selections[tab].has(sheet.stem) ? "checked" : "";

    var card = document.createElement("div");
    card.className = "sheet-card" + (showNsfw ? " flagged" : "") +
                     (selections[tab].has(sheet.stem) ? " selected" : "");
    card.dataset.stem = sheet.stem;

    // Confidence badge
    var confBadge = "";
    if (sheet.nsfw_confidence != null && !isApproved) {
      var pct = Math.round(sheet.nsfw_confidence * 100);
      var tip = sheet.flag_reason ? sheet.flag_reason.replace(/"/g, "&quot;") : "NSFW detected";
      confBadge = '<span class="conf-badge" title="' + tip + '">' + pct + '%</span>';
    }

    // Label chips (deduped)
    var labelBreakdown = "";
    if (showNsfw && sheet.flag_reason) {
      var chips = dedupeLabels(sheet.flag_reason).map(function(l) {
        var name = formatLabel(l.label);
        return '<span class="label-chip">' + name + (l.conf > 0 ? " " + l.conf + "%" : "") + '</span>';
      }).join("");
      labelBreakdown = '<div class="label-breakdown">' + chips + '</div>';
    }

    // Image
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

    // Bind image click with tab+idx for lightbox
    var img = card.querySelector(".sheet-img");
    if (img) {
      var _s = img.src, _t = tab, _i = idx;
      img.addEventListener("click", function() { openLightbox(_s, _t, _i); });
    }
    var hidden = card.querySelector(".sheet-img-hidden");
    if (hidden) {
      var _hs = hidden.dataset.src;
      hidden.addEventListener("click", function() { revealImage(hidden, _hs); });
    }

    grid.appendChild(card);
  });
}

function renderActions(tab, idx, sheet) {
  if (tab === "pending") {
    return '<div class="sheet-actions">' +
      '<button class="btn btn-success btn-sm" onclick="singleAction(\'pending\',\'approve\',' + idx + ')">✓ Approve</button>' +
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
    var ts = sheet.state_updated_at ? new Date(sheet.state_updated_at).toLocaleString() : "";
    return '<div class="meta-time">' + ts + '</div>';
  }
  return "";
}

// ── Pagination ────────────────────────────────────────────────────
function renderPagination(tab, page, totalPages, total) {
  var existing = document.getElementById("pagination-" + tab);
  if (existing) existing.remove();
  if (totalPages <= 1) return;

  var pag = document.createElement("div");
  pag.id = "pagination-" + tab;
  pag.className = "pagination-bar";

  var sizeHtml = '<select class="select select-sm" onchange="changePageSize(\'' + tab + '\', this.value)">';
  [20, 50, 100].forEach(function(n) {
    sizeHtml += '<option value="' + n + '"' + (n === pageSize ? " selected" : "") + '>' + n + ' per page</option>';
  });
  sizeHtml += '</select>';

  var prevHtml = page > 1 ? '<button class="btn btn-secondary btn-sm" onclick="gotoPage(\'' + tab + '\', ' + (page-1) + ')">← Prev</button>' : '';
  var nextHtml = page < totalPages ? '<button class="btn btn-secondary btn-sm" onclick="gotoPage(\'' + tab + '\', ' + (page+1) + ')">Next →</button>' : '';
  var infoHtml = '<span class="pag-info">Page ' + page + ' of ' + totalPages + ' (' + total + ' total)</span>';

  pag.innerHTML = sizeHtml + prevHtml + infoHtml + nextHtml;
  var grid = document.getElementById(tab + "-grid");
  if (grid) grid.parentNode.insertBefore(pag, grid);
}

function gotoPage(tab, page) {
  tabPage[tab] = page;
  if (!window._stems) window._stems = {};
  window._stems[tab] = {};
  renderTab(tab);
  updateBulkBar(tab);
  var content = document.querySelector(".content");
  if (content) content.scrollTop = 0;
}

function changePageSize(tab, size) {
  pageSize = parseInt(size);
  tabPage[tab] = 1;
  if (!window._stems) window._stems = {};
  window._stems[tab] = {};
  renderTab(tab);
  updateBulkBar(tab);
}

// ── Actions ───────────────────────────────────────────────────────
async function singleAction(tab, action, idx) {
  var stem = window._stems[tab][idx];
  await performAction(action, [stem]);
  await loadPage(tab);
  await loadStats();
}

function confirmSingle(tab, action, idx) {
  var stem = window._stems[tab][idx];
  showModal(
    "Confirm Deletion",
    "This will permanently delete the source video for \"" + stem + "\". This cannot be undone.",
    async function() { closeModal(); await singleAction(tab, action, idx); }
  );
}

async function bulkAction(tab, action) {
  var stems = Array.from(selections[tab]);
  if (!stems.length) return;
  await performAction(action, stems);
  await loadPage(tab);
  await loadStats();
  clearSelection(tab);
}

function confirmBulkAction(tab, action) {
  var n = selections[tab].size;
  if (!n) return;
  showModal(
    "Delete " + n + " item" + (n > 1 ? "s" : "") + "?",
    "This will permanently delete " + n + " source video file" + (n > 1 ? "s" : "") + ". This cannot be undone.",
    async function() { closeModal(); await bulkAction(tab, action); }
  );
}

async function performAction(action, stems) {
  var endpoints = {
    "approve":           "/api/sheets/approve",
    "reject":            "/api/sheets/reject",
    "quarantine-single": "/api/sheets/quarantine",
    "requeue":           "/api/sheets/requeue",
    "remove-rejected":   "/api/sheets/remove-rejected",
  };
  var url = endpoints[action];
  if (!url) return;

  if (action === "quarantine-single") {
    var res  = await fetch(url, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({stem:stems[0]})});
    var data = await res.json();
    toast(res.ok ? "Moved to quarantine ⚠" : "Error: " + (data.message || "unknown"), !res.ok);
    return;
  }
  if (action === "requeue") {
    for (var i = 0; i < stems.length; i++) {
      var res2 = await fetch(url, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({stem:stems[i]})});
      var d2   = await res2.json();
      if (!res2.ok) { toast("Error: " + (d2.message || "unknown"), true); return; }
    }
    toast("Re-queued ✓ — check Review shortly");
    return;
  }
  if (action === "remove-rejected") {
    var res3  = await fetch(url, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({stems:stems})});
    var data3 = await res3.json();
    var cnt   = (data3.removed || []).length;
    toast(res3.ok ? "Removed " + cnt + " entr" + (cnt > 1 ? "ies" : "y") + " ✓" : "Error removing", !res3.ok);
    return;
  }
  var res4  = await fetch(url, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({stems:stems})});
  var data4 = await res4.json();
  var labels = {approve:"Approved ✓", reject:"Rejected ✗"};
  toast(res4.ok ? (labels[action] || "Done") + " (" + stems.length + ")" : "Error: " + (data4.message || "unknown"), !res4.ok);
}

// ── Scan ──────────────────────────────────────────────────────────
async function checkScanStatus() {
  try {
    var res  = await fetch("/api/scan/status");
    var data = await res.json();
    setScanRunning(data.running);
  } catch(e) {}
}

function setScanRunning(running) {
  scanRunning = running;
  var btn = document.getElementById("scan-btn");
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
    var res = await fetch("/api/scan/stop", {method:"POST"});
    if (res.ok) { setScanRunning(false); toast("Scan stopping — current file will finish first"); }
  } else {
    var res2 = await fetch("/api/scan", {method:"POST"});
    if (res2.ok) { setScanRunning(true); toast("Scan started — check Logs for progress"); }
  }
}

// ── Selection ─────────────────────────────────────────────────────
function toggleSelectByIdx(tab, idx, checked) {
  var stem = window._stems[tab][idx];
  if (checked) selections[tab].add(stem);
  else         selections[tab].delete(stem);
  var card = document.querySelector("#" + tab + "-grid .sheet-card[data-stem='" + CSS.escape(stem) + "']");
  if (card) card.classList.toggle("selected", checked);
  updateBulkBar(tab);
}

function toggleSelectAll(tab) {
  var cb      = document.getElementById("select-all-" + tab);
  var checked = cb ? cb.checked : false;
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
  var sa = document.getElementById("select-all-" + tab);
  if (sa) sa.checked = true;
}

function clearSelection(tab) {
  selections[tab].clear();
  document.querySelectorAll("#" + tab + "-grid .sheet-checkbox").forEach(function(c) { c.checked = false; });
  var sa = document.getElementById("select-all-" + tab);
  if (sa) sa.checked = false;
  document.querySelectorAll("#" + tab + "-grid .sheet-card").forEach(function(c) { c.classList.remove("selected"); });
  updateBulkBar(tab);
}

function updateBulkBar(tab) {
  var bar   = document.getElementById("bulk-actions-" + tab);
  var count = document.getElementById("selected-count-" + tab);
  var saBtn = document.getElementById("bulk-select-all-" + tab);
  var total = tabSheets[tab].length;
  var n     = selections[tab].size;
  if (!bar) return;
  if (n > 0) {
    bar.style.display = "flex";
    if (count) count.textContent = n + " selected";
    if (saBtn) saBtn.style.display = n < total ? "inline-flex" : "none";
  } else {
    bar.style.display = "none";
  }
}

// ── Search ────────────────────────────────────────────────────────
async function onGlobalSearch() {
  var q        = ((document.getElementById("global-search") || {value:""}).value || "").trim();
  var clearBtn = document.getElementById("search-clear");
  if (q.length === 0) {
    if (clearBtn) clearBtn.style.display = "none";
    if (currentTab === "search") navigateTo("pending");
    return;
  }
  if (clearBtn) clearBtn.style.display = "block";

  document.querySelectorAll(".page").forEach(function(p) { p.classList.remove("active"); });
  document.querySelectorAll(".sidebar-link").forEach(function(l) { l.classList.remove("active"); });
  var searchPage = document.getElementById("page-search");
  if (searchPage) searchPage.classList.add("active");
  currentTab = "search";

  var heading = document.getElementById("search-heading");
  if (heading) heading.textContent = "Search: " + q;

  var container = document.getElementById("search-results");
  if (container) container.innerHTML = '<p style="color:var(--text-dim);padding:20px 0">Searching…</p>';

  var tabLabels = {pending:"Review", approved:"Approved", quarantined:"Quarantine", rejected:"Rejected"};
  var sections  = [];

  for (var i = 0; i < TABS.length; i++) {
    var tab = TABS[i];
    var res = await fetch("/api/sheets?state=" + tab + "&search=" + encodeURIComponent(q.toLowerCase()));
    var sheets = await res.json();
    tabSheets[tab] = sheets;
    if (sheets.length > 0) sections.push({tab:tab, label:tabLabels[tab], sheets:sheets});
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
    section.innerHTML = '<div class="search-section-title">' + sec.label + '<span class="nav-badge">' + sec.sheets.length + '</span></div>';

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
        var pct2 = Math.round(sheet.nsfw_confidence * 100);
        confBadge = '<span class="conf-badge">' + pct2 + '%</span>';
      }

      var imgHtml2 = '<div class="sheet-img-placeholder">No sheet</div>';
      if (sheet.has_sheet) {
        var src2 = "/api/sheets/image/" + encodeURIComponent(sheet.filename);
        imgHtml2 = isRejected
          ? '<div class="sheet-img-hidden" data-src="' + src2 + '"><div class="sheet-img-hidden-label">⚠ Click to reveal</div></div>'
          : '<img class="sheet-img" src="' + src2 + '" alt="" loading="lazy">';
      }

      var srcPath2  = (sheet.source_path || "Unknown").replace(/"/g, "&quot;");
      var card2 = document.createElement("div");
      card2.className = "sheet-card" + (showNsfw ? " flagged" : "");
      card2.dataset.stem = sheet.stem;
      card2.innerHTML =
        (showNsfw ? '<div class="flagged-overlay">⚠ NSFW</div>' : "") +
        imgHtml2 +
        '<div class="sheet-info">' +
          '<div class="sheet-name">' + confBadge + escapeHtml(sheet.stem) + '</div>' +
          '<div class="sheet-path" title="' + srcPath2 + '">' + escapeHtml(sheet.source_path || "Unknown") + '</div>' +
          renderActions(sec.tab, idx, sheet) +
        '</div>';

      var img2 = card2.querySelector(".sheet-img");
      if (img2) {
        var _ss = img2.src, _st = sec.tab, _si = idx;
        img2.addEventListener("click", function() { openLightbox(_ss, _st, _si); });
      }
      var hid2 = card2.querySelector(".sheet-img-hidden");
      if (hid2) {
        var _hs2 = hid2.dataset.src;
        hid2.addEventListener("click", function() { revealImage(hid2, _hs2); });
      }
      grid.appendChild(card2);
    });

    section.appendChild(grid);
    if (sec.sheets.length > 20) {
      var more = document.createElement("p");
      more.style.cssText = "color:var(--text-dim);font-size:12px;margin-top:8px";
      more.textContent = "+" + (sec.sheets.length - 20) + " more — go to " + sec.label + " tab to see all";
      section.appendChild(more);
    }
    container.appendChild(section);
  });
}

function clearSearch() {
  var input    = document.getElementById("global-search");
  var clearBtn = document.getElementById("search-clear");
  if (input)    input.value = "";
  if (clearBtn) clearBtn.style.display = "none";
  navigateTo("pending");
}

// ── Config ────────────────────────────────────────────────────────
async function loadConfig() {
  var res = await fetch("/api/config");
  var cfg = await res.json();
  // Set profile radio
  var profile = cfg.detection_profile || "balanced";
  var profileRadio = document.querySelector("input[name='detection-profile'][value='" + profile + "']");
  if (profileRadio) profileRadio.checked = true;
  applyProfile(profile);

  document.getElementById("cfg-zone-approve").value         = cfg.zone_auto_approve  != null ? cfg.zone_auto_approve  : 0.4;
  document.getElementById("cfg-zone-quarantine").value      = cfg.zone_quarantine     != null ? cfg.zone_quarantine     : 0.5;
  document.getElementById("cfg-zone-reject").value          = cfg.zone_auto_reject    != null ? cfg.zone_auto_reject    : 0.9;
  document.getElementById("cfg-nudenet-frames").value       = cfg.nudenet_frames      || 10;
  document.getElementById("cfg-watch-folders").value        = (cfg.watch_folders || []).join("\n");
  document.getElementById("cfg-scan-schedule-enabled").checked = !!cfg.scan_schedule_enabled;
  document.getElementById("cfg-scan-schedule").value        = cfg.scan_schedule       || "daily";
  document.getElementById("cfg-quarantine-dir").value       = cfg.quarantine_dir      || "";
  document.getElementById("cfg-quarantine-days").value      = cfg.quarantine_auto_reject_days != null ? cfg.quarantine_auto_reject_days : 0;
  document.getElementById("cfg-polling-enabled").checked    = !!cfg.polling_enabled;
  document.getElementById("cfg-poll-interval").value        = cfg.poll_interval_seconds || 600;
  document.getElementById("cfg-sonarr-url").value           = cfg.sonarr_url          || "";
  document.getElementById("cfg-sonarr-key").value           = cfg.sonarr_api_key      || "";
  document.getElementById("cfg-radarr-url").value           = cfg.radarr_url          || "";
  document.getElementById("cfg-radarr-key").value           = cfg.radarr_api_key      || "";
  document.getElementById("cfg-webhook-url").value          = cfg.webhook_url         || "";
  document.getElementById("cfg-webhook-quarantine").checked = !!cfg.webhook_on_quarantine;
  document.getElementById("cfg-webhook-reject").checked     = !!cfg.webhook_on_reject;
  document.getElementById("cfg-vcs-grid").value             = cfg.vcs_grid            || "4x4";
  document.getElementById("cfg-vcsi-timeout").value         = cfg.vcsi_timeout_seconds || 300;
  checkArrKeys();
}

// ── Detection profiles ───────────────────────────────────────────
var PROFILES = {
  conservative: {zone_auto_approve: 0.2, zone_quarantine: 0.55, zone_auto_reject: 0.85},
  balanced:     {zone_auto_approve: 0.4, zone_quarantine: 0.6,  zone_auto_reject: 0.85},
  aggressive:   {zone_auto_approve: 0.55,zone_quarantine: 0.65, zone_auto_reject: 0.85},
};

function applyProfile(profile) {
  var customSection = document.getElementById("zone-custom-section");
  if (profile === "custom") {
    if (customSection) customSection.style.display = "block";
    return;
  }
  if (customSection) customSection.style.display = "none";
  var p = PROFILES[profile];
  if (!p) return;
  document.getElementById("cfg-zone-approve").value    = p.zone_auto_approve;
  document.getElementById("cfg-zone-quarantine").value = p.zone_quarantine;
  document.getElementById("cfg-zone-reject").value     = p.zone_auto_reject;
}

async function saveConfig() {
  var folders = document.getElementById("cfg-watch-folders").value
    .split("\n").map(function(s) { return s.trim(); }).filter(Boolean);
  var payload = {
    detection_profile:           (document.querySelector("input[name='detection-profile']:checked") || {value:"balanced"}).value,
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
  var res = await fetch("/api/config", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(payload)});
  var msg = document.getElementById("config-msg");
  if (res.ok) { msg.className = "msg success"; msg.textContent = "✓ Configuration saved."; }
  else {
    var d = await res.json().catch(function() { return {}; });
    msg.className = "msg error"; msg.textContent = "✗ " + (d.message || "Failed to save.");
  }
  msg.style.display = "block";
  setTimeout(function() { msg.style.display = "none"; }, 4000);
}

function checkArrKeys() {
  var hasKeys = (document.getElementById("cfg-sonarr-key") || {value:""}).value.trim().length > 0 ||
                (document.getElementById("cfg-radarr-key") || {value:""}).value.trim().length > 0;
  var toggle  = document.getElementById("cfg-polling-enabled");
  var hint    = document.getElementById("polling-no-keys-hint");
  if (!toggle) return;
  if (!hasKeys) { toggle.disabled = true; toggle.checked = false; if (hint) hint.style.display = "block"; }
  else          { toggle.disabled = false; if (hint) hint.style.display = "none"; }
}

async function testConnection(service) {
  var btn    = document.getElementById("test-" + service + "-btn");
  var result = document.getElementById("test-" + service + "-result");
  btn.textContent = "…"; btn.disabled = true;
  await saveConfig();
  var res  = await fetch("/api/config/test-" + service, {method:"POST"});
  var data = await res.json();
  result.className   = res.ok ? "test-result success" : "test-result error";
  result.textContent = res.ok ? "✓ Connected — " + service + " v" + data.version : "✗ " + (data.message || "Connection failed");
  btn.textContent = "Test"; btn.disabled = false;
}

async function testWebhook() {
  var btn    = document.getElementById("test-webhook-btn");
  var result = document.getElementById("test-webhook-result");
  btn.textContent = "…"; btn.disabled = true;
  await saveConfig();
  var res  = await fetch("/api/config/test-webhook", {method:"POST"});
  var data = await res.json();
  result.className   = res.ok ? "test-result success" : "test-result error";
  result.textContent = res.ok ? "✓ Webhook delivered" : "✗ " + (data.message || "Failed");
  btn.textContent = "Test"; btn.disabled = false;
}

async function cleanDatabase() {
  var result = document.getElementById("clean-db-result");
  result.className = "test-result"; result.textContent = "Cleaning…";
  var res  = await fetch("/api/db/clean", {method:"POST"});
  var data = await res.json();
  result.className   = res.ok ? "test-result success" : "test-result error";
  result.textContent = res.ok
    ? (data.removed > 0 ? "✓ Removed " + data.removed + " stale record" + (data.removed > 1 ? "s" : "") : "✓ Database is clean")
    : "✗ Clean failed";
}

// ── Logs ──────────────────────────────────────────────────────────
async function loadLogs() {
  var lines = document.getElementById("log-lines").value;
  var level = document.getElementById("log-level").value;
  var res   = await fetch("/api/logs?lines=" + lines + "&level=" + level);
  var data  = await res.json();
  var out   = document.getElementById("log-output");
  out.innerHTML = data.lines.map(function(line) {
    var cls = "log-info";
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

function dedupeLabels(flagReason) {
  if (!flagReason) return [];
  var seen = {};
  flagReason.split(", ").forEach(function(l) {
    var m    = l.match(/^(.+?)\s*\((\d+)%\)$/);
    var key  = m ? m[1] : l.trim();
    var conf = m ? parseInt(m[2]) : 0;
    if (seen[key] == null || conf > seen[key]) seen[key] = conf;
  });
  return Object.keys(seen).map(function(k) { return {label: k, conf: seen[k]}; });
}

function formatLabel(label) {
  return label.replace(/_/g, " ").toLowerCase().replace(/(^|\s)\S/g, function(c) { return c.toUpperCase(); });
}

function openLightbox(src, tab, idx) {
  var existing = document.getElementById("lightbox");
  if (existing) existing.remove();

  _lightboxTab   = tab  != null ? tab  : null;
  _lightboxIdx   = idx  != null ? idx  : null;
  _lightboxTotal = tab  != null ? (tabSheets[tab] || []).length : 0;

  var sheet = (tab != null && idx != null) ? (tabSheets[tab] || [])[idx] : null;

  // Side panel content
  var titleHtml = sheet ? '<div class="lb-title">' + escapeHtml(sheet.stem) + '</div>' : '';

  var confHtml = "";
  if (sheet && sheet.nsfw_confidence) {
    var pct   = Math.round(sheet.nsfw_confidence * 100);
    var color = pct >= 80 ? "var(--danger)" : pct >= 50 ? "var(--warn)" : "var(--accent)";
    confHtml  = '<div class="lb-conf"><div class="lb-conf-score" style="color:' + color + '">' + pct + '%</div><div class="lb-conf-label">confidence</div></div>';
  }

  var labelsHtml = "";
  if (sheet && sheet.flag_reason) {
    var rows = dedupeLabels(sheet.flag_reason).map(function(l) {
      return '<div class="lb-label"><span class="lb-label-name">' + formatLabel(l.label) + '</span>' +
             (l.conf > 0 ? '<span class="lb-label-conf">' + l.conf + '%</span>' : '') + '</div>';
    }).join("");
    labelsHtml = '<div class="lb-labels-title">Detected</div><div class="lb-labels">' + rows + '</div>';
  } else if (sheet) {
    labelsHtml = '<div class="lb-no-flags">No flags detected</div>';
  }

  var actionsHtml = "";
  if (tab === "pending" && idx != null) {
    actionsHtml = '<div class="lb-actions">' +
      '<button class="btn btn-success lb-action" onclick="lightboxAction(\'approve\')">✓ Approve</button>' +
      '<button class="btn btn-danger lb-action"  onclick="lightboxAction(\'reject\')">✗ Reject</button>' +
      '</div>';
  } else if (tab === "quarantined" && idx != null) {
    actionsHtml = '<div class="lb-actions">' +
      '<button class="btn btn-success lb-action" onclick="lightboxAction(\'approve\')">✓ Restore</button>' +
      '<button class="btn btn-danger lb-action"  onclick="lightboxAction(\'reject\')">✗ Delete</button>' +
      '</div>';
  }

  var hasPrev = idx != null && idx > 0;
  var hasNext = idx != null && idx < _lightboxTotal - 1;
  var counter = idx != null ? '<div class="lb-counter">' + (idx + 1) + ' / ' + _lightboxTotal + '</div>' : '';

  var lb = document.createElement("div");
  lb.id        = "lightbox";
  lb.className = "lightbox";
  lb.innerHTML =
    '<div class="lb-outer" onclick="event.stopPropagation()">' +
      (hasPrev ? '<button class="lb-arrow lb-arrow-prev" onclick="lightboxNav(-1)">&#8249;</button>' : '<div class="lb-arrow-spacer"></div>') +
      '<div class="lb-img-wrap">' +
        '<img class="lb-img" src="' + src + '" alt="">' +
        counter +
      '</div>' +
      (hasNext ? '<button class="lb-arrow lb-arrow-next" onclick="lightboxNav(1)">&#8250;</button>'  : '<div class="lb-arrow-spacer"></div>') +
      '<div class="lb-panel">' +
        '<button class="lb-close" onclick="closeLightbox()">✕</button>' +
        titleHtml +
        confHtml +
        labelsHtml +
        actionsHtml +
        '<div class="lb-hint">← → navigate &nbsp;·&nbsp; Esc close' + (tab === "quarantined" ? ' &nbsp;·&nbsp; R restore &nbsp;·&nbsp; D delete' : (actionsHtml ? ' &nbsp;·&nbsp; A approve &nbsp;·&nbsp; R reject' : '')) + '</div>' +
      '</div>' +
    '</div>';

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
  openLightbox("/api/sheets/image/" + encodeURIComponent(sheet.filename), _lightboxTab, newIdx);
}

async function lightboxAction(action) {
  if (_lightboxTab == null || _lightboxIdx == null) return;
  var idx  = _lightboxIdx;
  var tab  = _lightboxTab;
  var next = idx < _lightboxTotal - 1 ? idx + 1 : (idx > 0 ? idx - 1 : null);

  if (action === "reject") {
    // Confirm then advance
    showModal(
      "Confirm Deletion",
      "This will permanently delete the source video. This cannot be undone.",
      async function() {
        closeModal();
        closeLightbox();
        await singleAction(tab, "reject", idx);
        // Advance to next if available
        if (next != null && tabSheets[tab] && tabSheets[tab][next] && tabSheets[tab][next].has_sheet) {
          var s = "/api/sheets/image/" + encodeURIComponent(tabSheets[tab][next].filename);
          openLightbox(s, tab, next);
        }
      }
    );
  } else {
    closeLightbox();
    await singleAction(tab, "approve", idx);
    // Advance to next if available
    if (next != null && tabSheets[tab] && tabSheets[tab][next] && tabSheets[tab][next].has_sheet) {
      var s2 = "/api/sheets/image/" + encodeURIComponent(tabSheets[tab][next].filename);
      openLightbox(s2, tab, next);
    }
  }
}

document.addEventListener("keydown", function(e) {
  if (!document.getElementById("lightbox")) return;
  if (e.key === "Escape")     { closeLightbox(); return; }
  if (e.key === "ArrowLeft")  { lightboxNav(-1); return; }
  if (e.key === "ArrowRight") { lightboxNav(1);  return; }
  // Shortcuts differ by tab
  if (_lightboxTab === "quarantined") {
    if (e.key === "r" || e.key === "R") { lightboxAction("approve"); return; } // Restore
    if (e.key === "d" || e.key === "D") { lightboxAction("reject");  return; } // Delete
  } else {
    if (e.key === "a" || e.key === "A") { lightboxAction("approve"); return; } // Approve
    if (e.key === "r" || e.key === "R") { lightboxAction("reject");  return; } // Reject
  }
});

function revealImage(el, src) {
  var img = document.createElement("img");
  img.className = "sheet-img";
  img.src = src; img.alt = ""; img.loading = "lazy";
  img.addEventListener("click", function() { openLightbox(src); });
  el.replaceWith(img);
}

// ── Sidebar toggle ─────────────────────────────────────────────────
function toggleSidebar() {
  var sidebar  = document.querySelector(".sidebar");
  var backdrop = document.getElementById("sidebar-backdrop");
  var isMobile = window.innerWidth <= 768;
  if (isMobile) {
    var hidden = sidebar.classList.toggle("collapsed");
    if (backdrop) backdrop.classList.toggle("visible", !hidden);
  } else {
    sidebar.classList.toggle("collapsed");
  }
  try { localStorage.setItem("sidebarCollapsed", sidebar.classList.contains("collapsed")); } catch(e) {}
}

// ── Toast ─────────────────────────────────────────────────────────
function toast(msg, isError) {
  var el = document.getElementById("toast");
  el.textContent = msg;
  el.style.color = isError ? "var(--danger)" : "var(--accent2)";
  el.style.display = "block";
  setTimeout(function() { el.style.display = "none"; }, 3000);
}

// ── Helpers ───────────────────────────────────────────────────────
function escapeHtml(s) {
  if (!s) return "";
  return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");
}
