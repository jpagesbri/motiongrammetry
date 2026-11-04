# The web UI, feature by feature

For how to use it, open **Help** in the app: a walkthrough with screenshots of every screen. This page
is the reference of what each part does and how it is implemented.

```bash
mg ui                       # http://127.0.0.1:8765, workspaces in ./workspaces
mg ui --root ~/scans --port 9000
```

- **Top bar** (`static/topbar.js`, styled by `topbar.css`): the same on every page, written by one
  module that names and links the pages (**Scan**, **Edit**, **Help**, **About**: renaming one is
  one line there) and the GitHub repository. The current scan sits apart on the right, as a chip
  marked "scan" (name only). Help and About have no scan of their own and show the one last
  opened on Scan or Edit (browser storage, `mg.scan`), or none; the Scan and Edit links keep that
  scan. Between the pages and the chip, the page's status line (the run on Scan, unsaved edits on
  Edit).
- **Scan chooser** (`static/scanchooser.js`, opened by the chip on every page): the scans with a
  thumbnail, the stages done (dots), what they hold, the last run (or "running"), and a larger card
  on hover. Picking one hands it to the page through `mountTopbar(id, hooks)`: Scan selects it,
  Edit switches to it (through the URL's `#scan`, which asks first about unsaved edits), Help and
  About only remember it for their links, since they are read beside a working page. At its foot,
  **New scan…** (name, local video path or upload, as the old left bar's form). Each scan's **⋯**
  menu has **Duplicate…** and **Delete…** on every page, plus **Upload a video…** / **Replace the
  video…** on the Scan page (the page adds its own items). A new or copied scan opens on the Scan
  page. Keyboard: arrows, Enter, → / ← for a scan's menu, Esc; a click outside closes it. The full
  scans and workspaces page (#24) will get a "Manage scans…" entry at the foot.
- **Export, on every page** (`static/export.js`, styled by `export.css`): the Export window is one
  module that every page uses. The top bar has **Export…** beside the scan chip (disabled when no
  scan is chosen: Help and About use the scan last opened), and each scan's **⋯** menu has
  **Export…** for that scan. On the Scan page, the Preview's Export panel and the assisted run's last
  step open the same window. It reads the scan's `/state` afresh each time it opens, so it is right
  on any page.
  - A scan with nothing to export yet gets a short window that says so, with **Go to Run** (on the
    Scan page it switches to that scan's Run tab; elsewhere it links to `/?tab=run#scan`).
  - On Edit, with edits not saved yet, the window says at the top that the export is made from the
    saved files only and leaves out what changed since the last save. It never exports the editor's
    unsaved state.
  - A page tells it what it can do through `exportHooks({ unsaved, orient, run, exported })`: Edit
    gives `unsaved` (sculpt edits, paint); Scan gives `orient` (fix the orientation in the Preview),
    `run` and `exported` (refresh the Export panel's print check). Without `orient`, "fix the
    orientation…" links to the Scan page (`/?open=orient#scan`).
  - The 3D preview loads three.js, its loaders, `projection.js` (the Persp / Ortho switch, shared
    with the Scan preview and the editor) and the view cube only when it first shows a file, so Help
    and About load them only then (each page has the import map).
  - The choices are remembered in this browser as before: contents and formats (`mg.export`) and
    each scan's placement and print settings (`mg.export.<scan>`).
- **Scrollbars** (`static/scrollbar.js`, styled by `scrollbar.css`; `topbar.js` imports it, so
  every page has it): the app draws its own scrollbar, thin and in the app's colours, the same in
  every browser, on every scroll area (the Scan page's panels, the editor's side panel, Help's text
  and contents, About's text and license boxes, the scan chooser's list, the windows). The thumb
  brightens when the pointer is over the area and more over the bar; it can be dragged, and a click
  on the track scrolls a page towards the pointer (held: more pages). The browser still does the
  scrolling (wheel, trackpad, touch, keys, scrolling to the focused element).
  - `scrollbar.css`, linked in each page's `<head>`, hides the browser's bars from the first paint
    (`scrollbar-width: none`, `::-webkit-scrollbar`) on everything except text fields and
    `data-scroll="native"`, and turns off the overscroll bounce (`overscroll-behavior: none`).
  - **The document never scrolls.** Scan and Edit are full-window layouts; Help and About are
    `<body class="paged">`: the top bar, then a `.page` box that scrolls (`topbar.css`). The top bar
    never moves, and the page's scrollbar starts under it. `.page` takes the focus on load, so Page
    Down and Space scroll it at once.
  - Scroll areas are found by themselves: any element whose CSS overflow is `auto` or `scroll`, at
    load or added later, gets a bar, so a new panel needs nothing more.
  - Each bar is a `position: fixed` element inside its area (`.mgsb`), placed over its right and
    bottom edges and clipped to what of the area shows. Inside: whatever covers the area covers the
    bar, and a click on it is a click inside the area. Fixed: it takes no place in the layout (grids,
    flex rows, gaps, sticky elements are untouched).
  - It is measured again on content changes (MutationObserver, text included), size changes
    (ResizeObserver on the area and its children), images loading, window resizes, scrolling of an
    outer area, when the pointer reaches the bar, and at every step of a drag, so the end of the
    track is always the end of the content. It is hidden from screen readers; the area itself stays
    scrollable by keyboard.
- **Tabs and modes** (`static/modes.js`, styled by `modes.css`, linked in the page's `<head>`): one
  control for every switch between views, so all pages look and behave the same. On Scan: **Run** /
  **Preview**, **Assisted** / **Manual**, **3D model** / **Frames & masks**, the log's **Live** /
  **History**; on Edit: **Sculpt mesh** / **Paint texture** and the view's **3D** / **3D + texture**
  / **Texture**. (Options of one view, such as the frame viewer's Photo / Overlay / … or the density's
  Relative / Absolute, stay small `.seg` button rows: they change a setting, not what is shown.)
  - One control, two looks. `.tabs`, a page's own tabs (Run / Preview on Scan, Sculpt mesh / Paint
    texture on Edit): words, the chosen one underlined in the accent colour, always in a `.pagebar`,
    the bar across the page's work area under the top bar, the same place on every page. It never
    scrolls away (the editor's side panel scrolls, so the tabs are not in it), and a page can add its
    own items after the tabs (Scan: the orientation note, side by side). The default, a mode inside a page: a segmented control, the
    chosen option raised and underlined. `.wide` shares the row. Both behave the same.
  - The page writes buttons with `data-value` in a `.modes` element and calls
    `modeControl(el, { onSelect })`, which returns `select(v)`, `all(on)` (side by side: both tabs
    shown selected) and `disable(v, off)`.
  - A WAI-ARIA tab list (`role="tablist"`, `role="tab"`, `aria-selected`, Run / Preview with
    `role="tabpanel"` panes), with a roving tabindex: one Tab stop per control, ← → (↑ ↓) to the next
    usable option, Home / End, selection follows the focus. Those keys stop at the control, so the
    page's own shortcuts on them (← → / Home / End step through the keyframes) don't fire as well.
  - The choice is remembered where it was: the tab (`mg.gtab`), the Preview's view (`mg.pv`) and
    side by side in browser storage, Assisted / Manual per scan (`mg.mode.<scan>`); the editor's mode
    in the URL (`?mode=paint`, kept on reload and when switching scans). Switching the editor's mode
    keeps both modes' unsaved edits; switches wait for each other, as a mode loads its mesh the first
    time.
- **About** (`/about`): the third-party software, AI models and Python packages the app uses, each
  with its license text (`credits.py`: Python packages are read from the installed distributions,
  the rest is listed there with texts in `licenses/`), what those licenses ask (VGGT-1B weights are
  CC BY-NC 4.0, OpenMVS is AGPL-3.0 and runs unmodified in its own container, NVIDIA's CUDA
  libraries come with PyTorch), and a THIRD_PARTY_NOTICES download; `mg notices -o FILE` writes the
  same file for redistribution.
- **Help** (`/help`): the full how-to with screenshots: filming, every screen and stage, every
  option (read live from `options.py`), presets, outputs, printing, editing and troubleshooting.
- **Scan** (`/`) has two tabs (the tabs and modes control; side by side on wide screens): **Run** and **Preview** (the 3D model, the keyframes and masks, the files). A dot on a tab means
  something new there; nothing switches tabs on its own.
- **Run tab**: one layout for both modes. On the left the steps, which are the pipeline (status,
  time, progress bar, a mark when there are warnings or advice); on the right the selected step's
  page (what it does, the run in progress with its sub-steps, the result, warnings and advice, its
  options); the log in a drawer at the bottom (opens by itself on a failure). Options are kept per
  scan (browser storage) and shared by both modes; a value set by the assistant, a preset or advice
  says why. Manual adds tick boxes on the steps, **Run selected** / **Check**, every option on each
  page and an **All steps** overview with each stage's result.
- **Assisted** (Run tab): a guided run. It asks what was filmed and how, picks starting options with
  the reason for each, then goes one stage at a time explaining every option; after each stage
  `advice.py` reads the results (keyframes, mask coverage, registered cameras, how much clean carved
  and how sure the masks were, ...) and offers fixes with "apply & re-run". While a step runs it
  shows the sub-steps, the latest result, the log on request and Stop (switching to Manual, the
  Preview or another scan never stops a run). The last step, **Place your model**, checks the
  orientation. It remembers where it was per scan (browser storage).
- **Scans** (a scan is one video and everything made from it; on disk a folder under
  `workspaces/`): listed in the scan chooser with a thumbnail (a snapshot of the 3D view taken when
  a run produces a mesh, a keyframe until then), keyframes, faces and last activity.
- **Manual** (Run tab) and the scan chooser: create a scan from a local video path or an upload, tick the stages to run,
  pick a preset, set basic or advanced options (generated from `options.py`), **Check** (pre-flight
  only), run and cancel (stages run as `mg` subprocesses). Shows the disk the depth maps will need.
  When a scan has both an MVS and a VGGT mesh, **Clean from** picks which one is carved.
- **Duplicate** (a scan's ⋯ menu): a new scan with the same keyframes and masks (optionally the SfM/MVS
  results too; copy-on-write on btrfs/XFS) to try other settings or VGGT without touching the
  original. VGGT writes its own cameras, so running it where SfM cameras exist fails pre-flight
  with a pointer to this (or `force`).
- **All steps** (Manual): one card per stage with live progress and the sub-step it refers to (e.g.
  `DensifyPointCloud: Estimated depth-maps 12`), timings and the key numbers from the manifest
  (registered frames, reprojection error, faces, which raw mesh was cleaned, ...). Outputs older
  than their inputs are flagged **stale** and their layers hidden, since they no longer line up
  with the cameras. **re-run from here** ticks that stage and the ones depending on it, staying on
  one path (MVS or VGGT) so the two are never mixed.
- **Failures**: a banner with the failing stage, the error, its hint and a link to the log;
  warnings and errors are listed on each stage card.
- **Log** (drawer): live stage output (COLMAP internals hidden by default), and a History tab with every
  stage run and the OpenMVS tool logs, each viewable or downloadable.
- **Export** (Preview tab): what the scan has ready (stale results flagged), the print check and
  the folder on disk.
- **Export…**: tick the contents and pick each one's format: textured model (GLB, OBJ + MTL + PNG),
  clean mesh with vertex colours (GLB, PLY, OBJ), 3D print (STL or 3MF; height, solid / shell /
  auto, which way the shell grows and how thick, minimum wall, stand, flat base), raw mesh (PLY,
  OBJ, GLB), dense point cloud (PLY) and cameras (the COLMAP model). From the pipeline or the
  custom version (raw mesh, cloud and cameras only from the pipeline), with orientation and a
  real-world height shared by everything (GLB in metres, the others in millimetres; the cameras
  stay in the reconstruction's frame). One content is one file, several are one zip with a folder
  each. Shows each built file in 3D and, for prints, the report. Choices are remembered.
- **Edit** (`/edit`): sculpt the clean mesh (draw, dig, inflate, deflate, smooth,
  flatten, pinch, grab, decimate (edge collapses inside the stroke down to the Density ÷k,
  applied on release, open edges and the stroke's border locked, no folds), cut away, with an option
  to cap what a cut opens; **Adaptive detail** (tick box) splits edges around each dab first, up to the
  Density ×k (1.5–8, default 2: k times the vertices the mesh has at that place when the stroke
  starts, each vertex's mean edge then; vertices added during the stroke inherit it, so one stroke
  counts once and a new one applies the factor again; or, with the **Absolute** switch, a target
  for both brushes as a share of the scan's typical density (13–800%; from the scan's typical edge, `scan_edge` from the server), which strokes
  converge on; at most 50,000 new vertices per stroke), by longest-edge bisection with the neighbours split too (no cracks), so
  the brushes always have vertices to shape; fill + merge to add
  volume, e.g. close the hole of a ring shape: by default only the region around the fill spheres
  is rebuilt (`meshops.local_merge`: the union surface at the mesh's own edge length, cut along the
  region's border and stitched to the untouched mesh; openings the spheres partly cover are taken
  in whole), or the whole mesh at a chosen voxel size; a plane cut
  that closes flat; close all holes; remove loose pieces; **restore**: brush over the raw mesh,
  shown as an amber ghost, to bring back what clean carved away; clean only deletes raw vertices,
  so restored triangles weld in seamlessly) and paint the texture on the model or on the flat atlas (brush,
  restore original, blur, smudge, clone, lighten, darken, saturate, desaturate, colour picker; clone
  on the model copies by place on the surface; **Rebuild layout** re-cuts the texture by the model's
  sides with an angle and a minimum piece size, or **Edit seams…** with the seams drawn on the
  model, cut along mesh edges and joined with a brush; see `atlas.py`; after a sculpt, **Texture
  my edits** runs the texture stage on the edited mesh and **Carry the texture over** transfers the
  old texture to it by nearest point, `atlas.transfer`; before either, and in Paint, the saved mesh
  is checked for what breaks or spoils texturing - pillows, bow-tie vertices, non-manifold edges,
  degenerate faces, pieces that flatten to no area, flipped faces - with how much of the mesh and
  the fix; **Sanitize mesh…** in Sculpt checks the mesh being edited and runs the fixes ticked, each
  with a time estimate for this machine and face count, as one undo step: see `meshfix.py`),
  with undo. Edits are saved as a **custom version** in `mesh/custom/` and never change the
  pipeline's files: a sculpt writes `custom/mesh_clean.ply` (its exports are made when asked); the
  texture stage then projects the photos onto the custom mesh (when it is newer than the clean
  mesh) into `custom/textured.ply`, which you can paint; painting the pipeline's textured model copies
  it into `custom/` first. Export offers both versions, the viewer has a **Custom
  (edited)** layer, and **Delete custom version** goes back to the pipeline's.
- **Keyframes**: thumbnail strip with optional mask overlay; click one to view from its camera,
  double-click to open it in the frame viewer.
- **Frames & masks** (Preview tab): every keyframe full size (`/image/<frames|masks>/<file>`) as
  photo, mask overlay, a compare slider, side by side or mask alone, zoom and pan, arrow keys. Each
  says how sharp it is against its neighbours, the mask's share of it and whether SfM placed it
  (`/frame-stats`, cached); **Flagged** steps through the ones worth a look (blurrier than the
  neighbours, no camera, an empty mask or one that jumps in size).
- **Orientation** (Preview → **Orient…**, or the assisted run's last step): click the spot that rests
  on the floor, turn in 90° steps, "this view is the front", free rotate / move handles, centre,
  drop to floor. Saved as `orientation.json` (the SfM → Y-up transform plus the SfM model it was set
  against: re-running SfM makes it lapse). `workspace_alignment(up="auto")` returns it, so every
  export uses it; exports made with an older one are outdated and made again when asked. Meshes
  stay in the SfM frame; nothing re-runs.
- **3D preview** (three.js): camera frusta, sparse points, dense cloud, raw / clean / textured
  mesh, all in one Y-up frame. **Display** (`W`): colours, solid grey, wireframe, or solid + wires
  (fading out where triangles are under a few pixels); **Highlight holes** draws the open edges in
  orange and counts the openings (also in the editor).
- **View cube** (`static/viewcube.js`, in the Scan preview, the Export preview and the editor):
  the camera's direction with Z-up labels as in the print files; click a face, edge or corner to
  look from there; a button frames the model, another (or key `5`) switches perspective /
  orthographic. Point clouds travel as packed binary (f32 xyz + u8 rgb, sampled
  server-side to ≤600k), meshes as cached GLB, all frusta are a single draw call, and the view
  renders only on interaction (draw-call / triangle counter bottom-right).

Needs no extra Python packages; three.js and three-mesh-bvh are bundled in `static/vendor/`, so it works offline.
