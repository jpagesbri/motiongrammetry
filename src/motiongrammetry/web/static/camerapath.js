// The assisted run's picture of a camera-path warning (sfm's camera_path check, advice.camera_path_plain),
// drawn from the scan's own data as inline SVG in the page's colours (CSS variables, so it follows the
// page's theme):
//
// - on the left, the camera centres seen from above (the scene's up axis), in keyframe order: the path
//   in green, where the views don't connect a red dashed jump, photos placed far away in orange,
//   pulled in to the edge with an arrow so the rest stays readable; for a collapse, the photos that
//   share a spot are drawn as one stack of cameras with their count
// - on the right, the keyframes 0…N as a strip: a red broken-link marker at each break, with its
//   keyframe, the far photos as an orange piece lifted off the strip; for a collapse, the keyframes
//   whose camera sits on the previous one's spot in orange
//
//   import { cameraPathFigure, flagIcon } from '/camerapath.js';
//   el.innerHTML = cameraPathFigure({ plain, path, frames, cams, toScene });
//
// plain: the advice's `plain` (its `breaks`); path: the manifest's sfm.camera_path; frames: the keyframe
// names in order; cams: the scene's cameras ({name, center}); toScene: raw SfM point -> [x, y, z] (Y up).

const W = 600, H = 168, MAP = { x: 8, y: 10, w: 196, h: 126 }, STRIP = { x0: 236, x1: 588, y: 84 };
const C = { ok: 'var(--ok)', bad: 'var(--bad)', warn: 'var(--warn)', muted: 'var(--muted)', text: 'var(--text)', bg: 'var(--panel2)', line: 'var(--line)' };
const r1 = (v) => Math.round(v * 10) / 10;

// Small icons for the card's list of problems, drawn in currentColor (1em).
const ICONS = {
  break: '<path d="M7.2 4.8l1.3-1.3a2.6 2.6 0 0 1 3.7 3.7l-1.3 1.3M8.8 11.2l-1.3 1.3a2.6 2.6 0 0 1-3.7-3.7l1.3-1.3M2.5 2.5l1.6 1.6M13.5 13.5l-1.6-1.6M2 6h1.8M10 14v-1.8"/>',
  far: '<circle cx="4" cy="11" r="1.6"/><circle cx="7" cy="9" r="1.6"/><path d="M8.5 7.5l5-5M9.5 2.5h4v4"/>',
  stack: '<rect x="2" y="7" width="9" height="6" rx="1"/><path d="M4 5h9v6M6 3h9v6"/>',
  mask: '<circle cx="8" cy="6" r="3" stroke-dasharray="2 1.5"/><path d="M3 14c.8-3 2.6-4.5 5-4.5s4.2 1.5 5 4.5" stroke-dasharray="2 1.5"/>',
};
export function flagIcon(name) {
  return `<svg class="cp-icon cp-icon-${name}" width="1em" height="1em" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICONS[name] || ''}</svg>`;
}

function camGlyph(x, y, colour, s = 1, cls = '') {  // a little camera: body and lens
  return `<g class="${cls}" transform="translate(${r1(x)} ${r1(y)}) scale(${s})"><rect x="-5" y="-3.5" width="8" height="7" rx="1.2" fill="${colour}"/><path d="M3 -2l3.5 -2v8l-3.5 -2z" fill="${colour}"/></g>`;
}

function brokenLink(x, y) {  // the break marker: a red disc with a white broken link
  return `<g class="cp-break" transform="translate(${r1(x)} ${r1(y)})"><circle r="9" fill="${C.bad}"/>
    <g transform="translate(-6 -6) scale(.75)" fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round">${ICONS.break}</g></g>`;
}

// keyframe index -> camera centre seen from above (x, z), for the posed keyframes
function centres(frames, cams, toScene) {
  const at = new Map(frames.map((n, i) => [n, i]));
  const out = [];
  cams.forEach((c, j) => {
    const k = at.has(c.name) ? at.get(c.name) : j;
    const [x, , z] = toScene(c.center);
    out.push({ k, x, z });
  });
  return out.sort((a, b) => a.k - b.k);
}

function mapSvg(pts, path) {
  if (pts.length < 2) return '';
  const far = new Set((path.far || []).map((f) => f.keyframe));
  const core = pts.filter((p) => !far.has(p.k));
  const use = core.length >= 2 ? core : pts;
  let [x0, x1, z0, z1] = [Infinity, -Infinity, Infinity, -Infinity];
  use.forEach((p) => { x0 = Math.min(x0, p.x); x1 = Math.max(x1, p.x); z0 = Math.min(z0, p.z); z1 = Math.max(z1, p.z); });
  const span = Math.max(x1 - x0, z1 - z0) || 1, pad = 10;
  const s = Math.min((MAP.w - 2 * pad) / span, (MAP.h - 2 * pad) / span);
  const cx = MAP.x + MAP.w / 2, cy = MAP.y + MAP.h / 2, mx = (x0 + x1) / 2, mz = (z0 + z1) / 2;
  const proj = (p) => {  // into the box; far photos pulled in to its edge
    let x = cx + (p.x - mx) * s, y = cy + (p.z - mz) * s, out = false;
    const hx = MAP.w / 2 - 6, hy = MAP.h / 2 - 6, f = Math.max(Math.abs(x - cx) / hx, Math.abs(y - cy) / hy);
    if (f > 1) { x = cx + (x - cx) / f; y = cy + (y - cy) / f; out = true; }
    return { x, y, out };
  };
  const P = new Map(pts.map((p) => [p.k, proj(p)]));
  const jump = new Set();
  (path.jumps || []).forEach((j) => { for (let k = j.from; k < j.to; k++) jump.add(k); });
  let segs = '';
  for (let i = 1; i < pts.length; i++) {
    const a = P.get(pts[i - 1].k), b = P.get(pts[i].k);
    const broken = [...Array(pts[i].k - pts[i - 1].k).keys()].some((d) => jump.has(pts[i - 1].k + d));
    segs += `<line x1="${r1(a.x)}" y1="${r1(a.y)}" x2="${r1(b.x)}" y2="${r1(b.y)}" stroke="${broken ? C.bad : C.ok}" stroke-width="${broken ? 1.8 : 1.4}" ${broken ? 'stroke-dasharray="4 3" class="cp-jump"' : ''} stroke-linecap="round"/>`;
  }
  let dots = '';
  pts.forEach((p) => {
    const q = P.get(p.k);
    if (far.has(p.k)) {
      dots += `<circle class="cp-far-cam" cx="${r1(q.x)}" cy="${r1(q.y)}" r="3.4" fill="${C.warn}"/>`;
      if (q.out) {  // an arrow outwards: it is farther than drawn
        const dx = q.x - cx, dy = q.y - cy, l = Math.hypot(dx, dy) || 1, ux = dx / l, uy = dy / l;
        dots += `<path d="M${r1(q.x - ux * 9)} ${r1(q.y - uy * 9)}L${r1(q.x - ux * 3)} ${r1(q.y - uy * 3)}" stroke="${C.warn}" stroke-width="1.4" marker-end="url(#cpArrow)"/>`;
      }
    } else dots += `<circle cx="${r1(q.x)}" cy="${r1(q.y)}" r="1.5" fill="${C.ok}"/>`;
  });
  const first = P.get(pts[0].k);
  return `${segs}${dots}<circle cx="${r1(first.x)}" cy="${r1(first.y)}" r="3" fill="none" stroke="${C.text}" stroke-width="1.2"/>`;
}

function collapsedMapSvg(pts) {  // photos sharing a spot: one stack of cameras each, with the count
  if (!pts.length) return '';
  let [x0, x1, z0, z1] = [Infinity, -Infinity, Infinity, -Infinity];
  pts.forEach((p) => { x0 = Math.min(x0, p.x); x1 = Math.max(x1, p.x); z0 = Math.min(z0, p.z); z1 = Math.max(z1, p.z); });
  const span = Math.max(x1 - x0, z1 - z0) || 1, tol = span * 1e-3;
  const groups = [];
  pts.forEach((p) => {
    const g = groups.find((q) => Math.hypot(q.x - p.x, q.z - p.z) <= tol);
    if (g) g.n++; else groups.push({ x: p.x, z: p.z, n: 1 });
  });
  const pad = 18, s = Math.min((MAP.w - 2 * pad) / span, (MAP.h - 2 * pad) / span);
  const cx = MAP.x + MAP.w / 2, cy = MAP.y + MAP.h / 2, mx = (x0 + x1) / 2, mz = (z0 + z1) / 2;
  let out = '';
  const placed = [];  // labels drawn so far: a label that would overlap one is left out
  const at = (g) => [cx + (g.x - mx) * s, cy + (g.z - mz) * s];
  groups.sort((a, b) => a.n - b.n).forEach((g) => {
    const [x, y] = at(g);
    if (g.n === 1) { out += camGlyph(x, y, C.ok, 0.7); return; }
    const layers = Math.min(4, g.n);
    for (let i = layers - 1; i >= 0; i--) out += camGlyph(x + i * 1.8, y - i * 1.8, i ? C.muted : C.warn, 0.75, i ? '' : 'cp-stack');
  });
  [...groups].filter((g) => g.n > 1).sort((a, b) => b.n - a.n).forEach((g) => {
    const [x, y] = at(g), lx = x + 10, ly = y + 3;
    if (placed.some(([px, py]) => Math.abs(px - lx) < 22 && Math.abs(py - ly) < 11)) return;
    placed.push([lx, ly]);
    out += `<text class="cp-count" x="${r1(lx)}" y="${r1(ly)}" font-size="9" fill="${C.text}">×${g.n}</text>`;
  });
  const stacked = groups.filter((g) => g.n > 1);
  out += `<text x="${MAP.x + 6}" y="${MAP.y + MAP.h + 27}" font-size="10" fill="${C.warn}">${stacked.reduce((t, g) => t + g.n, 0)} photos in ${stacked.length} stacks</text>`;
  return out;
}

function stripSvg(n, plain, path) {
  const x = (k) => STRIP.x0 + (n > 1 ? (k / (n - 1)) * (STRIP.x1 - STRIP.x0) : 0), y = STRIP.y;
  let out = `<rect x="${STRIP.x0 - 3}" y="${y - 4}" width="${STRIP.x1 - STRIP.x0 + 6}" height="8" rx="4" fill="${C.ok}" opacity=".85"/>`;
  // the far photos: an orange piece lifted off the strip
  const far = (path.far || []).map((f) => f.keyframe).sort((a, b) => a - b);
  const runs = [];
  far.forEach((k) => { const r = runs[runs.length - 1]; if (r && k === r[1] + 1) r[1] = k; else runs.push([k, k]); });
  runs.forEach(([a, b]) => {
    const xa = x(a) - 3, xb = x(b) + 3;
    out += `<g class="cp-far"><path d="M${r1(xa)} ${y - 5}V${y - 26}M${r1(xb)} ${y - 5}V${y - 26}" stroke="${C.warn}" stroke-dasharray="2 2"/>
      <rect x="${r1(xa)}" y="${y - 32}" width="${r1(Math.max(6, xb - xa))}" height="8" rx="4" fill="${C.warn}"/>
      <text x="${r1((xa + xb) / 2)}" y="${y - 38}" font-size="10" text-anchor="middle" fill="${C.warn}">far away</text></g>`;
  });
  // the breaks: a gap in the strip and a broken link, labelled with the keyframe
  (plain.breaks || []).forEach((b) => {
    const xm = (x(b.from) + x(b.to)) / 2;
    out += `<rect x="${r1(Math.min(x(b.from), xm - 2))}" y="${y - 5}" width="${r1(Math.max(4, x(b.to) - x(b.from)))}" height="10" fill="${C.bg}"/>`;
    out += brokenLink(xm, y);
    out += `<text x="${r1(xm)}" y="${y + 24}" font-size="10" text-anchor="middle" fill="${C.text}">${b.label}</text>`;
  });
  out += `<text x="${STRIP.x0}" y="${y + 40}" font-size="10" fill="${C.muted}">0</text>
    <text x="${STRIP.x1}" y="${y + 40}" font-size="10" text-anchor="end" fill="${C.muted}">${n - 1}</text>
    <text x="${(STRIP.x0 + STRIP.x1) / 2}" y="${y + 40}" font-size="10" text-anchor="middle" fill="${C.muted}">keyframes</text>`;
  return out;
}

function collapsedStripSvg(n, pts) {  // keyframes on the previous one's spot, in orange
  const x = (k) => STRIP.x0 + (n > 1 ? (k / (n - 1)) * (STRIP.x1 - STRIP.x0) : 0), y = STRIP.y;
  let span = 0;
  pts.forEach((p) => pts.forEach((q) => { span = Math.max(span, Math.abs(p.x - q.x), Math.abs(p.z - q.z)); }));
  const tol = (span || 1) * 1e-3, w = Math.max(1.5, (STRIP.x1 - STRIP.x0) / Math.max(n, 1));
  let out = `<rect x="${STRIP.x0 - 3}" y="${y - 4}" width="${STRIP.x1 - STRIP.x0 + 6}" height="8" rx="4" fill="${C.ok}" opacity=".85"/>`;
  for (let i = 1; i < pts.length; i++) {
    if (Math.hypot(pts[i].x - pts[i - 1].x, pts[i].z - pts[i - 1].z) <= tol) {
      out += `<rect class="cp-still" x="${r1(x(pts[i].k) - w / 2)}" y="${y - 6}" width="${r1(w)}" height="12" fill="${C.warn}"/>`;
    }
  }
  out += `<text x="${STRIP.x0}" y="${y + 24}" font-size="10" fill="${C.muted}">0</text>
    <text x="${STRIP.x1}" y="${y + 24}" font-size="10" text-anchor="end" fill="${C.muted}">${n - 1}</text>
    <text x="${(STRIP.x0 + STRIP.x1) / 2}" y="${y + 24}" font-size="10" text-anchor="middle" fill="${C.muted}">keyframes</text>`;
  return out;
}

function legend(items) {
  let x = STRIP.x0;
  return items.map(([colour, label, dash]) => {
    const g = `<line x1="${x}" y1="${H - 8}" x2="${x + 14}" y2="${H - 8}" stroke="${colour}" stroke-width="3" ${dash ? 'stroke-dasharray="3 2"' : ''}/>
      <text x="${x + 18}" y="${H - 4}" font-size="10" fill="${C.muted}">${label}</text>`;
    x += 26 + label.length * 5.4;
    return g;
  }).join('');
}

export function cameraPathFigure({ plain, path, frames = [], cams = [], toScene = (c) => c }) {
  const n = Math.max(frames.length, path.cameras || 0, ...cams.map((_, i) => i + 1), 2);
  const pts = centres(frames, cams, toScene);
  const collapsed = plain.kind === 'collapsed';
  const map = collapsed ? collapsedMapSvg(pts) : mapSvg(pts, path);
  const strip = collapsed ? collapsedStripSvg(n, pts) : stripSvg(n, plain, path);
  const keys = collapsed ? [[C.ok, 'camera moved'], [C.warn, 'same spot as before']]
    : [[C.ok, 'path'], [C.bad, "views don't connect", true], ...((path.far || []).length ? [[C.warn, 'placed far away']] : [])];
  return `<svg class="cp-figure" viewBox="0 0 ${W} ${H}" role="img" aria-label="${collapsed ? 'cameras stacked on the same spots' : 'the camera path with its breaks'}" xmlns="http://www.w3.org/2000/svg">
    <defs><marker id="cpArrow" viewBox="0 0 6 6" refX="5" refY="3" markerWidth="5" markerHeight="5" orient="auto"><path d="M0 0L6 3L0 6z" fill="${C.warn}"/></marker></defs>
    <rect x="${MAP.x}" y="${MAP.y}" width="${MAP.w}" height="${MAP.h}" rx="6" fill="none" stroke="${C.line}"/>
    <text x="${MAP.x + 6}" y="${MAP.y + MAP.h + 14}" font-size="10" fill="${C.muted}">${pts.length ? 'cameras seen from above' : 'no camera positions loaded'}</text>
    ${map}${strip}${legend(keys)}</svg>`;
}
