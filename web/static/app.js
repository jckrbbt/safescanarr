/* ── Safe Scanarr v1.0.5 ───────────────────────────────────────── */

const TABS     = ["pending", "approved", "quarantined", "rejected"];
let currentTab = "pending";
let tabSheets  = {};
let selections = {};
let statsTimer    = null;
let scanPollTimer = null;
let scanRunning   = false;
let pageSize      = 20;
let tabSort       = {pending: "risk", approved: "date", quarantined: "risk", rejected: "date"};
let approvedSourceFilter = "";
let tabPage       = {};
let searchAbort   = null;

TABS.forEach(function(t) {
  tabSheets[t]  = [];
  selections[t] = new Set();
  tabPage[t]    = 1;
});

// ── Auth / CSRF helpers ───────────────────────────────────────────
var _deleteOnReject = false;   // set from /api/version
var _authMethod     = "token";  // set from /api/version

function csrfToken() {
  var m = document.querySelector('meta[name="csrf-token"]');
  return m ? m.getAttribute("content") : "";
}

function redirectToLogin() {
  window.location.href = "/login";
}

function isInputTarget(el) {
  var tag = (el && el.tagName) ? el.tagName.toLowerCase() : "";
  return tag === "input" || tag === "textarea" || tag === "select" || (el && el.isContentEditable);
}

// Wrapper around fetch: attaches the CSRF header to state-changing requests and
// bounces the browser back to /login when the session has expired.
async function apiFetch(url, opts) {
  opts = opts || {};
  if (opts.method && opts.method !== "GET") {
    opts.credentials = opts.credentials || "same-origin";
    opts.headers = Object.assign({}, opts.headers || {}, {"X-CSRF-Token": csrfToken()});
  }
  var res = await window.fetch(url, opts);
  if (res.status === 401) {
    redirectToLogin();
    throw new Error("unauthenticated");
  }
  return res;
}

async function signOut() {
  try { await apiFetch("/logout", {method: "POST", headers: {"X-CSRF-Token": csrfToken()}}); } catch(e) {}
  redirectToLogin();
}

document.addEventListener("DOMContentLoaded", function() {
  loadVersion();
  loadStats();

  // Sync sort dropdowns to defaults defined in tabSort
  TABS.forEach(function(t) {
    var sel = document.querySelector("#page-" + t + " .sort-control select");
    if (sel) sel.value = tabSort[t];
  });

  // Deep-link: open the requested tab from ?tab=... (e.g. webhook notifications)
  var initialTab = "pending";
  try {
    var params = new URLSearchParams(window.location.search);
    var requested = params.get("tab");
    if (requested && (TABS.indexOf(requested) >= 0 || ["stats","config","logs"].indexOf(requested) >= 0)) {
      initialTab = requested;
    }
  } catch(e) {}
  navigateTo(initialTab);
  statsTimer    = setInterval(loadStats, 60000);
  checkScanStatus();
  scanPollTimer = setInterval(checkScanStatus, 5000);

  document.querySelectorAll(".sidebar-link").forEach(function(link) {
    link.addEventListener("click", function(e) {
      e.preventDefault();
      navigateTo(link.dataset.page);
    });
  });

  document.querySelectorAll(".bnav-item").forEach(function(btn) {
    if (btn.id === "bnav-more") return;
    btn.addEventListener("click", function() { navigateTo(btn.dataset.page); });
  });

  // Restore sidebar state
  try {
    var pref     = localStorage.getItem("sidebarCollapsed");
    var sidebar  = document.querySelector(".sidebar");
    var isMobile = window.matchMedia("(max-width: 900px)").matches;
    if (pref === null) { if (isMobile) sidebar.classList.add("collapsed"); }
    else if (pref === "true") sidebar.classList.add("collapsed");
  } catch(e) {}

  // Config dirty-state tracking (after initial load)
  initConfigDirtyTracking();

  // Keyboard shortcuts
  initKeyboardShortcuts();
});

// ── Navigation ────────────────────────────────────────────────────
function navigateTo(page) {
  currentTab = page;
  document.querySelectorAll(".page").forEach(function(p) { p.classList.remove("active"); });
  document.querySelectorAll(".sidebar-link").forEach(function(l) { l.classList.remove("active"); });
  document.querySelectorAll(".bnav-item").forEach(function(b) { b.classList.remove("active"); });
  var pageEl = document.getElementById("page-" + page);
  var linkEl = document.querySelector(".sidebar-link[data-page='" + page + "']");
  var bnEl   = document.querySelector(".bnav-item[data-page='" + page + "']");
  if (pageEl) pageEl.classList.add("active");
  if (linkEl) linkEl.classList.add("active");
  if (bnEl) bnEl.classList.add("active");
  if (TABS.includes(page)) loadPage(page);
  if (page === "config")   loadConfig();
  if (page === "logs")     loadLogs();
  if (page === "stats")    loadStatsPage();
}

// ── State system ──────────────────────────────────────────────────
function renderState(container, kind, opts) {
  opts = opts || {};
  if (typeof container === "string") container = document.getElementById(container);
  if (!container) return;
  container.innerHTML = "";
  container.style.display = "block";

  if (kind === "loading") {
    container.classList.add("is-loading");
    var cards = "";
    for (var i = 0; i < 8; i++) {
      cards += '<div class="skeleton-card"><div class="skeleton-img"></div><div class="skeleton-line"></div><div class="skeleton-line short"></div></div>';
    }
    container.innerHTML = cards;
    return;
  }

  var icons = {empty: "📭", error: "⚠️", search: "🔍"};
  container.classList.remove("is-loading");
  var icon  = opts.icon || icons[kind] || "";
  var title = opts.title || (kind === "empty" ? "Nothing here" : "Something went wrong");
  var body  = opts.body  || "";
  var action = opts.action;

  var html = '<div class="state">' +
    '<div class="state-icon">' + icon + '</div>' +
    '<div class="state-title">' + escapeHtml(title) + '</div>';
  if (body) html += '<div class="state-body">' + escapeHtml(body) + '</div>';
  if (action) {
    html += '<button class="btn btn-secondary" id="state-action-btn">' + escapeHtml(action.label) + '</button>';
  }
  html += '</div>';
  container.innerHTML = html;

  if (action) {
    var btn = container.querySelector("#state-action-btn");
    if (btn) btn.addEventListener("click", action.onClick);
  }
}

function clearState(container) {
  if (typeof container === "string") container = document.getElementById(container);
  if (!container) return;
  container.innerHTML = "";
  container.classList.remove("is-loading");
  container.style.display = "none";
}

// ── Version ───────────────────────────────────────────────────────
async function loadVersion() {
  try {
    var res = await apiFetch("/api/version");
    var d   = await res.json();
    document.getElementById("nav-version").textContent = "v" + d.version;
    _deleteOnReject = !!d.delete_on_reject;
    _authMethod = d.auth_method || "token";
  } catch(e) {}
}

// ── Stats ─────────────────────────────────────────────────────────
async function loadStats() {
  try {
    var res   = await apiFetch("/api/stats");
    var stats = await res.json();
    TABS.forEach(function(t) {
      var badge = document.getElementById("badge-" + t);
      var bnav  = document.getElementById("bnav-badge-" + t);
      var count = document.getElementById(t + "-count");
      var n = stats[t] || 0;
      if (badge) badge.textContent = n > 0 ? n : "";
      if (bnav)  bnav.textContent  = n > 0 ? n : "";
      if (count) count.textContent = n + " item" + (n !== 1 ? "s" : "");
    });
  } catch(e) {}
}

// ── Load a tab ────────────────────────────────────────────────────
async function loadPage(tab, resetPage) {
  if (resetPage) tabPage[tab] = 1;
  var search = (document.getElementById("global-search") || {value:""}).value || "";
  var url    = "/api/sheets?state=" + tab + "&search=" + encodeURIComponent(search.toLowerCase());
  if (tab === "approved" && approvedSourceFilter) url += "&source=" + approvedSourceFilter;

  clearState(tab + "-state");
  renderState(tab + "-state", "loading");
  try {
    var res = await apiFetch(url);
    tabSheets[tab] = await res.json();
    selections[tab].clear();
    if (!window._stems) window._stems = {};
    window._stems[tab] = {};
    renderTab(tab);
    updateBulkBar(tab);
  } catch (e) {
    clearState(tab + "-state");
    renderState(tab + "-state", "error", {
      title: "Could not load " + tab,
      body: e.message || "Network error",
      action: {label: "Retry", onClick: function() { loadPage(tab, true); }}
    });
  }
}

function filterApproved(source) {
  approvedSourceFilter = source;
  tabPage["approved"] = 1;
  loadPage("approved");
}

function sortTab(tab, sortBy) {
  tabSort[tab] = sortBy;
  tabPage[tab] = 1;
  if (!window._stems) window._stems = {};
  window._stems[tab] = {};
  renderTab(tab);
}

function applySortOrder(sheets, sortBy) {
  var sorted = sheets.slice();
  if (sortBy === "risk") {
    sorted.sort(function(a, b) { return (b.nsfw_confidence || 0) - (a.nsfw_confidence || 0); });
  } else if (sortBy === "title") {
    sorted.sort(function(a, b) { return a.stem.localeCompare(b.stem); });
  } else if (sortBy === "date") {
    sorted.sort(function(a, b) { return new Date(b.updated_at || 0) - new Date(a.updated_at || 0); });
  } else if (sortBy === "size") {
    sorted.sort(function(a, b) { return (b.size || 0) - (a.size || 0); });
  }
  return sorted;
}

function riskClass(confidence) {
  if (confidence == null) return "risk-low";
  var pct = confidence * 100;
  if (pct < 40) return "risk-low";
  if (pct < 75) return "risk-med";
  return "risk-high";
}

function riskBadge(confidence, reason, isApproved) {
  if (confidence == null) return "";
  var pct = Math.round(confidence * 100);
  var tip = reason ? escapeHtml(reason) : (pct === 0 ? "No risk detected" : "NSFW detected");
  var cls = isApproved ? "risk-badge-clean" : riskClass(confidence);
  var meterColor = isApproved ? "var(--text-dim)" : (pct >= 75 ? "var(--danger)" : pct >= 40 ? "var(--warn)" : "var(--accent2)");
  return '<span class="risk-badge ' + cls + '" title="' + tip + '">' +
           pct + '% risk' +
           '<span class="risk-meter" aria-hidden="true"><span class="risk-meter-bar" style="width:' + pct + '%;background:' + meterColor + '"></span></span>' +
         '</span>';
}

function renderTab(tab) {
  var grid  = document.getElementById(tab + "-grid");
  var empty = document.getElementById(tab + "-empty");
  var state = document.getElementById(tab + "-state");
  if (!grid) return;
  grid.innerHTML = "";

  var sheets = tabSheets[tab];
  if (sheets.length === 0) {
    if (empty) {
      empty.style.display = "block";
      var messages = {
        pending:     ["Nothing to review", "All caught up — run a scan to check for new media.", "▶ Run a Scan", function() { toggleScan(); }],
        approved:    ["No approved items yet", "Approved content appears here.", null, null],
        quarantined: ["Quarantine is empty", "Nothing is waiting for a decision.", null, null],
        rejected:    ["No rejected items", "Rejected items are logged here.", null, null]
      };
      var m = messages[tab];
      var action = m[2] ? {label: m[2], onClick: m[3]} : null;
      renderState(state, "empty", {icon: "📭", title: m[0], body: m[1], action: action});
    }
    renderPagination(tab, 1, 1, 0);
    return;
  }
  if (empty) empty.style.display = "none";
  clearState(state);

  // Apply sort
  var sortBy = tabSort[tab] || "risk";
  sheets = applySortOrder(sheets, sortBy);

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
    var showNsfw   = sheet.flagged && !isApproved && tab !== "quarantined" && tab !== "rejected";
    var checked    = selections[tab].has(sheet.stem) ? "checked" : "";

    var card = document.createElement("div");
    card.className = "sheet-card" + (showNsfw ? " flagged" : "") +
                     (selections[tab].has(sheet.stem) ? " selected" : "");
    card.dataset.stem = sheet.stem;
    card.tabIndex = 0;

    var confBadge = riskBadge(sheet.nsfw_confidence, sheet.flag_reason, isApproved);

    var labelBreakdown = "";
    if (showNsfw && sheet.flag_reason) {
      var chips = dedupeLabels(sheet.flag_reason).map(function(l) {
        var name = escapeHtml(formatLabel(l.label));
        return '<span class="label-chip">' + name + (l.conf > 0 ? " " + l.conf + "%" : "") + '</span>';
      }).join("");
      labelBreakdown = '<div class="label-breakdown">' + chips + '</div>';
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
    var srcPath = escapeHtml(sheet.source_path || "Unknown");

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
    card.addEventListener("keydown", function(e) {
      if (e.key === "Enter") {
        var img2 = card.querySelector(".sheet-img");
        if (img2) img2.click();
      }
    });

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
    _deleteOnReject ? "Confirm Deletion" : "Confirm Reject",
    _deleteOnReject
      ? "This will permanently delete the source video for \"" + stem + "\". This cannot be undone."
      : "This will move the source video for \"" + stem + "\" to quarantine. You can still restore or delete it later.",
    async function() { closeModal(); await singleAction(tab, action, idx); },
    "danger"
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
    _deleteOnReject ? "Delete " + n + " item" + (n > 1 ? "s" : "") + "?" : "Reject " + n + " item" + (n > 1 ? "s" : "") + "?",
    _deleteOnReject
      ? "This will permanently delete " + n + " source video file" + (n > 1 ? "s" : "") + ". This cannot be undone."
      : "This will move " + n + " source video file" + (n > 1 ? "s" : "") + " to quarantine. Nothing is deleted.",
    async function() { closeModal(); await bulkAction(tab, action); },
    "danger"
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
    var res  = await apiFetch(url, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({stem:stems[0]})});
    var data = await res.json();
    toast(res.ok ? "Moved to quarantine ⚠" : "Error: " + (data.message || "unknown"), !res.ok);
    return;
  }
  if (action === "requeue") {
    for (var i = 0; i < stems.length; i++) {
      var res2 = await apiFetch(url, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({stem:stems[i]})});
      var d2   = await res2.json();
      if (!res2.ok) { toast("Error: " + (d2.message || "unknown"), true); return; }
    }
    toast("Re-queued ✓ — check Review shortly");
    return;
  }
  if (action === "remove-rejected") {
    var res3  = await apiFetch(url, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({stems:stems})});
    var data3 = await res3.json();
    var cnt   = (data3.removed || []).length;
    toast(res3.ok ? "Removed " + cnt + " entr" + (cnt > 1 ? "ies" : "y") + " ✓" : "Error removing", !res3.ok);
    return;
  }
  var res4  = await apiFetch(url, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({stems:stems})});
  var data4 = await res4.json();
  var labels = {approve:"Approved ✓", reject:"Rejected ✗"};
  toast(res4.ok ? (labels[action] || "Done") + " (" + stems.length + ")" : "Error: " + (data4.message || "unknown"), !res4.ok);
}

// ── Scan ──────────────────────────────────────────────────────────
async function checkScanStatus() {
  try {
    var res  = await apiFetch("/api/scan/status");
    var data = await res.json();
    setScanRunning(data.running);
  } catch(e) {}
}

function setScanRunning(running) {
  scanRunning = running;
  var btn = document.getElementById("scan-btn");
  if (!btn) return;
  if (running) {
    btn.innerHTML = "■ <span>Stop Scan</span>";
    btn.className   = "btn btn-danger btn-sm";
  } else {
    btn.innerHTML = "▶ <span>Run Scan Now</span>";
    btn.className   = "btn btn-primary btn-sm";
  }
}

async function toggleScan() {
  if (scanRunning) {
    var res = await apiFetch("/api/scan/stop", {method:"POST"});
    if (res.ok) { setScanRunning(false); toast("Scan stopping — current file will finish first"); }
  } else {
    var res2 = await apiFetch("/api/scan", {method:"POST"});
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
var searchTimer = null;
function onGlobalSearchDebounced() {
  if (searchTimer) clearTimeout(searchTimer);
  searchTimer = setTimeout(onGlobalSearch, 250);
}

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
  if (!container) return;
  container.innerHTML = '';
  renderState(container, "loading");

  if (searchAbort) searchAbort.abort();
  searchAbort = new AbortController();

  var tabLabels = {pending:"Review", approved:"Approved", quarantined:"Quarantine", rejected:"Rejected"};
  var sections  = [];

  try {
    for (var i = 0; i < TABS.length; i++) {
      var tab = TABS[i];
      var res = await apiFetch("/api/sheets?state=" + tab + "&search=" + encodeURIComponent(q.toLowerCase()));
      var sheets = await res.json();
      tabSheets[tab] = sheets;
      if (sheets.length > 0) sections.push({tab:tab, label:tabLabels[tab], sheets:sheets});
    }
  } catch (e) {
    if (e.name !== "AbortError") {
      container.innerHTML = '';
      renderState(container, "error", {title: "Search failed", body: e.message, action: {label: "Retry", onClick: onGlobalSearch}});
      return;
    }
  }

  container.innerHTML = "";
  if (sections.length === 0) {
    renderState(container, "search", {title: "No results", body: "No matches for \"" + escapeHtml(q) + "\""});
    return;
  }

  sections.forEach(function(sec) {
    var section = document.createElement("div");
    section.className = "search-section";
    section.innerHTML = '<div class="search-section-title">' + escapeHtml(sec.label) + '<span class="nav-badge">' + sec.sheets.length + '</span></div>';

    var grid = document.createElement("div");
    grid.className = "sheets-grid";
    grid.id = "search-grid-" + sec.tab;

    if (!window._stems) window._stems = {};
    window._stems[sec.tab] = {};

    sec.sheets.slice(0, 20).forEach(function(sheet, idx) {
      window._stems[sec.tab][idx] = sheet.stem;
      var isApproved = sec.tab === "approved";
      var isRejected = sec.tab === "rejected";
      var showNsfw   = sheet.flagged && !isApproved && sec.tab !== "quarantined" && sec.tab !== "rejected";

      var confBadge = riskBadge(sheet.nsfw_confidence, sheet.flag_reason, isApproved);

      var imgHtml2 = '<div class="sheet-img-placeholder">No sheet</div>';
      if (sheet.has_sheet) {
        var src2 = "/api/sheets/image/" + encodeURIComponent(sheet.filename);
        imgHtml2 = isRejected
          ? '<div class="sheet-img-hidden" data-src="' + src2 + '"><div class="sheet-img-hidden-label">⚠ Click to reveal</div></div>'
          : '<img class="sheet-img" src="' + src2 + '" alt="" loading="lazy">';
      }

      var srcPath2  = escapeHtml(sheet.source_path || "Unknown");
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

// ── Stats page ───────────────────────────────────────────────────
async function loadStatsPage() {
  var container = document.getElementById("stats-content");
  var state     = document.getElementById("stats-state");
  if (!container) return;
  clearState(state);
  renderState(state, "loading");

  try {
    var res   = await apiFetch("/api/stats/full");
    var stats = await res.json();
    clearState(state);

    var by     = stats.by_state   || {};
    var bd     = stats.breakdown  || {};
    var total  = stats.total      || 0;

    function pct(n) { return total > 0 ? Math.round(n / total * 100) : 0; }
    function card(title, value, sub, color) {
      return '<div class="stat-card">' +
        '<div class="stat-value" style="color:' + (color || "var(--text)") + '">' + value + '</div>' +
        '<div class="stat-title">' + title + '</div>' +
        (sub ? '<div class="stat-sub">' + sub + '</div>' : '') +
      '</div>';
    }

    var summary =
      card("Total Scanned",  total, "", "var(--text)") +
      card("Approved",  (by.approved  || 0), pct(by.approved  || 0) + "% of total", "var(--accent2)") +
      card("Pending",   (by.pending   || 0), pct(by.pending   || 0) + "% of total", "var(--accent)") +
      card("Quarantined",(by.quarantined||0), pct(by.quarantined||0) + "% of total", "var(--warn)") +
      card("Rejected",  (by.rejected  || 0), pct(by.rejected  || 0) + "% of total", "var(--danger)") +
      card("Flagged",   stats.total_flagged || 0, pct(stats.total_flagged || 0) + "% of total", "var(--danger)");

    var autoApproved   = bd["approved_auto"]  || 0;
    var manualApproved = bd["approved_user"]  || 0;
    var autoRejected   = bd["rejected_auto"]  || 0;
    var manualRejected = bd["rejected_user"]  || 0;

    var breakdown =
      '<div class="stats-section-title">Auto vs Manual</div>' +
      '<div class="stat-row"><span class="stat-row-label">Auto-approved</span><span class="stat-row-value">' + autoApproved + '</span></div>' +
      '<div class="stat-row"><span class="stat-row-label">Manually approved</span><span class="stat-row-value">' + manualApproved + '</span></div>' +
      '<div class="stat-row"><span class="stat-row-label">Auto-rejected</span><span class="stat-row-value">' + autoRejected + '</span></div>' +
      '<div class="stat-row"><span class="stat-row-label">Manually rejected</span><span class="stat-row-value">' + manualRejected + '</span></div>' +
      (stats.avg_risk_flagged > 0 ? '<div class="stat-row"><span class="stat-row-label">Avg risk score (flagged)</span><span class="stat-row-value">' + Math.round(stats.avg_risk_flagged * 100) + '%</span></div>' : '');

    container.innerHTML =
      '<div class="stat-cards">' + summary + '</div>' +
      '<div class="stats-panel">' + breakdown + '</div>';
  } catch (e) {
    clearState(state);
    renderState(state, "error", {title: "Could not load stats", body: e.message, action: {label: "Retry", onClick: loadStatsPage}});
  }
}

// ── Config ────────────────────────────────────────────────────────
async function loadConfig() {
  var state = document.getElementById("config-state");
  renderState(state, "loading");
  try {
    var res   = await apiFetch("/api/config");
    var cfg = await res.json();
    clearState(state);
    fillConfigForm(cfg);
    setConfigDirty(false);
  } catch (e) {
    clearState(state);
    renderState(state, "error", {title: "Could not load config", body: e.message, action: {label: "Retry", onClick: loadConfig}});
  }
}

function fillConfigForm(cfg) {
  var profile = cfg.detection_profile || "balanced";
  var profileRadio = document.querySelector("input[name='detection-profile'][value='" + profile + "']");
  if (profileRadio) profileRadio.checked = true;
  applyProfile(profile);

  document.getElementById("cfg-zone-approve").value         = cfg.zone_auto_approve  != null ? cfg.zone_auto_approve  : 0.4;
  document.getElementById("cfg-zone-quarantine").value      = cfg.zone_quarantine     != null ? cfg.zone_quarantine     : 0.5;
  document.getElementById("cfg-zone-reject").value          = cfg.zone_auto_reject    != null ? cfg.zone_auto_reject    : 0.9;
  document.getElementById("cfg-nudenet-frames").value       = cfg.nudenet_frames      || 10;
  window._watchFolders = (cfg.watch_folders || []).slice();
  renderWatchFolders();
  document.getElementById("cfg-scan-schedule-enabled").checked = !!cfg.scan_schedule_enabled;
  document.getElementById("cfg-scan-schedule").value        = cfg.scan_schedule       || "daily";
  document.getElementById("cfg-quarantine-dir").value       = cfg.quarantine_dir      || "";
  document.getElementById("cfg-quarantine-days").value      = cfg.quarantine_auto_reject_days != null ? cfg.quarantine_auto_reject_days : 0;
  var dor = document.getElementById("cfg-delete-on-reject");
  if (dor) dor.checked = !!cfg.delete_on_reject;
  document.getElementById("cfg-polling-enabled").checked    = !!cfg.polling_enabled;
  document.getElementById("cfg-poll-interval").value        = cfg.poll_interval_seconds || 600;
  document.getElementById("cfg-sonarr-url").value           = cfg.sonarr_url          || "";
  document.getElementById("cfg-radarr-url").value           = cfg.radarr_url          || "";
  window._sonarrKeySet = !!cfg.sonarr_api_key_set;
  window._radarrKeySet = !!cfg.radarr_api_key_set;
  setKeyField("cfg-sonarr-key", window._sonarrKeySet);
  setKeyField("cfg-radarr-key", window._radarrKeySet);
  document.getElementById("cfg-webhook-url").value          = cfg.webhook_url         || "";
  document.getElementById("cfg-webhook-review").checked     = !!cfg.webhook_on_review;
  document.getElementById("cfg-webhook-quarantine").checked = !!cfg.webhook_on_quarantine;
  document.getElementById("cfg-webhook-reject").checked     = !!cfg.webhook_on_reject;
  document.getElementById("cfg-web-ui-url").value           = cfg.web_ui_url          || "";
  document.getElementById("cfg-vcs-grid").value             = cfg.vcs_grid            || "4x4";
  document.getElementById("cfg-vcsi-timeout").value         = cfg.vcsi_timeout_seconds || 300;
  checkArrKeys();
}

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

function initConfigDirtyTracking() {
  var form = document.getElementById("config-form");
  if (!form) return;
  var inputs = form.querySelectorAll("input, select, textarea");
  inputs.forEach(function(el) {
    el.addEventListener("input", function() { setConfigDirty(true); });
    el.addEventListener("change", function() { setConfigDirty(true); });
  });
}

function setConfigDirty(dirty) {
  var el = document.getElementById("config-dirty");
  if (el) el.style.display = dirty ? "inline" : "none";
}

async function saveConfig() {
  var folders = (window._watchFolders || []).slice();
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
    delete_on_reject:            !!((document.getElementById("cfg-delete-on-reject") || {}).checked),
    polling_enabled:             document.getElementById("cfg-polling-enabled").checked,
    poll_interval_seconds:       parseInt(document.getElementById("cfg-poll-interval").value),
    sonarr_url:                  document.getElementById("cfg-sonarr-url").value.trim(),
    radarr_url:                  document.getElementById("cfg-radarr-url").value.trim(),
    webhook_url:                 document.getElementById("cfg-webhook-url").value.trim(),
    webhook_on_review:           document.getElementById("cfg-webhook-review").checked,
    webhook_on_quarantine:       document.getElementById("cfg-webhook-quarantine").checked,
    webhook_on_reject:           document.getElementById("cfg-webhook-reject").checked,
    web_ui_url:                  document.getElementById("cfg-web-ui-url").value.trim(),
    vcs_grid:                    document.getElementById("cfg-vcs-grid").value.trim(),
    vcsi_timeout_seconds:        parseInt(document.getElementById("cfg-vcsi-timeout").value),
  };
  var sKey = document.getElementById("cfg-sonarr-key").value.trim();
  var rKey = document.getElementById("cfg-radarr-key").value.trim();
  if (sKey) payload.sonarr_api_key = sKey;
  if (rKey) payload.radarr_api_key = rKey;
  var res = await apiFetch("/api/config", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(payload)});
  var msg = document.getElementById("config-msg");
  if (res.ok) {
    msg.className = "msg success"; msg.textContent = "✓ Configuration saved.";
    window._sonarrKeySet = window._sonarrKeySet || !!sKey;
    window._radarrKeySet = window._radarrKeySet || !!rKey;
    setKeyField("cfg-sonarr-key", window._sonarrKeySet);
    setKeyField("cfg-radarr-key", window._radarrKeySet);
    setConfigDirty(false);
    checkArrKeys();
  }
  else {
    var d = await res.json().catch(function() { return {}; });
    msg.className = "msg error"; msg.textContent = "✗ " + (d.message || "Failed to save.");
  }
  msg.style.display = "block";
  setTimeout(function() { msg.style.display = "none"; }, 4000);
}

// ── Watch folder list + browser ──────────────────────────────────
window._watchFolders   = [];
window._fsCurrentPath  = null;

function renderWatchFolders() {
  var container = document.getElementById("cfg-watch-folders-list");
  if (!container) return;
  var list = window._watchFolders || [];
  if (list.length === 0) {
    container.innerHTML = '<div class="folder-empty-hint">No folders configured yet.</div>';
    return;
  }
  container.innerHTML = list.map(function(p, idx) {
    return '<div class="folder-row">' +
      '<span class="folder-row-path" title="' + escapeHtml(p) + '">' + escapeHtml(p) + '</span>' +
      '<button type="button" class="folder-row-remove" title="Remove" onclick="removeWatchFolder(' + idx + ')">✕</button>' +
    '</div>';
  }).join("");
}

function removeWatchFolder(idx) {
  window._watchFolders.splice(idx, 1);
  renderWatchFolders();
  setConfigDirty(true);
}

function openFolderBrowser() {
  var existing = document.getElementById("folder-browser");
  if (existing) existing.remove();
  var overlay = document.createElement("div");
  overlay.id = "folder-browser";
  overlay.className = "modal-overlay";
  overlay.innerHTML =
    '<div class="modal folder-browser-modal" role="dialog" aria-modal="true">' +
      '<h2>Add Watch Folder</h2>' +
      '<div id="folder-browser-path" class="folder-browser-path"></div>' +
      '<div id="folder-browser-list" class="folder-browser-list">' +
        '<div class="folder-browser-empty">Loading…</div>' +
      '</div>' +
      '<div class="modal-actions">' +
        '<button class="btn btn-primary" id="folder-browser-use">Use This Folder</button>' +
        '<button class="btn btn-secondary" id="folder-browser-cancel">Cancel</button>' +
      '</div>' +
    '</div>';
  document.body.appendChild(overlay);
  overlay.addEventListener("click", function(e) { if (e.target === overlay) closeFolderBrowser(); });
  document.getElementById("folder-browser-use").onclick = function() {
    var p = window._fsCurrentPath;
    if (!p) return;
    if ((window._watchFolders || []).indexOf(p) < 0) {
      window._watchFolders.push(p);
      renderWatchFolders();
      setConfigDirty(true);
    }
    closeFolderBrowser();
  };
  document.getElementById("folder-browser-cancel").onclick = closeFolderBrowser;
  loadFsPath("");
  trapFocus(overlay);
}

function closeFolderBrowser() {
  var el = document.getElementById("folder-browser");
  if (el) el.remove();
  window._fsCurrentPath = null;
}

async function loadFsPath(path) {
  var url = "/api/fs/list" + (path ? "?path=" + encodeURIComponent(path) : "");
  var res, data;
  try {
    res  = await apiFetch(url);
    data = await res.json();
  } catch(e) {
    return;
  }
  var listEl = document.getElementById("folder-browser-list");
  if (!listEl) return;
  if (!res.ok) {
    listEl.innerHTML = '<div class="folder-browser-empty">' + escapeHtml((data && data.message) || "Error") + '</div>';
    return;
  }
  window._fsCurrentPath = data.path;
  var pathEl = document.getElementById("folder-browser-path");
  if (pathEl) pathEl.textContent = data.path;

  var html = "";
  if (data.parent && data.parent !== data.path) {
    html += '<div class="folder-browser-item up" data-parent="' + escapeHtml(data.parent) + '">⬆ ..</div>';
  }
  if (!data.entries || data.entries.length === 0) {
    html += '<div class="folder-browser-empty">No subdirectories. You can still select this folder.</div>';
  } else {
    html += data.entries.map(function(e) {
      return '<div class="folder-browser-item" data-path="' + escapeHtml(e.path) + '">📁 ' + escapeHtml(e.name) + '</div>';
    }).join("");
  }
  listEl.innerHTML = html;

  listEl.querySelectorAll(".folder-browser-item").forEach(function(item) {
    item.addEventListener("click", function() {
      var p = item.dataset.path || item.dataset.parent;
      if (p) loadFsPath(p);
    });
  });
}

function setKeyField(id, isSet) {
  var el = document.getElementById(id);
  if (!el) return;
  el.value = "";
  el.placeholder = isSet ? "•••••••• configured" : "Not set";
}

function checkArrKeys() {
  var hasKeys = !!(window._sonarrKeySet || window._radarrKeySet) ||
                (document.getElementById("cfg-sonarr-key") || {value:""}).value.trim().length > 0 ||
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
  var res  = await apiFetch("/api/config/test-" + service, {method:"POST"});
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
  var res  = await apiFetch("/api/config/test-webhook", {method:"POST"});
  var data = await res.json();
  result.className   = res.ok ? "test-result success" : "test-result error";
  result.textContent = res.ok ? "✓ Webhook delivered" : "✗ " + (data.message || "Failed");
  btn.textContent = "Test"; btn.disabled = false;
}

async function cleanDatabase() {
  var result = document.getElementById("clean-db-result");
  result.className = "test-result"; result.textContent = "Cleaning…";
  var res  = await apiFetch("/api/db/clean", {method:"POST"});
  var data = await res.json();
  result.className   = res.ok ? "test-result success" : "test-result error";
  if (res.ok) {
    var parts = [];
    if (data.removed > 0) parts.push(data.removed + " stale record" + (data.removed > 1 ? "s" : ""));
    if (data.sheets  > 0) parts.push(data.sheets  + " contact sheet" + (data.sheets  > 1 ? "s" : ""));
    result.textContent = parts.length > 0 ? "✓ Removed " + parts.join(" and ") : "✓ Database is clean";
  } else {
    result.textContent = "✗ Clean failed";
  }
}

// ── Backup / restore ─────────────────────────────────────────────
async function restoreDatabase(file) {
  if (!file) return;
  if (!confirm("This will REPLACE the entire database. Continue?")) return;
  var result = document.getElementById("backup-result");
  result.className = "test-result";
  result.textContent = "Restoring database…";
  var fd = new FormData();
  fd.append("file", file);
  try {
    var res = await apiFetch("/api/backup/restore/db", {method:"POST", body: fd});
    var d   = await res.json();
    if (res.ok) {
      result.className   = "test-result success";
      result.textContent = "✓ Database restored — reload the page to see the new data.";
    } else {
      result.className   = "test-result error";
      result.textContent = "✗ " + (d.message || "Restore failed");
    }
  } catch(e) {
    result.className   = "test-result error";
    result.textContent = "✗ " + e.message;
  }
}

async function restoreVcsArchive(file) {
  if (!file) return;
  if (!confirm("This will REPLACE all VCS thumbnails. Continue?")) return;
  var result = document.getElementById("backup-result");
  result.className = "test-result";
  result.textContent = "Restoring VCS thumbnails (this may take a while)…";
  var fd = new FormData();
  fd.append("file", file);
  try {
    var res = await apiFetch("/api/backup/restore/vcs", {method:"POST", body: fd});
    var d   = await res.json();
    if (res.ok) {
      result.className   = "test-result success";
      result.textContent = "✓ Restored " + (d.extracted || 0) + " thumbnail" + ((d.extracted || 0) === 1 ? "" : "s") + ".";
    } else {
      result.className   = "test-result error";
      result.textContent = "✗ " + (d.message || "Restore failed");
    }
  } catch(e) {
    result.className   = "test-result error";
    result.textContent = "✗ " + e.message;
  }
}

// ── Logs ──────────────────────────────────────────────────────────
async function loadLogs() {
  var lines = document.getElementById("log-lines").value;
  var level = document.getElementById("log-level").value;
  try {
    var res   = await apiFetch("/api/logs?lines=" + lines + "&level=" + level);
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
  } catch (e) {
    var out = document.getElementById("log-output");
    if (out) out.innerHTML = '<div class="log-line log-error">Failed to load logs: ' + escapeHtml(e.message) + '</div>';
  }
}

// ── Modal ─────────────────────────────────────────────────────────
var _modalReturnFocus = null;
function showModal(title, body, onConfirm, confirmVariant) {
  _modalReturnFocus = document.activeElement;
  document.getElementById("modal-title").textContent = title;
  document.getElementById("modal-body").textContent  = body;
  var confirmBtn = document.getElementById("modal-confirm");
  confirmBtn.onclick = onConfirm;
  confirmBtn.className = "btn " + (confirmVariant === "danger" ? "btn-danger" : "btn-primary");
  var overlay = document.getElementById("modal-overlay");
  overlay.style.display = "flex";
  trapFocus(overlay);
  // Focus the safe button by default
  var cancelBtn = overlay.querySelector(".modal-actions .btn-secondary");
  if (cancelBtn) cancelBtn.focus();
}
function closeModal() {
  document.getElementById("modal-overlay").style.display = "none";
  if (_modalReturnFocus && _modalReturnFocus.focus) {
    try { _modalReturnFocus.focus(); } catch(e) {}
  }
  _modalReturnFocus = null;
}

function trapFocus(container) {
  var focusable = container.querySelectorAll('button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])');
  if (focusable.length === 0) return;
  var first = focusable[0];
  var last  = focusable[focusable.length - 1];
  container.addEventListener("keydown", function(e) {
    if (e.key !== "Tab") return;
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault();
      first.focus();
    }
  });
}

// ── Lightbox ──────────────────────────────────────────────────────
var _lightboxTab   = null;
var _lightboxIdx   = null;
var _lightboxTotal = 0;
var _lightboxReturnFocus = null;

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

function buildLightboxContent(lb, src, tab, idx) {
  _lightboxTab   = tab;
  _lightboxIdx   = idx;
  _lightboxTotal = tab != null ? (tabSheets[tab] || []).length : 0;

  var sheet = (tab != null && idx != null) ? (tabSheets[tab] || [])[idx] : null;
  var titleHtml = sheet ? '<div class="lb-title">' + escapeHtml(sheet.stem) + '</div>' : '';

  var confHtml = "";
  if (sheet && sheet.nsfw_confidence) {
    var pct   = Math.round(sheet.nsfw_confidence * 100);
    var cls   = riskClass(sheet.nsfw_confidence);
    var color = pct >= 75 ? "var(--danger)" : pct >= 40 ? "var(--warn)" : "var(--accent2)";
    confHtml  = '<div class="lb-risk"><div class="lb-risk-score ' + cls + '" style="color:' + color + '">' + pct + '%</div><div class="lb-risk-label">confidence</div></div>';
  }

  var labelsHtml = "";
  if (sheet && sheet.flag_reason) {
    var rows = dedupeLabels(sheet.flag_reason).map(function(l) {
      return '<div class="lb-label"><span class="lb-label-name">' + escapeHtml(formatLabel(l.label)) + '</span>' +
             (l.conf > 0 ? '<span class="lb-label-risk">' + l.conf + '%</span>' : '') + '</div>';
    }).join("");
    labelsHtml = '<div class="lb-labels-title">Detected</div><div class="lb-labels">' + rows + '</div>';
  } else if (sheet) {
    labelsHtml = '<div class="lb-no-flags">No flags detected</div>';
  }

  var actionsHtml = "";
  if (tab === "pending" && idx != null) {
    actionsHtml = '<div class="lb-actions">' +
      '<button class="btn btn-success lb-action" data-action="approve">✓ Approve</button>' +
      '<button class="btn btn-danger lb-action" data-action="reject">✗ Reject</button>' +
      '</div>';
  } else if (tab === "quarantined" && idx != null) {
    actionsHtml = '<div class="lb-actions">' +
      '<button class="btn btn-success lb-action" data-action="approve">✓ Restore</button>' +
      '<button class="btn btn-danger lb-action" data-action="reject">✗ Delete</button>' +
      '</div>';
  }

  var hasPrev = idx != null && idx > 0;
  var hasNext = idx != null && idx < _lightboxTotal - 1;
  var counter = idx != null ? '<div class="lb-counter">' + (idx + 1) + ' / ' + _lightboxTotal + '</div>' : '';

  lb.innerHTML =
    '<button class="lightbox-close" aria-label="Close">✕</button>' +
    '<div class="lb-outer" role="dialog" aria-modal="true">' +
      '<div class="lb-img-wrap">' +
        '<img class="lb-img" src="' + src + '" alt="">' +
        counter +
      '</div>' +
      '<div class="lb-panel">' +
        titleHtml +
        confHtml +
        labelsHtml +
        actionsHtml +
        '<div class="lb-hint">← → navigate &nbsp;·&nbsp; Esc close' + (tab === "quarantined" ? ' &nbsp;·&nbsp; R restore &nbsp;·&nbsp; D delete' : (actionsHtml ? ' &nbsp;·&nbsp; A approve &nbsp;·&nbsp; R reject' : '')) + '</div>' +
      '</div>' +
    '</div>';

  lb.querySelectorAll(".lb-action").forEach(function(btn) {
    btn.addEventListener("click", function() { lightboxAction(btn.dataset.action); });
  });
  lb.querySelector(".lightbox-close").addEventListener("click", closeLightbox);
  trapFocus(lb);
}

function openLightbox(src, tab, idx) {
  var lb = document.getElementById("lightbox");
  _lightboxReturnFocus = document.activeElement;
  document.body.style.overflow = "hidden";
  if (!lb) {
    lb = document.createElement("div");
    lb.id        = "lightbox";
    lb.className = "lightbox";
    document.body.appendChild(lb);

    // Lightbox swipe gestures (bound once)
    var startX = null, startY = null, startEl = null;
    lb.addEventListener("touchstart", function(e) {
      if (!e.touches || !e.touches[0]) return;
      startX = e.touches[0].clientX;
      startY = e.touches[0].clientY;
      startEl = e.target;
    }, {passive: true});
    lb.addEventListener("touchend", function(e) {
      if (startX == null || startY == null || !e.changedTouches || !e.changedTouches[0]) return;
      var dx = e.changedTouches[0].clientX - startX;
      var dy = e.changedTouches[0].clientY - startY;
      startX = null; startY = null;
      if (Math.abs(dx) < 60 && Math.abs(dy) < 90) return;

      var panel = lb.querySelector(".lb-panel");
      var fromPanel = startEl && panel && panel.contains(startEl);
      var fromImgWrap = startEl && lb.querySelector(".lb-img-wrap") && lb.querySelector(".lb-img-wrap").contains(startEl);

      if (Math.abs(dx) >= 60 && Math.abs(dx) > Math.abs(dy) * 1.3) {
        lightboxNav(dx < 0 ? 1 : -1);
        return;
      }
      if (dy > 0 && Math.abs(dy) >= 90 && (fromPanel || fromImgWrap)) {
        if (!fromPanel || (panel && panel.scrollTop === 0)) {
          closeLightbox();
        }
      }
    }, {passive: true});
  }
  buildLightboxContent(lb, src, tab, idx);
  lb.style.display = "flex";

  // Preload neighbours
  if (tab != null && idx != null) {
    [-1, 1].forEach(function(d) {
      var nIdx = idx + d;
      if (nIdx >= 0 && nIdx < _lightboxTotal) {
        var s = tabSheets[tab][nIdx];
        if (s && s.has_sheet) {
          var img = new Image();
          img.src = "/api/sheets/image/" + encodeURIComponent(s.filename);
        }
      }
    });
  }
}

function closeLightbox() {
  var lb = document.getElementById("lightbox");
  if (lb) lb.style.display = "none";
  document.body.style.overflow = "";
  _lightboxTab = null;
  _lightboxIdx = null;
  if (_lightboxReturnFocus && _lightboxReturnFocus.focus) {
    try { _lightboxReturnFocus.focus(); } catch(e) {}
  }
  _lightboxReturnFocus = null;
}

function lightboxNav(dir) {
  if (_lightboxTab == null || _lightboxIdx == null) return;
  var newIdx = _lightboxIdx + dir;
  if (newIdx < 0 || newIdx >= _lightboxTotal) return;
  var sheet = tabSheets[_lightboxTab][newIdx];
  if (!sheet || !sheet.has_sheet) return;
  var lb = document.getElementById("lightbox");
  buildLightboxContent(lb, "/api/sheets/image/" + encodeURIComponent(sheet.filename), _lightboxTab, newIdx);
}

async function lightboxAction(action) {
  if (_lightboxTab == null || _lightboxIdx == null) return;
  var idx  = _lightboxIdx;
  var tab  = _lightboxTab;
  var next = idx < _lightboxTotal - 1 ? idx + 1 : (idx > 0 ? idx - 1 : null);

  if (action === "reject") {
    showModal(
      _deleteOnReject ? "Confirm Deletion" : "Confirm Reject",
      _deleteOnReject
        ? "This will permanently delete the source video. This cannot be undone."
        : "This will move the source video to quarantine. Nothing is deleted.",
      async function() {
        closeModal();
        closeLightbox();
        await singleAction(tab, "reject", idx);
        if (next != null && tabSheets[tab] && tabSheets[tab][next] && tabSheets[tab][next].has_sheet) {
          var s = "/api/sheets/image/" + encodeURIComponent(tabSheets[tab][next].filename);
          openLightbox(s, tab, next);
        }
      },
      "danger"
    );
  } else {
    closeLightbox();
    await singleAction(tab, "approve", idx);
    if (next != null && tabSheets[tab] && tabSheets[tab][next] && tabSheets[tab][next].has_sheet) {
      var s2 = "/api/sheets/image/" + encodeURIComponent(tabSheets[tab][next].filename);
      openLightbox(s2, tab, next);
    }
  }
}

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
  var isMobile = window.matchMedia("(max-width: 900px)").matches;
  if (isMobile) {
    var hidden = sidebar.classList.toggle("collapsed");
    if (backdrop) backdrop.classList.toggle("visible", !hidden);
  } else {
    sidebar.classList.toggle("collapsed");
  }
  try { localStorage.setItem("sidebarCollapsed", sidebar.classList.contains("collapsed")); } catch(e) {}
}

// ── Toast stack ───────────────────────────────────────────────────
function toast(msg, isError) {
  var container = document.getElementById("toast-container");
  if (!container) {
    container = document.createElement("div");
    container.id = "toast-container";
    container.setAttribute("role", "status");
    container.setAttribute("aria-live", "polite");
    document.body.appendChild(container);
  }
  var el = document.createElement("div");
  el.className = "toast " + (isError ? "error" : "success");
  el.innerHTML = '<span>' + escapeHtml(msg) + '</span>';
  var close = document.createElement("span");
  close.textContent = "✕";
  close.style.cursor = "pointer";
  close.style.marginLeft = "8px";
  close.setAttribute("aria-label", "Dismiss");
  close.onclick = function() { removeToast(el); };
  el.appendChild(close);
  el.addEventListener("click", function(e) { if (e.target !== close) removeToast(el); });
  container.appendChild(el);
  var delay = isError ? 4000 : 2500;
  setTimeout(function() { removeToast(el); }, delay);
}

function removeToast(el) {
  if (!el.parentNode) return;
  el.style.opacity = "0";
  setTimeout(function() {
    if (el.parentNode) el.parentNode.removeChild(el);
  }, 200);
}

// ── Mobile swipe to open/close sidebar ────────────────────────────
(function() {
  var EDGE_TRIGGER_PX = 24;
  var SWIPE_THRESHOLD = 60;
  var DIRECTION_RATIO = 1.3;

  var startX = null, startY = null, fromEdge = false, fromInsideSidebar = false;

  function onStart(e) {
    if (!window.matchMedia("(max-width: 900px)").matches) return;
    var t = e.touches && e.touches[0];
    if (!t) return;
    startX = t.clientX;
    startY = t.clientY;
    fromEdge = startX <= EDGE_TRIGGER_PX;
    var sidebar = document.querySelector(".sidebar");
    fromInsideSidebar = sidebar && sidebar.contains(e.target) && !sidebar.classList.contains("collapsed");
  }

  function onEnd(e) {
    if (startX === null) return;
    var t = e.changedTouches && e.changedTouches[0];
    if (!t) { startX = null; return; }
    var dx = t.clientX - startX;
    var dy = t.clientY - startY;
    startX = null; startY = null;

    if (Math.abs(dx) < SWIPE_THRESHOLD) return;
    if (Math.abs(dx) < Math.abs(dy) * DIRECTION_RATIO) return;

    var sidebar = document.querySelector(".sidebar");
    if (!sidebar) return;
    var isOpen  = !sidebar.classList.contains("collapsed");

    if (dx > 0 && fromEdge && !isOpen) {
      toggleSidebar();
      return;
    }
    if (dx < 0 && isOpen && (fromInsideSidebar || fromEdge || true)) {
      toggleSidebar();
    }
  }

  document.addEventListener("touchstart", onStart, {passive: true});
  document.addEventListener("touchend",   onEnd,   {passive: true});
})();

// ── Keyboard shortcuts ────────────────────────────────────────────
function initKeyboardShortcuts() {
  document.addEventListener("keydown", function(e) {
    var lightbox = document.getElementById("lightbox");
    if (lightbox && lightbox.style.display !== "none") {
      if (e.key === "Escape")     { closeLightbox(); return; }
      if (e.key === "ArrowLeft")  { lightboxNav(-1); return; }
      if (e.key === "ArrowRight") { lightboxNav(1);  return; }
      if (_lightboxTab === "quarantined") {
        if (e.key === "r" || e.key === "R") { lightboxAction("approve"); return; }
        if (e.key === "d" || e.key === "D") { lightboxAction("reject");  return; }
      } else {
        if (e.key === "a" || e.key === "A") { lightboxAction("approve"); return; }
        if (e.key === "r" || e.key === "R") { lightboxAction("reject");  return; }
      }
      return;
    }

    // Modal Esc-to-cancel
    var modal = document.getElementById("modal-overlay");
    if (modal && modal.style.display !== "none" && e.key === "Escape") {
      closeModal();
      return;
    }

    // Folder browser Esc
    var fb = document.getElementById("folder-browser");
    if (fb && fb.style.display !== "none" && e.key === "Escape") {
      closeFolderBrowser();
      return;
    }

    // Shortcut help Esc
    var help = document.getElementById("shortcut-overlay");
    if (help && e.key === "Escape") {
      help.remove();
      return;
    }

    if (isInputTarget(e.target)) return;

    if (e.key === "/") {
      e.preventDefault();
      var search = document.getElementById("global-search");
      if (search) search.focus();
      return;
    }
    if (e.key === "Escape") {
      clearSearch();
      return;
    }
    if (e.key === "?" && !e.shiftKey) {
      showShortcutHelp();
      return;
    }
    if (e.key >= "1" && e.key <= "4") {
      var idx = parseInt(e.key) - 1;
      navigateTo(TABS[idx]);
      return;
    }
  });
}

function showShortcutHelp() {
  var existing = document.getElementById("shortcut-overlay");
  if (existing) { existing.remove(); return; }
  var overlay = document.createElement("div");
  overlay.id = "shortcut-overlay";
  overlay.className = "shortcut-overlay";
  overlay.innerHTML =
    '<div class="shortcut-modal">' +
      '<h2>Keyboard shortcuts</h2>' +
      '<div class="shortcut-row"><span>Focus search</span><span class="shortcut-key">/</span></div>' +
      '<div class="shortcut-row"><span>Clear search</span><span class="shortcut-key">Esc</span></div>' +
      '<div class="shortcut-row"><span>Switch tabs</span><span class="shortcut-key">1 – 4</span></div>' +
      '<div class="shortcut-row"><span>Lightbox: prev / next</span><span class="shortcut-key">← / →</span></div>' +
      '<div class="shortcut-row"><span>Lightbox: approve / reject</span><span class="shortcut-key">A / R</span></div>' +
      '<div class="shortcut-row"><span>Close lightbox / modal</span><span class="shortcut-key">Esc</span></div>' +
      '<div class="shortcut-row"><span>Show this help</span><span class="shortcut-key">?</span></div>' +
      '<div class="shortcut-row"><span>Hide help</span><span class="shortcut-key">? / Esc</span></div>' +
      '<div class="modal-actions" style="margin-top:16px"><button class="btn btn-secondary" onclick="document.getElementById(\'shortcut-overlay\').remove()">Close</button></div>' +
    '</div>';
  overlay.addEventListener("click", function(e) { if (e.target === overlay) overlay.remove(); });
  document.body.appendChild(overlay);
  trapFocus(overlay);
}

// ── Helpers ───────────────────────────────────────────────────────
function escapeHtml(s) {
  if (!s) return "";
  return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;").replace(/'/g,"&#039;");
}
