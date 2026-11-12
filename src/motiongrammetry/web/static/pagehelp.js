// Help on every page (Scan, Edit, About; the Help page is the help): a ? button in the top bar, and
// the ? key, open a short window about the page and what it shows now (Scan: Run's Assisted or
// Manual, the Preview's 3D model or Frames & masks, both in side by side; Edit: Sculpt or Paint). It
// says what that view's options do, in a few items, and links to the matching section of Help. The
// text is here, as data (VIEWS), and nowhere else; Help has the long version, so keep the two in step.
//
//   import { pageHelp, hint } from '/pagehelp.js';
//   mountTopbar('scan', …);
//   pageHelp('scan', () => ['scan-manual', 'scan-3d']);   // the views shown now, from VIEWS
//
// Hints on the pages share one look (pagehelp.css): small and muted, class "pghint". A hint that shows
// every time (key reminders, intros) gets data-hint="<id>" and a ✕ that hides it in this browser
// (mg.hints.off); what it said is in this window, which can bring the closed hints back. hint(id, html)
// writes one for markup a page renders itself.
//
// Esc closes the window, and the page's shortcuts get no keys while it is open (as in the Export
// window). pagehelp.css styles it, and the button; the icon is Tabler's help-circle (icons.js).

import { icon } from '/icons.js';

// Each page: its name in the window's title, and a line on what the page is for.
const PAGES = {
  scan: { title: 'Scan', intro: 'Runs the pipeline on the scan chosen at the top right (<b>Run</b>) and shows the results (<b>Preview</b>). On a wide screen, <b>side by side</b> shows both. <b>Export…</b> in the top bar takes files out.' },
  edit: { title: 'Edit', intro: 'Edits are saved as a <b>custom version</b> of the scan; the pipeline\'s files are never changed. <b>Sculpt mesh</b> changes the shape, <b>Paint texture</b> the colours: sculpt → save → texture the edit → paint.' },
  about: { title: 'About', intro: '' },
};

// Each view: what it is called, what its options do (a few items, HTML), and its section of Help.
export const VIEWS = {
  'scan-assisted': { page: 'scan', label: 'Run · Assisted', more: '#assisted', items: [
    'One step at a time: what the step does, its main options with the value suggested for your capture and why. The rest are under <i>all options of this step</i>.',
    '<b>Run this step</b>, then <b>Continue</b>. After each step it reads the result and may suggest a change, with <b>Apply &amp; re-run</b>.',
    '<b>Preset</b>: named sets of options for common capture problems. The options are kept per scan and shared with Manual.',
    'Switching mode, tab or scan never stops a run: only <b>Stop this step</b> does (click twice).',
    'The last step, <b>Place your model</b>, checks that it stands the right way before you export.',
  ] },
  'scan-manual': { page: 'scan', label: 'Run · Manual', more: '#run', items: [
    'The steps on the left: green done, blue running, orange out of date, red failed. Click one for its page: the result, its warnings and its options.',
    'Tick steps and press <b>Run selected</b>; <b>Check</b> runs the pre-flight checks only. <b>Tick this and later steps</b> picks a step and all that depend on it.',
    '<b>All steps</b>: every step\'s result and warnings, and the general options.',
    'An outlined option is your own change; a value set for a reason (what you filmed, a preset, advice) says why.',
    'The <b>Log</b> drawer at the bottom: the live output and the history of every run. It opens by itself when a step fails.',
  ] },
  'scan-3d': { page: 'scan', label: 'Preview · 3D model', more: '#viewer', items: [
    '<b>Layers</b> (top left): cameras, sparse points, dense cloud, raw mesh, clean mesh, textured model and your custom version. The most advanced one shows; a dimmed one with a clock is outdated: point at it for why.',
    'Left- or right-drag: turn. Middle-drag or <kbd>Space</kbd>+drag: pan. Wheel: zoom (as in the editor). Click a keyframe below to look through its camera; double-click it to open it big.',
    '<b>Keyframes</b>: the player steps through them (first, previous, play / pause, next, last); <kbd>←</kbd> <kbd>→</kbd> step too, <kbd>Home</kbd> / <kbd>End</kbd> first and last. Play shows 2 a second, stops on the last and pauses when you move the view. A keyframe SfM didn\'t place has no camera: the view stays.',
    'The spheres at the bottom of Layers pick the display (<kbd>W</kbd> cycles): colours, solid grey, wireframe, solid + wires; the shown one has a ring.',
    '<b>View options</b> (in Layers, click to open): <b>Point size</b>, while sparse points or the dense cloud show, and <b>Highlight holes</b>, which draws every opening in orange (a blue <i>holes</i> on the folded line says it is on).',
    'The view cube (bottom left): click a face to look straight at it. <b>Persp / Ortho</b> (<kbd>5</kbd>) switches the projection.',
    'The orientation button (at the right of the spheres) sets which way is up and where the origin is; every export uses it.',
  ] },
  'scan-frames': { page: 'scan', label: 'Preview · Frames & masks', more: '#frames', items: [
    '<b>Photo</b>, <b>Overlay</b>, <b>Compare</b>, <b>Side by side</b>, <b>Mask</b>: how the keyframe and its mask are shown.',
    '<kbd>←</kbd> <kbd>→</kbd> step through the keyframes (<kbd>Home</kbd> / <kbd>End</kbd>: first and last). Scroll to zoom, drag to pan, double-click to fit.',
    '<b>Flagged</b> steps only through the ones worth a look: blurrier than their neighbours, no camera, or a mask that found nothing or jumps in size.',
    'The bar at the bottom is the subject\'s share of each keyframe; yellow and red ones are flagged.',
    '<b>View from this camera</b> shows the 3D model from that keyframe.',
  ] },
  'edit-sculpt': { page: 'edit', label: 'Sculpt mesh', more: '#sculpt', items: [
    'Left-drag on the mesh: the tool. Off it, or right-drag: turn. Middle-drag or <kbd>Space</kbd>+drag: pan. Wheel: zoom.',
    '<kbd>[</kbd> <kbd>]</kbd> radius · <kbd>Shift</kbd> smooth · <kbd>Ctrl</kbd> invert · <kbd>Ctrl+Z</kbd> / <kbd>Ctrl+Shift+Z</kbd> undo / redo · <kbd>Ctrl+S</kbd> save. Each tool has its key on its button.',
    '<b>Fill</b> places spheres at the depth of the surface around the brush (so it fills a hole you look through); <b>Merge</b> turns mesh + fill into one closed surface. <b>Plane cut</b>: drag a line, check the preview, <kbd>Enter</kbd> to cut. <b>Restore</b>: brush over the amber ghost (the raw mesh) to bring back what clean carved away.',
    '<b>Sanitize mesh…</b> checks for what breaks texturing and fixes what you choose, in one undo step.',
    'Saving makes a custom mesh; its texture is then out of date. In Paint, <b>Texture my edits</b> or <b>Carry the texture over</b> gives the new shape its own.',
  ] },
  'edit-paint': { page: 'edit', label: 'Paint texture', more: '#paint', items: [
    'Left-drag on the model or on the flat texture: paint. <kbd>Alt</kbd>+click: pick a colour. <kbd>Ctrl</kbd>+click: the clone source.',
    '3D: right-drag turn, middle-drag or <kbd>Space</kbd>+drag pan. Flat texture: right- or middle-drag or <kbd>Space</kbd>+drag pan, wheel zoom. <kbd>[</kbd> <kbd>]</kbd> radius · <kbd>Ctrl+Z</kbd> / <kbd>Ctrl+Shift+Z</kbd> undo / redo · <kbd>Ctrl+S</kbd> save.',
    'On the model, paint reaches only surface you can see, and is painted across seams on both sides.',
    '<b>Texture layout</b>: the texture comes cut into thousands of tiny pieces, by the photo each came from. <b>Rebuild layout</b> cuts it by the model\'s sides into fewer, bigger pieces, easy to paint on the flat texture; <b>Edit seams…</b> cuts and joins them by hand.',
    'Re-running texture replaces the paint (<b>Delete custom version</b> goes back to the photo texture).',
  ] },
  about: { page: 'about', label: 'About', more: '#quickstart', items: [
    '<b>This installation</b>: the version (run from a checkout, it names the branch) and where each step runs, on the GPU or the CPU.',
    '<b>Built with</b>, <b>AI models</b>, <b>Content</b> and <b>Python packages</b>: what motiongrammetry builds on, each with its license.',
    '<b>For distributors</b>: what redistributing it involves, and every license text in one file.',
  ], moreLabel: 'Installing and updating, in Help →' },
};

const OFF = 'mg.hints.off';   // browser storage: the hints closed with their ✕
const RETIRED = new Set(['film']);  // hints the pages no longer have: not counted among the closed ones
const offHints = () => { try { return new Set(JSON.parse(localStorage.getItem(OFF) || '[]').filter((id) => !RETIRED.has(id))); } catch { return new Set(); } };
const saveOff = (s) => { try { s.size ? localStorage.setItem(OFF, JSON.stringify([...s])) : localStorage.removeItem(OFF); } catch {} };
const X = '<button type="button" class="pghint-x" title="hide this hint: it stays in this page\'s help (?)" aria-label="hide this hint">✕</button>';

/** A hint a page renders itself: small and muted, with a ✕ that hides it in this browser ('' once hidden). */
export function hint(id, html, tag = 'span') {
  return offHints().has(id) ? '' : `<${tag} class="pghint" data-hint="${id}">${html}${X}</${tag}>`;
}

let page = null, views = null, modal = null, opener = null;
const $ = (id) => document.getElementById(id);

/** Put the help button in the top bar (after mountTopbar) and bind ?. `current()` lists the views
 *  shown now (keys of VIEWS); without it, the page is one view of its own name. */
export function pageHelp(id, current = () => [id]) {
  page = id; views = current;
  const bar = document.querySelector('header.topbar');
  const btn = Object.assign(document.createElement('button'), { className: 'tophelp', id: 'topHelp', type: 'button',
    title: 'help on this page (?)', innerHTML: icon('help-circle') });
  btn.setAttribute('aria-label', 'help on this page');
  btn.setAttribute('aria-haspopup', 'dialog');
  bar.insertBefore(btn, bar.querySelector('a.gh'));
  btn.addEventListener('click', () => openHelp());
  // ? (Shift+/) opens it, unless the focus is in a text field
  addEventListener('keydown', (e) => {
    if (e.key !== '?' || e.ctrlKey || e.metaKey || e.altKey || inText(e.target) || helpOpen() || otherWindow()) return;
    e.preventDefault(); openHelp();
  });
  // hints on the page: a ✕ on each one that can be hidden, and hidden ones stay hidden
  const off = offHints();
  for (const el of document.querySelectorAll('.pghint[data-hint]')) {
    if (!el.querySelector('.pghint-x')) el.insertAdjacentHTML('beforeend', X);
    el.classList.toggle('off', off.has(el.dataset.hint));
  }
  document.addEventListener('click', (e) => {
    const x = e.target.closest('.pghint-x'); if (!x) return;
    const id = x.closest('[data-hint]').dataset.hint, s = offHints();
    s.add(id); saveOff(s);
    document.querySelectorAll(`[data-hint="${id}"]`).forEach((el) => el.classList.add('off'));
  });
}

// another window is open (Export, the scan chooser's, the page's own): ? is not for this page then
const otherWindow = () => !!document.querySelector('.ex-modal:not([hidden]), .sc-modal:not([hidden]), .modal:not([hidden])');
const TEXT_INPUTS = /^(text|search|email|url|tel|password|number|date|datetime-local|month|time|week)$/;
/** Is the focus in a field that takes typed text (a text-like input, select, textarea, editable)? Not a
 *  checkbox, radio, button or slider: keys such as ? or W are the page's there. */
export function inText(t) {
  if (!(t instanceof Element)) return false;
  if (t.isContentEditable || t.matches('textarea, select')) return true;
  return t.matches('input') && TEXT_INPUTS.test(t.type || 'text');
}

/** Is the help window open? */
export const helpOpen = () => !!modal && !modal.hidden;

function build() {
  if (modal) return;
  modal = Object.assign(document.createElement('div'), { className: 'ph-modal', id: 'helpModal', hidden: true });
  modal.setAttribute('role', 'dialog'); modal.setAttribute('aria-modal', 'true'); modal.setAttribute('aria-labelledby', 'phTitle');
  modal.innerHTML = `<div class="ph-win">
      <div class="ph-head"><span class="ph-ico" aria-hidden="true">${icon('help-circle')}</span><b id="phTitle"></b>
        <button class="ph-x" id="phClose" title="close (Esc)" aria-label="close">✕</button></div>
      <div class="ph-body" id="phBody"></div>
      <div class="ph-foot"><span class="muted"><kbd>?</kbd> opens this help · <kbd>Esc</kbd> closes it</span>
        <button type="button" class="ph-hints" id="phHints" hidden></button></div>
    </div>`;
  document.body.append(modal);
  $('phClose').addEventListener('click', closeHelp);
  modal.addEventListener('mousedown', (e) => { if (e.target === modal) closeHelp(); });
  // The page's shortcuts get no keys while it is open; Escape closes it wherever the focus is.
  modal.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeHelp(); e.stopPropagation(); });
  addEventListener('keydown', (e) => {
    if (modal.hidden || modal.contains(e.target)) return;
    e.stopPropagation(); if (e.key === 'Escape') closeHelp();
  }, true);
  $('phHints').addEventListener('click', () => {
    saveOff(new Set());
    document.querySelectorAll('.pghint.off').forEach((el) => el.classList.remove('off'));
    $('phHints').hidden = true;
  });
}

/** Open the help window for the page and what it shows now. */
export function openHelp() {
  build();
  const p = PAGES[page] || { title: page, intro: '' };
  const shown = (views?.() || [page]).filter((v) => VIEWS[v]);
  const newTab = page === 'about' ? '' : ' target="_blank" rel="noopener"';  // Scan and Edit keep their work
  $('phTitle').textContent = `Help · ${p.title}`;
  $('phBody').innerHTML = (p.intro ? `<p class="ph-intro">${p.intro}</p>` : '') + shown.map((v) => {
    const h = VIEWS[v];
    return `<div class="ph-view" data-view="${v}">${h.label !== p.title ? `<h3>${h.label.replace('&', '&amp;')}</h3>` : ''}
      <ul>${h.items.map((i) => `<li>${i}</li>`).join('')}</ul>
      <a class="ph-more" href="/help${h.more}"${newTab}>${h.moreLabel || 'Read more in Help →'}</a></div>`;
  }).join('');
  const off = offHints().size;
  $('phHints').hidden = !off;
  $('phHints').textContent = `Show the ${off === 1 ? 'hint' : `${off} hints`} you closed`;
  if (modal.hidden) opener = document.activeElement;
  modal.hidden = false;
  $('topHelp')?.setAttribute('aria-expanded', 'true');
  $('phClose').focus({ preventScroll: true });
}

export function closeHelp() {
  if (!modal || modal.hidden) return;
  modal.hidden = true;
  $('topHelp')?.setAttribute('aria-expanded', 'false');
  opener?.focus?.({ preventScroll: true }); opener = null;
  // Opened with nothing focused (the ? key), the focus would stay on the hidden ✕, and the window's
  // key handler would keep every key from the page until a click.
  if (modal.contains(document.activeElement)) document.activeElement.blur();
}
