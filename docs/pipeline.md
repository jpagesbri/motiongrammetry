# The pipeline

What each stage does, the command line, how failures are caught, and the lessons behind the defaults.

## Stages

`mg` is the glue the off-the-shelf tools are missing: it turns a raw orbit video into
the inputs SfM/MVS/splatting expect, picks the best reconstruction backend for the
machine it runs on, and turns their raw output (subject + floor + walls + floaters)
into just the subject.

```
video ──► frames ──► mask ──► poses ──────────► dense mesh ──► clean ──► texture ──► textured.ply
          motion-    BiRefNet/ COLMAP 4 /          OpenMVS        silhouette  OpenMVS
          aware      IS-Net    GLOMAP + loop       ── or ──       carving,
          keyframes            closure             VGGT + TSDF    floaters,
                               ── or ──            ── or ──       manifold
                               VGGT (feed-forward) 2DGS (GPU)
```

| stage | what it adds |
|---|---|
| `mg frames` | Keyframes spaced by **viewpoint change** (optical-flow parallax), not time: pauses collapse, fast sweeps get more frames; the sharpest frame of each segment is kept and blurry ones are rejected against their local neighbourhood. |
| `mg mask` | Subject segmentation (`--mask-model person` for people). **BiRefNet** on an NVIDIA GPU with at least 11 GB, IS-Net / U²-Net on CPU (BiRefNet takes ~1 min and ~7 GB RAM per frame on a laptop CPU; force it with `object-hq` / `person-hq`). Masks follow the COLMAP convention. |
| `mg sfm` | pycolmap 4 SIFT → sequential matching + sparse all-pairs **loop closure** (no vocab tree needed) → **GLOMAP** global mapper (falls back to incremental), undistortion. Single-focal `RADIAL` camera by default (see below). `--mask-sfm` for turntables. |
| `mg mvs` | OpenMVS dense point cloud + Delaunay mesh (CPU; native or Docker). |
| `mg vggt` | **VGGT** feed-forward cameras + depth fused with a TSDF (numpy + marching cubes) — no SfM/MVS at all; robust to blur and low texture. Also writes a COLMAP model so every later stage works. |
| `mg export-2dgs` | Self-contained **2D Gaussian Splatting** dataset + run script for a CUDA host (`--masked` trains on the subject only). |
| `mg clean` | **Silhouette carving** with the masks (removes floor/walls by visual-hull reasoning, tolerant to per-view mask mistakes), **support-plane removal** (desk/floor found by RANSAC on background points of the dense cloud, validated against the subject), floater removal, manifold repair, optional decimation. GLB is **gravity-aligned** (Y-up from the desk normal, else the mean camera up-vector) and centred. |
| `mg texture` | OpenMVS texturing of the *cleaned* mesh → `textured.ply` + PNG. |
| `mg export` | The files to take out (GLB, OBJ, PLY, the 3D print as STL / 3MF, the dense cloud, the COLMAP cameras), made when asked and reused until the scan changes (`exports.py`; the web UI's Export window). |
| `mg doctor` | Which backends are usable on this machine. |

## Command line

```bash
mg run capture.mp4 work/                          # objects
mg run capture.mp4 work/ --preset person          # people
mg run capture.mp4 work/ --preset low-texture     # dark/plain objects
mg run capture.mp4 work/ --method vggt            # feed-forward (GPU recommended)
mg check work/                                    # what would fail, and why
mg logs work/ --list                              # run history
```

Taking files out with `mg export` (made when asked, reused until the scan changes; several
contents make one zip, `work-export.zip`, with a folder per content):

```bash
mg export work/ textured --out exports/           # -> exports/work-textured.glb (exports/ made if missing)
mg export work/ print --out exports/              # -> exports/work-print.stl, next to it
mg export work/ print:3mf --out shoe.3mf          # -> shoe.3mf
```

`--out` is a folder when it ends in `/`, is a folder already, or is a new name without an
extension (`--out exports`); a missing folder is made, with its parents, and the file goes inside
under its own name. A new name with an extension is the file itself (its missing folders are
made too, and a warning says so if the extension isn't the export's). A file that is already there
is replaced, and the last line says so: `-> …/work-print.stl (replaced the file that was there)`.
That line always names the file written. Without `--out`, the file goes in the current folder.

Or stage by stage (each reads/writes `work/`, see `workspace.py`):

```bash
mg frames capture.mp4 work --num 150
mg mask work --mask-model person
mg sfm work
mg mvs work --resolution-level 1
mg clean work
mg texture work
```

GPU surface reconstruction with 2DGS:

```bash
mg export-2dgs work --masked          # -> work/splat/ (or --out DIR: a new or empty folder, or an earlier dataset, replaced)
# on a CUDA machine with https://github.com/hbb1/2d-gaussian-splatting:
work/splat/run_2dgs.sh
# back here:
mg clean work --mesh work/splat/out/train/ours_30000/fuse_post.ply
```

## Which path?

| | `mvs` (default) | `vggt` | 2DGS |
|---|---|---|---|
| hardware | CPU | CUDA (CPU: ≤24 frames) | CUDA |
| detail | high, textured | medium (518 px depth) | high, smooth surfaces |
| robustness | needs texture + sharp frames | very robust | robust, needs good poses |
| scale | arbitrary (SfM) | arbitrary | from SfM |

## Failures, warnings and logs

Every stage (`mg <stage>`, `mg run`, or the UI) runs through one wrapper (`pipeline.py`):

1. **Pre-flight checks** refuse to start on known problems: missing or *stale* inputs (e.g. masks
   older than the keyframes, a mesh built from an older SfM), missing OpenMVS / Docker daemon /
   image, not enough disk for the depth maps (estimated per resolution level), low RAM, unreadable
   video. `mg check WORK` runs them alone; `--force` overrides.
2. **Quality gates** after each stage turn silent bad results into warnings or failures with a hint:
   too few / blurry keyframes, camera barely moving, empty or background-grabbing masks, poor or
   partial SfM registration, high reprojection error, stretched or implausible focal length,
   disconnected models, near-empty meshes, cleanup removing everything. `--strict` fails on warnings.
3. **Termination**: the pipeline stops at the first failing stage and marks the rest *skipped*.
   Cancel/Ctrl-C/SIGTERM unwinds cleanly (OpenMVS containers are named and killed, partial depth
   maps removed); a watchdog kills OpenMVS steps silent for `--idle-timeout` minutes; exit codes
   from OpenMVS are explained (out of memory, segfault, disk full).
4. **Logs**: each run writes the complete output, including COLMAP/OpenMVS native output, to
   `work/logs/<time>-<stage>.log`, plus a line in `work/logs/history.jsonl` with status, duration,
   options and diagnostics. `mg logs WORK` shows the last log, `mg logs WORK --list` the history;
   the UI has a failure banner, per-stage log links and a history browser.

Exit codes: `0` ok · `1` failed · `2` quality gate failed · `3` pre-flight failed · `130` cancelled.

### Presets for common problems

`mg presets` lists them; use `--preset NAME` (explicit options still win) or pick one in the UI.

| preset | for |
|---|---|
| `low-texture` | dark / plain / shiny subject on a featureless surface |
| `turntable` | fixed camera, rotating object |
| `blurry` | fast or low-light capture |
| `person` | a person holding still |
| `glossy-desk` | reflective support surface |
| `low-disk` | little disk or RAM |
| `quick-preview` | a fast sanity check of a capture |

All options (and their help) are in `options.py`; `mg <stage> --help` lists the relevant ones.

## Capture tips

- Orbit 2–3 times at different heights; keep the subject ~60% of the frame.
- Fast shutter (lock exposure, bright even light) beats high resolution: motion blur kills SfM.
- **People:** they must hold still for the whole orbit (sit them down, ~40–60 s). Breathing and
  hair still blur detail; for moving subjects you need a multi-camera rig.
- Avoid shiny, transparent and featureless surfaces for the `mvs` path; try `vggt` or 2DGS there.

## Lessons baked into the defaults

- **Single focal length.** With COLMAP's `OPENCV` model (separate fx/fy) GLOMAP converged, on one
  of two identical runs, to fy/fx = 1.12: 0.36 px reprojection error, but a vertically stretched
  model that no longer matched the masks, so carving deleted the subject. `RADIAL` fixed it
  (f = 749.1 px vs. 746.7 ground truth); `mg sfm` warns if fx/fy diverge.
- **Low SIFT threshold.** A black mouse on a white desk gave ~200-300 SIFT keypoints per frame
  with COLMAP's default `peak_threshold` and only 45/92 frames registered; 0.0015 gives ~5k and
  registered 92/92. (COLMAP 4's ALIKED+LightGlue would be better still, but the pycolmap PyPI
  wheel is built without ONNX.)
- **Keep the better mapper.** When GLOMAP registers too few frames the incremental mapper is tried
  too, and the model with more registered frames wins (it can be worse: 2/92 on that clip).
- **Ground plane only when it is trustworthy.** A featureless desk yields almost no sparse points,
  so RANSAC on SfM points found a "plane" through the subject. The plane is fitted on mask-
  background points of the dense cloud and rejected unless the subject sits above it (a few points
  below are tolerated: reflections in a glossy desk reconstruct as geometry under the surface).
- **Salient-object masks are not view-consistent** (a background box or the pedestal is "subject"
  in some frames only). Carving votes across views (`--bg-tolerance`, default 15%) instead of
  trusting any single mask.
- **A few bad masks still punch holes into the subject.** After carving, each carved patch that
  is enclosed by kept surface, small (`--fill-holes`, default 5% of the subject's area; 0 turns
  it off) and only disputed by the masks (background in at most half its views on average) is
  put back from the raw mesh. Real gaps, e.g. between arm and body, are background in most views
  and stay carved. Holes the raw mesh never had geometry for are not filled.
- **3D printing (`mg export WS print`, the Export window)** needs one closed, upright, real-size solid
  that balances, which a scan is not. The export makes one (Z-up, scaled to `--print-height` mm,
  bottom 2% flattened) and
  rebuilds it on a ~0.3 mm voxel grid: inside is what a flood fill from outside cannot reach, so
  surfaces that cross each other (a lid cutting through the scan made 2,483 crossings on a
  test scan, and a 6x8x10 cm head "weighed" 18 cm3) cannot confuse it, and walls thinner than
  `--print-min-wall` (0.8 mm) are thickened. `--print-mode auto` closes openings with smoothed
  lids (solid), unless they would invent more than a third of the surface, as for a face filmed
  from the front only: then it prints the scanned surface as a `--print-shell` (3 mm) shell
  instead of a cone-shaped guess. A scan only measured the outer skin, so material is only ever
  added behind it (`--print-shell-side inward`, the default; growing both ways moved a face's
  surface 0.83 mm off the scan and melted the eyes, inward keeps it within 0.19 mm). Which side
  is "behind" comes from the cameras: rays from the real camera positions hit the visible
  surface from the front (98-99.9% on real scans); geometry alone gets a face mask backwards. When the centre of mass is not over the model's own base,
  `--print-stand auto` adds a 2 mm disc under it. Whatever is left (invented share, stand,
  thin walls, pieces, overhangs) is reported on the clean card, in the Preview's print check
  and in Export, each with the option that changes it. Without a support plane, "up" is the normal of the
  camera orbit, ~10 degrees truer than the mean camera up-vector on a partial hand-held orbit.
- **OpenMVS quirks:** relative image paths resolve against the working folder (so it is set to
  `mvs/`), OBJ export segfaults (PLY + texture PNG is used), bow-tie vertices crash the texture
  atlas (cleanup removes them), and Ubuntu's `_FORTIFY_SOURCE=3` aborts `DensifyPointCloud`.
- **Open3D 0.20's TSDF integration** silently produced empty volumes here, so TSDF fusion is
  implemented directly.

## Tested

On a synthetic Blender orbit (960×540, 180 frames, pause + speed changes, floor and clutter) on
an 8-core laptop CPU without a GPU: 45 keyframes → 45/45 registered (0.32 px) → ~28k-face
textured subject with floor and boxes removed, ~6.5 min end to end (masks 1.2 min, OpenMVS 4–5 min).
On a real 17 s 4K/60 phone clip of a black mouse on a white desk (rolling camera, subject
leaving the frame for ~1 s): 92 keyframes, 92/92 registered (0.87 px), textured mouse with desk
removed, ~9 min on the same CPU. Dark glossy flanks still have holes — the classic-MVS limit;
that is the case for `vggt` / 2DGS on a GPU.
Not yet tested here: the VGGT network itself (needs the 5 GB weights; its post-processing is
unit-tested) and 2DGS training (needs CUDA; the exported dataset was checked against the legacy
COLMAP binary layout the 3DGS-family loaders read).

## Tests

```bash
pytest                                                 # unit tests (synthetic cameras/meshes)
blender -b -P tools/synth_orbit.py -- /tmp/orbit       # synthetic orbit for end-to-end runs
ffmpeg -framerate 30 -i /tmp/orbit/%04d.png -pix_fmt yuv420p orbit.mp4 && mg run orbit.mp4 work
```
