# Tabler Icons (vendored)

The app's icons come from [Tabler Icons](https://tabler.io/icons) (MIT, see `LICENSE`), outline
style: 24 × 24, drawn with a 2 px stroke in `currentColor`. Only the icons the app uses are here,
copied unchanged, so the app works offline.

- Version: **@tabler/icons 3.49.0** (npm), files from `icons/outline/<name>.svg` in the package.
- Used through `static/icons.js`: `icon('eye')` returns the inline SVG, sized 1em, in the text colour.

| Icon | Tabler name | Used for |
|---|---|---|
| `eye.svg` | `eye` | Export window: preview a ready file |
| `help-circle.svg` | `help-circle` | Top bar: the page's help (`pagehelp.js`) |
| `clock-exclamation.svg` | `clock-exclamation` | Preview layers: a layer made before an earlier step changed |

## Adding an icon

1. Find it on https://tabler.io/icons (outline), and copy `icons/outline/<name>.svg` from the same
   package version into this folder (`npm pack @tabler/icons@3.49.0`, or
   `https://cdn.jsdelivr.net/npm/@tabler/icons@3.49.0/icons/outline/<name>.svg`).
2. Add its name to `NAMES` in `static/icons.js`, and a row to the table above.

Then `icon('<name>')` works on every page.
