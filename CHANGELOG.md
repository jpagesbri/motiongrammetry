# Changelog

Versions follow [semantic versioning](https://semver.org): while it is 0.x, a minor version
(0.2.0) can change behaviour or options; a patch version (0.1.1) only fixes things.

To release: set `version` in `pyproject.toml` (the only place it is written), move the
Unreleased notes under a heading with that version and the date, commit, then
`git tag -a vX.Y.Z -m "motiongrammetry X.Y.Z"` and push the tag. `./install.sh update` builds
the image with that version, and the About page and `mg --version` show it.

## Unreleased

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
