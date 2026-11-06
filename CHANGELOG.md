# Changelog

Versions follow [semantic versioning](https://semver.org): while it is 0.x, a minor version
(0.2.0) can change behaviour or options; a patch version (0.1.1) only fixes things.

To release: set `version` in `pyproject.toml` (the only place it is written), move the
Unreleased notes under a heading with that version and the date, commit, then
`git tag -a vX.Y.Z -m "motiongrammetry X.Y.Z"` and push the tag. `./install.sh update` builds
the image with that version, and the About page and `mg --version` show it.

## Unreleased

- **sfm says plainly when the cameras collapse onto each other** (part of #49): an SfM run can put
  many cameras on top of each other (seen with `--mask-sfm` on an orbit video: 60 cameras at 23
  positions, at a normal 0.57 px error). The camera-path check reported it as dozens of path breaks
  with absurd multiples (×538416…) and doorway advice. Now, when at least a quarter of the steps
  between keyframes are ~0 (under 1/2000 of the usual distance of the cameras from their centre),
  it gives one warning, `cameras-collapsed`: "N of M cameras sit on top of each other (only K
  distinct positions): SfM did not recover the motion", with advice that fits: not a filming
  problem; `mask_sfm` is for turntables (camera fixed); run SfM without it, or with the incremental
  mapper and exhaustive matching. The assisted advice offers that re-run, `mg check-cameras`
  says the same (exit 2), `--strict` stops the pipeline.
  For an ordinary break, the advice now says to try the incremental mapper with exhaustive matching
  first (it fixed the break on the orbit scan, where the defaults broke 3 runs out of 3), and the
  assisted run offers that re-run; filming again comes after.
- **A check on the camera path in sfm** (part of #49): an SfM can place every keyframe at
  sub-pixel error and still be wrong as a whole, with part of a walk placed far away or a room
  built twice, where neighbouring views shared too little. `mg sfm` now follows the cameras in
  keyframe order and warns, on the stage card, in the log and in the assisted run's advice, when
  the camera moved over 10× the median step from one keyframe to the next
  (`camera-path-jump`, naming the keyframes and the multiple) or when cameras sit far from all the
  others (`cameras-far-away`), with capture advice: walk slowly through doorways, keep part of the
  room you are leaving in view while you turn, no quick pans or close-ups in passing; for an
  object, a steady orbit. `--strict` makes it stop the pipeline. `mg check-cameras WORK` runs the
  check on a scan's existing SfM model, reading only. Help's troubleshooting and filming tips say
  the same.
- **New masks outdate an SfM that used them** (part of #14): with `mask_sfm` (`--mask-sfm`, the
  turntable preset), SfM matches features inside the masks only, yet re-running the masks left it,
  MVS and the meshes marked up to date. Now the cameras count as stale when the masks changed after
  an SfM that used them, and so does everything after: the stage cards, `mg check`, the Export
  window's stale marks, pre-flight ("the masks changed after sfm ran with them") and **re-run from
  here** on the masks, which now ticks SfM and MVS too, as does the assisted advice to try another
  mask model. An SfM run without masks is still left alone by new masks (only clean and texture).
- **Help on every page** (part of #23): a **?** button at the right of the top bar on Scan, Edit
  and About, or the `?` key, opens a short help for what the page shows now (Scan: Assisted, Manual,
  the 3D model or Frames & masks; Edit: Sculpt or Paint), with **Read more in Help →** to the
  matching section. Esc closes it. The pages' hints have one small, muted look; the ones shown every
  time (the Preview's key reminders, the keyframe strip's, All steps' intro, "only Stop does", the
  texture layout's) can be closed with their ✕, and the help window brings them back. The editor's
  long mouse and keys text at the foot of its side panel moved into that window.
- **Export from any page** (part of #23): **Export…** is in the top bar next to the scan, on Scan,
  Edit, Help and About, and in each scan's ⋯ menu. It opens the same Export window as before (the
  contents, formats, sizes, downloads and remembered settings), which the assisted run's last step
  still opens too. A scan with nothing to export yet says so and points to Run. On Edit, with
  unsaved edits, the window says that it exports the saved files only. The Preview tab's Export side
  panel is gone: the 3D view takes the whole width (the orientation tools still open in a column on
  the right while you orient). What the panel showed is in the window: what the scan has ready and
  its folder on disk at the foot, and the 3D print check under the 3D print row. Its "Open in
  editor" button is gone too: use Edit in the top bar. A ready file has an eye next to it that shows
  that file in the window's 3D view at once, without making it again (not for outdated files, nor
  for OBJ, cameras or a cloud too big to show). The eye is the first of the app's icons from Tabler
  Icons (MIT), bundled with the app.

- **One control for tabs and modes** (part of #23): Run / Preview, Assisted / Manual, 3D model /
  Frames & masks and the log's Live / History on the Scan page, and Sculpt mesh / Paint texture and
  3D / 3D + texture / Texture on the Edit page, are one control. Sculpt mesh / Paint texture are now
  tabs like Run / Preview, in the same bar under the top bar (no longer in the side panel, where
  they scrolled out of sight); the modes inside a page are a
  row of options with the chosen one raised and underlined. The keyboard works on all of
  them (Tab to reach one, ← → to switch, Home / End), and screen readers hear them as tabs. The
  editor's mode is kept in the address (`?mode=paint`), so a reload stays in Paint.

- **The app draws its own scrollbars** (#53): every scroll area on Scan, Edit, Help and About, and
  the scan chooser's list, has the same thin scrollbar in the app's colours, in every browser,
  instead of the browser's own (wide and light in Chrome on Linux, different in Firefox). The thumb
  brightens on hover and can be dragged; clicking the track scrolls by a page. The wheel,
  trackpad, touch and keyboard scroll as before. The top bar never moves: on Help and About the
  text scrolls under it, and there is no overscroll bounce any more.

- **Choose the scan from the top bar** (part of #23): the scan chip at the top right opens the
  list of scans on every page, with thumbnails, the stages done and the last run. Picking one
  opens it on the Scan page, switches the editor to it (asking first about unsaved edits), or, on
  Help and About, makes their Scan and Edit links lead to it. The Scan page's left bar and the
  editor's own scan dropdown are gone, so both pages are wider. **New scan…** is at the foot of
  the list. Each scan has a **⋯** menu with **Duplicate…** and **Delete…** (on every page) and, on
  the Scan page, **Upload a video…** / **Replace the video…**. The New scan window only makes new
  scans: uploading with no name typed no longer puts the video into the selected scan (that is
  the scan's own Upload / Replace the video now).

- **Pages renamed, one top bar** (part of #23): **Generate** is now **Scan** and **Edit & paint**
  is now **Edit**, in the top bar, the buttons, the advice, Help and the docs. The top bar is
  written once (`topbar.js`) for every page instead of being copied into each. The current scan's
  name no longer trails the page names as "· shoe": it has its own chip on the right, marked
  "scan". Help and About show the scan you last opened there, and their Scan and Edit links go
  back to it.

- **Closing holes round a stray face makes no pillow**: a face (or a short strip of faces) left
  inside a hole, joined to the rest only at its corners, was taken for a hole of its own. Close
  all holes and the Cut stroke's cap gave it a lid that was the same face turned over: a pillow,
  which crashes texturing, and counted it as a closed hole. Now that stray face gets no lid: when
  the hole round it is closed it is removed and the lid covers its spot, and the editor says so.
  Sanitize's Close holes no longer counts it as a hole either. A face on its own, touching
  nothing, is left as it is. Ordinary holes close exactly as before.

- **Holes at a busy vertex are found and closed**: finding the holes gave up after turning
  through 64 faces around one vertex. A hole whose rim passes a vertex with more faces than that
  (the pole of a fine sphere has 80; a fan can have thousands) was not found, so Close holes,
  Close all holes and Cap left it open. Now it works whatever the number of faces, at the same
  speed (about 0.45 s on a 2M-face mesh). Holes it already found come out exactly the same.

- **Hand-drawn seams and solidify skip NumPy's slow hash**: NumPy 2.3+ answers `np.isin` and a
  plain `np.unique` with a hash table, about 20x slower than sorting on large integer keys. #40
  fixed this for the mesh check. Now the texture layout's hand-drawn seams, the print solidify's
  open-edge test, and Restore / the voxel merge sort instead. With seams on a 1M-face mesh, the
  seam lookup takes 0.35 s instead of 0.94 s. On a 1M-face partial scan, the open-edge test
  takes 0.37 s instead of 0.49 s. Results are exactly the same.

- **Close holes is faster**: Sanitize mesh's Close holes (and the editor's Close all holes / Cap)
  sorted each lid's triangles into inside and outside one at a time in Python, with a numpy call
  per triangle edge. A hole with a fine rim gets a lid of up to 80,000 points, so on the 1M-face
  test mesh that was ~480,000 calls for two holes. It is now done in one pass over all triangles.
  Close holes takes about 2 s instead of 4-4.7 s on that mesh (2M faces once fixed) and 0.3 s
  instead of 0.9 s on a 240k-face one. The lids come out exactly the same.

- **Saving keeps the right face of a pillow**: a "pillow" is one triangle stored twice, wound both
  ways. The editor's save and the clean stage dropped one copy but kept the first, and after editing
  that is often the copy wound against its neighbours, so the saved mesh had flipped faces (the mesh
  check before Paint / Texture my edits reported them). They now keep the copy wound like its
  neighbours, as Sanitize mesh does; texturing an older mesh drops the same faces.
- **One lock file**: `uv.lock` was out of date (version 0.1.0, no scipy) and nothing used it; CI and
  the install guide use `requirements-lock.txt`. It is removed and ignored, so `uv run` no longer
  leaves a changed file behind.
- **Version of a checkout**: run from a git checkout, `mg --version` and the About page add the
  branch (`0.2.0-fix-some-bug`, or `-main` past a release), so a local instance says which code
  it runs; installed copies and the Docker image show the plain version.
- **GitHub icon** in the top bar: it was drawn from a mistyped copy of the logo (a misshapen ear
  and head); it is now GitHub's own mark, kept once in `topbar.css` (#20).
- **CONTRIBUTING.md and a pull request template**: a branch per change, a local instance to try
  it, and a list of what the approver has to validate before the merge.
- **Mesh check and Sanitize mesh**: Paint, **Texture my edits** and **Carry the texture over** now
  check the mesh first and say what would break or spoil texturing (opposite-winding "pillow"
  faces, bow-tie vertices, non-manifold edges, degenerate faces, pieces that flatten to no area,
  flipped faces), how much of the mesh it is and the fix (#12). The sculpt editor's new
  **Sanitize mesh…** shows what the check found and repairs what you tick: degenerate and
  duplicate faces, non-manifold parts, small loose pieces, orientation and normals, holes up to a
  size, unreferenced vertices, and reports self-intersections. Each fix shows how long it takes on
  this machine for this mesh; they run as one undo step and say what they changed (#29).
- **Mesh check, faster**: on a million-face mesh the check takes about 6 s instead of 27 s, and
  removing degenerate and duplicate faces 1–2 s instead of 19 s, with the same results.
  NumPy 2.3+ finds distinct values with a hash table, which is much slower than sorting on these
  edge keys; the check now sorts, and compares faces by one number each instead of by rows.
- **Sanitize mesh, Close holes**: a small "hole" whose rim ran around a flap of faces folded across
  it was closed with a lid that repeated the flap the other way. That left pillows and an edge with
  three faces, so the next run took them out, reopened the hole and closed it the same way again,
  and the saved mesh came out with flipped faces. Now the flap goes with its mirror in the lid: one
  run closes the hole, and running the fixes again changes nothing.
- **Self-intersections shown on the model**: Sanitize mesh… listed *Detect self-intersections* among
  the fixes, with a checkbox, though it never changed anything. It is now a line under the fixes:
  how many faces cut through others and in how many spots, and **Show them**, which marks them in
  red on the model and takes the view from spot to spot (◀ ▶, the largest first; Hide or Esc, or
  any edit, takes the marks away), so you can smooth or carve the ones that show as a crease.
- **Upload video…**: with no name typed it replaced the selected scan's video without asking, and
  with an existing scan's name typed it silently replaced *that* scan's video. Now it asks before
  replacing, refuses a name that is taken, and the server refuses a replacement nobody confirmed or
  one during a run; a failed upload leaves the old video whole, and the upload it replaces is
  removed (part of #25).
- **Run times**: a running step showed **1h 00** from the start, and a time left of many hours,
  whenever the browser's time zone differed from the server's (the container runs in UTC). Steps
  are now timed against the server's clock, and OpenMVS run in Docker logs in the host's zone (#21).
- **Old edited meshes and texturing**: a mesh saved before 0.2.0 could still hold duplicate
  ("pillow") or degenerate faces, and "Texture my edits" then died in OpenMVS. The texture step now
  leaves those faces out (from a cleaned copy; the mesh itself is untouched), the pre-flight check
  says how many, and if OpenMVS still fails on broken geometry (`std::out_of_range`, as an abort
  or a segfault) the error names it and says how to fix it (#1, #2).
- **Tests on every pull request**: a GitHub Actions workflow runs the tests on pull requests into
  `main` and after each merge, on the locked versions, and checks they import the checked-out
  code; the tests themselves now stop if they would import another installed copy (#9).
- **3D preview after a run**: a mesh made while the preview was open was cut off when approached,
  and moving around felt wrong until a reload: the view kept the clipping distances and framing of
  what it first showed (the sparse points). The clipping planes now follow the camera, and the view
  is framed again when a re-run SfM moves the scene or the first mesh appears (#22).
- **Adaptive detail and Decimate: Density instead of Detail.** The Detail slider was a share of the
  brush radius, so it depended on the zoom: most of its range did nothing on a normal scan, and 3%
  multiplied the vertices by 10–30. The new **Density** control is a factor of the mesh's density
  where you brush, as it is when the stroke starts: ×1.5 to ×8, default ×2, the same at any zoom or
  brush size. A coarse part gets the same factor of its own density, and Decimate uses the control
  the other way (÷2 is half the vertices). One stroke counts once, however often it passes over an
  area; a new stroke applies the factor again, also after the other tool (×1.5 after ÷4 adds about
  half to what Decimate left). The **Absolute** switch sets a target instead (13% to 800% of the scan's
  typical density) that both brushes aim at, so an area can take as many strokes as it needs. Measured on the shoe, orbit and her_face, the result is within about 25% of the factor. Adaptive detail now works
  only on the surface under the brush (not the other side of a thin part), and one stroke adds at
  most 50,000 vertices and says so. It no longer pauses about half a second each time it adds
  vertices on a dense mesh. It also no longer leaves small cracks, which the save then cut out with
  the faces around them (#13, #4).
- **Texture layout packing**: Rebuild layout, Apply seams and Carry the texture over pack the
  pieces by their outlines instead of their enclosing rectangles, so they nest into each other's
  hollows. The image is about half texture instead of a bit over a third (her_face: 38% to 51%,
  5502x5277 to 4667x4668 at the same detail), so rebuilt textures are smaller (#5).
- **Textures split over several images** (OpenMVS splits one larger than 8192 px): the viewer, the
  editor and GLB exports showed every face with the first image; now each image goes on its own
  faces (and paint goes to the right one). Rebuild layout and Carry the texture over read them
  too, and write one image, lowering the detail if it would pass 8192 px, which the message
  says (#7).
- **Carry the texture over onto closed holes**: new surface (a lid, a merged volume) took the colour
  of the nearest old point, so the shoe's closed sole showed its insole through it, ringed with
  streaks stretched from the rim. New surface now takes a smooth blend of the colours around it,
  fading in from the old surface with no edge (#6).
- **Browser tests** (#8): `tests/browser/` drives the pages in headless Chromium (Playwright, in
  the `dev` extra) against `mg ui` on two small scans built at test time: the top bar and the scan
  chooser on every page, the tabs and modes and what they remember, scrolling at several window
  sizes, the Export window (a real GLB download, the eye, nothing to export, unsaved edits), and the
  editor (a sculpt stroke and undo, Adaptive detail, Decimate, clone, seams, the texture layout,
  carrying the texture over). A test fails on any page error or console error. GitHub runs them on
  every pull request, with a trace of each failure; locally they skip until the browser is
  installed (`.venv/bin/playwright install chromium-headless-shell`, see CONTRIBUTING.md).

### Fixed

- **The page's keys after closing the help or Export window**: the help window opened with the `?`
  key, or the Export window opened from a scan's ⋯ menu, left the focus on its hidden ✕ when closed
  with Esc, and kept every key from the page until a click (the editor's tool keys, Ctrl+Z, `W`, the
  arrows). Closing either window now gives the page its keys back at once. (Found by the new browser
  tests.)
- **Edit seams… keeps the piece count**: after Ctrl+Z, and sometimes after a stroke, the line under
  the seam tools lost its "N pieces ·" and showed only the seam edges, as the seams redrawn a frame
  later wrote over it. (Found by the new browser tests.)
- **`mg clean --no-masks` keeps the scene, not a few stray triangles** (part of #49): without masks,
  clean kept the piece with the largest area. A badly placed camera leaves a few huge triangles far
  from the rest, and on a walk through a flat those 7,165 faces won over the 1.87 million-face flat
  itself. Clean now keeps the piece with the most faces, where the depth maps agree. On object scans
  run without masks it now keeps the subject: on two of three test scans the largest piece was
  background behind it. With masks, the small pieces kept are measured against that piece too,
  and a piece of a few large triangles (large area, few faces) is dropped. Object scans cleaned with
  masks come out the same.
- **`mg export --out` into a folder that isn't there yet** (#50): `--out exports/` wrote a file
  named `exports`, and the next export replaced it without a word. Now a trailing `/`, an existing
  folder, or a new name without an extension is a folder: it is made (with its parents) and each
  export goes inside under its own name (`work-print.stl`). A new name with an extension is the
  file, with its missing folders made, and a warning if the extension isn't the export's. A file
  that is already there is still replaced, but the last line says so (`-> … (replaced the file
  that was there)`), and it always names the file written. A path that can't be a folder (inside a
  file) is a clear error before the export starts, not a traceback. `mg export --help` lists the
  rules.
- **`mg export-2dgs --out` no longer deletes a folder that isn't its dataset**: it replaced
  whatever folder `--out` named, as a whole. It now refuses a folder that holds other files (no
  `run_2dgs.sh` from an earlier export), and a file.

## 0.2.0 — 2026-11-02

- **Export**: pick the contents to take out (textured model, clean mesh, 3D print, raw mesh,
  dense point cloud, cameras) and each one's format; several come as one zip. New formats:
  3MF for prints, OBJ for the clean and raw meshes, the dense cloud as PLY and the COLMAP
  camera model. The Preview's file list is replaced by a short summary and the print check.
- **Editor, clone on the model**: copies by place on the surface instead of by texture offset,
  which jumped to unrelated texels at every texture seam; strokes keep their alignment until a
  new source is picked.
- **Editor, texture layout**: **Rebuild layout** cuts the texture again by the model's sides
  (normals on a smoothed copy, an angle and a minimum piece size), flattens the pieces (LSCM),
  packs them and copies the colours across: a scan's 20 000 tiny pieces become a few hundred
  whole parts.
- **Editor, seams by hand**: **Edit seams…** shows the texture's seams on the model; **Cut**
  draws new ones along the mesh's edges, **Join** brushes them away, and **Apply seams** rebuilds
  the layout with exactly those pieces (one that cannot lie flat is cut further, and says so).
  The pieces show in colours as you draw; **Preview** / **Preview texture** show the new texture
  at low resolution without saving; **Undo layout** goes back; the editor updates in place.
- **Editor, mesh density**: **Adaptive detail** (tick box) adds vertices around each dab where
  the mesh is too coarse to shape (longest-edge bisection, no cracks; the stroke and its new
  vertices undo together), and the **Decimate** brush (E) merges edges shorter than the Detail
  size where you brush, without folds, keeping open edges. Saving a sculpt takes about a second.
- **Exports on request**: the pipeline and the editor keep only their working files; GLB, OBJ,
  STL, 3MF, ... are made when asked (Export window, new `mg export`) and reused until what they
  come from changes (then marked outdated). Clean runs and sculpt saves are about a minute
  faster, orientation changes save at once. The print check comes from the last print export;
  the scan's print options are the Export window's defaults.
- **Editor, texture after sculpting**: paint mode offers **Texture my edits** (the texture step on
  the edited mesh, run from the editor) and **Carry the texture over** (no photos: a layout for the
  edited mesh coloured from the nearest point of the old texture; removed pieces go, closed holes
  take the colour around them; also for VGGT scans).
- **Docker**: the container is named after the project, `motiongrammetry` (or
  `motiongrammetry-<folder>` for another data folder), instead of Compose's
  `motiongrammetry-motiongrammetry-1`; a container under the old name is replaced on its next
  start, with no rebuild.
- **Texture my edits on a sculpt**: saving a sculpt that leaves two faces on the same three
  vertices, traversed both ways (a degenerate two-face "pillow"), made the texture step fail with
  `TextureMesh failed (exit -6)` / `std::out_of_range` in OpenMVS's seam leveling. Open3D's
  duplicate-triangle cleanup compares ordered triples, so it kept such pairs; the mesh cleanup
  and the editor now drop them before the mesh is written.
- **Carrying a texture over**: the same degenerate pair made the new layout collapse (an 8119x17
  image, every piece a sliver, nothing painted, `nan%` reported) and the model came out blank. A
  piece that flattens to no area is no longer scaled up by millions, and a layout that paints
  almost none of its image (under 5%) is refused instead of replacing a good texture with a blank
  one; long, thin objects, whose layouts are long and thin too, are not mistaken for it.

## 0.1.0 — 2026-10-31

The first version.

- **Pipeline**: keyframes chosen by camera motion, subject masks (BiRefNet, IS-Net, U²-Net),
  camera positions with COLMAP 4 and GLOMAP, dense mesh and texture with OpenMVS, cleanup by
  carving with the masks, optional VGGT path. Checks before each stage and quality gates after
  it, with hints; logs and run history per scan. Presets, including one for drone footage of
  large structures.
- **Web interface**: Run tab with Assisted (a guided run that explains each step and suggests
  fixes) and Manual modes; Preview with the 3D model, display modes, hole highlighting, a view
  cube with perspective and orthographic views, and a frame viewer for keyframes and masks;
  orientation tool (click the floor, turn, set the front); Export to STL, GLB, OBJ and PLY at a
  chosen size; duplicate and delete scans; About page with every component's license; Help with
  a worked example.
- **Editor**: sculpt brushes, cut, plane cut, fill and local merge, restore from the raw mesh,
  close holes with lids that follow their rim, remove loose pieces, and texture painting.
  Edits are saved as a custom version next to the pipeline's files.
- **3D printing**: watertight print files rebuilt on a voxel grid, solid or shell, with a
  stand and flat base when needed and a report of what to check.
- **Install**: one Docker image with CUDA builds for NVIDIA GPUs (CPU fallback), installed and
  updated with `./install.sh`; settings remembered per data folder; VGGT as an opt-in layer.
- **License**: Apache-2.0.
