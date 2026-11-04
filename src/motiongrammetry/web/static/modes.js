// The one control for tabs and modes: wherever a page switches between views (Scan: Run / Preview,
// Assisted / Manual, 3D model / Frames & masks, the log's Live / History; Edit: Sculpt mesh / Paint
// texture, 3D / 3D + texture / Texture), it is this control, styled by modes.css, so every page looks
// and behaves the same.
//
// The page writes the options as buttons in an element of class "modes", each with data-value:
//   <div class="modes tabs" aria-label="Run or Preview"><button data-value="run">Run</button>…</div>
// and calls modeControl(el, { onSelect }). One control, two looks (modes.css): "tabs" for a page's
// own tabs (Run / Preview, Sculpt / Paint: underlined words in a bar), the default for a mode inside
// a page (a segmented control); "wide" fills the row. Both behave the same.
//
// It is a WAI-ARIA tab list: role="tablist", each option role="tab" with aria-selected, one of them
// in the Tab order (roving tabindex). Arrow keys (← → and ↑ ↓) move to the next option that is not
// disabled and select it, Home / End to the first and last; selection follows the focus, as each view
// shows at once. The keys stop here, so a page's own shortcuts on the same keys (← → step through the
// keyframes) don't also fire. Remembering the choice is the page's: it knows where (browser storage
// per scan, the URL), and gives it back with select().

/** Make `el` (class "modes", buttons with data-value) a tab list.
 *  onSelect(value) runs when the person picks an option (click or keys), not when the page calls
 *  select(). Returns { el, get value, select(value), all(on), disable(value, off) }. */
export function modeControl(el, { onSelect = () => {}, label } = {}) {
  const opts = () => [...el.querySelectorAll(':scope > button[data-value]')];
  let value = null, every = false;
  el.setAttribute('role', 'tablist');
  if (label) el.setAttribute('aria-label', label);
  for (const b of opts()) { b.type = 'button'; b.setAttribute('role', 'tab'); }

  function paint() {
    const list = opts();
    // the focusable option: the selected one, else the first that can be used
    const focusable = list.find((b) => b.dataset.value === value && !b.disabled) || list.find((b) => !b.disabled);
    for (const b of list) {
      const on = every || b.dataset.value === value;
      b.setAttribute('aria-selected', on ? 'true' : 'false');
      b.tabIndex = b === focusable ? 0 : -1;
    }
    el.toggleAttribute('aria-multiselectable', every);
  }
  function pick(v) {
    if (v == null) return;
    const changed = v !== value;
    value = v; paint();
    onSelect(v, changed);
  }
  el.addEventListener('click', (e) => {
    const b = e.target.closest('button[data-value]');
    if (b && b.parentElement === el && !b.disabled) pick(b.dataset.value);
  });
  el.addEventListener('keydown', (e) => {
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    const list = opts().filter((b) => !b.disabled);
    const at = list.indexOf(document.activeElement);
    if (at < 0) return;
    const go = { ArrowRight: at + 1, ArrowDown: at + 1, ArrowLeft: at - 1, ArrowUp: at - 1, Home: 0, End: list.length - 1 }[e.key];
    if (go === undefined) return;
    e.preventDefault(); e.stopPropagation();
    const b = list[(go + list.length) % list.length];
    b.focus();
    pick(b.dataset.value);
  });

  return {
    el,
    get value() { return value; },
    /** Show `v` as selected (the page changed it, or gives back what it remembered). */
    select(v) { value = v; paint(); },
    /** Show every option selected (Run and Preview side by side), or back to one. */
    all(on) { every = !!on; paint(); },
    /** An option that can't be used now (Edit: Paint before there is a texture). */
    disable(v, off) {
      const b = opts().find((x) => x.dataset.value === v);
      if (b) { b.disabled = !!off; paint(); }
    },
  };
}
