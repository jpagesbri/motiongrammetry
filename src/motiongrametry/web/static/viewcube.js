// A view cube for every 3D view: shows which way the camera looks (faces Front/Back/Left/Right/
// Top/Bottom and the X Y Z axes, Z up as in the print files), and turns the view on a click: a face
// looks straight at it, near an edge or a corner the 45° view in between. The frame button fits
// the model. Plain 2D canvas, no three.js: it reads the camera's rotation and moves the camera
// about the orbit target.
//
//   const cube = addViewCube(hostElement, { camera, controls, render, fit, projection });
//   (camera: the camera or a function returning the current one; projection: optional, an object
//   with .camera and .set(orthographic) as the pages' projection() makes, adds a Persp / Ortho button)
//   cube.update();          // after moving the camera by code (OrbitControls changes are followed)
//   cube.el.hidden = true;  // e.g. when the 3D view is not shown

// The scene is Y-up (three.js); the labels follow the Z-up convention of slicers and CAD:
// X = +x, Y = -z (away from the front), Z = +y.
const FACES = [
  { name: 'Front', n: [0, 0, 1], u: [1, 0, 0], v: [0, 1, 0] },
  { name: 'Back', n: [0, 0, -1], u: [-1, 0, 0], v: [0, 1, 0] },
  { name: 'Right', n: [1, 0, 0], u: [0, 0, -1], v: [0, 1, 0] },
  { name: 'Left', n: [-1, 0, 0], u: [0, 0, 1], v: [0, 1, 0] },
  { name: 'Top', n: [0, 1, 0], u: [1, 0, 0], v: [0, 0, -1] },
  { name: 'Bottom', n: [0, -1, 0], u: [1, 0, 0], v: [0, 0, 1] },
];
const AXES = [  // from the bottom-front-left corner, along the cube's edges (between the two faces named)
  { label: 'X', d: [1, 0, 0], color: '#ef5d5d', faces: ['Front', 'Bottom'] },
  { label: 'Y', d: [0, 0, -1], color: '#4cc38a', faces: ['Left', 'Bottom'] },
  { label: 'Z', d: [0, 1, 0], color: '#5aa9ff', faces: ['Front', 'Left'] },
];
const ORIGIN = [-1, -1, 1];
const add = (a, b, k = 1) => [a[0] + k * b[0], a[1] + k * b[1], a[2] + k * b[2]];
const norm = (a) => { const l = Math.hypot(...a) || 1; return a.map((x) => x / l); };

let styled = false;
function style() {
  if (styled) return;
  styled = true;
  const css = document.createElement('style');
  css.textContent = `.vcube { position: absolute; left: 10px; bottom: 10px; z-index: 3; display: flex; align-items: flex-end; gap: 6px; user-select: none; }
  .vcube[hidden] { display: none; }
  .vcube canvas { width: 124px; height: 124px; cursor: pointer; display: block; }
  .vcube button { width: 30px; height: 30px; border-radius: 50%; border: 1px solid #262c37; background: rgba(22,26,33,.88);
    color: #d8dee9; cursor: pointer; display: grid; place-items: center; padding: 0; margin-bottom: 8px; }
  .vcube button:hover { border-color: #5aa9ff; }
  .vcube .vproj { width: auto; height: 24px; border-radius: 12px; padding: 0 9px; font: 600 11px system-ui, sans-serif; letter-spacing: .03em; }
  .vcube .vbtns { display: flex; flex-direction: column; align-items: flex-start; gap: 6px; margin-bottom: 8px; }
  .vcube .vbtns button { margin-bottom: 0; }`;
  document.head.appendChild(css);
}

export function addViewCube(host, { camera: cameraOpt, controls, render, fit, projection }) {
  style();
  const camera_ = () => (projection ? projection.camera : typeof cameraOpt === 'function' ? cameraOpt() : cameraOpt);
  const el = document.createElement('div');
  el.className = 'vcube';
  el.innerHTML = `<canvas title="click a face, edge or corner to look from there"></canvas><div class="vbtns">
    ${projection ? '<button class="vproj" title="perspective or orthographic (no foreshortening: sizes and parallel lines stay true) · key 5"></button>' : ''}
    <button class="vfit" title="frame the model"><svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4">
      <path d="M1.5 5V1.5H5M11 1.5h3.5V5M14.5 11v3.5H11M5 14.5H1.5V11"/><path d="M8 4.5l3 1.7v3.6L8 11.5 5 9.8V6.2z"/><path d="M5 6.2l3 1.7 3-1.7M8 7.9v3.6"/></svg></button></div>`;
  host.appendChild(el);
  const cv = el.querySelector('canvas'), g = cv.getContext('2d');
  const SIZE = 124, S = 25, C = SIZE / 2 + 5;  // centre a little low-right: the axes reach up-left
  let hover = null, anim = 0;

  function view(p) {  // world direction -> [screen x, screen y, depth towards the viewer]
    const m = camera_().matrixWorldInverse.elements;
    const x = m[0] * p[0] + m[4] * p[1] + m[8] * p[2], y = m[1] * p[0] + m[5] * p[1] + m[9] * p[2], z = m[2] * p[0] + m[6] * p[1] + m[10] * p[2];
    return [C + x * S, C - y * S, z];
  }
  function visible() {  // faces towards the camera, farthest first
    return FACES.map((f) => ({ f, c: view(f.n) })).filter(({ c }) => c[2] > 1e-3).sort((a, b) => a.c[2] - b.c[2]);
  }
  function draw() {
    camera_().updateMatrixWorld();
    if (projBtn) projBtn.textContent = projection.camera.isOrthographicCamera ? 'Ortho' : 'Persp';
    const dpr = devicePixelRatio || 1;
    if (cv.width !== SIZE * dpr) { cv.width = cv.height = SIZE * dpr; }
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, SIZE, SIZE);
    const o = view([0, 0, 0]), shown = new Set(visible().map(({ f }) => f.name));
    const axis = (a) => {
      const base = view(ORIGIN), end = view(add(ORIGIN, a.d, 2.45)), lab = view(add(ORIGIN, a.d, 2.8));
      g.strokeStyle = a.color; g.lineWidth = 2;
      g.beginPath(); g.moveTo(base[0], base[1]); g.lineTo(end[0], end[1]); g.stroke();
      g.fillStyle = a.color; g.font = '700 13px system-ui, sans-serif'; g.textAlign = 'center'; g.textBaseline = 'middle';
      g.fillText(a.label, Math.min(SIZE - 6, Math.max(6, lab[0])), Math.min(SIZE - 6, Math.max(6, lab[1])));
    };
    const front = (a) => a.faces.some((n) => shown.has(n));
    AXES.filter((a) => !front(a)).forEach(axis);  // along a hidden edge: behind the cube, only its tip shows
    for (const { f, c } of visible()) {
      const corner = (a, b) => view(add(add(f.n, f.u, a), f.v, b));
      const pts = [corner(-1, -1), corner(1, -1), corner(1, 1), corner(-1, 1)];
      g.beginPath(); pts.forEach(([x, y], i) => (i ? g.lineTo(x, y) : g.moveTo(x, y))); g.closePath();
      const light = 34 + Math.round(16 * c[2]);
      g.fillStyle = hover?.face === f ? '#2d4f78' : `rgb(${light},${light + 4},${light + 10})`;
      g.fill(); g.strokeStyle = '#5b6575'; g.lineWidth = 1; g.stroke();
      if (hover?.face === f && hover.part) {  // the edge or corner that would be picked
        const [a, b] = hover.part;
        const lo = (t) => (t ? (t > 0 ? [0.55, 1] : [-1, -0.55]) : [-0.55, 0.55]);
        const [ua, ub] = lo(a), [va, vb] = lo(b);
        const r = [corner(ua, va), corner(ub, va), corner(ub, vb), corner(ua, vb)];
        g.beginPath(); r.forEach(([x, y], i) => (i ? g.lineTo(x, y) : g.moveTo(x, y))); g.closePath();
        g.fillStyle = 'rgba(90,169,255,.55)'; g.fill();
      }
      // the label, drawn in the face's plane (parallel projection: an affine map)
      const pu = view(f.u), pv = view(f.v), k = 1 / 30;
      g.save();
      g.setTransform(dpr * (pu[0] - o[0]) * k, dpr * (pu[1] - o[1]) * k, -dpr * (pv[0] - o[0]) * k, -dpr * (pv[1] - o[1]) * k, dpr * c[0], dpr * c[1]);
      g.fillStyle = `rgba(230,236,245,${Math.min(1, 0.25 + c[2])})`;
      g.font = '600 14px system-ui, sans-serif'; g.textAlign = 'center'; g.textBaseline = 'middle';
      g.fillText(f.name, 0, 0);
      g.restore();
    }
    AXES.filter(front).forEach(axis);
  }
  function hit(e) {  // which face, and which edge / corner of it, is under the pointer
    const r = cv.getBoundingClientRect(), x = e.clientX - r.left, y = e.clientY - r.top;
    for (const { f, c } of visible().reverse()) {
      const pu = view(f.u), pv = view(f.v), o = view([0, 0, 0]);
      const ux = pu[0] - o[0], uy = pu[1] - o[1], vx = pv[0] - o[0], vy = pv[1] - o[1];
      const det = ux * vy - uy * vx; if (Math.abs(det) < 1e-6) continue;
      const dx = x - c[0], dy = y - c[1], a = (dx * vy - dy * vx) / det, b = (ux * dy - uy * dx) / det;
      if (Math.abs(a) <= 1 && Math.abs(b) <= 1) {
        const pa = Math.abs(a) > 0.55 ? Math.sign(a) : 0, pb = Math.abs(b) > 0.55 ? Math.sign(b) : 0;
        return { face: f, part: pa || pb ? [pa, pb] : null, dir: norm(add(add(f.n, f.u, pa), f.v, pb)) };
      }
    }
    return null;
  }
  function lookFrom(dir, then) {  // turn the camera about the target, keeping its distance
    const camera = camera_(), t = controls.target, p = camera.position;
    const from = norm([p.x - t.x, p.y - t.y, p.z - t.z]), dist = Math.hypot(p.x - t.x, p.y - t.y, p.z - t.z);
    let to = dir;
    if (Math.abs(to[1]) > 0.999) to = norm([to[0], to[1], to[2] + 1e-3]);  // straight up / down: keep the orbit stable
    if (!then && from[0] * to[0] + from[1] * to[1] + from[2] * to[2] < -0.9) {  // the other side: go round, not through
      const side = Math.abs(from[1]) < 0.9 ? norm([-from[2], 0, from[0]]) : [1, 0, 0];
      return lookFrom(side, () => lookFrom(dir, true));
    }
    const t0 = performance.now(), id = ++anim;
    const step = (now) => {
      if (id !== anim) return;
      const k = Math.min(1, (now - t0) / 320), e = k * k * (3 - 2 * k);
      const d = norm([from[0] + (to[0] - from[0]) * e, from[1] + (to[1] - from[1]) * e, from[2] + (to[2] - from[2]) * e]);
      camera.position.set(t.x + d[0] * dist, t.y + d[1] * dist, t.z + d[2] * dist);
      camera.lookAt(t.x, t.y, t.z);
      controls.update(); render(); draw();
      if (k < 1) requestAnimationFrame(step); else if (typeof then === 'function') then();
    };
    requestAnimationFrame(step);
  }
  cv.addEventListener('pointermove', (e) => { const h = hit(e); if (h?.face !== hover?.face || String(h?.part) !== String(hover?.part)) { hover = h; draw(); } });
  cv.addEventListener('pointerleave', () => { hover = null; draw(); });
  cv.addEventListener('click', (e) => { const h = hit(e); if (h) lookFrom(h.dir); });
  const projBtn = el.querySelector('.vproj');
  projBtn?.addEventListener('click', () => { projection.set(!projection.camera.isOrthographicCamera); render(); draw(); });
  el.querySelector('.vfit').addEventListener('click', () => { fit(); draw(); });
  controls.addEventListener('change', draw);
  draw();
  return { el, update: draw };
}
