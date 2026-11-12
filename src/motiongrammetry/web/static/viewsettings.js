// The 3D view's settings, the same on the Scan page's preview and in the editor (#26): View options,
// a line that folds and opens on the page's own viewer settings, then a row of small spheres, one per
// display mode, each drawn in that mode; a click shows it, the shown one is ringed. W is the page's
// (its key guards differ); it calls setDisplay like a click does.
//
//   import { viewSettings, holesOption } from '/viewsettings.js';     // + <link rel="stylesheet" href="/viewsettings.css">
//   const vs = viewSettings(box, {
//     modes: { textured: 'colours', solid: 'solid', wire: 'wire', solidwire: 'solidwire' },  // the page's ids -> looks
//     display: 'textured', onDisplay: (d) => …,  // a sphere clicked: the page shows d and calls vs.setDisplay(d)
//     openKey: 'mg.viewopts',                     // open or folded, per viewer
//     options: [ptRow, holesOption()],           // what View options holds (nodes or markup)
//     trailing: button,                          // at the end of the sphere row (the preview's orientation)
//   });
//
// The box (its background and fixed width) is the page's: .vs-box. It keeps one width whatever is open
// or ticked; long lines wrap inside it. A checkbox in the options with data-note="holes" shows that
// note on the folded line while it is ticked, so a setting that changes the view never hides folded.
// The spheres are drawn once per page by a throwaway renderer, with the viewers' own materials and
// lights, into images: no live views.

import * as THREE from 'three';

export const LOOKS = ['colours', 'solid', 'wire', 'solidwire'];
const NAMES = { colours: 'Colours', solid: 'Solid grey', wire: 'Wireframe', solidwire: 'Solid + wires' };
const LABEL = 'View options';

// Highlight holes and its count, in both pages: the count's short form shows, the sentence in its tooltip.
export const holesOption = () =>
  '<label data-note="holes" data-note-title="Highlight holes is on" title="edges that border a single triangle: holes, openings, cuts left open">'
  + '<input type="checkbox" id="holesChk"> Highlight holes</label><div id="holesInfo"></div>';
// the count of openings: short on screen, the full sentence on hover
export function holesCount(el, loops, edges, what = '') {
  const n = (x) => x.toLocaleString();
  el.textContent = loops ? `${what}${n(loops)} opening${loops === 1 ? '' : 's'} · ${n(edges)} edges` : `${what}closed, no holes`;
  el.title = loops ? `${what}${n(loops)} opening${loops === 1 ? '' : 's'} (${n(edges)} open edges), drawn in orange` : '';
}

let images = null;
function paintedMap() {  // "Colours": a small, saturated painted landscape, so it never reads as grey
  const cv = Object.assign(document.createElement('canvas'), { width: 256, height: 128 }), g = cv.getContext('2d');
  const grd = g.createLinearGradient(0, 0, 0, 128);
  [[0, '#2f7fe0'], [.36, '#7cc0f0'], [.44, '#f0b040'], [.56, '#5aa83a'], [.8, '#3d7a2a'], [1, '#7a4a22']].forEach(([o, c]) => grd.addColorStop(o, c));
  g.fillStyle = grd; g.fillRect(0, 0, 256, 128);
  let seed = 7; const rnd = () => (seed = (seed * 16807) % 2147483647) / 2147483647;
  const pal = ['#e0602a', '#f5c842', '#2a6fd0', '#d23c3c', '#58b030', '#f0ece0', '#9a5a2a'];
  for (let i = 0; i < 160; i++) {
    g.globalAlpha = .35 + rnd() * .4; g.fillStyle = pal[Math.floor(rnd() * pal.length)];
    g.beginPath(); g.ellipse(rnd() * 256, 40 + rnd() * 88, 3 + rnd() * 12, 2 + rnd() * 6, rnd() * 3, 0, 7); g.fill();
  }
  const t = new THREE.CanvasTexture(cv); t.colorSpace = THREE.SRGBColorSpace;
  return t;
}
function drawSpheres() {  // look -> PNG data URL, or {} without WebGL (plain discs then)
  if (images) return images;
  images = {};
  const px = 22 * Math.min(2, Math.max(1, Math.round(window.devicePixelRatio || 1)));
  let r;
  try { r = new THREE.WebGLRenderer({ antialias: true, alpha: true, preserveDrawingBuffer: true }); } catch { return images; }
  r.outputColorSpace = THREE.SRGBColorSpace; r.setClearColor(0x000000, 0); r.setSize(px, px, false);
  const sc = new THREE.Scene();
  sc.add(new THREE.HemisphereLight(0xffffff, 0x404050, 1.6));  // the viewers' lights, the sun riding on the camera
  const cam = new THREE.PerspectiveCamera(30, 1, 0.1, 20), light = new THREE.DirectionalLight(0xffffff, 1.4);
  cam.add(light); light.position.set(1, 2, 1); sc.add(cam);
  cam.position.set(0.9, 0.6, 0.9).setLength(3.98); cam.lookAt(0, 0, 0); cam.updateMatrixWorld();
  const map = paintedMap(), geo = new THREE.SphereGeometry(1, 10, 7);
  const solid = new THREE.MeshStandardMaterial({ color: 0xc4cad3, roughness: .8, metalness: 0, polygonOffset: true, polygonOffsetFactor: 1, polygonOffsetUnits: 1 });
  const mats = {  // as the viewers set them, the wires at their fullest (a close-up)
    colours: [new THREE.MeshBasicMaterial({ map })],
    solid: [solid],
    wire: [new THREE.MeshBasicMaterial({ color: 0xb4c2d4, wireframe: true, transparent: true, opacity: .82, depthWrite: false })],
    solidwire: [solid, new THREE.MeshBasicMaterial({ color: 0x1c2430, wireframe: true, transparent: true, opacity: .6, depthWrite: false })],
  };
  for (const look of LOOKS) {
    const ms = mats[look].map((m) => new THREE.Mesh(geo, m));
    ms.forEach((m) => { m.rotation.y = -0.6; sc.add(m); });
    r.render(sc, cam);
    images[look] = r.domElement.toDataURL('image/png');
    ms.forEach((m) => sc.remove(m));
  }
  new Set(Object.values(mats).flat()).forEach((m) => m.dispose()); map.dispose(); geo.dispose();
  r.dispose(); r.forceContextLoss();
  return images;
}

export function viewSettings(box, { modes, display, onDisplay, openKey, options = '', trailing = null }) {
  box.classList.add('vs-box');
  const opts = document.createElement('div');
  opts.className = 'vopts'; opts.id = 'viewOpts';
  opts.innerHTML = `<button type="button" class="vopts-tog" id="viewOptsTog" aria-expanded="false" aria-controls="viewOptsBody">`
    + `<span class="caret" aria-hidden="true">▸</span><span>${LABEL}</span><span class="vopts-on" id="viewOptsNote" hidden></span></button>`
    + '<div class="vopts-body" id="viewOptsBody" hidden></div>';
  const body = opts.querySelector('.vopts-body'), tog = opts.querySelector('.vopts-tog'), note = opts.querySelector('.vopts-on');
  for (const o of [options].flat()) if (typeof o === 'string') body.insertAdjacentHTML('beforeend', o); else body.append(o);
  const row = document.createElement('div');
  row.className = 'disp-row'; row.id = 'dispRow';
  row.setAttribute('role', 'group'); row.setAttribute('aria-label', 'display (W cycles)');
  const img = drawSpheres();
  row.innerHTML = Object.entries(modes).map(([id, look]) => `<button type="button" class="sph" data-display="${id}" title="${NAMES[look]} (W)" `
    + `aria-label="${NAMES[look]}"><img alt=""${img[look] ? ` src="${img[look]}"` : ''}></button>`).join('') + '<span class="grow"></span>';
  if (trailing) row.append(trailing);
  box.append(opts, row);

  const refresh = () => {  // the folded line's note: a noted setting that is on
    const on = [...body.querySelectorAll('[data-note]')].find((l) => l.querySelector('input')?.checked && !l.hidden);
    note.hidden = !on || !body.hidden;
    note.textContent = on?.dataset.note || ''; note.title = on?.dataset.noteTitle || '';
  };
  const open = (o, keep = true) => {
    tog.setAttribute('aria-expanded', o); body.hidden = !o;
    if (keep) try { localStorage.setItem(openKey, o ? '1' : '0'); } catch {}
    refresh();
  };
  const setDisplay = (d) => row.querySelectorAll('.sph').forEach((b) => {
    const on = b.dataset.display === d; b.classList.toggle('on', on); b.setAttribute('aria-pressed', on);
  });
  tog.addEventListener('click', () => open(body.hidden));
  body.addEventListener('change', refresh);
  row.addEventListener('click', (e) => { const b = e.target.closest('.sph'); if (b) onDisplay(b.dataset.display); });
  let wasOpen = false;
  try { wasOpen = localStorage.getItem(openKey) === '1'; } catch {}
  open(wasOpen, false); setDisplay(display);
  return { setDisplay, refresh, row, body, showSpheres: (on) => { row.hidden = !on; } };
}
