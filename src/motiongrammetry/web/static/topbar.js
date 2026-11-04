// The bar across the top of every page: the one place that names the pages and links them, so a
// page is renamed here and nowhere else. Each page has an empty <header class="topbar"> and calls
// mountTopbar('<its id>') before its own code runs; topbar.css styles it.
//
// The bar has three parts: the pages (left), the page's own status line (topStatus, filled by
// the page), and the current scan (the chip on the right, then the GitHub link). The chip is the
// scan chooser (scanchooser.js): the list of scans, New scan… and each scan's ⋯ menu. Beside it,
// Export… opens the Export window (export.js) for that scan, on every page; each scan's ⋯ menu has
// Export… too, for that scan.
// Importing scrollbar.js gives every page the app's own scrollbar on all its scroll areas.

import '/scrollbar.js';
import { initChooser, chooserScan } from '/scanchooser.js';
import { openExport } from '/export.js';
export { updateScans, uploadVideo, thumbUrl } from '/scanchooser.js';

export const PAGES = [
  { id: 'scan', label: 'Scan', href: '/', title: 'run the pipeline on a scan, preview it and export it' },
  { id: 'edit', label: 'Edit', href: '/edit', title: 'sculpt the clean mesh and paint the texture' },
  { id: 'help', label: 'Help', href: '/help', title: 'the full how-to' },
  { id: 'about', label: 'About', href: '/about', title: 'the open-source software it builds on, and their licenses' },
];
export const pageLabel = (id) => PAGES.find((p) => p.id === id).label;

const REPO = 'https://github.com/jpagesbri/motiongrammetry';
const LAST = 'mg.scan';   // browser storage: the scan last opened on Scan or Edit, for Help and About
const store = {
  get: () => { try { return localStorage.getItem(LAST); } catch { return null; } },
  set: (v) => { try { v ? localStorage.setItem(LAST, v) : localStorage.removeItem(LAST); } catch {} },
};
// Pages that hold a scan open (Scan, Edit) open Help and About in a new tab, so nothing is lost.
const HOLDS_SCAN = new Set(['scan', 'edit']);
const HELP_ANCHOR = { edit: '#edit' };

let scan = null;

const esc = (t) => String(t).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

/** Render the bar into the page's <header class="topbar">. `id` is one of the PAGES ids.
 *  `hooks` tell the scan chooser what the page does with a scan (see scanchooser.js):
 *  open(name) when one is picked, created(name) after New scan… or Duplicate…, deleted(name, scans)
 *  after Delete…, actions(scan) for extra items in a scan's ⋯ menu (Export… is added after them on
 *  every page). Without them (Help, About), a
 *  picked scan is only remembered, for the Scan and Edit links: these pages are read beside a working
 *  one, so they stay put; a new or copied scan opens on the Scan page, where it is run. */
export function mountTopbar(id, hooks = {}) {
  const bar = document.querySelector('header.topbar');
  const newTab = HOLDS_SCAN.has(id) ? ' target="_blank"' : '';
  const links = PAGES.map((p) => p.id === id
    ? `<a class="on" aria-current="page" title="${esc(p.title)}">${esc(p.label)}</a>`
    : `<a data-page="${p.id}" href="${p.href}${p.id === 'help' ? HELP_ANCHOR[id] || '' : ''}"` +
      `${p.id === 'help' || p.id === 'about' ? newTab : ''} title="${esc(p.title)}">${esc(p.label)}</a>`).join('');
  bar.innerHTML = `<b>motiongrammetry</b>
    <nav class="screens">${links}</nav>
    <span class="topstatus muted" id="topStatus"></span>
    <button class="scanchip none" id="topScan" aria-haspopup="dialog" aria-expanded="false" title="choose a scan, or make a new one"><span class="k">scan</span><span class="v" id="topScanName">choose…</span><span class="caret" aria-hidden="true">▾</span></button>
    <button class="topexport" id="topExport" disabled title="choose a scan first">Export…</button>
    <a class="gh" href="${REPO}" target="_blank" rel="noopener" title="motiongrammetry on GitHub" aria-label="motiongrammetry on GitHub"></a>`;
  initChooser(document.getElementById('topScan'), {
    open: (name) => { store.set(name); showScan(name, true); },
    created: (name) => { location.href = '/#' + encodeURIComponent(name); },
    ...hooks,
    actions: (w) => [...(hooks.actions?.(w) || []),
      { label: 'Export…', title: `take files out of ${w.name}: the textured model, the clean mesh, a 3D print, …`, run: () => openExport(w.name) }],
    deleted: (name, scans) => { forgetScan(name); if (!HOLDS_SCAN.has(id) && scan === name) showScan(null, true); hooks.deleted?.(name, scans); },
  });
  document.getElementById('topExport').addEventListener('click', () => openExport(scan));
  // Help and About have no scan of their own: they show the one last opened (or ?scan=).
  if (!HOLDS_SCAN.has(id)) showScan(new URLSearchParams(location.search).get('scan') || store.get(), true);
}

/** The page's current scan (null: none). Shown in the chip and kept in the Scan / Edit links. */
export function setScan(name) {
  showScan(name, false);
  if (name) store.set(name);
}

/** A scan was deleted: Help and About stop offering it. */
export function forgetScan(name) {
  if (store.get() === name) store.set(null);
}

function showScan(name, remembered) {
  scan = name || null;
  const chip = document.getElementById('topScan');
  chooserScan(scan);
  chip.title = (!scan ? 'no scan open' : remembered ? `the scan you last opened: ${scan}` : `the scan open on this page: ${scan}`)
    + ': click to choose another, or make a new one';
  const ex = document.getElementById('topExport');
  ex.disabled = !scan;
  ex.title = scan ? `take files out of ${scan}: the textured model, the clean mesh, a 3D print, …` : 'choose a scan first';
  for (const a of document.querySelectorAll('header.topbar a[data-page="scan"], header.topbar a[data-page="edit"]')) {
    const p = PAGES.find((x) => x.id === a.dataset.page);
    a.href = p.href + (scan ? '#' + encodeURIComponent(scan) : '');
  }
}
