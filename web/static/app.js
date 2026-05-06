/* ── Safe Scanarr v0.6 ────────────────────────────────────────── */

const TABS     = ["pending", "approved", "quarantined", "rejected"];
let currentTab = "pending";
let tabSheets  = {};        // { tabName: [{...}] }
let selections = {};        // { tabName: Set<stem> }
let statsTimer = null;

TABS.forEach(t => {
  tabSheets[t]  = [];
  selections[t] = new Set();
});

document.addEventListener("DOMContentLoaded", () => {
  loadVersion();
  loadStats();
  navigateTo("pending");
  statsTimer = setInterval(loadStats, 60000);

  document.querySelectorAll(".nav-link").forEach(link => {
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
  document.querySelectorAll(".nav-link").forEach(l => l.classList.remove("active"));
  const pageEl = document.getElementById(`page-${page}`);
  const linkEl = document.querySelector(`[data-page="${page}"]`);
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
  const res   = await fetch("/api/stats");
  const stats = await res.json();
  TABS.forEach(t => {
    const badge = document.getElementById(`badge-${t}`);
    const count = document.getElementById(`${t}-count`);
    const n = stats[t] || 0;
    if (badge) badge.textContent = n > 0 ? n : "";
    if (count) count.textContent = `${n} item${n !== 1 ? "s" : ""}`;
  });
}

// ── Load a tab ────────────────────────────────────────────────────
async function loadPage(tab) {
  const search = document.getElementById("global-search")?.value?.toLowerCase() || "";
  const res    = await fetch(`/api/sheets?state=${tab}&search=${encodeURIComponent(search)}`);
  tabSheets[tab] = await res.json();
  selections[tab].clear();
  renderTab(tab);
  updateBulkBar(tab);
}

function renderTab(tab) {
  const grid  = document.getElementById(`${tab}-grid`);
  const empty = document.getElementById(`${tab}-empty`);
  if (!grid) return;
  grid.innerHTML = "";
  window._stems  = window._stems || {};
  window._stems[tab] = {};

  const sheets = tabSheets[tab];
  if (sheets.length === 0) {
    if (empty) empty.style.display = "block";
    return;
  }
  if (empty) empty.style.display = "none";

  sheets.forEach((sheet, idx) => {
    window._stems[tab][idx] = sheet.stem;
    const card = document.createElement("div");
    card.className = "sheet-card" + (sheet.flagged ? " flagged" : "") +
                     (selections[tab].has(sheet.stem) ? " selected" : "");
    card.dataset.stem = sheet.stem;

    const conf    = sheet.nsfw_confidence != null
                  ? `<span class="conf-badge">${Math.round(sheet.nsfw_confidence * 100)}%</span>` : "";
    const checked = selections[tab].has(sheet.stem) ? "checked" : "";
    const imgSrc  = sheet.has_sheet
                  ? `/api/sheets/image/${encodeURIComponent(sheet.filename)}`
                  : null;

    card.innerHTML = `
      <div class="sheet-select">
        <input type="checkbox" class="sheet-checkbox" data-tab="${tab}" data-idx="${idx}" ${checked}
               onchange="toggleSelectByIdx('${tab}', ${idx}, this.checked)">
      </div>
      ${sheet.flagged ? '<div class="flagged-overlay">⚠ NSFW</div>' : ""}
      ${imgSrc
        ? `<img class="sheet-img" src="${imgSrc}" alt="" onclick="openLightbox(this.src)" loading="lazy">`
        : `<div class="sheet-img-placeholder">No sheet</div>`}
      <div class="sheet-info">
        <div class="sheet-name">${conf}${sheet.stem}</div>
        <div class="sheet-path" title="${sheet.source_path || ""}">${sheet.source_path || "Unknown"}</div>
        ${renderActions(tab, idx, sheet)}
      </div>`;
    grid.appendChild(card);
  });
}

function renderActions(tab, idx, sheet) {
  if (tab === "pending") return `
    <div class="sheet-actions">
      <button class="btn btn-success btn-sm" onclick="singleAction('pending','approve',${idx})">✓ Approve</button>
      <button class="btn btn-warn btn-sm" onclick="singleAction('pending','quarantine-single',${idx})">⚠ Quarantine</button>
      <button class="btn btn-danger btn-sm" onclick="confirmSingle('pending','reject',${idx})">✗ Reject</button>
    </div>`;
  if (tab === "approved") return `
    <div class="sheet-actions">
      <button class="btn btn-secondary btn-sm" onclick="singleAction('approved','requeue',${idx})">↻ Re-queue</button>
    </div>`;
  if (tab === "quarantined") return `
    <div class="sheet-actions">
      <button class="btn btn-success btn-sm" onclick="singleAction('quarantined','approve',${idx})">✓ Restore</button>
      <button class="btn btn-danger btn-sm" onclick="confirmSingle('quarantined','reject',${idx})">✗ Delete</button>
    </div>`;
  if (tab === "rejected") return `
    <div class="sheet-info-meta">
      <span class="meta-time">${sheet.state_updated_at ? new Date(sheet.state_updated_at).toLocaleString() : ""}</span>
    </div>`;
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
    action === "reject" ? "Reject & Delete?" : "Confirm",
    `This will permanently delete the source video for "${stem}". This cannot be undone.`,
    async () => { closeModal(); await singleAction(tab, action, idx); }
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
    `${action === "reject" ? "Reject" : "Confirm"} ${n} item${n > 1 ? "s" : ""}?`,
    `This will permanently delete ${n} source video file${n > 1 ? "s" : ""}. This cannot be undone.`,
    async () => { closeModal(); await bulkAction(tab, action); }
  );
}

async function performAction(action, stems) {
  const endpoints = {
    "approve":          "/api/sheets/approve",
    "reject":           "/api/sheets/reject",
    "quarantine-single":"/api/sheets/quarantine",
    "requeue":          "/api/sheets/requeue",
  };

  const url = endpoints[action];
  if (!url) return;

  if (action === "quarantine-single") {
    // Single item only
    const res  = await fetch(url, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({stem: stems[0]}),
    });
    const data = await res.json();
    if (res.ok) toast("Moved to quarantine ⚠");
    else toast("Error: " + (data.message || "unknown"), true);
    return;
  }

  if (action === "requeue") {
    for (const stem of stems) {
      const res  = await fetch(url, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({stem}),
      });
      const data = await res.json();
      if (!res.ok) { toast("Error: " + (data.message || "unknown"), true); return; }
    }
    toast("Re-queued ✓ — check Review shortly");
    return;
  }

  const res  = await fetch(url, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({stems}),
  });
  const data = await res.json();
  if (res.ok) {
    const labels = {"approve": "Approved ✓", "reject": "Rejected ✗"};
    toast((labels[action] || "Done") + ` (${stems.length})`);
  } else {
    toast("Error: " + (data.message || "unknown"), true);
  }
}

async function triggerScan() {
  await fetch("/api/scan", {method: "POST"});
  toast("Scan started — check Logs for progress");
}

// ── Selection ─────────────────────────────────────────────────────
function toggleSelectByIdx(tab, idx, checked) {
  const stem = window._stems[tab][idx];
  if (checked) selections[tab].add(stem);
  else         selections[tab].delete(stem);
  const card = document.querySelector(`#${tab}-grid .sheet-card[data-stem="${CSS.escape(stem)}"]`);
  if (card) card.classList.toggle("selected", checked);
  updateBulkBar(tab);
}

function toggleSelectAll(tab) {
  const checked = document.getElementById(`select-all-${tab}`).checked;
  document.querySelectorAll(`#${tab}-grid .sheet-checkbox`).forEach(cb => {
    cb.checked = checked;
    toggleSelectByIdx(tab, parseInt(cb.dataset.idx), checked);
  });
}

function selectAllVisible(tab) {
  document.querySelectorAll(`#${tab}-grid .sheet-checkbox`).forEach(cb => {
    cb.checked = true;
    toggleSelectByIdx(tab, parseInt(cb.dataset.idx), true);
  });
  const sa = document.getElementById(`select-all-${tab}`);
  if (sa) sa.checked = true;
}

function clearSelection(tab) {
  selections[tab].clear();
  document.querySelectorAll(`#${tab}-grid .sheet-checkbox`).forEach(cb => cb.checked = false);
  const sa = document.getElementById(`select-all-${tab}`);
  if (sa) sa.checked = false;
  document.querySelectorAll(`#${tab}-grid .sheet-card`).forEach(c => c.classList.remove("selected"));
  updateBulkBar(tab);
}

function updateBulkBar(tab) {
  const bar   = document.getElementById(`bulk-actions-${tab}`);
  const count = document.getElementById(`selected-count-${tab}`);
  const saBtn = document.getElementById(`bulk-select-all-${tab}`);
  const total = tabSheets[tab].length;
  const n     = selections[tab].size;
  if (!bar) return;
  if (n > 0) {
    bar.style.display = "flex";
    if (count) count.textContent = `${n} selected`;
    if (saBtn) saBtn.style.display = n < total ? "inline-flex" : "none";
  } else {
    bar.style.display = "none";
  }
}

// ── Global search ─────────────────────────────────────────────────
function onGlobalSearch() {
  if (TABS.includes(currentTab)) loadPage(currentTab);
}

// ── Config ────────────────────────────────────────────────────────
async function loadConfig() {
  const res = await fetch("/api/config");
  const cfg = await res.json();

  document.getElementById("cfg-zone-approve").value        = cfg.zone_auto_approve ?? 0.1;
  document.getElementById("cfg-zone-quarantine").value     = cfg.zone_quarantine   ?? 0.4;
  document.getElementById("cfg-zone-reject").value         = cfg.zone_auto_reject  ?? 0.85;
  document.getElementById("cfg-nudenet-frames").value      = cfg.nudenet_frames    ?? 10;
  document.getElementById("cfg-watch-folders").value       = (cfg.watch_folders || []).join("\n");
  document.getElementById("cfg-scan-schedule-enabled").checked = !!cfg.scan_schedule_enabled;
  document.getElementById("cfg-scan-schedule").value       = cfg.scan_schedule     || "daily";
  document.getElementById("cfg-quarantine-dir").value      = cfg.quarantine_dir    || "";
  document.getElementById("cfg-quarantine-days").value     = cfg.quarantine_auto_reject_days ?? 0;
  document.getElementById("cfg-polling-enabled").checked   = !!cfg.polling_enabled;
  document.getElementById("cfg-poll-interval").value       = cfg.poll_interval_seconds || 600;
  document.getElementById("cfg-sonarr-url").value          = cfg.sonarr_url        || "";
  document.getElementById("cfg-sonarr-key").value          = cfg.sonarr_api_key    || "";
  document.getElementById("cfg-radarr-url").value          = cfg.radarr_url        || "";
  document.getElementById("cfg-radarr-key").value          = cfg.radarr_api_key    || "";
  document.getElementById("cfg-webhook-url").value         = cfg.webhook_url       || "";
  document.getElementById("cfg-webhook-quarantine").checked= !!cfg.webhook_on_quarantine;
  document.getElementById("cfg-webhook-reject").checked    = !!cfg.webhook_on_reject;
  document.getElementById("cfg-vcs-grid").value            = cfg.vcs_grid          || "4x4";
  document.getElementById("cfg-vcs-quality").value         = cfg.vcs_quality       ?? 80;
  document.getElementById("cfg-vcsi-timeout").value        = cfg.vcsi_timeout_seconds || 300;
  checkArrKeys();
}

async function saveConfig() {
  const folders = document.getElementById("cfg-watch-folders").value
    .split("\n").map(s => s.trim()).filter(Boolean);

  const payload = {
    zone_auto_approve:          parseFloat(document.getElementById("cfg-zone-approve").value),
    zone_quarantine:            parseFloat(document.getElementById("cfg-zone-quarantine").value),
    zone_auto_reject:           parseFloat(document.getElementById("cfg-zone-reject").value),
    nudenet_frames:             parseInt(document.getElementById("cfg-nudenet-frames").value),
    watch_folders:              folders,
    scan_schedule_enabled:      document.getElementById("cfg-scan-schedule-enabled").checked,
    scan_schedule:              document.getElementById("cfg-scan-schedule").value,
    quarantine_dir:             document.getElementById("cfg-quarantine-dir").value.trim(),
    quarantine_auto_reject_days:parseInt(document.getElementById("cfg-quarantine-days").value),
    polling_enabled:            document.getElementById("cfg-polling-enabled").checked,
    poll_interval_seconds:      parseInt(document.getElementById("cfg-poll-interval").value),
    sonarr_url:                 document.getElementById("cfg-sonarr-url").value.trim(),
    sonarr_api_key:             document.getElementById("cfg-sonarr-key").value.trim(),
    radarr_url:                 document.getElementById("cfg-radarr-url").value.trim(),
    radarr_api_key:             document.getElementById("cfg-radarr-key").value.trim(),
    webhook_url:                document.getElementById("cfg-webhook-url").value.trim(),
    webhook_on_quarantine:      document.getElementById("cfg-webhook-quarantine").checked,
    webhook_on_reject:          document.getElementById("cfg-webhook-reject").checked,
    vcs_grid:                   document.getElementById("cfg-vcs-grid").value.trim(),
    vcs_quality:                parseInt(document.getElementById("cfg-vcs-quality").value),
    vcsi_timeout_seconds:       parseInt(document.getElementById("cfg-vcsi-timeout").value),
  };

  const res = await fetch("/api/config", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify(payload),
  });
  const msg = document.getElementById("config-msg");
  if (res.ok) {
    msg.className   = "msg success";
    msg.textContent = "✓ Configuration saved.";
  } else {
    const d = await res.json();
    msg.className   = "msg error";
    msg.textContent = "✗ " + (d.message || "Failed to save.");
  }
  msg.style.display = "block";
  setTimeout(() => msg.style.display = "none", 4000);
}

function checkArrKeys() {
  const hasKeys = document.getElementById("cfg-sonarr-key").value.trim().length > 0 ||
                  document.getElementById("cfg-radarr-key").value.trim().length > 0;
  const toggle  = document.getElementById("cfg-polling-enabled");
  const hint    = document.getElementById("polling-no-keys-hint");
  if (!hasKeys) {
    toggle.disabled    = true;
    toggle.checked     = false;
    hint.style.display = "block";
  } else {
    toggle.disabled    = false;
    hint.style.display = "none";
  }
}

async function testConnection(service) {
  const btn    = document.getElementById(`test-${service}-btn`);
  const result = document.getElementById(`test-${service}-result`);
  btn.textContent = "…"; btn.disabled = true;
  await saveConfig();
  const res  = await fetch(`/api/config/test-${service}`, {method: "POST"});
  const data = await res.json();
  result.className   = res.ok ? "test-result success" : "test-result error";
  result.textContent = res.ok
    ? `✓ Connected — ${service} v${data.version}`
    : `✗ ${data.message || "Connection failed"}`;
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
  result.textContent = res.ok ? "✓ Webhook delivered" : `✗ ${data.message || "Failed"}`;
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
    ? (data.removed > 0 ? `✓ Removed ${data.removed} stale record${data.removed > 1 ? "s" : ""}` : "✓ Database is clean")
    : "✗ Clean failed";
}

// ── Logs ──────────────────────────────────────────────────────────
async function loadLogs() {
  const lines  = document.getElementById("log-lines").value;
  const level  = document.getElementById("log-level").value;
  const params = new URLSearchParams({lines, level});
  const res    = await fetch("/api/logs?" + params);
  const data   = await res.json();
  const out    = document.getElementById("log-output");
  out.innerHTML = data.lines.map(line => {
    let cls = "log-info";
    if (line.includes("[ERROR]"))   cls = "log-error";
    else if (line.includes("[WARNING]")) cls = "log-warn";
    else if (line.includes("[DEBUG]"))   cls = "log-debug";
    return `<div class="log-line ${cls}">${escapeHtml(line)}</div>`;
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
function openLightbox(src) {
  const lb     = document.createElement("div");
  lb.className = "lightbox";
  lb.innerHTML = `<img src="${src}">`;
  lb.onclick   = () => lb.remove();
  document.body.appendChild(lb);
}

// ── Toast ─────────────────────────────────────────────────────────
function toast(msg, isError = false) {
  const el        = document.getElementById("toast");
  el.textContent  = msg;
  el.style.color  = isError ? "var(--danger)" : "var(--accent2)";
  el.style.display = "block";
  setTimeout(() => el.style.display = "none", 3000);
}

// ── Helpers ───────────────────────────────────────────────────────
function escapeHtml(s) {
  return s.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
}
