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
  if (page === 'review')   loadSheets();
  if (page === 'config')   loadConfig();
  if (page === 'database') { dbPage = 1; loadDb(); loadDbStats(); }
  if (page === 'logs')     loadLogs();
}

// ── Version ───────────────────────────────────────────────────────
async function loadVersion() {
  const res = await fetch('/api/version');
  const d = await res.json();
  document.getElementById('nav-version').textContent = 'v' + d.version;
}

// ── Review ────────────────────────────────────────────────────────
async function loadSheets() {
  const res = await fetch('/api/sheets');
  allSheets = await res.json();
  selectedStems.clear();
  updateBulkBar();
  renderSheets(allSheets);
}

function filterSheets() {
  const q = document.getElementById('review-search').value.toLowerCase();
  const filtered = q ? allSheets.filter(s => s.stem.toLowerCase().includes(q)) : allSheets;
  renderSheets(filtered);
}

function renderSheets(sheets) {
  const grid  = document.getElementById('sheets-grid');
  const empty = document.getElementById('sheets-empty');
  document.getElementById('review-count').textContent = `${allSheets.length} pending`;
  grid.innerHTML = '';

  if (sheets.length === 0) {
    empty.style.display = 'block';
    return;
  }
  empty.style.display = 'none';

  sheets.forEach(sheet => {
    const card = document.createElement('div');
    card.className = 'sheet-card' + (selectedStems.has(sheet.stem) ? ' selected' : '');
    card.id = `card-${sheet.stem}`;

    const srcPath = sheet.source_path || 'Unknown source';
    card.innerHTML = `
      <div class="sheet-select">
        <input type="checkbox" class="sheet-checkbox" data-stem="${sheet.stem}"
               ${selectedStems.has(sheet.stem) ? 'checked' : ''}
               onchange="toggleSelect('${sheet.stem}', this.checked)">
      </div>
      <img class="sheet-img"
           src="/api/sheets/image/${encodeURIComponent(sheet.filename)}"
           alt="${sheet.stem}" onclick="openLightbox(this.src)" loading="lazy">
      <div class="sheet-info">
        <div class="sheet-name" title="${sheet.stem}">${sheet.stem}</div>
        <div class="sheet-path" title="${srcPath}">${srcPath}</div>
        <div class="sheet-actions">
          <button class="btn btn-success btn-sm" onclick="markReviewed('${sheet.stem}')">✓ Reviewed</button>
          <button class="btn btn-danger btn-sm" onclick="confirmDeleteMedia('${sheet.stem}')">🗑 Delete Media</button>
        </div>
      </div>`;
    grid.appendChild(card);
  });
}

function toggleSelect(stem, checked) {
  if (checked) selectedStems.add(stem);
  else         selectedStems.delete(stem);
  const card = document.getElementById(`card-${stem}`);
  if (card) card.classList.toggle('selected', checked);
  updateBulkBar();
}

function toggleSelectAll() {
  const checked = document.getElementById('select-all').checked;
  const visible = document.querySelectorAll('.sheet-checkbox');
  visible.forEach(cb => {
    cb.checked = checked;
    toggleSelect(cb.dataset.stem, checked);
  });
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
    // Show "select all" if not everything is selected
    const selectAllBtn = document.getElementById('bulk-select-all');
    if (selectAllBtn) {
      selectAllBtn.style.display = selectedStems.size < total ? 'inline-flex' : 'none';
    }
  } else {
    bar.style.display = 'none';
  }
}

async function bulkReviewed() {
  const stems = [...selectedStems];
  const res = await fetch('/api/sheets/bulk-reviewed', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({stems}),
  });
  if (res.ok) {
    stems.forEach(stem => removeCard(stem));
    selectedStems.clear();
    updateBulkBar();
    toast(`${stems.length} sheets marked reviewed ✓`);
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
  const res = await fetch('/api/sheets/bulk-delete-media', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({stems}),
  });
  if (res.ok) {
    stems.forEach(stem => removeCard(stem));
    selectedStems.clear();
    updateBulkBar();
    toast(`${stems.length} media file${stems.length > 1 ? 's' : ''} deleted ✓`);
  }
}

async function markReviewed(stem) {
  const res = await fetch('/api/sheets/reviewed', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({stem})});
  if (res.ok) { removeCard(stem); toast('Marked as reviewed ✓'); }
  else toast('Error marking reviewed', true);
}

function confirmDeleteMedia(stem) {
  showModal(
    'Delete Media?',
    `This will permanently delete the source video for "${stem}" and tell Sonarr/Radarr to find an alternative. This cannot be undone.`,
    () => deleteMedia(stem)
  );
}

async function deleteMedia(stem) {
  closeModal();
  const res  = await fetch('/api/sheets/delete-media', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({stem})});
  const data = await res.json();
  if (res.ok) { removeCard(stem); toast('Media deleted ✓'); }
  else toast('Error: ' + (data.message || 'unknown'), true);
}

function removeCard(stem) {
  // Remove from allSheets array
  allSheets = allSheets.filter(s => s.stem !== stem);
  selectedStems.delete(stem);
  const card = document.getElementById(`card-${stem}`);
  if (card) {
    card.style.transition = 'opacity .3s';
    card.style.opacity = '0';
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

// ── Config ────────────────────────────────────────────────────────
async function loadConfig() {
  const res = await fetch('/api/config');
  const cfg = await res.json();
  document.getElementById('cfg-watch-folders').value = (cfg.watch_folders || []).join('\n');
  document.getElementById('cfg-output-dir').value    = cfg.output_dir    || '';
  document.getElementById('cfg-sonarr-url').value    = cfg.sonarr_url    || '';
  document.getElementById('cfg-sonarr-key').value    = cfg.sonarr_api_key || '';
  document.getElementById('cfg-radarr-url').value    = cfg.radarr_url    || '';
  document.getElementById('cfg-radarr-key').value    = cfg.radarr_api_key || '';
  document.getElementById('cfg-poll-interval').value = cfg.poll_interval_seconds || 600;
  document.getElementById('cfg-vcs-grid').value      = cfg.vcs_grid      || '4x4';
  document.getElementById('cfg-vcsi-timeout').value  = cfg.vcsi_timeout_seconds || 300;
}

async function saveConfig() {
  const folders = document.getElementById('cfg-watch-folders').value
    .split('\n').map(s => s.trim()).filter(Boolean);
  const payload = {
    watch_folders:         folders,
    output_dir:            document.getElementById('cfg-output-dir').value.trim(),
    sonarr_url:            document.getElementById('cfg-sonarr-url').value.trim(),
    sonarr_api_key:        document.getElementById('cfg-sonarr-key').value.trim(),
    radarr_url:            document.getElementById('cfg-radarr-url').value.trim(),
    radarr_api_key:        document.getElementById('cfg-radarr-key').value.trim(),
    poll_interval_seconds: parseInt(document.getElementById('cfg-poll-interval').value),
    vcs_grid:              document.getElementById('cfg-vcs-grid').value.trim(),
    vcsi_timeout_seconds:  parseInt(document.getElementById('cfg-vcsi-timeout').value),
  };
  const res = await fetch('/api/config', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload),
  });
  const msg = document.getElementById('config-msg');
  if (res.ok) {
    msg.className = 'msg success';
    msg.textContent = '✓ Configuration saved.';
  } else {
    const d = await res.json();
    msg.className = 'msg error';
    msg.textContent = '✗ ' + (d.message || 'Failed to save.');
  }
  msg.style.display = 'block';
  setTimeout(() => msg.style.display = 'none', 4000);
}

async function testConnection(service) {
  const btn     = document.getElementById(`test-${service}-btn`);
  const result  = document.getElementById(`test-${service}-result`);
  btn.textContent = '…';
  btn.disabled    = true;

  // Save current values first so the test uses what's in the form
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
    tr.innerHTML = `
      <td><span class="${cls}">${row.status.toUpperCase()}</span></td>
      <td>${row.name}</td>
      <td class="path-cell" title="${row.path}">${row.path}</td>
      <td>${ts}</td>`;
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
    if (line.includes('[ERROR]'))   cls = 'log-error';
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
  const lb = document.createElement('div');
  lb.className = 'lightbox';
  lb.innerHTML = `<img src="${src}">`;
  lb.onclick   = () => lb.remove();
  document.body.appendChild(lb);
}

// ── Toast ─────────────────────────────────────────────────────────
function toast(msg, isError = false) {
  const el = document.getElementById('toast');
  el.textContent  = msg;
  el.style.color  = isError ? 'var(--danger)' : 'var(--accent2)';
  el.style.display = 'block';
  setTimeout(() => el.style.display = 'none', 3000);
}

// ── Helpers ───────────────────────────────────────────────────────
function escapeHtml(s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}
