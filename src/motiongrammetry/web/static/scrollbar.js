// The app's own scrollbar, drawn the same in every browser, on every scroll area of every page.
// topbar.js imports this file, so each page that has the top bar gets it; scrollbar.css styles it.
//
// Only the drawing changes: the browser still does the scrolling (wheel, trackpad, touch, keys,
// scrolling to the focused element), and its own bar is hidden (scrollbar.css, on the .mgsb-host
// class this file adds). Each scroll area is found by itself: any element whose computed overflow is
// auto or scroll, now or when it is added later, gets a bar, so a new panel needs no extra work.
// data-scroll="native" on an element keeps the browser's bar there; text fields keep theirs.
//
// A bar is a position: fixed <div class="mgsb"> inside its scroll area, placed over the area's
// right (and bottom) edge. Being inside, it is covered by what covers the area (a window, the scan
// chooser) and counts as inside it for clicks; being fixed, it is out of the layout and does not
// scroll with the content. It is clipped to what of the area is visible. The whole page's own
// scrolling (Help, About) gets one bar too, on the right of the window.

const MIN_THUMB = 24;   // px: the thumb never gets smaller than this
const bars = new Map(); // scroll area -> its bar
let docBar = null;

const TEXT_FIELDS = new Set(['TEXTAREA', 'SELECT', 'INPUT']);
const scrollsOn = (s) => /^(auto|scroll|overlay)$/.test(s);

/** Is `el` a scroll area that gets the app's bar? */
function wants(el) {
  if (el === document.documentElement || el === document.body || TEXT_FIELDS.has(el.tagName)) return false;
  if (el.closest('.mgsb, [data-scroll="native"]')) return false;
  const s = getComputedStyle(el);
  return scrollsOn(s.overflowY) || scrollsOn(s.overflowX);
}

function makeBar(el, isDoc = false) {
  const box = document.createElement('div');
  box.className = 'mgsb' + (isDoc ? ' mgsb-doc' : '');
  box.setAttribute('aria-hidden', 'true');  // the area stays scrollable by keys and screen readers; this is only drawing
  box.innerHTML = '<div class="mgsb-track v"><div class="mgsb-thumb"></div></div><div class="mgsb-track h"><div class="mgsb-thumb"></div></div>';
  const b = { el, box, isDoc, v: axis(box, 'v'), h: axis(box, 'h') };
  for (const a of [b.v, b.h]) wire(b, a);
  return b;
}
const axis = (box, k) => {
  const track = box.querySelector('.mgsb-track.' + k);
  return { k, track, thumb: track.firstChild, len: 0, size: 0, on: false };
};

// ---- geometry ----------------------------------------------------------------------------------

/** The area's padding box on screen, the part of it not clipped away, and where a fixed box lands. */
function placeOf(b) {
  if (b.isDoc) {
    const d = document.documentElement;
    return { x: 0, y: 0, w: d.clientWidth, h: d.clientHeight, clip: null };
  }
  const el = b.el, r = el.getBoundingClientRect();
  if (!r.width && !r.height) return null;
  const x = r.left + el.clientLeft, y = r.top + el.clientTop;
  const w = el.clientWidth || r.width, h = el.clientHeight || r.height;
  // a fixed box escapes its ancestors' clipping: clip it by hand to them and to the window
  let L = Math.max(x, 0), T = Math.max(y, 0), R = Math.min(x + w, innerWidth), B = Math.min(y + h, innerHeight);
  let cbx = 0, cby = 0, cbFound = false;
  for (let p = el.parentElement; p && p !== document.documentElement; p = p.parentElement) {
    const s = getComputedStyle(p);
    if (!cbFound && (s.transform !== 'none' || s.filter !== 'none' || s.perspective !== 'none' ||
        (s.backdropFilter && s.backdropFilter !== 'none') || /paint|layout|strict|content/.test(s.contain) ||
        /transform|filter|perspective/.test(s.willChange))) {
      const q = p.getBoundingClientRect();  // that ancestor, not the window, is where "fixed" is measured from
      cbx = q.left + p.clientLeft; cby = q.top + p.clientTop; cbFound = true;
    }
    if (s.overflowX !== 'visible' || s.overflowY !== 'visible') {
      const q = p.getBoundingClientRect(), px = q.left + p.clientLeft, py = q.top + p.clientTop;
      if (s.overflowX !== 'visible') { L = Math.max(L, px); R = Math.min(R, px + p.clientWidth); }
      if (s.overflowY !== 'visible') { T = Math.max(T, py); B = Math.min(B, py + p.clientHeight); }
    }
  }
  if (R <= L || B <= T) return null;
  return { x: x - cbx, y: y - cby, w, h, clip: [T - y, x + w - R, y + h - B, L - x] };
}

function update(b) {
  const el = b.el;
  if (!b.isDoc && b.box.parentNode !== el) el.append(b.box);  // the area's content was rewritten (innerHTML)
  const sh = el.scrollHeight, ch = el.clientHeight, sw = el.scrollWidth, cw = el.clientWidth;
  const s = b.isDoc ? null : getComputedStyle(el);
  b.v.on = sh > ch + 1 && (!s || scrollsOn(s.overflowY));
  b.h.on = sw > cw + 1 && (!s || scrollsOn(s.overflowX));
  const at = (b.v.on || b.h.on) && placeOf(b);
  b.box.hidden = !at;
  if (!at) return;
  const st = b.box.style;
  st.left = at.x + 'px'; st.top = at.y + 'px'; st.width = at.w + 'px'; st.height = at.h + 'px';
  st.clipPath = at.clip && at.clip.some((c) => c > 0.5) ? `inset(${at.clip.map((c) => Math.max(0, c) + 'px').join(' ')})` : '';
  b.box.classList.toggle('both', b.v.on && b.h.on);
  b.v.track.hidden = !b.v.on; b.h.track.hidden = !b.h.on;
  if (b.v.on) b.v.len = b.v.track.clientHeight;
  if (b.h.on) b.h.len = b.h.track.clientWidth;
  thumbs(b);
}

/** Size and move the thumbs to the area's scroll position (on every scroll). */
function thumbs(b) {
  const el = b.el;
  for (const a of [b.v, b.h]) {
    if (!a.on) continue;
    const view = a.k === 'v' ? el.clientHeight : el.clientWidth;
    const full = a.k === 'v' ? el.scrollHeight : el.scrollWidth;
    const pos = a.k === 'v' ? el.scrollTop : Math.abs(el.scrollLeft);
    a.size = Math.min(a.len, Math.max(MIN_THUMB, a.len * view / full));
    const t = full > view ? pos / (full - view) : 0;
    const off = Math.round((a.len - a.size) * Math.min(1, Math.max(0, t)));
    if (a.k === 'v') { a.thumb.style.height = a.size + 'px'; a.thumb.style.transform = `translateY(${off}px)`; }
    else { a.thumb.style.width = a.size + 'px'; a.thumb.style.transform = `translateX(${off}px)`; }
    a.off = off;
  }
}

// ---- dragging the thumb, clicking the track ------------------------------------------------------

function wire(b, a) {
  const V = a.k === 'v';
  const scrollTo = (v) => b.el.scrollTo({ [V ? 'top' : 'left']: v, behavior: 'instant' });
  const pos = () => (V ? b.el.scrollTop : b.el.scrollLeft);
  a.track.addEventListener('pointerdown', (e) => {
    if (e.button !== 0) return;
    e.preventDefault();  // no focus change, no text selection: the page keeps its keys
    e.stopPropagation();
    a.track.setPointerCapture(e.pointerId);
    b.box.classList.add('drag-' + a.k);
    update(b);  // measure now: the last drawing can be a frame old
    const view = V ? b.el.clientHeight : b.el.clientWidth;
    const full = V ? b.el.scrollHeight : b.el.scrollWidth;
    const at = (ev) => { const r = a.track.getBoundingClientRect(); return V ? ev.clientY - r.top : ev.clientX - r.left; };
    let move, timer = 0;
    if (e.target === a.thumb) {  // drag: the thumb follows the pointer
      const from = at(e), start = pos(), per = (full - view) / Math.max(1, a.len - a.size);
      move = (ev) => scrollTo(start + (at(ev) - from) * per);
    } else {  // the track: a page towards the pointer, then more while held, until the thumb reaches it
      let p = at(e);
      const page = () => {
        const dir = p < a.off ? -1 : p > a.off + a.size ? 1 : 0;
        if (!dir) return 0;
        const was = pos();
        scrollTo(was + dir * view * 0.875); thumbs(b);
        return pos() !== was;  // stop at the end
      };
      move = (ev) => { p = at(ev); };
      if (page()) timer = setTimeout(function again() { if (page()) timer = setTimeout(again, 60); }, 350);
    }
    const end = () => {
      clearTimeout(timer);
      b.box.classList.remove('drag-' + a.k);
      a.track.removeEventListener('pointermove', move);
      a.track.removeEventListener('pointerup', end);
      a.track.removeEventListener('pointercancel', end);
    };
    a.track.addEventListener('pointermove', move);
    a.track.addEventListener('pointerup', end);
    a.track.addEventListener('pointercancel', end);
  });
  a.track.addEventListener('click', (e) => e.stopPropagation());  // a click on the bar is not a click on the page under it
}

// ---- finding the scroll areas, and keeping the bars right ---------------------------------------

function attach(el) {
  if (bars.has(el) || !wants(el)) return;
  const b = makeBar(el);
  bars.set(el, b);
  el.classList.add('mgsb-host');
  el.append(b.box);
  el.addEventListener('scroll', () => thumbs(b), { passive: true });
  sizes.observe(el);
  for (const c of el.children) if (c !== b.box) sizes.observe(c);
  update(b);
}
function scan(root) {
  if (root.nodeType !== 1 || root.closest?.('.mgsb')) return;
  attach(root);
  for (const el of root.querySelectorAll('*')) if (!el.classList.contains('mgsb') && !el.closest('.mgsb')) attach(el);
}

let pending = false, rescan = new Set();
function schedule() {
  if (pending) return;
  pending = true;
  requestAnimationFrame(() => {
    pending = false;
    for (const n of rescan) if (n.isConnected) scan(n);
    rescan.clear();
    for (const [el, b] of bars) {
      if (!el.isConnected) { bars.delete(el); sizes.unobserve(el); continue; }
      for (const c of el.children) if (c !== b.box) sizes.observe(c);  // new content, whose size can change on its own
      update(b);
    }
    if (docBar) update(docBar);
  });
}

const sizes = new ResizeObserver(schedule);
const ours = (n) => (n.nodeType === 1 ? n : n.parentElement)?.closest('.mgsb');

function start() {
  document.documentElement.classList.add('mgsb-root');
  docBar = makeBar(document.scrollingElement || document.documentElement, true);
  document.body.append(docBar.box);
  sizes.observe(document.documentElement);
  scan(document.body);
  update(docBar);
  new MutationObserver((records) => {
    let any = false;
    for (const r of records) {
      if (ours(r.target)) continue;  // the bars' own drawing
      if (r.type === 'childList') {
        const added = [...r.addedNodes].filter((n) => n.nodeType === 1 && !n.classList.contains('mgsb'));
        const removed = [...r.removedNodes].some((n) => n.nodeType === 1 && !n.classList.contains('mgsb'));
        if (!added.length && !removed && r.target.classList?.contains('mgsb-host')) {
          if ([...r.removedNodes].some((n) => n.classList?.contains('mgsb'))) any = true;  // a bar was dropped: put it back
          continue;
        }
        for (const n of added) rescan.add(n);
      } else if (r.type === 'attributes' && r.target.nodeType === 1) {
        attach(r.target);  // a class or style can make an element scroll
      }
      any = true;
    }
    if (any) schedule();
  }).observe(document.body, { childList: true, subtree: true, attributes: true, characterData: true,
    attributeFilter: ['class', 'style', 'hidden', 'open', 'data-scroll'] });
  // the page or a panel scrolled: bars of the areas inside it move with them
  addEventListener('scroll', (e) => {
    const t = e.target;
    if (t === document || t === document.documentElement) { thumbs(docBar); for (const b of bars.values()) update(b); return; }
    for (const [el, b] of bars) if (el !== t && t.contains?.(el)) update(b);
  }, { capture: true, passive: true });
  addEventListener('resize', schedule);
  addEventListener('load', () => { rescan.add(document.body); schedule(); });
  for (const ev of ['transitionend', 'animationend', 'toggle']) addEventListener(ev, schedule, true);
  document.fonts?.ready.then(schedule);
}

/** Look again for scroll areas and redraw every bar (a page can call it after a change the
 *  observers would not see, such as a style sheet swapped in). */
export function refreshScrollbars() { rescan.add(document.body); schedule(); }

if (document.body) start(); else addEventListener('DOMContentLoaded', start, { once: true });
