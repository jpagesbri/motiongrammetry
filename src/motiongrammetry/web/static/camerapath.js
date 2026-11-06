// The camera path of a scan, large and interactive, for SfM's camera-path warnings (sfm's camera_path
// check). The notes only offer it ("View path"); it opens in the page, over it, like the Export window:
// ✕, Escape or a click outside closes it, and the page gets its keys back.
//
// - left, the cameras seen from above (the scene's up axis), in keyframe order: the path in green,
//   where the views don't connect a red dashed jump, photos placed far away in orange, pulled in to
//   the edge with an arrow so the rest stays readable; for a collapse, the photos that share a spot
//   as one stack with its count. Wheel zooms, drag pans, "Reset view" puts it back.
// - below, the keyframes 0…N as a strip: a red broken-link marker at each break, the far photos as an
//   orange piece lifted off it; for a collapse, the keyframes on the previous one's spot in orange.
// - hover a camera (or the strip, or use ← →): its keyframe, thumbnail and what is wrong with it, and
//   the same keyframe marked on the map and the strip. Hover a break: both its keyframes marked, and
//   "Keyframes 62 → 63 don't connect". Click a camera: that keyframe in the Frames viewer.
//
//   import { openPathView, flagIcon, pathBreaks } from '/camerapath.js';
//   openPathView({ path, frames, cams, toScene, thumb, onPick });
//
// path: the manifest's sfm.camera_path; frames: the keyframe names in order; cams: the scene's cameras
// ({name, center}); toScene: raw SfM point -> [x, y, z] (Y up); thumb: name -> image URL; onPick(k).

const C = { ok: 'var(--ok)', bad: 'var(--bad)', warn: 'var(--warn)', muted: 'var(--muted)', text: 'var(--text)', bg: 'var(--panel2)', line: 'var(--line)', accent: 'var(--accent)' };
const r1 = (v) => Math.round(v * 10) / 10;
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

// Small icons for the problems listed in a note's Details, drawn in currentColor (1em).
const ICONS = {
  break: '<path d="M7.2 4.8l1.3-1.3a2.6 2.6 0 0 1 3.7 3.7l-1.3 1.3M8.8 11.2l-1.3 1.3a2.6 2.6 0 0 1-3.7-3.7l1.3-1.3M2.5 2.5l1.6 1.6M13.5 13.5l-1.6-1.6M2 6h1.8M10 14v-1.8"/>',
  far: '<circle cx="4" cy="11" r="1.6"/><circle cx="7" cy="9" r="1.6"/><path d="M8.5 7.5l5-5M9.5 2.5h4v4"/>',
  stack: '<rect x="2" y="7" width="9" height="6" rx="1"/><path d="M4 5h9v6M6 3h9v6"/>',
  mask: '<circle cx="8" cy="6" r="3" stroke-dasharray="2 1.5"/><path d="M3 14c.8-3 2.6-4.5 5-4.5s4.2 1.5 5 4.5" stroke-dasharray="2 1.5"/>',
};
export function flagIcon(name) {
  return `<svg class="cp-icon cp-icon-${name}" width="1em" height="1em" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICONS[name] || ''}</svg>`;
}

// Jumps that follow each other (0→1, 1→2, 2→3) are one break (at keyframes 0–3), as advice.camera_path_breaks.
export function pathBreaks(path) {
  const out = [];
  [...(path.jumps || [])].sort((a, b) => a.from - b.from).forEach((j) => {
    const last = out[out.length - 1];
    if (last && j.from === last.to) last.to = j.to; else out.push({ from: j.from, to: j.to });
  });
  out.forEach((b) => { b.label = b.to - 1 > b.from ? `${b.from}–${b.to - 1}` : `${b.from}`; });
  return out;
}

// The scan's data, once: each placed keyframe with its place seen from above and what is wrong with it.
function model({ path, frames, cams, toScene }) {
  const at = new Map(frames.map((n, i) => [n, i]));
  const pts = cams.map((c, j) => { const [x, , z] = toScene(c.center); return { k: at.has(c.name) ? at.get(c.name) : j, name: c.name, x, z }; })
    .sort((a, b) => a.k - b.k);
  const n = Math.max(frames.length, path.cameras || 0, pts.length ? pts[pts.length - 1].k + 1 : 0, 2);
  const far = new Set((path.far || []).map((f) => f.keyframe));
  const jumps = (path.jumps || []).map((j) => ({ from: j.from, to: j.to }));
  const collapsed = !!path.collapsed;
  let x0 = Infinity, x1 = -Infinity, z0 = Infinity, z1 = -Infinity;
  const core = pts.filter((p) => !far.has(p.k)), use = core.length >= 2 ? core : pts;
  use.forEach((p) => { x0 = Math.min(x0, p.x); x1 = Math.max(x1, p.x); z0 = Math.min(z0, p.z); z1 = Math.max(z1, p.z); });
  const span = Math.max(x1 - x0, z1 - z0) || 1;
  const groups = [];  // photos on the same spot (a collapse): one stack
  if (collapsed) {
    const tol = span * 1e-3;
    pts.forEach((p) => {
      const g = groups.find((q) => Math.hypot(q.x - p.x, q.z - p.z) <= tol);
      if (g) g.ks.push(p.k); else groups.push({ x: p.x, z: p.z, ks: [p.k] });
      p.group = g || groups[groups.length - 1];
    });
  }
  pts.forEach((p, i) => {
    const j = jumps.find((q) => q.from === p.k || q.to === p.k);
    p.status = far.has(p.k) ? 'placed far away' : j ? `views don't connect here: ${j.from} → ${j.to}`
      : collapsed && p.group.ks.length > 1 ? `on top of ${p.group.ks.length - 1} other${p.group.ks.length > 2 ? 's' : ''}` : 'OK';
    p.bad = far.has(p.k) ? 'warn' : j ? 'bad' : collapsed && p.group.ks.length > 1 ? 'warn' : 'ok';
    p.i = i;
  });
  // the still steps of a collapse: a keyframe on the previous one's spot
  const still = collapsed ? pts.filter((p, i) => i && Math.hypot(p.x - pts[i - 1].x, p.z - pts[i - 1].z) <= span * 1e-3).map((p) => p.k) : [];
  return { pts, n, far, jumps, breaks: pathBreaks(path), collapsed, groups, still, box: { x0, x1, z0, z1, span } };
}

const STYLE = `
.cpv-modal { position: fixed; inset: 0; z-index: 66; background: rgba(5,7,10,.6); display: grid; place-items: center;
  font: 13px/1.4 system-ui, -apple-system, "Segoe UI", sans-serif; color: var(--text); }
.cpv-modal[hidden] { display: none !important; }
.cpv-win { width: 94vw; height: 90vh; background: var(--panel); border: 1px solid var(--line); border-radius: 12px;
  box-shadow: 0 20px 60px rgba(0,0,0,.6); display: flex; flex-direction: column; overflow: hidden; }
.cpv-head { display: flex; align-items: center; gap: 14px; padding: 10px 14px; border-bottom: 1px solid var(--line); flex-wrap: wrap; }
.cpv-head b { font-size: 15px; }
.cpv-legend { display: flex; gap: 14px; color: var(--muted); font-size: 12px; flex-wrap: wrap; }
.cpv-legend i { display: inline-block; width: 16px; height: 0; border-top: 3px solid; vertical-align: middle; margin-right: 5px; }
.cpv-head .cpv-say { margin-left: auto; color: var(--text); font-weight: 600; min-height: 1.4em; }
.cpv-btn { background: var(--panel2); border: 1px solid var(--line); border-radius: 6px; padding: 4px 10px; cursor: pointer; color: var(--text); font: inherit; }
.cpv-btn:hover { border-color: var(--accent); }
.cpv-x { background: none; border: 0; color: var(--muted); cursor: pointer; font-size: 18px; }
.cpv-x:hover { color: var(--text); }
.cpv-body { flex: 1; min-height: 0; display: grid; grid-template-rows: minmax(0, 1fr) 96px; position: relative; }
.cpv-map, .cpv-strip { width: 100%; height: 100%; display: block; outline: none; }
.cpv-map { cursor: grab; } .cpv-map.drag { cursor: grabbing; }
.cpv-map:focus-visible { box-shadow: inset 0 0 0 2px var(--accent); }
.cpv-strip { border-top: 1px solid var(--line); cursor: crosshair; }
.cpv-tip { position: absolute; pointer-events: none; background: var(--panel2); border: 1px solid var(--line); border-radius: 8px;
  padding: 6px; box-shadow: 0 8px 24px rgba(0,0,0,.5); font-size: 12px; max-width: 220px; z-index: 2; }
.cpv-tip img { display: block; width: 200px; height: 112px; object-fit: cover; border-radius: 5px; background: var(--bg); margin-bottom: 4px; }
.cpv-tip .st.bad { color: var(--bad); } .cpv-tip .st.warn { color: var(--warn); } .cpv-tip .st.ok { color: var(--ok); }
.cpv-tip .cpv-hint { color: var(--muted); font-size: 11px; }
.cpv-hit { cursor: pointer; }
`;

let modal = null, opener = null, S = null;

function ensureModal() {
  if (modal) return;
  document.head.append(Object.assign(document.createElement('style'), { textContent: STYLE }));
  modal = Object.assign(document.createElement('div'), { className: 'cpv-modal', id: 'pathView', hidden: true });
  modal.setAttribute('role', 'dialog'); modal.setAttribute('aria-modal', 'true'); modal.setAttribute('aria-label', 'Camera path');
  modal.innerHTML = `<div class="cpv-win"><div class="cpv-head"><b>Camera path</b><span class="cpv-legend" id="cpvLegend"></span>
      <span class="cpv-say" id="cpvSay" aria-live="polite"></span>
      <button class="cpv-btn" id="cpvReset" title="fit the whole path again">Reset view</button>
      <button class="cpv-x" id="cpvClose" title="close (Esc)" aria-label="close">✕</button></div>
    <div class="cpv-body" id="cpvBody"><svg class="cpv-map" id="cpvMap" tabindex="0" aria-label="cameras seen from above; arrow keys step through the keyframes"></svg>
      <svg class="cpv-strip" id="cpvStrip"></svg><div class="cpv-tip" id="cpvTip" hidden></div></div></div>`;
  document.body.append(modal);
  modal.addEventListener('mousedown', (e) => { if (e.target === modal) closePathView(); });
  modal.querySelector('#cpvClose').addEventListener('click', closePathView);
  modal.querySelector('#cpvReset').addEventListener('click', () => { S.view = { s: 1, tx: 0, ty: 0 }; draw(); });
  // While it is open the page's shortcuts get no keys: ← → step through the keyframes, Escape closes.
  addEventListener('keydown', (e) => {
    if (!modal || modal.hidden) return;
    e.stopImmediatePropagation();
    if (e.key === 'Escape') { e.preventDefault(); closePathView(); }
    else if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') { e.preventDefault(); step(e.key === 'ArrowRight' ? 1 : -1); }
    else if (e.key === 'Home' || e.key === 'End') { e.preventDefault(); focusCam(e.key === 'Home' ? 0 : S.m.pts.length - 1); }
  }, true);
  const map = modal.querySelector('#cpvMap'), strip = modal.querySelector('#cpvStrip');
  map.addEventListener('wheel', (e) => {
    e.preventDefault();
    const r = map.getBoundingClientRect(), mx = e.clientX - r.left, my = e.clientY - r.top, v = S.view;
    const f = Math.exp(-e.deltaY * 0.0015), s = Math.max(0.5, Math.min(40, v.s * f)), k = s / v.s;
    v.tx = mx - (mx - v.tx) * k; v.ty = my - (my - v.ty) * k; v.s = s; draw();
  }, { passive: false });
  let drag = null;
  map.addEventListener('mousedown', (e) => { drag = { x: e.clientX, y: e.clientY, tx: S.view.tx, ty: S.view.ty, moved: false }; map.classList.add('drag'); });
  addEventListener('mousemove', (e) => {
    if (!drag) return;
    const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
    if (Math.abs(dx) + Math.abs(dy) > 3) drag.moved = true;
    if (drag.moved) { S.view.tx = drag.tx + dx; S.view.ty = drag.ty + dy; draw(); }
  });
  addEventListener('mouseup', () => { if (drag) { map.classList.remove('drag'); setTimeout(() => { drag = null; }, 0); } });
  map.addEventListener('mouseover', (e) => {
    const t = e.target.closest('[data-k], [data-jump]'); if (!t) return;
    if (t.dataset.k != null) hoverCam(+t.dataset.k, e); else hoverJump(+t.dataset.jump, e);
  });
  map.addEventListener('mouseout', (e) => { if (e.target.closest('[data-k], [data-jump]')) clearHover(); });
  map.addEventListener('click', (e) => {
    const t = e.target.closest('[data-k]');
    if (t && !(drag && drag.moved) && S.onPick) { const k = S.m.pts[+t.dataset.k].k; closePathView(); S.onPick(k); }
  });
  strip.addEventListener('mousemove', (e) => {
    const t = e.target.closest('[data-break]');
    if (t) { hoverBreak(+t.dataset.break, e); return; }
    const r = strip.getBoundingClientRect(), k = Math.round(((e.clientX - r.left - S.sx0) / (S.sx1 - S.sx0)) * (S.m.n - 1));
    const p = nearest(k); if (p) hoverCam(p.i, e, true);
  });
  strip.addEventListener('mouseleave', clearHover);
  new ResizeObserver(() => { if (!modal.hidden) draw(); }).observe(modal.querySelector('#cpvBody'));
}

function nearest(k) {
  let best = null;
  S.m.pts.forEach((p) => { if (!best || Math.abs(p.k - k) < Math.abs(best.k - k)) best = p; });
  return best;
}

// map coordinates: fit the cameras (far ones excluded) in the map, then the user's zoom and pan
function project(p) {
  const { box } = S.m, w = S.mw, h = S.mh, pad = 40;
  const s0 = Math.min((w - 2 * pad) / box.span, (h - 2 * pad) / box.span);
  let x = w / 2 + (p.x - (box.x0 + box.x1) / 2) * s0, y = h / 2 + (p.z - (box.z0 + box.z1) / 2) * s0, out = false;
  if (S.m.far.has(p.k)) {  // far photos: pulled in to the edge of the fitted path, an arrow says they go on
    const hx = (w - 2 * pad) / 2 + 18, hy = (h - 2 * pad) / 2 + 18, f = Math.max(Math.abs(x - w / 2) / hx, Math.abs(y - h / 2) / hy);
    if (f > 1) { x = w / 2 + (x - w / 2) / f; y = h / 2 + (y - h / 2) / f; out = true; }
  }
  const v = S.view;
  return { x: x * v.s + v.tx, y: y * v.s + v.ty, out };
}

function draw() {
  const map = modal.querySelector('#cpvMap'), strip = modal.querySelector('#cpvStrip');
  const mr = map.getBoundingClientRect(), sr = strip.getBoundingClientRect();
  S.mw = mr.width; S.mh = mr.height; S.sw = sr.width; S.sx0 = 40; S.sx1 = sr.width - 40;
  const { pts, m } = { pts: S.m.pts, m: S.m };
  S.P = pts.map(project);
  let g = `<defs><marker id="cpvArrow" viewBox="0 0 6 6" refX="5" refY="3" markerWidth="6" markerHeight="6" orient="auto"><path d="M0 0L6 3L0 6z" fill="${C.warn}"/></marker></defs>`;
  // the path, keyframe after keyframe; the jumps red and dashed, each one hoverable
  for (let i = 1; i < pts.length; i++) {
    const a = S.P[i - 1], b = S.P[i];
    const ji = m.jumps.findIndex((j) => j.from >= pts[i - 1].k && j.to <= pts[i].k);
    if (ji >= 0) {
      g += `<g class="cpv-hit" data-jump="${ji}"><line x1="${r1(a.x)}" y1="${r1(a.y)}" x2="${r1(b.x)}" y2="${r1(b.y)}" stroke="transparent" stroke-width="12"/>
        <line class="cp-jump" x1="${r1(a.x)}" y1="${r1(a.y)}" x2="${r1(b.x)}" y2="${r1(b.y)}" stroke="${C.bad}" stroke-width="2.4" stroke-dasharray="7 5"/></g>`;
    } else if (!m.collapsed) {
      g += `<line x1="${r1(a.x)}" y1="${r1(a.y)}" x2="${r1(b.x)}" y2="${r1(b.y)}" stroke="${C.ok}" stroke-width="1.8" stroke-linecap="round" opacity=".85"/>`;
    }
  }
  if (m.collapsed) {  // stacks: one per spot, with its count
    m.groups.forEach((gr) => {
      const p = S.P[pts.findIndex((q) => q.group === gr)], big = gr.ks.length > 1;
      for (let l = Math.min(4, gr.ks.length) - 1; l >= 0; l--) {
        g += `<g transform="translate(${r1(p.x + l * 2.5)} ${r1(p.y - l * 2.5)})"><rect x="-7" y="-5" width="11" height="10" rx="1.5" fill="${l ? C.muted : big ? C.warn : C.ok}"/><path d="M4 -3l4.5 -2.5v11l-4.5 -2.5z" fill="${l ? C.muted : big ? C.warn : C.ok}"/></g>`;
      }
      if (big) g += `<text class="cp-count" x="${r1(p.x + 16)}" y="${r1(p.y + 4)}" font-size="12" fill="${C.text}">×${gr.ks.length}</text>`;
    });
  }
  pts.forEach((p, i) => {
    const q = S.P[i], col = p.bad === 'bad' ? C.bad : p.bad === 'warn' ? C.warn : C.ok;
    if (m.far.has(p.k) && q.out) {
      const dx = q.x - S.mw / 2, dy = q.y - S.mh / 2, l = Math.hypot(dx, dy) || 1;
      g += `<path d="M${r1(q.x - dx / l * 16)} ${r1(q.y - dy / l * 16)}L${r1(q.x - dx / l * 6)} ${r1(q.y - dy / l * 6)}" stroke="${C.warn}" stroke-width="1.8" marker-end="url(#cpvArrow)"/>`;
    }
    const r = m.collapsed ? 7 : p.bad === 'ok' ? 3.2 : 5;
    g += `<circle class="cpv-cam cpv-hit${m.far.has(p.k) ? ' cp-far-cam' : ''}" data-k="${i}" cx="${r1(q.x)}" cy="${r1(q.y)}" r="${r}" fill="${m.collapsed ? 'transparent' : col}"/>`;
  });
  if (pts.length) {
    const p0 = S.P[0], left = p0.x > S.mw - 60;
    g += `<circle cx="${r1(p0.x)}" cy="${r1(p0.y)}" r="7" fill="none" stroke="${C.text}" stroke-width="1.4"/><text x="${r1(p0.x + (left ? -10 : 10))}" y="${r1(p0.y - 8)}" font-size="12" text-anchor="${left ? 'end' : 'start'}" fill="${C.muted}">start</text>`;
  }
  g += `<g id="cpvMark"></g>`;
  if (!pts.length) g += `<text x="20" y="30" fill="${C.muted}">no camera positions loaded</text>`;
  map.innerHTML = g;
  strip.innerHTML = stripSvg();
  modal.querySelector('#cpvLegend').innerHTML = (m.collapsed
    ? [[C.ok, 'camera moved'], [C.warn, 'same spot as another']]
    : [[C.ok, 'path'], [C.bad, "views don't connect"], ...(m.far.size ? [[C.warn, 'placed far away']] : [])])
    .map(([c, t]) => `<span><i style="border-color:${c}"></i>${t}</span>`).join('') + '<span>wheel: zoom · drag: pan · ← →: keyframes · click: open it</span>';
  if (S.hl != null) mark(S.hl);
}

function sx(k) { return S.sx0 + (S.m.n > 1 ? k / (S.m.n - 1) : 0) * (S.sx1 - S.sx0); }

function stripSvg() {
  const m = S.m, y = 48;
  let out = `<rect x="${S.sx0 - 4}" y="${y - 6}" width="${S.sx1 - S.sx0 + 8}" height="12" rx="6" fill="${C.ok}" opacity=".85"/>`;
  if (m.collapsed) {
    const w = Math.max(2, (S.sx1 - S.sx0) / m.n);
    m.still.forEach((k) => { out += `<rect class="cp-still" x="${r1(sx(k) - w / 2)}" y="${y - 9}" width="${r1(w)}" height="18" fill="${C.warn}"/>`; });
  }
  const far = [...m.far].sort((a, b) => a - b), runs = [];
  far.forEach((k) => { const r = runs[runs.length - 1]; if (r && k === r[1] + 1) r[1] = k; else runs.push([k, k]); });
  runs.forEach(([a, b]) => {
    const xa = sx(a) - 4, xb = sx(b) + 4;
    out += `<g class="cp-far"><path d="M${r1(xa)} ${y - 7}V${y - 26}M${r1(xb)} ${y - 7}V${y - 26}" stroke="${C.warn}" stroke-dasharray="3 2"/>
      <rect x="${r1(xa)}" y="${y - 34}" width="${r1(Math.max(8, xb - xa))}" height="9" rx="4" fill="${C.warn}"/>
      <text x="${r1(xb + 6)}" y="${y - 26}" font-size="12" fill="${C.warn}">far away</text></g>`;
  });
  m.breaks.forEach((b, i) => {
    const xm = (sx(b.from) + sx(b.to)) / 2;
    out += `<g class="cp-break cpv-hit" data-break="${i}" transform="translate(${r1(xm)} ${y})"><rect x="${r1(sx(b.from) - xm)}" y="-7" width="${r1(Math.max(5, sx(b.to) - sx(b.from)))}" height="14" fill="${C.bg}"/>
      <circle r="11" fill="${C.bad}"/><g transform="translate(-7 -7) scale(.875)" fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round">${ICONS.break}</g>
      <text y="28" font-size="12" text-anchor="middle" fill="${C.text}">${b.label}</text></g>`;
  });
  out += `<text x="${S.sx0}" y="${y + 40}" font-size="12" fill="${C.muted}">0</text><text x="${S.sx1}" y="${y + 40}" font-size="12" text-anchor="end" fill="${C.muted}">${m.n - 1}</text>
    <text x="${(S.sx0 + S.sx1) / 2}" y="${y + 40}" font-size="12" text-anchor="middle" fill="${C.muted}">keyframes</text><g id="cpvSmark"></g>`;
  return out;
}

// mark keyframes on the map and the strip (a ring and a line)
function mark(list) {
  S.hl = list;
  const ring = (i) => { const q = S.P[i]; return q ? `<circle cx="${r1(q.x)}" cy="${r1(q.y)}" r="11" fill="none" stroke="${C.accent}" stroke-width="2.5" class="cpv-ring"/>` : ''; };
  modal.querySelector('#cpvMark').innerHTML = list.map(ring).join('');
  modal.querySelector('#cpvSmark').innerHTML = list.map((i) => `<line class="cpv-sring" x1="${r1(sx(S.m.pts[i].k))}" x2="${r1(sx(S.m.pts[i].k))}" y1="14" y2="70" stroke="${C.accent}" stroke-width="2.5"/>`).join('');
}

function tip(html, x, y) {
  const t = modal.querySelector('#cpvTip'), body = modal.querySelector('#cpvBody').getBoundingClientRect();
  t.innerHTML = html; t.hidden = false;
  const w = t.offsetWidth, h = t.offsetHeight;
  t.style.left = `${Math.max(4, Math.min(body.width - w - 4, x + 16))}px`;
  t.style.top = `${Math.max(4, Math.min(body.height - h - 4, y + 16))}px`;
}

function camTip(i, x, y) {
  const p = S.m.pts[i];
  const same = S.m.collapsed && p.group.ks.length > 1 ? `<div class="cpv-hint">keyframes here: ${p.group.ks.slice(0, 8).join(', ')}${p.group.ks.length > 8 ? '…' : ''}</div>` : '';
  tip(`<img src="${esc(S.thumb(p.name))}" alt=""><b>Keyframe ${p.k}</b><div class="st ${p.bad}">${esc(p.status)}</div>${same}<div class="cpv-hint">click: open in Frames</div>`, x, y);
  modal.querySelector('#cpvSay').textContent = `Keyframe ${p.k}: ${p.status}`;
}

function at(e) { const b = modal.querySelector('#cpvBody').getBoundingClientRect(); return [e.clientX - b.left, e.clientY - b.top]; }

function hoverCam(i, e) {
  S.cur = i; mark([i]); const [x, y] = at(e); camTip(i, x, y);
}
function pairOf(from, to) {
  const a = S.m.pts.findIndex((p) => p.k === from), b = S.m.pts.findIndex((p) => p.k === to);
  return [a, b].filter((v) => v >= 0);
}
function sayPair(from, to, e) {
  mark(pairOf(from, to));
  const text = `Keyframes ${from} → ${to} don't connect`;
  modal.querySelector('#cpvSay').textContent = text;
  const [x, y] = at(e); tip(`<b>${text}</b><div class="cpv-hint">the path jumps between them</div>`, x, y);
}
function hoverJump(ji, e) { const j = S.m.jumps[ji]; sayPair(j.from, j.to, e); }
function hoverBreak(bi, e) { const b = S.m.breaks[bi]; sayPair(b.from, b.to, e); }
function clearHover() {
  if (S.kbd) return;
  modal.querySelector('#cpvTip').hidden = true; modal.querySelector('#cpvSay').textContent = '';
  mark([]);
}
function focusCam(i) {
  if (!S.m.pts.length) return;
  i = Math.max(0, Math.min(S.m.pts.length - 1, i)); S.cur = i; S.kbd = true; mark([i]);
  const q = S.P[i], mr = modal.querySelector('#cpvMap').getBoundingClientRect(), b = modal.querySelector('#cpvBody').getBoundingClientRect();
  camTip(i, q.x + mr.left - b.left, q.y + mr.top - b.top);
}
function step(d) { focusCam(S.cur == null ? 0 : S.cur + d); }

export const pathViewOpen = () => !!modal && !modal.hidden;

export function openPathView({ path, frames = [], cams = [], toScene = (c) => c, thumb = () => '', onPick = null }) {
  ensureModal();
  S = { m: model({ path, frames, cams, toScene }), view: { s: 1, tx: 0, ty: 0 }, thumb, onPick, hl: null, cur: null, kbd: false };
  opener = document.activeElement;
  modal.hidden = false;
  modal.querySelector('#cpvTip').hidden = true; modal.querySelector('#cpvSay').textContent = '';
  draw();
  modal.querySelector('#cpvMap').focus({ preventScroll: true });
  modal.querySelector('#cpvMap').addEventListener('mousemove', () => { S.kbd = false; }, { once: true });
}

export function closePathView() {
  if (!modal || modal.hidden) return;
  modal.hidden = true;
  opener?.focus?.({ preventScroll: true }); opener = null;
  if (modal.contains(document.activeElement)) document.activeElement.blur();
}
