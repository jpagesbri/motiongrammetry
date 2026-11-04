// The Export window, the same on every page (Scan, Edit, Help, About): pick what to take out of a
// scan (each content in a format that suits it; several come as one zip), how to place it and, for a
// print, how the solid is made; the result is shown in 3D with its report. Files are made when asked
// and kept until something they come from changes.
//
// topbar.js puts an Export… button beside the scan chip and an Export… item in each scan's ⋯ menu,
// both opening this window; the Scan page's own Export… buttons (the Preview's Export panel, the
// assisted run's last step) open it too. A page tells it what it knows through exportHooks():
//   unsaved(name) -> [what] edits on this page not saved yet (Edit: 'sculpt edits', 'paint'): the window
//                           says it exports the saved files only
//   orient(name)            "fix the orientation…" on this page (Scan); without it, a link to the Scan page
//   run(name)               "Go to Run" when there is nothing to export (Scan); without it, a link
//   exported(name, state)   after an export, with the scan's new state (Scan: its Export panel's print check)
// export.css styles it. three.js and the 3D preview are loaded the first time a file is shown.

const enc = encodeURIComponent;
const esc = (t) => String(t ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const api = async (path, opts = {}) => {
  const r = await fetch(path, opts);
  const body = (r.headers.get('content-type') || '').includes('json') ? await r.json() : null;
  if (!r.ok) throw new Error(body?.error || r.statusText);
  return body;
};
const post = (path, body) => api(path, { method: 'POST', body: JSON.stringify(body) });

export const EX_CONTENTS = [
  { id: 'textured', label: 'Textured model', about: 'the photo texture · viewers, Blender, the web, AR',
    formats: { glb: 'GLB', obj: 'OBJ + MTL + PNG' }, stage: 'texture', needs: 'run texture first' },
  { id: 'mesh', label: 'Clean mesh', about: 'a colour per vertex, no texture · editing, measuring',
    formats: { glb: 'GLB', ply: 'PLY', obj: 'OBJ' }, stage: 'clean', needs: 'run clean first' },
  { id: 'print', label: '3D print', about: 'one closed solid, Z-up, in mm · slicers',
    formats: { stl: 'STL', '3mf': '3MF' }, stage: 'clean', needs: 'run clean first' },
  { id: 'raw', label: 'Raw mesh', about: 'before cleanup, background and all', formats: { ply: 'PLY', obj: 'OBJ', glb: 'GLB' },
    stage: 'mvs', needs: 'run mvs or vggt first', pipelineOnly: true },
  { id: 'cloud', label: 'Dense point cloud', about: 'the dense points, background and all', formats: { ply: 'PLY' },
    stage: 'mvs', needs: 'run mvs first', pipelineOnly: true },
  { id: 'cameras', label: 'Cameras', about: 'COLMAP model: poses and sparse points · splatting, NeRF', formats: { colmap: 'COLMAP' },
    stage: 'sfm', needs: 'run sfm first', pipelineOnly: true },
];
/** Is content `c` older than what it comes from, in the pipeline's or the custom version of a scan's /state? */
export function exStale(c, custom, state) {
  if (custom) return c.id === 'textured' && !!state?.custom?.texture_stale;
  return (state?.stale || []).includes(c.stage);
}
/** Whether a scan's /state has anything to export. */
export const hasExports = (state) => !!(state?.exportable?.pipeline?.length || state?.exportable?.custom?.length);
export const exSize = (b) => b >= 1e9 ? (b / 1e9).toFixed(1) + ' GB' : b >= 1e6 ? (b / 1e6).toFixed(1) + ' MB' : (b / 1e3).toFixed(0) + ' kB';

const EX_FIELDS = { exHeight: 'height_mm', exMode: 'mode', exSide: 'shell_side', exShell: 'shell', exWall: 'min_wall',
                    exStand: 'stand', exBase: 'base', exUp: 'up' };

let hooks = {};
let modal = null, scan = null, st = null, view = null, built = [], opener = null, openSeq = 0;
const $ = (id) => document.getElementById(id);

/** What the page does around an export (see the top of this file). */
export function exportHooks(h) { hooks = { ...hooks, ...h }; }
/** Is the Export window open? */
export const exportOpen = () => !!modal && !modal.hidden;

const FORM = `<div class="ex-win">
    <div class="ex-head"><b>Export</b><span class="muted" id="exScan"></span><button class="ex-x" id="exClose" title="close (Esc)" aria-label="close">✕</button></div>
    <div class="ex-none" id="exNone" hidden></div>
    <div class="ex-body" id="exBody">
      <div class="ex-form"><div class="ex-scroll">
        <div class="ex-unsaved" id="exUnsaved" role="note" hidden></div>
        <div class="ex-sec">Version</div>
        <label class="row">From <select id="exSource"><option value="pipeline">the pipeline</option><option value="custom">your edits (custom)</option></select></label>
        <div class="ex-sec">Contents</div>
        <div id="exItems"></div>
        <div class="ex-sec">Placement</div>
        <label class="row" title="how to stand the model up: the support plane, the camera orbit, or as SfM left it">Up <select id="exUp">
          <option value="auto">auto</option><option value="plane">support plane</option><option value="camera">camera orbit</option><option value="none">as reconstructed</option></select></label>
        <div class="opt-help" style="margin:-2px 0 6px">standing wrong? <a id="exOrient">fix the orientation…</a></div>
        <label class="row" title="the clean model's real height; everything exported together is scaled the same">Height (mm) <input type="number" id="exHeight" min="1" step="1" placeholder="as scanned"></label>
        <div class="opt-help" id="exHeightHelp">a scan has no scale of its own: GLB in metres, the others in millimetres</div>
        <div id="exPrint">
          <div class="ex-sec">3D print</div>
          <label class="row" title="solid: openings closed with lids; shell: only the scanned surface; auto: shell when lids would invent over a third">Print as <select id="exMode">
            <option value="auto">auto</option><option value="solid">solid</option><option value="shell">shell</option></select></label>
          <label class="row" title="inward keeps the scanned surface exactly where it was measured">Shell grows <select id="exSide">
            <option value="inward">inward (keeps the scanned surface)</option><option value="outward">outward</option><option value="both">both ways</option></select></label>
          <label class="row">Shell thickness (mm) <input type="number" id="exShell" min="0.4" step="0.1" value="3"></label>
          <label class="row" title="thinner walls are thickened behind the scanned surface">Minimum wall (mm) <input type="number" id="exWall" min="0" step="0.1" value="0.8"></label>
          <label class="row">Stand <select id="exStand"><option value="auto">when it would topple</option><option value="none">never</option></select></label>
          <label class="row" title="the bottom share of the height flattened so it stands (solid only)">Flat base (%) <input type="number" id="exBase" min="0" max="20" step="0.5" value="2"></label>
        </div>
        </div><div class="ex-foot">
        <div class="btns"><button class="btn primary" id="exBuild">Export</button><a class="btn" id="exDownload" hidden download>Download</a></div>
        <div class="muted" id="exStatus" style="margin-top:6px;font-size:12px"></div>
        <div class="ex-files" id="exFiles"></div>
        <div id="exIssues"></div>
      </div></div>
      <div class="ex-preview" id="exPreview"><select class="ex-pick" id="exPick" hidden title="which file to look at"></select>
        <div class="muted ex-empty" id="exEmpty">Export to see the result here</div></div>
    </div>
  </div>`;

function build() {
  if (modal) return;
  modal = Object.assign(document.createElement('div'), { className: 'ex-modal', id: 'exportModal', hidden: true, innerHTML: FORM });
  modal.setAttribute('role', 'dialog'); modal.setAttribute('aria-modal', 'true'); modal.setAttribute('aria-label', 'Export');
  document.body.append(modal);
  $('exClose').addEventListener('click', closeExport);
  modal.addEventListener('mousedown', (e) => { if (e.target === modal) closeExport(); });
  // The page's shortcuts get no keys while the window is open; Escape closes it wherever the focus is.
  modal.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeExport(); e.stopPropagation(); });
  addEventListener('keydown', (e) => {
    if (modal.hidden || modal.contains(e.target)) return;
    e.stopPropagation(); if (e.key === 'Escape') closeExport();
  }, true);
  $('exOrient').addEventListener('click', (e) => {
    if (!hooks.orient) return;  // the link (to the Scan page) does it
    e.preventDefault(); closeExport(); hooks.orient(scan);
  });
  $('exSource').addEventListener('change', () => { renderItems(); reset(); });
  $('exMode').addEventListener('change', fields);
  Object.keys(EX_FIELDS).forEach((id) => $(id).addEventListener('change', check));
  $('exBuild').addEventListener('click', exportNow);
  $('exPick').addEventListener('change', () => preview(built[Number($('exPick').value)]));
}

export function closeExport() {
  if (!modal || modal.hidden) return;
  modal.hidden = true; openSeq++;
  opener?.focus?.({ preventScroll: true }); opener = null;
}

/** Open the Export window for scan `name` (its state is read afresh: it may have changed on another page). */
export async function openExport(name) {
  if (!name) return;
  build();
  const seq = ++openSeq;
  opener = modal.hidden ? document.activeElement : opener;
  scan = name; st = null;
  $('exScan').textContent = '· ' + name;
  $('exBody').hidden = true; $('exNone').hidden = false; $('exNone').innerHTML = '<p class="muted">loading…</p>';
  modal.querySelector('.ex-win').classList.add('small');
  modal.hidden = false; $('exClose').focus({ preventScroll: true });
  let state;
  try { state = await api(`/api/workspaces/${enc(name)}/state`); }
  catch (e) { if (seq === openSeq) $('exNone').innerHTML = `<p class="ex-err">could not read ${esc(name)}: ${esc(e.message)}</p>`; return; }
  if (seq !== openSeq) return;  // closed, or opened for another scan, in the meantime
  st = state;
  if (!hasExports(st)) return nothingYet(seq);
  modal.querySelector('.ex-win').classList.remove('small');
  $('exNone').hidden = true; $('exBody').hidden = false;
  const unsaved = (hooks.unsaved?.(name) || []).filter(Boolean);
  $('exUnsaved').hidden = !unsaved.length;
  $('exUnsaved').innerHTML = unsaved.length ? `<b>You have unsaved ${esc(unsaved.join(' and '))}.</b> Export uses the saved
    files only: it leaves out what you changed since the last save. Save first to export what you see.` : '';
  const c = st.custom || {}, hasCustom = (st.exportable?.custom || []).length > 0;
  $('exSource').querySelector('[value=custom]').disabled = !hasCustom;
  $('exSource').value = hasCustom && (c.mesh || c.texture) ? 'custom' : 'pipeline';
  $('exUp').querySelector('[value=auto]').textContent = st.orientation?.user ? 'your orientation' : 'auto';
  $('exOrient').href = '/?open=orient#' + enc(name);
  const saved = savedFields(), d = defaults();
  for (const id of Object.keys(EX_FIELDS)) $(id).value = saved[id] ?? d[id];
  $('exHeight').dataset.print = st.export_defaults?.height_mm ?? 100;
  $('exItems').innerHTML = '';
  reset(); renderItems();
  preview(null, 'Export to see the result here');
}

// Nothing to take out yet: say so, and point to the Run tab, where the pipeline makes it.
async function nothingYet(seq) {
  const w = await api('/api/workspaces').then((l) => l.find((x) => x.name === scan)).catch(() => null);
  if (seq !== openSeq) return;
  const running = w?.job === 'running';
  $('exNone').innerHTML = `<p><b>${esc(scan)}</b> has nothing to export yet.</p>
    <p class="muted">Exports are made from the pipeline's results: the textured model, the clean mesh, a 3D print, the raw
      mesh, the dense cloud or the cameras. ${running ? 'A run is going on: they show here as its stages finish.' : 'Run the pipeline on this scan first.'}</p>
    <div class="btns"><a class="btn primary" id="exToRun" href="/?tab=run#${enc(scan)}">Go to Run</a><button class="btn" id="exNoneClose">Close</button></div>`;
  $('exNoneClose').addEventListener('click', closeExport);
  $('exToRun').addEventListener('click', (e) => {
    if (!hooks.run) return;
    e.preventDefault(); const name = scan; closeExport(); hooks.run(name);
  });
}

// the scan's own settings (its print_* and up options: the assisted run sets them from what was
// filmed) until you change them for that scan
function defaults() {
  const d = st?.export_defaults || {};
  return { exHeight: '', exMode: d.mode ?? 'auto', exSide: d.shell_side ?? 'inward', exShell: d.shell ?? 3, exWall: d.min_wall ?? 0.8,
           exStand: d.stand ?? 'auto', exBase: 100 * (d.base ?? 0.02), exUp: d.up ?? 'auto' };
}
// Browser storage: the contents and formats last chosen (mg.export, for every scan), and each scan's
// placement and print settings (mg.export.<scan>). The same on every page.
function saved() { try { return JSON.parse(localStorage.getItem('mg.export') || '{}'); } catch { return {}; } }
function savedFields() { try { return JSON.parse(localStorage.getItem('mg.export.' + scan) || '{}'); } catch { return {}; } }
function save() {
  const items = Object.fromEntries(EX_CONTENTS.map((c) => [c.id, { on: $('exOn-' + c.id).checked, format: $('exFmt-' + c.id).value }]));
  try {
    localStorage.setItem('mg.export', JSON.stringify({ items }));
    localStorage.setItem('mg.export.' + scan, JSON.stringify(Object.fromEntries(Object.keys(EX_FIELDS).map((id) => [id, $(id).value]))));
  } catch {}
}
function options() {
  const o = {};
  for (const [id, key] of Object.entries(EX_FIELDS)) {
    const val = $(id).value;
    if (val !== '') o[key] = $(id).type === 'number' ? (key === 'base' ? Number(val) / 100 : Number(val)) : val;
  }
  return o;
}
const exUrl = (file) => `/api/workspaces/${enc(scan)}/exports/${enc(file)}?t=${Date.now()}`;

// Each row says whether its file is ready (exporting it again takes no time) or outdated (it is made again).
let checkTimer = 0;
function check() {
  clearTimeout(checkTimer);
  checkTimer = setTimeout(async () => {
    const items = chosen(), name = scan;
    EX_CONTENTS.forEach((c) => { const el = $('exSt-' + c.id); if (el) el.textContent = ''; });
    if (!items.length) return;
    try {
      const r = await post(`/api/workspaces/${enc(name)}/export/status`, { items, source: $('exSource').value, options: options() });
      if (name !== scan) return;
      for (const it of r.items) {
        const el = $('exSt-' + it.content); if (!el) continue;
        el.className = 'ex-state ' + it.state;
        el.textContent = it.state === 'ready' ? `ready · ${exSize(it.size)}` : it.state === 'outdated' ? 'outdated: made again' : '';
        el.title = it.state === 'ready' ? 'already made from the files as they are now, with these settings' : it.state === 'outdated' ? 'made earlier, from older files or other settings' : '';
      }
    } catch { /* the status is a hint: Export still works */ }
  }, 250);
}
function renderItems() {
  const custom = $('exSource').value === 'custom', have = new Set(st.exportable?.[$('exSource').value] || []);
  const was = saved().items || {}, prev = Object.fromEntries(EX_CONTENTS.map((c) => [c.id, $('exOn-' + c.id)?.checked]));
  const first = EX_CONTENTS.find((c) => have.has(c.id))?.id;
  $('exItems').innerHTML = EX_CONTENTS.map((c) => {
    const ok = have.has(c.id), why = custom && c.pipelineOnly ? 'only in the pipeline version' : c.needs;
    const fmt = was[c.id]?.format in c.formats ? was[c.id].format : Object.keys(c.formats)[0];
    const on = ok && (prev[c.id] ?? was[c.id]?.on ?? c.id === first);
    return `<div class="ex-item${ok ? '' : ' off'}"><input type="checkbox" id="exOn-${c.id}"${on ? ' checked' : ''}${ok ? '' : ' disabled'}>
      <label for="exOn-${c.id}">${c.label}${exStale(c, custom, st) ? ' <span class="stale" title="an earlier stage was re-run since">stale</span>' : ''}
        <span class="ex-state" id="exSt-${c.id}"></span><small>${ok ? c.about : why}</small></label>
      ${Object.keys(c.formats).length < 2 ? `<span class="ex-one">${c.formats[fmt]}<input type="hidden" id="exFmt-${c.id}" value="${fmt}"></span>`
        : `<select id="exFmt-${c.id}"${ok ? '' : ' disabled'}>${Object.entries(c.formats)
        .map(([v, l]) => `<option value="${v}"${v === fmt ? ' selected' : ''}>${l}</option>`).join('')}</select>`}</div>`;
  }).join('');
  EX_CONTENTS.forEach((c) => {
    $('exOn-' + c.id).addEventListener('change', fields);
    $('exFmt-' + c.id).addEventListener('change', () => { save(); check(); });
  });
  fields();
}
function chosen() {
  return EX_CONTENTS.filter((c) => $('exOn-' + c.id)?.checked).map((c) => ({ content: c.id, format: $('exFmt-' + c.id).value }));
}
function fields() {
  const on = chosen().map((i) => i.content), placed = on.some((c) => c !== 'cameras');
  $('exPrint').hidden = !on.includes('print');
  $('exUp').closest('label').hidden = $('exHeight').closest('label').hidden = !placed;
  $('exOrient').parentElement.hidden = $('exHeightHelp').hidden = !placed;
  $('exHeight').placeholder = on.includes('print') ? `as scanned (print: ${$('exHeight').dataset.print || 100})` : 'as scanned';
  const shellish = $('exMode').value !== 'solid';
  $('exSide').closest('label').hidden = !shellish; $('exShell').closest('label').hidden = !shellish;
  $('exBuild').disabled = !on.length;
  $('exBuild').textContent = on.length > 1 ? `Export ${on.length} as a zip` : 'Export';
  check();
}
function reset() {
  $('exStatus').textContent = ''; $('exIssues').innerHTML = ''; $('exFiles').innerHTML = ''; $('exDownload').hidden = true;
}

async function exportNow() {
  save();
  const items = chosen(), opts = options(), name = scan;
  $('exBuild').disabled = true; reset();
  $('exStatus').textContent = items.some((i) => i.content === 'print') ? 'exporting (the print is rebuilt as a solid: about half a minute)…' : 'exporting…';
  const t0 = Date.now();
  try {
    const r = await post(`/api/workspaces/${enc(name)}/export`, { items, source: $('exSource').value, options: opts });
    if (name !== scan) return;
    $('exDownload').href = exUrl(r.file); $('exDownload').hidden = false; $('exDownload').textContent = `Download ${r.file}`;
    const p = r.files.find((f) => f.content === 'print')?.info || {};
    $('exStatus').textContent = `${exSize(r.size)} · ${r.cached ? 'already made, reused' : Math.round((Date.now() - t0) / 1000) + ' s'}`
      + (p.print_size_mm ? ` · print ${p.print_size_mm.join(' × ')} mm · ${p.print_watertight ? 'watertight' : 'NOT watertight'}` : '')
      + (p.print?.mode ? ` · as ${p.print.mode}` : '');
    if (r.bundle) $('exFiles').innerHTML = '<div class="ex-sec">In the zip (or one by one)</div>' + r.files.map((f) =>
      `<a href="${exUrl(f.file)}" download>${esc(f.label)} · ${EX_CONTENTS.find((c) => c.id === f.content).formats[f.format]}<span>${exSize(f.size)}</span></a>`).join('');
    $('exIssues').innerHTML = (r.issues || []).map((d) => `<div class="diag ${d.level}">${esc(d.message)}${d.hint ? `<span class="h">→ ${esc(d.hint)}</span>` : ''}</div>`).join('');
    // a dense cloud can be hundreds of MB: too much for the browser, it is only downloaded
    built = r.files.filter((f) => (f.preview || ['glb', 'stl', 'ply'].includes(f.format)) && (f.preview || f.size < 150e6));
    $('exPick').innerHTML = built.map((f, i) => `<option value="${i}">${esc(f.label)}</option>`).join('');
    $('exPick').hidden = built.length < 2;
    api(`/api/workspaces/${enc(name)}/state`).then((s) => {  // the print check, the rows' state
      if (name === scan) { st = s; check(); }
      hooks.exported?.(name, s);
    }).catch(() => {});
    if (built.length) await preview(built[0]);
    else preview(null, r.files.length > 1 ? 'nothing in this export can be shown here' : 'a zip of several files: no preview');
  } catch (e) { if (name === scan) $('exStatus').textContent = 'failed: ' + e.message; }
  fields();
}

// The 3D view of an exported file. three.js, its loaders, the projection and the view cube are
// loaded the first time, so pages that never show one (Help, About) don't load them.
async function preview(f, why) {
  const box = $('exPreview');
  if (!f) { $('exEmpty').hidden = false; $('exEmpty').textContent = why; if (view?.mesh) { view.mesh.visible = false; view.draw(); } return; }
  const THREE = await import('three');
  if (!view) {
    const [{ OrbitControls }, { projection }] = await Promise.all([import('three/addons/controls/OrbitControls.js'), import('/projection.js')]);
    const r = new THREE.WebGLRenderer({ antialias: true }); r.setPixelRatio(Math.min(devicePixelRatio, 2));
    r.outputColorSpace = THREE.SRGBColorSpace; box.appendChild(r.domElement);
    const sc = new THREE.Scene(); sc.background = new THREE.Color(0x0f1115);
    const cam = new THREE.PerspectiveCamera(40, 1, 0.01, 1e5);
    sc.add(new THREE.HemisphereLight(0xffffff, 0x404050, 1.5)); const sun = new THREE.DirectionalLight(0xffffff, 1.6);
    cam.add(sun); sun.position.set(1, 2, 1); sc.add(cam);
    const ctl = new OrbitControls(cam, r.domElement);
    view = { r, sc, cam, ctl, mesh: null, draw: () => { const w = box.clientWidth, h = box.clientHeight; if (!w || !h) return; r.setSize(w, h, false); view.proj.resize(w / h); r.render(sc, view.proj.camera); } };
    view.proj = projection(cam, ctl, sun, () => { view.draw(); view.cube?.update(); });
    ctl.addEventListener('change', view.draw);
    new ResizeObserver(view.draw).observe(box);
    import('/viewcube.js').then(({ addViewCube }) => { view.cube = addViewCube(box, { projection: view.proj, controls: ctl, render: view.draw, fit: () => view.frame?.() }); })
      .catch(() => {});
  }
  $('exEmpty').hidden = true;
  if (view.mesh) { view.sc.remove(view.mesh); view.mesh = null; }
  const file = f.preview || f.file, url = exUrl(file), ext = file.split('.').pop();
  let obj;
  if (ext === 'stl' || ext === 'ply') {
    const { STLLoader } = await import('three/addons/loaders/STLLoader.js');
    const { PLYLoader } = await import('three/addons/loaders/PLYLoader.js');
    const geo = await new (ext === 'stl' ? STLLoader : PLYLoader)().loadAsync(url);
    const colours = !!geo.attributes.color;
    if (ext === 'ply' && !geo.index) {  // a point cloud
      const size = (geo.computeBoundingBox(), geo.boundingBox.getSize(new THREE.Vector3()).length());
      obj = new THREE.Points(geo, new THREE.PointsMaterial({ size: size / 600, vertexColors: colours, color: colours ? 0xffffff : 0xd0d4da }));
    } else {
      geo.computeVertexNormals();
      obj = new THREE.Mesh(geo, new THREE.MeshStandardMaterial({ color: 0xd0d4da, roughness: .8, vertexColors: colours }));
    }
    if (ext === 'stl') obj.rotation.x = -Math.PI / 2;  // slicers are Z-up, the preview Y-up
  } else {
    const { GLTFLoader } = await import('three/addons/loaders/GLTFLoader.js');
    obj = (await new GLTFLoader().loadAsync(url)).scene;
    obj.traverse((o) => { if (o.isMesh && o.material?.map) o.material = new THREE.MeshBasicMaterial({ map: o.material.map }); });
  }
  view.sc.add(obj); view.mesh = obj;
  view.frame = () => {
    const b = new THREE.Box3().setFromObject(obj), size = b.getSize(new THREE.Vector3()).length(), c = b.getCenter(new THREE.Vector3());
    view.ctl.target.copy(c); view.cam.near = size / 200; view.cam.far = size * 20; view.cam.updateProjectionMatrix();
    view.cam.position.copy(c).add(new THREE.Vector3(0.25, 0.2, 1).normalize().multiplyScalar(size * 1.35)); view.cam.lookAt(c);
    view.proj.framed(); view.draw();
  };
  view.frame();
}
