// The one way the Scan page shows a warning, a suggestion or a result: Assisted's advice at every step,
// Manual's stage cards and step pages, the failure banner and the pre-flight check. A note is
//
//   [icon] Short title            (at most 5 words)
//   one short line                (at most 12 words)
//   [figure]                      (only where it explains something: the camera path)
//   [buttons]                     (1-3 words each, verb first)
//   [confirmation]                (in the page: what a button will do, before it does it)
//   ▸ Details                     (closed: the full message, hint, numbers, option names)
//
// Short texts come from the server: diagnostics carry `title` and `line` (diagnostics.SHORT, and
// the numbers of the case), advice items `short`, `line`, `action` and `changes` (advice.py).
//
//   import { noteHtml, diagNote, diagShort, confirmHtml } from '/notes.js';

const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const LEVEL = { fix: 'warn', warning: 'warn', error: 'bad', tip: 'info', info: 'info', ok: 'ok' };
const ICON = { warn: '⚠', bad: '✕', info: 'ℹ', ok: '✓' };

let SHORT = {};  // code -> [title, line], for diagnostics stored before they carried their own
export function setShortForms(table) { SHORT = table || {}; }

export function diagShort(d) {
  const [title, line] = SHORT[d.code] || [String(d.code || 'problem').replace(/-/g, ' ').replace(/^./, (c) => c.toUpperCase()), ''];
  return { title: d.title || title, line: d.line || line };
}

// The full text of diagnostics, for a note's Details.
export function diagDetails(diags) {
  return diags.map((d) => `<div class="diag ${d.level}">${esc(d.message)}${d.hint ? `<span class="h">→ ${esc(d.hint)}</span>` : ''}</div>`).join('');
}

export function noteHtml({ level = 'info', title, line = '', figure = '', buttons = '', extra = '', details = '', id = '', compact = false, attrs = '' }) {
  const lv = LEVEL[level] || 'info';
  return `<div class="note ${lv}${compact ? ' compact' : ''}"${id ? ` id="${id}"` : ''} ${attrs}>
    <div class="note-head"><span class="note-ico" aria-hidden="true">${ICON[lv]}</span><b class="note-title">${esc(title)}</b></div>
    ${line ? `<div class="note-line">${esc(line)}</div>` : ''}${figure}
    ${buttons ? `<div class="btns">${buttons}</div>` : ''}${extra}
    ${details ? `<details class="note-more"><summary>Details</summary><div class="note-body">${details}</div></details>` : ''}</div>`;
}

export function diagNote(d, opts = {}) {
  const s = diagShort(d);
  return noteHtml({ level: d.level, title: s.title, line: s.line, details: diagDetails([d]), ...opts });
}

// The in-page confirmation: a title (at most 6 words), up to 3 rows, one warning line, two buttons.
export function confirmHtml({ title, rows = [], warning = '', yes, no = 'Cancel' }) {
  return `<b class="nc-title">${esc(title)}</b>
    ${rows.slice(0, 3).map(([k, v]) => `<div class="nc-row"><span>${esc(k)}</span><span>${v}</span></div>`).join('')}
    ${warning ? `<div class="nc-warn">⚠ ${esc(warning)}</div>` : ''}
    <div class="btns"><button class="btn primary" data-nc="yes">${esc(yes)}</button><button class="btn" data-nc="no">${esc(no)}</button></div>`;
}
