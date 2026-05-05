/* ── Safe Scanarr UI ──────────────────────────────────────────── */

let currentPage = 'review';
let dbPage = 1;
let allSheets = [];
let selectedStems = new Set();

document.addEventListener('DOMContentLoaded', () => {
  loadVersion();
  navigateTo('review');
  document.querySelectorAll('.nav-link').forEach(link => {
    link.addEventListener('click', e => {
      e.preventDefault();
      navigateTo(link.dataset.page);
    });
  });
});

function navigateTo(page) {
  currentPage = page;
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav-link').forEach(l => l.classList.remove('active'));
  document.getElementById(`page-${page}`).classList.add('active');
  document.querySelector(`[data-page="${page}"]`).classList.add('active');
  if (page === 'review')       loadSheets();
  if (page === 'auto-handled') loadAutoHandled();
  if (page === 'config')       loadConfig();
  if (page === 'database')     { dbPage = 1; loadDb(); loadDbStats(); }
  if (page === 'logs')         loadLogs();
}

// ── Version ───────────────────────────────────────────────────────
async function loadVersion() {
  const res = await fetch('/api/version');
  const d   = await res.json();
  document.getElementById('nav-version').textContent = 'v' + d.version;
}

// ── Review ────────────────────────────────────────────────────────
async function loadSheets() {
  const res = await fetch('/api/sheets');
  allSheets = await res.json();
  selectedStems.clear();
  window._sheetStems = {};
  updateBulkBar();
  renderSheets(allSheets);
}

function filterSheets() {
  const q        = document.getElementById('review-search').value.toLowerCase();
  const filtered = q ? allSheets.filter(s => s.stem.toLowerCase().includes(q)) : allSheets;
  renderSheets(filtered);
}

function renderSheets(sheets) {
  const grid  = document.getElementById('sheets-grid');
  const empty = document.getElementById('sheets-empty');
  document.getElementById('review-count').textContent = `${allSheets.length} pending`;
  grid.innerHTML  = '';
  window._sheetStems = {};

  if (sheets.length === 0) {
    empty.style.display = 'block';
    return;
  }
  empty.style.display = 'none';

  sheets.forEach((sheet, idx) => {
    window._sheetStems[idx] = sheet.stem;

    const card     = document.createElement('div');
    card.className = 'sheet-card' + (selectedStems.has(sheet.stem) ? ' selected' : '');
    card.dataset.stem = sheet.stem;

    const srcPath  = sheet.source_path || 'Unknown source';
    const checked  = selectedStems.has(sheet.stem) ? 'checked' : '';
    const flagBadge = sheet.flagged
      ? `<span class="flag-badge" title="${sheet.flag_reason || 'NSFW detected'}">⚠ NSFW</span>`
      : '';
    if (sheet.flagged) card.classList.add('flagged');

    card.innerHTML = `
      <div class="sheet-select">
        <input type="checkbox" class="sheet-checkbox" data-idx="${idx}" ${checked}
               onchange="toggleSelectByIdx(${idx}, this.checked)">
      </div>
      ${sheet.flagged ? '<div class="flagged-overlay">⚠ NSFW DETECTED</div>' : ''}
      <img class="sheet-img"
           src="/api/sheets/image/${encodeURIComponent(sheet.filename)}"
           alt="" onclick="openLightbox(this.src)" loading="lazy">
      <div class="sheet-info">
        <div class="sheet-name" title="${sheet.stem}">${flagBadge}${sheet.stem}</div>
        <div class="sheet-path" title="${srcPath}">${srcPath}</div>
        <div class="sheet-actions">
          <button class="btn btn-success btn-sm" onclick="markReviewedByIdx(${idx})">✓ Reviewed</button>
          <button class="btn btn-danger btn-sm" onclick="confirmDeleteByIdx(${idx})">🗑 Delete Media</button>
        </div>
      </div>`;
    grid.appendChild(card);
  });
}

// ── Selection ─────────────────────────────────────────────────────
function toggleSelectByIdx(idx, checked) {
  toggleSelect(window._sheetStems[idx], checked);
}

function toggleSelect(stem, checked) {
  if (checked) selectedStems.add(stem);
  else         selectedStems.delete(stem);
  const card = document.querySelector(`.sheet-card[data-stem="${CSS.escape(stem)}"]`);
  if (card) card.classList.toggle('selected', checked);
  updateBulkBar();
}

function toggleSelectAll() {
  const checked = document.getElementById('select-all').checked;
  document.querySelectorAll('.sheet-checkbox').forEach(cb => {
    cb.checked = checked;
    toggleSelectByIdx(parseInt(cb.dataset.idx), checked);
  });
}

function selectAllVisible() {
  document.querySelectorAll('.sheet-checkbox').forEach(cb => {
    cb.checked = true;
    toggleSelectByIdx(parseInt(cb.dataset.idx), true);
  });
  document.getElementById('select-all').checked = true;
}

function clearSelection() {
  selectedStems.clear();
  document.querySelectorAll('.sheet-checkbox').forEach(cb => cb.checked = false);
  document.getElementById('select-all').checked = false;
  document.querySelectorAll('.sheet-card').forEach(c => c.classList.remove('selected'));
  updateBulkBar();
}

function updateBulkBar() {
  const bar   = document.getElementById('bulk-actions');
  const count = document.getElementById('selected-count');
  const total = document.querySelectorAll('.sheet-card').length;
  if (selectedStems.size > 0) {
    bar.style.display = 'flex';
    count.textContent = `${selectedStems.size} selected`;
    const selectAllBtn = document.getElementById('bulk-select-all');
    if (selectAllBtn) selectAllBtn.style.display = selectedStems.size < total ? 'inline-flex' : 'none';
  } else {
    bar.style.display = 'none';
  }
}

// ── Bulk actions ──────────────────────────────────────────────────
async function bulkReviewed() {
  const stems = [...selectedStems];
  const res   = await fetch('/api/sheets/bulk-reviewed', {
    method:  'POST',
    headers: {'Content-Type': 'application/json'},
    body:    JSON.stringify({stems}),
  });
  if (res.ok) {
    const data = await res.json();
    data.reviewed.forEach(stem => removeCardByStem(stem));
    selectedStems.clear();
    updateBulkBar();
    toast(`${data.reviewed.length} sheets marked reviewed ✓`);
  } else {
    toast('Error marking reviewed', true);
  }
}

function confirmBulkDelete() {
  const n = selectedStems.size;
  showModal(
    `Delete ${n} Media File${n > 1 ? 's' : ''}?`,
    `This will permanently delete ${n} source video file${n > 1 ? 's' : ''} and tell Sonarr/Radarr to find alternatives. This cannot be undone.`,
    bulkDeleteMedia
  );
}

async function bulkDeleteMedia() {
  closeModal();
  const stems = [...selectedStems];
  const res   = await fetch('/api/sheets/bulk-delete-media', {
    method:  'POST',
    headers: {'Content-Type': 'application/json'},
    body:    JSON.stringify({stems}),
  });
  if (res.ok) {
    stems.forEach(stem => removeCardByStem(stem));
    selectedStems.clear();
    updateBulkBar();
    toast(`${stems.length} media file${stems.length > 1 ? 's' : ''} deleted ✓`);
  }
}

// ── Single sheet actions ──────────────────────────────────────────
async function markReviewed(stem) {
  const res = await fetch('/api/sheets/reviewed', {
    method:  'POST',
    headers: {'Content-Type': 'application/json'},
    body:    JSON.stringify({stem}),
  });
  if (res.ok) { removeCardByStem(stem); toast('Marked as reviewed ✓'); }
  else toast('Error marking reviewed', true);
}

function markReviewedByIdx(idx) { markReviewed(window._sheetStems[idx]); }

function confirmDeleteByIdx(idx) {
  const stem = window._sheetStems[idx];
  showModal(
    'Delete Media?',
    `This will permanently delete the source video for "${stem}" and tell Sonarr/Radarr to find an alternative. This cannot be undone.`,
    () => deleteMedia(stem)
  );
}

async function deleteMedia(stem) {
  closeModal();
  const res  = await fetch('/api/sheets/delete-media', {
    method:  'POST',
    headers: {'Content-Type': 'application/json'},
    body:    JSON.stringify({stem}),
  });
  const data = await res.json();
  if (res.ok) { removeCardByStem(stem); toast('Media deleted ✓'); }
  else toast('Error: ' + (data.message || 'unknown'), true);
}

function removeCardByStem(stem) {
  allSheets = allSheets.filter(s => s.stem !== stem);
  selectedStems.delete(stem);
  const card = document.querySelector(`.sheet-card[data-stem="${CSS.escape(stem)}"]`);
  if (card) {
    card.style.transition = 'opacity .3s';
    card.style.opacity    = '0';
    setTimeout(() => {
      card.remove();
      document.getElementById('review-count').textContent = `${allSheets.length} pending`;
      if (document.querySelectorAll('.sheet-card').length === 0)
        document.getElementById('sheets-empty').style.display = 'block';
    }, 300);
  }
  updateBulkBar();
}

async function triggerScan() {
  await fetch('/api/scan', {method: 'POST'});
  toast('Scan started — check Logs for progress');
}

// ── Auto-handled ──────────────────────────────────────────────────
async function loadAutoHandled() {
  const res   = await fetch('/api/auto-handled');
  const items = await res.json();
  const grid  = document.getElementById('auto-handled-grid');
  const empty = document.getElementById('auto-handled-empty');
  grid.innerHTML = '';

  if (items.length === 0) {
    empty.style.display = 'block';
    return;
  }
  empty.style.display = 'none';

  items.forEach(item => {
    const card = document.createElement('div');
    card.className = 'sheet-card auto-handled-card';
    const ts = new Date(item.handled_at).toLocaleString();
    const confidence = item.confidence ? `${Math.round(item.confidence * 100)}%` : 'N/A';
    const imgSrc = item.has_sheet
      ? `/api/auto-handled/image/${encodeURIComponent(item.sheet_name)}`
      : '';

    card.innerHTML = `
      ${imgSrc ? `<img class="sheet-img" src="${imgSrc}" alt="" onclick="openLightbox(this.src)" loading="lazy">` : '<div class="sheet-img-placeholder">No sheet</div>'}
      <div class="sheet-info">
        <div class="sheet-name">${item.name}</div>
        <div class="sheet-path" title="${item.path}">${item.path}</div>
        <div class="auto-meta">
          <span class="badge-danger">Flagged</span>
          <span class="auto-reason">${item.reason || 'NSFW detected'}</span>
          <span class="auto-confidence">Confidence: ${confidence}</span>
          <span class="auto-time">${ts}</span>
        </div>
        <div class="sheet-actions">
          <button class="btn btn-secondary btn-sm" onclick="dismissAutoHandled(${item.id})">✕ Dismiss</button>
        </div>
      </div>`;
    grid.appendChild(card);
  });
}

async function dismissAutoHandled(id) {
  const res = await fetch(`/api/auto-handled/${id}/dismiss`, {method: 'POST'});
  if (res.ok) { loadAutoHandled(); toast('Dismissed ✓'); }
  else toast('Error dismissing', true);
}

// ── Config ────────────────────────────────────────────────────────
async function loadConfig() {
  const res = await fetch('/api/config');
  const cfg = await res.json();

  // Scan mode
  const modeVal = cfg.scan_mode || 'review';
  document.querySelector(`input[name="scan-mode"][value="${modeVal}"]`).checked = true;

  document.getElementById('cfg-watch-folders').value  = (cfg.watch_folders || []).join('\n');
  document.getElementById('cfg-sonarr-url').value     = cfg.sonarr_url    || '';
  document.getElementById('cfg-sonarr-key').value     = cfg.sonarr_api_key || '';
  document.getElementById('cfg-radarr-url').value     = cfg.radarr_url    || '';
  document.getElementById('cfg-radarr-key').value     = cfg.radarr_api_key || '';
  document.getElementById('cfg-polling-enabled').checked   = !!cfg.polling_enabled;
  document.getElementById('cfg-scan-schedule-enabled').checked = !!cfg.scan_schedule_enabled;
  document.getElementById('cfg-scan-schedule').value            = cfg.scan_schedule || 'daily';
  document.getElementById('cfg-nudenet-threshold').value  = cfg.nudenet_threshold ?? 0.6;
  document.getElementById('cfg-poll-interval').value  = cfg.poll_interval_seconds || 600;
  document.getElementById('cfg-vcs-grid').value       = cfg.vcs_grid      || '4x4';
  document.getElementById('cfg-vcsi-timeout').value   = cfg.vcsi_timeout_seconds || 300;
}

async function saveConfig() {
  const folders  = document.getElementById('cfg-watch-folders').value
    .split('\n').map(s => s.trim()).filter(Boolean);
  const scanMode = document.querySelector('input[name="scan-mode"]:checked')?.value || 'review';

  const payload = {
    scan_mode:             scanMode,
    watch_folders:         folders,
    sonarr_url:            document.getElementById('cfg-sonarr-url').value.trim(),
    sonarr_api_key:        document.getElementById('cfg-sonarr-key').value.trim(),
    radarr_url:            document.getElementById('cfg-radarr-url').value.trim(),
    radarr_api_key:        document.getElementById('cfg-radarr-key').value.trim(),
    polling_enabled:       document.getElementById('cfg-polling-enabled').checked,
    nudenet_threshold:     parseFloat(document.getElementById('cfg-nudenet-threshold').value),
    poll_interval_seconds: parseInt(document.getElementById('cfg-poll-interval').value),
    scan_schedule_enabled: document.getElementById('cfg-scan-schedule-enabled').checked,
    scan_schedule:         document.getElementById('cfg-scan-schedule').value,
    vcs_grid:              document.getElementById('cfg-vcs-grid').value.trim(),
    vcsi_timeout_seconds:  parseInt(document.getElementById('cfg-vcsi-timeout').value),
  };

  const res = await fetch('/api/config', {
    method:  'POST',
    headers: {'Content-Type': 'application/json'},
    body:    JSON.stringify(payload),
  });

  const msg = document.getElementById('config-msg');
  if (res.ok) {
    msg.className   = 'msg success';
    msg.textContent = '✓ Configuration saved.';
  } else {
    const d = await res.json();
    msg.className   = 'msg error';
    msg.textContent = '✗ ' + (d.message || 'Failed to save.');
  }
  msg.style.display = 'block';
  setTimeout(() => msg.style.display = 'none', 4000);
}

async function testConnection(service) {
  const btn    = document.getElementById(`test-${service}-btn`);
  const result = document.getElementById(`test-${service}-result`);
  btn.textContent = '…';
  btn.disabled    = true;
  await saveConfig();
  const res  = await fetch(`/api/config/test-${service}`, {method: 'POST'});
  const data = await res.json();
  if (res.ok) {
    result.className   = 'test-result success';
    result.textContent = `✓ Connected — ${service} v${data.version}`;
  } else {
    result.className   = 'test-result error';
    result.textContent = `✗ ${data.message || 'Connection failed'}`;
  }
  btn.textContent = 'Test';
  btn.disabled    = false;
}

async function cleanDatabase() {
  const result = document.getElementById('clean-db-result');
  result.className   = 'test-result';
  result.textContent = 'Cleaning…';
  const res  = await fetch('/api/db/clean', {method: 'POST'});
  const data = await res.json();
  if (res.ok) {
    result.className   = 'test-result success';
    result.textContent = data.removed > 0
      ? `✓ Removed ${data.removed} stale record${data.removed > 1 ? 's' : ''}`
      : '✓ Database is clean — nothing to remove';
  } else {
    result.className   = 'test-result error';
    result.textContent = '✗ Clean failed';
  }
}

// ── Database ──────────────────────────────────────────────────────
async function loadDbStats() {
  const res = await fetch('/api/db/stats');
  const s   = await res.json();
  document.getElementById('db-stats').innerHTML = `
    <span>Total: ${s.total}</span>
    <span style="color:var(--accent2)">OK: ${s.ok}</span>
    <span style="color:var(--danger)">Errors: ${s.errors}</span>`;
}

async function loadDb() {
  const search   = document.getElementById('db-search').value;
  const status   = document.getElementById('db-status-filter').value;
  const params   = new URLSearchParams({page: dbPage, per_page: 50, search, status});
  const res      = await fetch('/api/db/files?' + params);
  const data     = await res.json();
  const tbody    = document.getElementById('db-tbody');
  tbody.innerHTML = '';

  data.rows.forEach(row => {
    const tr  = document.createElement('tr');
    const cls = row.status === 'ok' ? 'status-ok' : 'status-error';
    const ts  = new Date(row.updated_at).toLocaleString();
    const flagCell = row.flagged ? `<span class="flag-badge">⚠ NSFW</span>` : '';

    // Use data attribute for path to avoid any escaping issues
    const btn = document.createElement('button');
    btn.className   = 'btn btn-secondary btn-sm';
    btn.textContent = '↻ Re-queue';
    btn.dataset.path = row.path;
    btn.addEventListener('click', () => requeueFile(btn.dataset.path));

    tr.innerHTML = `
      <td><span class="${cls}">${row.status.toUpperCase()}</span>${flagCell}</td>
      <td>${row.name}</td>
      <td class="path-cell" title="${row.path}">${row.path}</td>
      <td>${ts}</td>
      <td></td>`;
    tr.querySelector('td:last-child').appendChild(btn);
    tbody.appendChild(tr);
  });

  const totalPages = Math.ceil(data.total / data.per_page);
  const pg = document.getElementById('db-pagination');
  pg.innerHTML = '';
  if (totalPages > 1) {
    if (dbPage > 1) {
      const b = document.createElement('button');
      b.className = 'btn btn-secondary btn-sm';
      b.textContent = '← Prev';
      b.onclick = () => { dbPage--; loadDb(); };
      pg.appendChild(b);
    }
    const info = document.createElement('span');
    info.textContent = `Page ${dbPage} of ${totalPages} (${data.total} total)`;
    pg.appendChild(info);
    if (dbPage < totalPages) {
      const b = document.createElement('button');
      b.className = 'btn btn-secondary btn-sm';
      b.textContent = 'Next →';
      b.onclick = () => { dbPage++; loadDb(); };
      pg.appendChild(b);
    }
  }
}

async function requeueFile(path) {
  const res  = await fetch('/api/db/requeue', {
    method:  'POST',
    headers: {'Content-Type': 'application/json'},
    body:    JSON.stringify({path}),
  });
  const data = await res.json();
  if (res.ok) toast('Re-queued ✓ — check Review shortly');
  else toast('Error: ' + data.message, true);
}

// ── Logs ──────────────────────────────────────────────────────────
async function loadLogs() {
  const lines  = document.getElementById('log-lines').value;
  const level  = document.getElementById('log-level').value;
  const params = new URLSearchParams({lines, level});
  const res    = await fetch('/api/logs?' + params);
  const data   = await res.json();
  const out    = document.getElementById('log-output');
  out.innerHTML = data.lines.map(line => {
    let cls = 'log-info';
    if (line.includes('[ERROR]'))        cls = 'log-error';
    else if (line.includes('[WARNING]')) cls = 'log-warn';
    else if (line.includes('[DEBUG]'))   cls = 'log-debug';
    return `<div class="log-line ${cls}">${escapeHtml(line)}</div>`;
  }).join('');
  out.scrollTop = out.scrollHeight;
}

// ── Modal ─────────────────────────────────────────────────────────
function showModal(title, body, onConfirm) {
  document.getElementById('modal-title').textContent   = title;
  document.getElementById('modal-body').textContent    = body;
  document.getElementById('modal-confirm').onclick     = onConfirm;
  document.getElementById('modal-overlay').style.display = 'flex';
}
function closeModal() {
  document.getElementById('modal-overlay').style.display = 'none';
}

// ── Lightbox ──────────────────────────────────────────────────────
function openLightbox(src) {
  const lb     = document.createElement('div');
  lb.className = 'lightbox';
  lb.innerHTML = `<img src="${src}">`;
  lb.onclick   = () => lb.remove();
  document.body.appendChild(lb);
}

// ── Toast ─────────────────────────────────────────────────────────
function toast(msg, isError = false) {
  const el        = document.getElementById('toast');
  el.textContent  = msg;
  el.style.color  = isError ? 'var(--danger)' : 'var(--accent2)';
  el.style.display = 'block';
  setTimeout(() => el.style.display = 'none', 3000);
}

// ── Helpers ───────────────────────────────────────────────────────
function escapeHtml(s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}
