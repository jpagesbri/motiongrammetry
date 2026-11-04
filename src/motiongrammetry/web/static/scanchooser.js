// The scan chooser: the top bar's scan chip (topbar.js) opens it, on every page. It lists the
// scans with a thumbnail, their stages and last run; picking one hands it to the page (Scan
// selects it, Edit switches to it, Help and About remember it). It also holds what used to be the
// Scan page's left bar: New scan… (a name, a video path or an upload), and per scan, in its "⋯"
// menu, Duplicate…, Delete… and whatever the page adds (the Scan page: upload or replace the video).
// A full page to manage scans and workspaces (#24) can be linked from the foot of the list.
// topbar.css styles it.

const STAGES = ['frames', 'mask', 'sfm', 'mvs', 'vggt', 'clean', 'texture'];
const esc = (t) => String(t ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const enc = encodeURIComponent;
const api = async (path, opts = {}) => {
  const r = await fetch(path, opts);
  const body = (r.headers.get('content-type') || '').includes('json') ? await r.json() : null;
  if (!r.ok) throw new Error(body?.error || r.statusText);
  return body;
};
const post = (path, body) => api(path, { method: 'POST', body: JSON.stringify(body) });
export const thumbUrl = (w) => `/api/workspaces/${enc(w.name)}/preview.jpg?v=${w.preview || 'k'}`;
const fmtN = (n) => n >= 1e6 ? (n / 1e6).toFixed(2) + 'M' : n >= 1e3 ? (n / 1e3).toFixed(1) + 'k' : String(n);
const ago = (t) => {
  if (!t) return '';
  const s = Date.now() / 1000 - t;
  return s < 90 ? 'just now' : s < 3600 ? `${Math.round(s / 60)} min ago` : s < 86400 ? `${Math.round(s / 3600)} h ago` : `${Math.round(s / 86400)} d ago`;
};
const videoName = (w) => (w.video ? String(w.video).split('/').pop() : '');
const contents = (w) => [w.frames && `${w.frames} keyframes`, w.faces && `${fmtN(w.faces)} faces`, w.textured && 'textured',
  w.custom && 'edited'].filter(Boolean).join(' · ');
const runLine = (w) => (w.job === 'running' ? 'running now' : w.last_status ? `last run: ${w.last_status}` : 'not run yet')
  + (w.job !== 'running' && w.last_run ? ' · ' + ago(w.last_run) : '');

let hooks = { open() {}, created() {}, deleted() {}, actions: () => [] };
let chip = null, panel = null, card = null, modal = null;
let scans = null, current = null, loading = null;

/** Wire the chip. `h`: open(name), created(name), deleted(name), actions(scan) -> [{label, title, run}]. */
export function initChooser(chipEl, h) {
  hooks = { ...hooks, ...h };
  chip = chipEl;
  panel = Object.assign(document.createElement('div'), { className: 'sc-panel', id: 'scanPanel', hidden: true });
  panel.setAttribute('role', 'dialog'); panel.setAttribute('aria-label', 'choose a scan');
  card = Object.assign(document.createElement('div'), { className: 'sc-card', hidden: true });
  modal = Object.assign(document.createElement('div'), { className: 'sc-modal', id: 'scanModal', hidden: true });
  document.body.append(panel, card, modal);
  chip.addEventListener('click', (e) => { e.stopPropagation(); panel.hidden ? openPanel() : closePanel(); });
  chip.addEventListener('keydown', (e) => { if (e.key === 'ArrowDown' && panel.hidden) { e.preventDefault(); openPanel(); } });
  panel.addEventListener('keydown', panelKeys);
  panel.addEventListener('click', (e) => e.stopPropagation());
  modal.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeModal(); e.stopPropagation(); });  // page shortcuts stay out
  modal.addEventListener('mousedown', (e) => { if (e.target === modal) closeModal(); });
  addEventListener('click', () => { if (!panel.hidden) closePanel(false); });
  panel.addEventListener('focusout', (e) => { if (e.relatedTarget && !panel.contains(e.relatedTarget)) closePanel(false); });
  // Escape closes the list or the window wherever the focus is, and while a window is open the
  // page's shortcuts get no keys.
  addEventListener('keydown', (e) => {
    if (!modal.hidden && !modal.contains(e.target)) { e.stopPropagation(); if (e.key === 'Escape') closeModal(); }
    else if (!panel.hidden && !panel.contains(e.target) && e.key === 'Escape') { e.stopPropagation(); closePanel(); }
  }, true);
  addEventListener('resize', () => { if (!panel.hidden) place(); });
  refresh();
}

/** The page's scan changed (null: none): the chip's name and thumbnail follow. */
export function chooserScan(name) { current = name || null; showChip(); }

/** The page fetched /api/workspaces itself (the Scan page does, on every change): reuse it. */
export function updateScans(list) { scans = list; showChip(); if (!panel.hidden && !panel.querySelector('.sc-menu')) renderPanel(); }

async function refresh() {
  loading ||= api('/api/workspaces').then((l) => { scans = l; showChip(); return l; }).catch(() => scans || []).finally(() => { loading = null; });
  return loading;
}

function showChip() {
  if (!chip) return;
  const w = scans?.find((x) => x.name === current), img = chip.querySelector('.t'), v = chip.querySelector('.v');
  chip.classList.toggle('none', !current);
  v.textContent = current || 'choose…';
  img.hidden = !(w && w.preview != null);
  if (!img.hidden && img.dataset.src !== thumbUrl(w)) img.src = img.dataset.src = thumbUrl(w);
}

// ---------------------------------------------------------------- the list
async function openPanel() {
  panel.hidden = false; chip.setAttribute('aria-expanded', 'true');
  renderPanel(); place();
  focusRow();
  await refresh();  // fresh status (a run may have ended since)
  if (!panel.hidden && !panel.querySelector('.sc-menu')) { const f = document.activeElement?.dataset?.name; renderPanel(); place(); focusRow(f); }
}
function closePanel(refocus = true) {
  if (panel.hidden) return;
  panel.hidden = true; card.hidden = true; chip.setAttribute('aria-expanded', 'false');
  if (refocus && panel.contains(document.activeElement)) chip.focus({ preventScroll: true });
}
function place() {
  const r = chip.getBoundingClientRect();
  panel.style.top = r.bottom + 6 + 'px';
  panel.style.right = Math.max(8, innerWidth - r.right) + 'px';
}
function focusRow(name = current) {
  const b = [...panel.querySelectorAll('.sc-pick')];
  (b.find((x) => x.dataset.name === name) || b[0] || panel.querySelector('.sc-new'))?.focus();
}
function renderPanel() {
  const list = scans || [];
  panel.innerHTML = `<div class="sc-list" role="listbox">${list.length ? list.map((w) => `
    <div class="sc-row${w.name === current ? ' on' : ''}" data-row="${esc(w.name)}">
      <button class="sc-pick" data-name="${esc(w.name)}" role="option" aria-selected="${w.name === current}" title="open ${esc(w.name)}">
        ${w.preview != null ? `<img src="${thumbUrl(w)}" alt="" loading="lazy">` : '<span class="noimg"></span>'}
        <span class="sc-text"><span class="sc-name"><b>${esc(w.name)}</b><span class="dots">${STAGES.map((s) =>
          `<i class="${w.stages?.[s] ? 'on' : ''}" title="${s}${w.stages?.[s] ? ' done' : ''}"></i>`).join('')}</span>${w.job === 'running' ? '<span class="sc-run">running</span>' : ''}</span>
          <small>${esc(contents(w) || (w.video ? videoName(w) : 'no video yet'))}</small>
          <small class="${w.last_status === 'failed' && w.job !== 'running' ? 'bad' : ''}">${esc(runLine(w))}</small></span>
      </button>
      <button class="sc-more" data-more="${esc(w.name)}" aria-haspopup="menu" title="more for ${esc(w.name)}: duplicate, delete…">⋯</button>
    </div>`).join('') : `<div class="sc-empty">${scans ? 'No scans yet.' : 'loading…'}</div>`}</div>
    <div class="sc-foot"><button class="sc-new" title="a new scan from a video: a path on this computer, or an upload">+ New scan…</button></div>`;
  // (#24 will add "Manage scans…" to .sc-foot, for the full list of scans and workspaces.)
  panel.querySelectorAll('.sc-pick').forEach((b) => {
    b.addEventListener('click', () => pick(b.dataset.name));
    b.addEventListener('mouseenter', () => showCard(b));
    b.addEventListener('focus', () => { if (b.matches(':focus-visible')) showCard(b); });  // the keyboard's, not the click that opened the list
    b.addEventListener('mouseleave', () => { card.hidden = true; });
    b.addEventListener('blur', () => { card.hidden = true; });
  });
  panel.querySelectorAll('.sc-more').forEach((b) => b.addEventListener('click', () => toggleMenu(b)));
  panel.querySelector('.sc-new').addEventListener('click', () => { closePanel(false); openNew(); });
}
function pick(name) {
  closePanel(); card.hidden = true;
  if (name !== current) hooks.open(name);
}
function showCard(b) {  // a bigger picture and the details, beside the list
  const w = scans?.find((x) => x.name === b.dataset.name); if (!w || panel.querySelector('.sc-menu')) return;
  card.innerHTML = `${w.preview != null ? `<img src="${thumbUrl(w)}" alt="">` : ''}<b>${esc(w.name)}</b>
    <div class="muted">${esc(contents(w) || 'no results yet')}</div><div class="muted">${esc(runLine(w))}</div>
    ${w.video ? `<div class="muted small">${esc(videoName(w))}</div>` : '<div class="muted small">no video yet</div>'}`;
  const r = b.getBoundingClientRect(), p = panel.getBoundingClientRect();
  card.hidden = false;
  card.style.top = Math.max(8, Math.min(r.top, innerHeight - card.offsetHeight - 8)) + 'px';
  card.style.left = Math.max(8, p.left - card.offsetWidth - 8) + 'px';
}

// The "⋯" menu of one scan: the page's own actions, then Duplicate and Delete.
function toggleMenu(btn) {
  const open = panel.querySelector('.sc-menu');
  if (open) { const same = open.dataset.for === btn.dataset.more; closeMenu(); if (same) return; }
  const w = scans?.find((x) => x.name === btn.dataset.more); if (!w) return;
  card.hidden = true;
  const items = [...(hooks.actions(w) || []),
    { label: 'Duplicate…', title: w.stages?.frames ? 'a copy with the same keyframes and masks, to try other settings or VGGT without touching this one'
      : 'make the keyframes first: a copy starts from them', disabled: !w.stages?.frames, run: () => openDuplicate(w) },
    { sep: true },
    { label: 'Delete…', danger: true, title: "remove this scan's folder and everything made from it", run: () => openDelete(w) }];
  const m = Object.assign(document.createElement('div'), { className: 'sc-menu' });
  m.setAttribute('role', 'menu'); m.dataset.for = w.name;
  m.innerHTML = items.map((it, i) => it.sep ? '<hr>' : `<button role="menuitem" data-i="${i}" class="${it.danger ? 'danger' : ''}"
    ${it.disabled ? 'disabled' : ''} title="${esc(it.title || '')}">${esc(it.label)}</button>`).join('');
  m.querySelectorAll('[data-i]').forEach((b) => b.addEventListener('click', () => {
    const it = items[+b.dataset.i]; closeMenu(); closePanel(false); it.run();
  }));
  btn.closest('.sc-row').append(m);
  btn.setAttribute('aria-expanded', 'true');
  m.querySelector('button:not([disabled])')?.focus();
}
function closeMenu(refocus = false) {
  const m = panel.querySelector('.sc-menu'); if (!m) return;
  const btn = panel.querySelector(`.sc-more[data-more="${CSS.escape(m.dataset.for)}"]`);
  m.remove(); btn?.setAttribute('aria-expanded', 'false');
  if (refocus) btn?.focus({ preventScroll: true });
}
function panelKeys(e) {
  e.stopPropagation();  // the page's own shortcuts (the editor's tools, the frame viewer's arrows) stay out
  const menu = panel.querySelector('.sc-menu');
  if (e.key === 'Escape') { e.preventDefault(); if (menu) closeMenu(true); else closePanel(); return; }
  if (!['ArrowDown', 'ArrowUp', 'Home', 'End', 'ArrowRight', 'ArrowLeft'].includes(e.key)) return;
  e.preventDefault();
  const a = document.activeElement;
  if (e.key === 'ArrowRight' && a.classList.contains('sc-pick')) { a.parentElement.querySelector('.sc-more').click(); return; }
  if (e.key === 'ArrowLeft') { if (menu) closeMenu(true); else if (a.classList.contains('sc-more')) a.parentElement.querySelector('.sc-pick').focus(); return; }
  const all = menu ? [...menu.querySelectorAll('button:not([disabled])')] : [...panel.querySelectorAll('.sc-pick, .sc-new')];
  let i = all.indexOf(a.classList.contains('sc-more') ? a.parentElement.querySelector('.sc-pick') : a);
  i = e.key === 'Home' ? 0 : e.key === 'End' ? all.length - 1 : (i + (e.key === 'ArrowDown' ? 1 : -1) + all.length) % all.length;
  all[i]?.focus();
}

// ---------------------------------------------------------------- the windows
function openModal(title, sub, body) {
  modal.innerHTML = `<div class="sc-win" role="dialog" aria-modal="true" aria-label="${esc(title)}">
    <div class="sc-head"><b>${esc(title)}</b><span class="muted">${sub ? '· ' + esc(sub) : ''}</span><button class="sc-x" title="close (Esc)">✕</button></div>
    <div class="sc-body">${body}</div></div>`;
  modal.hidden = false;
  modal.querySelector('.sc-x').addEventListener('click', closeModal);
  modal.querySelectorAll('[data-cancel]').forEach((b) => b.addEventListener('click', closeModal));
  return (sel) => modal.querySelector(sel);
}
function closeModal() {
  if (modal.hidden || modal.dataset.busy) return;
  modal.hidden = true; modal.innerHTML = '';
  chip.focus({ preventScroll: true });
}
const busy = (on) => { if (on) modal.dataset.busy = '1'; else delete modal.dataset.busy; };

/** A video file into a scan, with progress (0-1). `replace`: the scan already has one, and that is fine. */
export function uploadVideo(name, file, { replace = false, progress } = {}) {
  return new Promise((ok, fail) => {
    const xhr = new XMLHttpRequest();
    xhr.open('PUT', `/api/workspaces/${enc(name)}/video?name=${enc(file.name)}${replace ? '&replace=1' : ''}`);
    xhr.upload.onprogress = (e) => { if (e.lengthComputable) progress?.(e.loaded / e.total); };
    xhr.onload = () => (xhr.status < 300 ? ok() : fail(new Error((() => { try { return JSON.parse(xhr.responseText).error; } catch { return xhr.statusText; } })())));
    xhr.onerror = () => fail(new Error('upload failed'));
    xhr.send(file);
  });
}

// New scan: the old left bar's form. Create makes it (from a local path, or empty); Upload video…
// makes it under the typed name, or, with no name, under the file's (with a free suffix if taken).
export function openNew() {
  const $ = openModal('New scan', '', `
    <label class="sc-field">Name <input type="text" id="scNewName" placeholder="mouse-01" autocomplete="off" spellcheck="false"></label>
    <label class="sc-field">Video on this computer <input type="text" id="scNewPath" placeholder="local video path (optional)" spellcheck="false"></label>
    <div class="sc-btns"><button class="sc-btn primary" id="scCreate">Create</button>
      <label class="sc-btn" title="upload a video file into the new scan: named as typed, or after the file">Upload video…<input type="file" id="scUpload" accept="video/*" hidden></label>
      <button class="sc-btn" data-cancel>Cancel</button></div>
    <progress id="scProg" value="0" max="1" hidden></progress>
    <div class="sc-help">Create with a path uses that video where it is; Upload video… copies a file into the scan. Without a name, an upload names the scan after its file.</div>
    <div class="sc-err" id="scErr"></div>`);
  const err = (e) => { $('#scErr').textContent = e.message; busy(false); };
  $('#scNewName').focus();
  $('#scNewName').addEventListener('keydown', (e) => { if (e.key === 'Enter') $('#scCreate').click(); });
  $('#scNewPath').addEventListener('keydown', (e) => { if (e.key === 'Enter') $('#scCreate').click(); });
  $('#scCreate').addEventListener('click', async () => {
    $('#scErr').textContent = '';
    const body = { name: $('#scNewName').value.trim(), video_path: $('#scNewPath').value.trim() || null };
    try { busy(true); await post('/api/workspaces', body); done(body.name); } catch (e) { err(e); }
  });
  $('#scUpload').addEventListener('change', async () => {
    const file = $('#scUpload').files[0]; if (!file) return;
    $('#scErr').textContent = '';
    const typed = $('#scNewName').value.trim();
    try {
      busy(true);
      let name = typed;
      if (typed) {
        await post('/api/workspaces', { name }).catch((e) => {
          throw /already exists/.test(e.message)
            ? new Error(`a scan named "${typed}" already exists: choose another name, or use "Replace the video…" in that scan's ⋯ menu on the Scan page`) : e;
        });
      } else {
        const base = file.name.replace(/\.[^.]+$/, '').replace(/[^A-Za-z0-9._-]/g, '-').slice(0, 60) || 'scan';
        const taken = new Set((await api('/api/workspaces')).map((w) => w.name));
        name = base;
        for (let i = 2; taken.has(name); i++) name = `${base}-${i}`;
        await post('/api/workspaces', { name });
      }
      const prog = $('#scProg'); prog.hidden = false;
      await uploadVideo(name, file, { progress: (f) => { prog.value = f; } });
      done(name);
    } catch (e) { err(e); $('#scProg').hidden = true; } finally { $('#scUpload').value = ''; }
  });
  function done(name) { busy(false); closeModal(); refresh(); hooks.created(name); }
}

function openDuplicate(w) {
  const $ = openModal('Duplicate scan', w.name, `
    <p>A new scan with the same keyframes and masks, to try other settings or VGGT without touching <b>${esc(w.name)}</b>.</p>
    <label class="sc-field">Name of the copy <input type="text" id="scDupName" value="${esc(w.name)}-copy" autocomplete="off" spellcheck="false"></label>
    <label class="sc-check" title="SfM/MVS copies are instant on btrfs/XFS, a full copy elsewhere"><input type="checkbox" id="scDupRecon"> include SfM / MVS results</label>
    <div class="sc-btns"><button class="sc-btn primary" id="scDupGo">Duplicate</button><button class="sc-btn" data-cancel>Cancel</button></div>
    <div class="sc-err" id="scErr"></div>`);
  $('#scDupName').focus(); $('#scDupName').select();
  $('#scDupName').addEventListener('keydown', (e) => { if (e.key === 'Enter') $('#scDupGo').click(); });
  $('#scDupGo').addEventListener('click', async () => {
    const name = $('#scDupName').value.trim(), go = $('#scDupGo');
    $('#scErr').textContent = ''; go.disabled = true; go.textContent = 'Copying…'; busy(true);
    try {
      await post(`/api/workspaces/${enc(w.name)}/duplicate`, { name, reconstruction: $('#scDupRecon').checked });
      busy(false); closeModal(); refresh(); hooks.created(name);
    } catch (e) { busy(false); $('#scErr').textContent = e.message; go.disabled = false; go.textContent = 'Duplicate'; }
  });
}

// Delete a scan: its folder and everything in it, after typing its name.
function openDelete(w) {
  const name = w.name;
  const $ = openModal('Delete scan', name, `
    <p>This removes the scan's folder and everything in it: keyframes, masks, cameras, raw and clean meshes, the textured
    model, your edits and orientation, exports, and the video if it was uploaded. <b>It cannot be undone.</b></p>
    <p class="muted" id="scDelInfo">measuring…</p>
    <label class="sc-field inline">Type <code>${esc(name)}</code> to confirm <input type="text" id="scDelConfirm" autocomplete="off" spellcheck="false"></label>
    <div class="sc-btns"><button class="sc-btn danger-solid" id="scDelGo" disabled>Delete for good</button><button class="sc-btn" data-cancel>Cancel</button></div>
    <div class="sc-err" id="scErr"></div>`);
  const go = $('#scDelGo'), box = $('#scDelConfirm');
  box.focus();
  box.addEventListener('input', () => { go.disabled = box.value !== name; });
  box.addEventListener('keydown', (e) => { if (e.key === 'Enter' && !go.disabled) go.click(); });
  const sz = (b) => b >= 1e9 ? (b / 1e9).toFixed(1) + ' GB' : b >= 1e6 ? (b / 1e6).toFixed(0) + ' MB' : b >= 1e3 ? (b / 1e3).toFixed(0) + ' kB' : b + ' bytes';
  api(`/api/workspaces/${enc(name)}/size`).then((z) => {
    const info = $('#scDelInfo'); if (!info) return;
    info.innerHTML = `<b>${esc(z.path)}</b><br>${z.files.toLocaleString()} file${z.files === 1 ? '' : 's'}, ${sz(z.bytes)}`
      + ` · frees ${z.frees_exact ? '' : 'about '}<b>${sz(z.frees)}</b> of disk space`
      + (z.frees_exact && z.shared > 1e6 ? `<br>${sz(z.shared)} of its data is shared with copies of it (Duplicate or a backup); that stays on disk while they exist.` : '')
      + (z.video_outside ? `<br>The video it was made from stays where it is: ${esc(z.video_outside)}` : '')
      + (w.job === 'running' ? '<br>A run is using it: stop the run first.' : '');
  }).catch(() => { const info = $('#scDelInfo'); if (info) info.textContent = ''; });
  go.addEventListener('click', async () => {
    go.disabled = true; go.textContent = 'Deleting…'; $('#scErr').textContent = ''; busy(true);
    try {
      await post(`/api/workspaces/${enc(name)}/delete`, { confirm: name });
      for (const k of ['mg.run.', 'mg.assist.', 'mg.mode.', 'mg.export.']) try { localStorage.removeItem(k + name); } catch {}
      busy(false); closeModal();
      await refresh();
      hooks.deleted(name, scans || []);
    } catch (e) { busy(false); $('#scErr').textContent = e.message; go.textContent = 'Delete for good'; go.disabled = box.value !== name; }
  });
}
