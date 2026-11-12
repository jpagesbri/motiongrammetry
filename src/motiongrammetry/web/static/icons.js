// The app's icons: Tabler Icons (MIT), vendored in vendor/tabler-icons/ with only the icons used;
// its README says which version they come from and how to add one (copy the SVG, add its name here).
//
//   import { icon } from '/icons.js';
//   button.innerHTML = icon('eye');
//
// icon(name) returns the icon as inline SVG markup: 1em square (CSS can set another size) and drawn
// in currentColor, so it takes the text's size and colour; hidden from screen readers (the button
// that holds it carries the label).

const NAMES = ['eye', 'help-circle', 'clock-exclamation', 'player-skip-back', 'player-track-prev', 'player-play', 'player-pause',
  'player-track-next', 'player-skip-forward', 'rotate-3d'];

const svgs = Object.fromEntries(await Promise.all(NAMES.map(async (name) => {
  try {
    const r = await fetch(`/vendor/tabler-icons/${name}.svg`);
    return [name, r.ok ? await r.text() : ''];
  } catch { return [name, '']; }  // no icon rather than no page
})));

export function icon(name) {
  const svg = svgs[name];
  if (svg === undefined) { console.warn(`icon '${name}' is not vendored: see vendor/tabler-icons/README.md`); return ''; }
  return svg.trim().replace(/\s+(width|height|class)="[^"]*"/g, '').replace(/\s*\n\s*/g, ' ')
    .replace('<svg', `<svg class="icon icon-${name}" width="1em" height="1em" aria-hidden="true" focusable="false"`);
}
