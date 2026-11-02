# Changelog

Versions follow [semantic versioning](https://semver.org): while it is 0.x, a minor version
(0.2.0) can change behaviour or options; a patch version (0.1.1) only fixes things.

To release: set `version` in `pyproject.toml` (the only place it is written), move the
Unreleased notes under a heading with that version and the date, commit, then
`git tag -a vX.Y.Z -m "motiongrammetry X.Y.Z"` and push the tag. `./install.sh update` builds
the image with that version, and the About page and `mg --version` show it.

## Unreleased

- **Version of a checkout**: run from a git checkout, `mg --version` and the About page add the
  branch (`0.2.0-fix-some-bug`, or `-main` past a release), so a local instance says which code
  it runs; installed copies and the Docker image show the plain version.
- **GitHub icon** in the top bar: it was drawn from a mistyped copy of the logo (a misshapen ear
  and head); it is now GitHub's own mark, kept once in `topbar.css` (#20).
- **CONTRIBUTING.md and a pull request template**: a branch per change, a local instance to try
  it, and a list of what the approver has to validate before the merge.
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
