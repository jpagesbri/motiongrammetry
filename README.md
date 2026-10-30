# motiongrametry

Walk around a person or object with your phone, get a cleaned, textured 3D mesh.

## Quick start

You need [Docker](https://docs.docker.com/engine/install/) (on Windows: Docker Desktop with WSL2, and
run this inside WSL). For the GPU: an NVIDIA card with driver 580 or newer and the
[NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html);
without them everything runs on the CPU, about twice as slowly.

```bash
git clone <this repository> motiongrametry && cd motiongrametry
./install.sh
```

It checks Docker and the GPU (and says what to install when something is missing), asks whether you
want the optional VGGT step (non-commercial license, NVIDIA GPU), builds the image (30-60 minutes the
first time), starts the app and opens http://127.0.0.1:8765. Your scans and the downloaded AI models go
to `~/motiongrametry`. Then: Help in the app, or

```bash
./install.sh stop | logs | status     # control it (it also starts again after a reboot)
./install.sh update                   # after `git pull`: rebuild, same settings
./install.sh --with-vggt              # add VGGT later (or --without-vggt)
```

Details, options and a direct install without Docker (for development) are below.

`mg` is the glue the off-the-shelf tools are missing: it turns a raw orbit video into
the inputs SfM/MVS/splatting expect, picks the best reconstruction backend for the
machine it runs on, and turns their raw output (subject + floor + walls + floaters)
into just the subject.

```
video ──► frames ──► mask ──► poses ──────────► dense mesh ──► clean ──► texture ──► model.glb
          motion-    BiRefNet/ COLMAP 4 /          OpenMVS        silhouette  OpenMVS
          aware      IS-Net    GLOMAP + loop       ── or ──       carving,
          keyframes            closure             VGGT + TSDF    floaters,
                               ── or ──            ── or ──       manifold, Y-up GLB
                               VGGT (feed-forward) 2DGS (GPU)
```

| stage | what it adds |
|---|---|
| `mg frames` | Keyframes spaced by **viewpoint change** (optical-flow parallax), not time: pauses collapse, fast sweeps get more frames; the sharpest frame of each segment is kept and blurry ones are rejected against their local neighbourhood. |
| `mg mask` | Subject segmentation (`--mask-model person` for people). **BiRefNet** when onnxruntime has CUDA, IS-Net / U²-Net on CPU (BiRefNet takes ~1 min and ~7 GB RAM per frame on a laptop CPU; force it with `object-hq` / `person-hq`). Masks follow the COLMAP convention. |
| `mg sfm` | pycolmap 4 SIFT → sequential matching + sparse all-pairs **loop closure** (no vocab tree needed) → **GLOMAP** global mapper (falls back to incremental), undistortion. Single-focal `RADIAL` camera by default (see below). `--mask-sfm` for turntables. |
| `mg mvs` | OpenMVS dense point cloud + Delaunay mesh (CPU; native or Docker). |
| `mg vggt` | **VGGT** feed-forward cameras + depth fused with a TSDF (numpy + marching cubes) — no SfM/MVS at all; robust to blur and low texture. Also writes a COLMAP model so every later stage works. |
| `mg export-2dgs` | Self-contained **2D Gaussian Splatting** dataset + run script for a CUDA host (`--masked` trains on the subject only). |
| `mg clean` | **Silhouette carving** with the masks (removes floor/walls by visual-hull reasoning, tolerant to per-view mask mistakes), **support-plane removal** (desk/floor found by RANSAC on background points of the dense cloud, validated against the subject), floater removal, manifold repair, optional decimation. GLB is **gravity-aligned** (Y-up from the desk normal, else the mean camera up-vector) and centred. |
| `mg texture` | OpenMVS texturing of the *cleaned* mesh → `model.glb`. |
| `mg doctor` | Which backends are usable on this machine. |

## Install with Docker (everything in one image)

```bash
./install.sh                        # checks Docker and the GPU, builds the image once, starts http://127.0.0.1:8765
docker/install.sh --workspaces ~/scans   # use an existing scans folder (remembered from then on)
docker/install.sh --with-vggt     # add the optional VGGT step (shows its licenses, asks you to accept)
docker/install.sh stop | logs | status | update
```

One image (`docker/Dockerfile`) holds the app, COLMAP (pycolmap) and OpenMVS, plus the mask models'
runtime. The `nvidia` variant builds OpenMVS with CUDA and uses the GPU builds of COLMAP and ONNX
Runtime, so the depth maps (mvs), the masks and SfM's features and matching run on the GPU when one
is passed in; everything else, and everything on a machine without a GPU, runs on the CPU. The
Python packages are locked in `docker/requirements-nvidia.txt` (from `requirements-nvidia.in`).

- **GPU**: `--gpu auto` (default) uses an NVIDIA GPU when Docker can reach it; that needs an NVIDIA
  driver >= 580 and the NVIDIA Container Toolkit on the host, and the script prints the commands for
  your distribution when the toolkit is missing (or starts on the CPU). `--gpu none` forces the CPU.
  The same image runs either way: OpenMVS is in it twice (CUDA and CPU builds, a launcher picks), and
  COLMAP and the masks choose the GPU only when one is really there. AMD GPUs run on the CPU for now.
- **Size**: the image is about 9 GB (most of it NVIDIA's CUDA and cuDNN); the first build takes
  30-60 minutes, later ones only rebuild the app layer.
- **Data**: `~/motiongrametry` (or `--data DIR`) holds the scans (`workspaces/`, or `--workspaces DIR`
  for an existing folder), the downloaded AI models and `.env` with these settings. Files are written
  as your user. Videos in `videos/` (or `--videos DIR`) can be opened by path as `/videos/...`.
- **Port**: `--port N` (default 8765, bound to 127.0.0.1 only).
- **Remembered**: the settings are saved in `DATA/.env`; later runs and `update` reuse them (scans
  folder, port, GPU choice, VGGT), and a flag changes one. Each data folder is its own Docker project,
  so a second setup (`--data` elsewhere, e.g. a test) never replaces the first.
- **VGGT** (`--with-vggt`, `--without-vggt`): a layer on top of the image (`docker/Dockerfile.vggt`,
  about 11 GB more) with its own Python environment, `/opt/vggt`, which the vggt step runs in: PyTorch needs
  NVIDIA's CUDA 13 pip packages while the GPU build of COLMAP in `/opt/venv` needs the CUDA 12 ones
  (`docker/requirements-vggt.txt`). The script shows the terms first and installs only after you type
  "accept" (or `--accept-vggt-license`): the facebook/VGGT-1B weights are CC BY-NC 4.0, non-commercial
  only. The weights download on first use into `DATA/models`; ones already in
  `~/.cache/huggingface` are reused.
- **Licenses**: the image contains OpenMVS (AGPL-3.0) built unchanged, with its source and VCGLib's in
  `/usr/share/doc/openmvs`, and NVIDIA's CUDA runtime and cuDNN under NVIDIA's terms; `/about` in the
  app lists everything. VGGT is only in the `-vggt` layer, added on request.
- `mg doctor` (inside: `docker compose ... exec motiongrametry mg doctor`) and the About page say
  which steps run on the GPU.

## Install directly (development, without Docker)

Tested on Linux (x86-64, CachyOS/Arch) with an 8-core laptop CPU, 16 GB RAM and no NVIDIA GPU.
Everything below runs on CPU; a CUDA GPU only matters for the optional VGGT / 2DGS paths.

### 1. Prerequisites

| what | why | check |
|---|---|---|
| **Python 3.12** (3.10–3.13 allowed) | the version everything was tested with; uv fetches it if your system has another | `uv python list` |
| **[uv](https://docs.astral.sh/uv/)** | creates the virtualenv and installs dependencies | `uv --version` |
| **Docker**, daemon running, your user allowed to use it | runs OpenMVS (dense reconstruction + texturing) | `docker info` |
| **~15 GB free disk** where your workspaces live | ~0.75 GB for the OpenMVS image, ~5 GB of temporary depth maps for a 120-frame capture at half resolution (`mg check` tells you per capture) | `df -h .` |
| internet on first use | Python packages, the OpenMVS build, the mask model (~170 MB per model, cached in `~/.rembg/models`), three.js for the web UI (jsdelivr CDN) | |
| optional: Blender, ffmpeg | only for the synthetic test video in `tools/` | |

Docker without `sudo` (log out and back in afterwards):

```bash
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"
```

> **Keep workspaces off `/tmp`.** On many distributions `/tmp` is a RAM-backed tmpfs of half your
> RAM; dense reconstruction writes several GB there and fails. `mg ui` defaults to `./workspaces`.

### 2. Install the Python package

```bash
cd motiongrametry
uv venv -p 3.12                          # downloads Python 3.12 if needed
uv pip install -e '.[mask,dev]'          # core + segmentation + tests
# optional: the exact versions this was tested with
# uv pip install -r requirements-lock.txt
source .venv/bin/activate                # puts `mg` on your PATH (fish: .venv/bin/activate.fish)
```

### 3. Build the OpenMVS image (once, ~20 min)

```bash
docker build -f docker/openmvs.Dockerfile -t motiongrametry/openmvs:2.3.0 docker/
```

The image on Docker Hub (`openmvs/openmvs-ubuntu`) is from 2019 and cannot texture a cleaned mesh,
so the Dockerfile builds OpenMVS 2.3 from source with the fixes Ubuntu 24.04 needs (see comments
in it). If OpenMVS is installed natively (`DensifyPointCloud` on PATH) it is used instead of Docker;
a different image name can be set with `MG_OPENMVS_IMAGE`.

### 4. Check the installation

```bash
mg doctor          # which backends are available
pytest             # 32 tests, ~15 s, no network needed
```

`mg doctor` should show `ok` for pycolmap, OpenMVS (`docker (motiongrametry/openmvs:2.3.0)`) and
rembg. The `torch` / `vggt` lines are optional.

### 5. First run

```bash
mg ui                                   # then open http://127.0.0.1:8765
```

In the UI: **New scan** → name it → **Upload video…** (or paste a local path and **Create**) →
leave the default stages ticked → **Check** → **Run selected**. Or from the terminal:

```bash
mg check work/ --video capture.mp4      # what would fail, and why (nothing is run)
mg run capture.mp4 work/                # ~10 min for a 15-20 s phone clip on a laptop CPU
mg logs work/ --list                    # what ran, how long, warnings
```

To try it without a capture of your own, `examples/orbit.mp4` is a 6 s synthetic orbit (Blender,
rendered by `tools/synth_orbit.py`): `mg run examples/orbit.mp4 work/ --num 45 --sample-fps 30`
takes ~7 min on a laptop CPU.

The result is `work/mesh/model.glb` (textured, Y-up) and `work/mesh/mesh_clean.glb`
(vertex colours). For a quick test of a new capture use `--preset quick-preview`.

### Optional: VGGT feed-forward path

```bash
uv pip install torch torchvision huggingface_hub einops safetensors
uv pip install --no-deps git+https://github.com/facebookresearch/vggt   # its own deps pin numpy<2
```

Weights (~5 GB) download from Hugging Face on first use. Wants a CUDA GPU; on CPU keep
`--max-frames` ≤ 24 and expect ~8-10 GB RAM. *(Not yet run on this machine; its post-processing is unit-tested.)*

### Setup problems

| symptom | fix |
|---|---|
| `requires-python` error from `uv pip install` | the venv uses another Python: `rm -rf .venv && uv venv -p 3.12` |
| `mg: command not found` | `source .venv/bin/activate`, or use `.venv/bin/mg` |
| `docker-down` / `permission denied ... docker.sock` | start the daemon; add yourself to the `docker` group and log in again |
| `openmvs-image-missing` | step 3 was skipped or used another tag |
| OpenMVS build fails at `nanoflann` / `libjxl` / `IMWRITE_JPEGXL` | you edited the Dockerfile's base image or tag; it is pinned to Ubuntu 24.04 + OpenMVS v2.3.0 on purpose |
| `disk-low` / `disk-full` during `mvs` | move the workspace to a bigger disk, or `--resolution-level 2` / `--preset low-disk` |
| mask stage very slow / swapping | you picked an `-hq` model on CPU; use `object` / `person` |
| UI shows no 3D view | the browser cannot reach cdn.jsdelivr.net (three.js), or WebGL is disabled |

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

## Web UI

```bash
mg ui                       # http://127.0.0.1:8765, workspaces in ./workspaces
mg ui --root ~/scans --port 9000
```

- **About** (`/about`): the third-party software, AI models and Python packages the app uses, each
  with its license text (`credits.py`: Python packages are read from the installed distributions,
  the rest is listed there with texts in `licenses/`), what those licenses ask (VGGT-1B weights are
  CC BY-NC 4.0, OpenMVS is AGPL-3.0 and runs unmodified in its own container, NVIDIA's CUDA
  libraries come with PyTorch), and a THIRD_PARTY_NOTICES download; `mg notices -o FILE` writes the
  same file for redistribution.
- **Help** (`/help`): the full how-to with screenshots: filming, every screen and stage, every
  option (read live from `options.py`), presets, outputs, printing, editing and troubleshooting.
- **Generate** has two tabs (side by side on wide screens): **Run** and **Preview** (the 3D model, the keyframes and masks, the files). A dot on a tab means
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
  `workspaces/`): listed with a thumbnail (a snapshot of the 3D view taken when a run produces a
  mesh, a keyframe until then), keyframes, faces and last activity; hover for a larger card.
- **Manual** (Run tab) and the scan list: create a scan from a local video path or an upload, tick the stages to run,
  pick a preset, set basic or advanced options (generated from `options.py`), **Check** (pre-flight
  only), run and cancel (stages run as `mg` subprocesses). Shows the disk the depth maps will need.
  When a scan has both an MVS and a VGGT mesh, **Clean from** picks which one is carved.
- **Duplicate scan**: a new scan with the same keyframes and masks (optionally the SfM/MVS
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
- **Files** (Preview tab): download links for the results (textured GLB, clean mesh, 3D-print STL,
  PLYs, raw mesh, dense cloud) with their size, a zip of `mesh/`, and the folder they are in; stale
  files are marked.
- **Export…** (Preview tab): one file at a time with its own settings, remembered per file type:
  3D print STL (height, solid / shell / auto, which way the shell grows and how thick, minimum
  wall, stand, flat base), textured GLB, vertex-colour GLB, textured OBJ (zip with MTL and PNG) or
  PLY, from the pipeline or the custom version, with orientation and a real-world height (GLB in
  metres, the others in millimetres). Shows the built file in 3D and, for prints, the report.
- **Edit & paint** (`/edit`): sculpt the clean mesh (draw, dig, inflate, deflate, smooth,
  flatten, pinch, grab, cut away, with an option to cap what a cut opens; fill + merge to add
  volume, e.g. close the hole of a ring shape: by default only the region around the fill spheres
  is rebuilt (`meshops.local_merge`: the union surface at the mesh's own edge length, cut along the
  region's border and stitched to the untouched mesh; openings the spheres partly cover are taken
  in whole), or the whole mesh at a chosen voxel size; a plane cut
  that closes flat; close all holes; remove loose pieces; **restore**: brush over the raw mesh,
  shown as an amber ghost, to bring back what clean carved away; clean only deletes raw vertices,
  so restored triangles weld in seamlessly) and paint the texture on the model or on the flat atlas (brush,
  restore original, blur, smudge, clone, lighten, darken, saturate, desaturate, colour picker),
  with undo. Edits are saved as a **custom version** in `mesh/custom/` and never change the
  pipeline's files: a sculpt writes `custom/mesh_clean.ply`, its GLB and a custom print STL; the
  texture stage then projects the photos onto the custom mesh (when it is newer than the clean
  mesh) into `custom/model.glb`, which you can paint; painting the pipeline's textured model copies
  it into `custom/` first. Outputs and the zip list both versions, the viewer has a **Custom
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
  export, GLB, print STL and later clean / texture run uses it; saving rebuilds the files that have
  it baked in (GLBs, print STLs) in the background. Meshes stay in the SfM frame; nothing re-runs.
- **3D preview** (three.js): camera frusta, sparse points, dense cloud, raw / clean / textured
  mesh, all in one Y-up frame. **Display** (`W`): colours, solid grey, wireframe, or solid + wires
  (fading out where triangles are under a few pixels); **Highlight holes** draws the open edges in
  orange and counts the openings (also in the editor).
- **View cube** (`static/viewcube.js`, in the Generate preview, the Export preview and the editor):
  the camera's direction with Z-up labels as in the print files; click a face, edge or corner to
  look from there; a button frames the model, another (or key `5`) switches perspective /
  orthographic. Point clouds travel as packed binary (f32 xyz + u8 rgb, sampled
  server-side to ≤600k), meshes as cached GLB, all frusta are a single draw call, and the view
  renders only on interaction (draw-call / triangle counter bottom-right).

Needs no extra Python packages; three.js loads from jsdelivr, so the browser needs internet access.

## Use

```bash
mg run capture.mp4 work/                          # objects
mg run capture.mp4 work/ --preset person          # people
mg run capture.mp4 work/ --preset low-texture     # dark/plain objects
mg run capture.mp4 work/ --method vggt            # feed-forward (GPU recommended)
mg check work/                                    # what would fail, and why
mg logs work/ --list                              # run history
```

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
mg export-2dgs work --masked          # -> work/splat/
# on a CUDA machine with https://github.com/hbb1/2d-gaussian-splatting:
work/splat/run_2dgs.sh
# back here:
mg clean work --mesh work/splat/out/train/ours_30000/fuse_post.ply
```

## Capture tips

- Orbit 2–3 times at different heights; keep the subject ~60% of the frame.
- Fast shutter (lock exposure, bright even light) beats high resolution: motion blur kills SfM.
- **People:** they must hold still for the whole orbit (sit them down, ~40–60 s). Breathing and
  hair still blur detail; for moving subjects you need a multi-camera rig.
- Avoid shiny, transparent and featureless surfaces for the `mvs` path; try `vggt` or 2DGS there.

## Which path?

| | `mvs` (default) | `vggt` | 2DGS |
|---|---|---|---|
| hardware | CPU | CUDA (CPU: ≤24 frames) | CUDA |
| detail | high, textured | medium (518 px depth) | high, smooth surfaces |
| robustness | needs texture + sharp frames | very robust | robust, needs good poses |
| scale | arbitrary (SfM) | arbitrary | from SfM |

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
- **3D printing (`mesh/print.stl`)** needs one closed, upright, real-size solid that balances, which
  a scan is not. Clean writes one (Z-up, scaled to `--print-height` mm, bottom 2% flattened) and
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
  thin walls, pieces, overhangs) is reported on the clean card and under the STL in Outputs,
  each with the option that changes it. Without a support plane, "up" is the normal of the
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
