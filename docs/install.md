# Installing motiongrametry

The [README](../README.md#setup) has the short version. This page has every option of the Docker
install and the direct install for development.

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
| internet on first use | Python packages, the OpenMVS build, the mask model (~170 MB per model, cached in `~/.rembg/models`) | |
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
pytest             # ~75 tests, a few minutes, no network needed
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
| UI shows no 3D view | WebGL is disabled in the browser |
