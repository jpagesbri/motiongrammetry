"""Pre-flight checks (before a stage starts) and quality gates (after it finishes).

Pre-flight errors stop a stage before it wastes time or overwrites good results;
gates turn silent bad results (2/92 frames registered, a mesh carved to nothing)
into explicit warnings or failures with a hint on what to change.
"""

from __future__ import annotations

import functools
import importlib.util
import math
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from .diagnostics import Diag, error, info, warn
from .workspace import Workspace

GB = 1e9


# ---- environment probes ---------------------------------------------------------------------
def mem_available() -> int | None:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024
    except OSError:
        pass
    return None


def disk_free(path: Path) -> int:
    while not path.exists():
        path = path.parent
    return shutil.disk_usage(path).free


def has_module(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


@functools.lru_cache(maxsize=1)
def vggt_env() -> dict | None:
    """The separate Python environment the vggt step runs in (MG_VGGT_PYTHON, the Docker image's VGGT
    layer), when this process is not it: {"python", "cuda"} or {"python", "error"}."""
    py = os.environ.get("MG_VGGT_PYTHON")
    # compare environments, not interpreters: both venvs' python links to the same system python
    if not py or not Path(py).exists() or Path(py).parent.parent.resolve() == Path(sys.prefix).resolve():
        return None
    try:
        r = subprocess.run([py, "-c", "import torch, vggt, huggingface_hub, einops, safetensors; "
                            "print(int(torch.cuda.is_available()))"], capture_output=True, text=True, timeout=180)
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"python": py, "error": str(e)}
    if r.returncode:
        return {"python": py, "error": (r.stderr.strip().splitlines() or ["import failed"])[-1]}
    return {"python": py, "cuda": r.stdout.strip().endswith("1")}


def nvidia_gpu() -> str | None:
    """Name of the first NVIDIA GPU this process can use (inside Docker: the one passed in)."""
    if not shutil.which("nvidia-smi"):
        return None
    try:
        r = subprocess.run(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.strip().splitlines()[0] if r.returncode == 0 and r.stdout.strip() else None


def nvidia_gpu_memory() -> float | None:
    """Total memory of the first NVIDIA GPU in GiB."""
    if not shutil.which("nvidia-smi"):
        return None
    try:
        r = subprocess.run(["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
                           capture_output=True, text=True, timeout=10)
        return float(r.stdout.split()[0]) / 1024 if r.returncode == 0 and r.stdout.strip() else None
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None


def accel() -> dict:
    """Where each step that can use a GPU will run, and why: {step: (device, note)}."""
    gpu = nvidia_gpu()
    out = {}
    try:
        import pycolmap

        cuda = bool(getattr(pycolmap, "has_cuda", False))
        out["sfm"] = ("GPU", "SIFT features and matching on CUDA") if cuda and gpu else \
            ("CPU", "no GPU passed in" if cuda else "CPU build of pycolmap (pycolmap-cuda12 adds CUDA)")
    except ImportError:
        out["sfm"] = ("missing", "pycolmap not installed")
    if importlib.util.find_spec("onnxruntime"):
        import onnxruntime

        cuda = "CUDAExecutionProvider" in onnxruntime.get_available_providers()
        out["mask"] = ("GPU", "ONNX Runtime CUDA") if cuda and gpu else \
            ("CPU", "no GPU passed in" if cuda else "CPU build of ONNX Runtime (onnxruntime-gpu adds CUDA)")
    else:
        out["mask"] = ("missing", "the mask extra is not installed")
    densify = shutil.which("DensifyPointCloud")
    if densify:
        try:
            helptext = subprocess.run([densify, "--help"], capture_output=True, text=True, timeout=20).stdout
        except (OSError, subprocess.TimeoutExpired):
            helptext = ""
        cuda = "cuda" in helptext.lower() or bool(os.environ.get("MG_OPENMVS_CUDA"))  # the image has both builds
        out["mvs"] = ("GPU", "OpenMVS depth maps and meshing on CUDA") if cuda and gpu else \
            ("CPU", "no GPU passed in" if cuda else "OpenMVS built without CUDA")
    else:
        out["mvs"] = ("CPU", "OpenMVS runs in its own container (docker/openmvs.Dockerfile, CPU build)") if shutil.which("docker") \
            else ("missing", "OpenMVS not installed")
    out["texture"] = ("GPU", "parts of OpenMVS texturing on CUDA") if out["mvs"][0] == "GPU" else \
        ("CPU", out["mvs"][1])
    env = vggt_env()
    if importlib.util.find_spec("torch"):
        import torch

        out["vggt"] = ("GPU", "PyTorch CUDA") if torch.cuda.is_available() else ("CPU", "slow: at most ~24 frames")
    elif env and "error" not in env:
        out["vggt"] = ("GPU", "PyTorch CUDA, in its own environment") if env["cuda"] else ("CPU", "slow: at most ~24 frames")
    elif env:
        out["vggt"] = ("missing", f"its environment does not work: {env['error']}")
    else:
        out["vggt"] = ("missing", "not installed (optional; Docker: install.sh --with-vggt)")
    return {"gpu": gpu, "steps": out}


def openmvs_problem() -> Diag | None:
    from . import mvs

    if shutil.which("DensifyPointCloud"):
        return None
    if not shutil.which("docker"):
        return error("openmvs-missing", "OpenMVS is not installed and Docker is not available",
                     "install OpenMVS, or Docker and build docker/openmvs.Dockerfile")
    try:
        daemon = subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"],
                                capture_output=True, text=True, timeout=15)
    except subprocess.TimeoutExpired:
        return error("docker-unresponsive", "the Docker daemon does not answer", "restart Docker")
    if daemon.returncode != 0:
        return error("docker-down", "Docker is installed but its daemon is not reachable",
                     "start it (systemctl start docker) and check your user is in the docker group")
    image = subprocess.run(["docker", "image", "inspect", mvs.DOCKER_IMAGE], capture_output=True)
    if image.returncode != 0:
        return error("openmvs-image-missing", f"Docker image {mvs.DOCKER_IMAGE} not found",
                     f"docker build -f docker/openmvs.Dockerfile -t {mvs.DOCKER_IMAGE} docker/")
    return None


def ply_counts(path: Path) -> dict[str, int]:
    """Element counts from a PLY header without loading the file."""
    counts = {}
    with open(path, "rb") as f:
        for _ in range(64):
            line = f.readline().decode(errors="replace").strip()
            m = re.match(r"element (\w+) (\d+)", line)
            if m:
                counts[m.group(1)] = int(m.group(2))
            if line == "end_header" or not line:
                break
    return counts


def _stale_input(ws: Workspace, stage: str, inputs: list[str]) -> list[Diag]:
    stale = set(ws.stale_stages())
    out = []
    for up in inputs:
        if up in stale:
            why = f"{up} output is older than what it was computed from"
            if up == "sfm" and _masks_changed_since_sfm(ws):
                why = "the masks changed after sfm ran with them (mask_sfm)"
            out.append(error("stale-input", f"{why}, so {stage} would mix results from different runs",
                             f"re-run {up} first (or pass --force to use it anyway)"))
    return out


def _masks_changed_since_sfm(ws: Workspace) -> bool:
    """SfM ran with the masks (mask_sfm) and the masks are newer than it."""
    sfm, mask = ws.output_mtime("sfm"), ws.output_mtime("mask")
    return "mask" in ws.sfm_upstream() and None not in (sfm, mask) and mask > sfm + 1.0


# ---- pre-flight -----------------------------------------------------------------------------
def preflight(stage: str, ws: Workspace, o: dict, video: str | None = None) -> list[Diag]:
    return {"frames": _pre_frames, "mask": _pre_mask, "sfm": _pre_sfm, "mvs": _pre_mvs, "vggt": _pre_vggt,
            "clean": _pre_clean, "texture": _pre_texture}[stage](ws, o, video)


def _pre_frames(ws, o, video):
    import cv2

    if not video:
        return [error("no-video", "no input video given")]
    p = Path(video)
    if not p.is_file():
        return [error("no-video", f"video not found: {video}")]
    cap = cv2.VideoCapture(str(p))
    ok = cap.isOpened() and cap.read()[0]
    n = cap.get(cv2.CAP_PROP_FRAME_COUNT)
    fps = cap.get(cv2.CAP_PROP_FPS) or 0
    w, h = cap.get(cv2.CAP_PROP_FRAME_WIDTH), cap.get(cv2.CAP_PROP_FRAME_HEIGHT)
    cap.release()
    if not ok:
        return [error("video-unreadable", f"OpenCV cannot decode {p.name}",
                      "convert it: ffmpeg -i in.mov -c:v libx264 -crf 18 out.mp4")]
    d = []
    duration = n / fps if fps else 0
    if 0 < duration < 5:
        d.append(warn("short-video", f"video is only {duration:.1f} s long",
                      "a full orbit usually needs 20-60 s; raise sample_fps if you keep it"))
    if min(w, h) < 720:
        d.append(warn("low-resolution", f"video is {int(w)}x{int(h)}", "record at 1080p or 4K"))
    if n and o["num"] > n:
        d.append(warn("num-exceeds-frames", f"asked for {o['num']} keyframes from a {int(n)}-frame video"))
    need = o["num"] * 0.8e6
    if disk_free(ws.root) < 2 * need:
        d.append(error("disk-low", f"{disk_free(ws.root) / GB:.1f} GB free; keyframes alone need "
                       f"~{need / GB:.1f} GB and later stages much more", "free space or use another workspace root"))
    return d


def _pre_mask(ws, o, video):
    if not ws.frame_paths():
        return [error("no-frames", "no keyframes yet", "run frames first")]
    if not (has_module("rembg") and has_module("onnxruntime")):
        return [error("missing-dependency", "rembg/onnxruntime not installed",
                      "uv pip install 'motiongrammetry[mask]'")]
    d = _stale_input(ws, "mask", [])
    from .mask import resolve_model

    model = resolve_model(o["mask_model"])
    mem = mem_available()
    if "birefnet" in model and mem is not None and mem < 8 * GB:
        d.append(warn("low-memory", f"{model} needs ~7 GB RAM on CPU, {mem / GB:.1f} GB available",
                      "use mask_model object/person (IS-Net/U2Net) or close other programs"))
    return d


def _pre_sfm(ws, o, video):
    n = len(ws.frame_paths())
    if n < 3:
        return [error("no-frames", f"{n} keyframes; SfM needs at least 3 (realistically 30+)", "run frames first")]
    d = []
    if n < 20:
        d.append(warn("few-frames", f"only {n} keyframes", "raise num; 80-200 is typical for an orbit"))
    if o["mask_sfm"]:
        if not ws.has_masks():
            d.append(error("no-masks", "mask_sfm needs masks", "run mask first"))
        d += _stale_input(ws, "sfm", ["mask"])
    return d


def _pre_mvs(ws, o, video):
    if not (ws.dense / "sparse").exists():
        return [error("no-sfm", "no undistorted SfM model", "run sfm first")]
    d = _stale_input(ws, "mvs", ["sfm"])
    problem = openmvs_problem()
    if problem:
        d.append(problem)
    from .mvs import depth_map_bytes

    need = depth_map_bytes(ws, o["resolution_level"])
    free = disk_free(ws.mvs)
    if need * 1.15 > free:
        d.append(error("disk-low", f"depth maps need ~{need / GB:.1f} GB, {free / GB:.1f} GB free"
                       + (" (on a RAM-backed /tmp)" if str(ws.root).startswith("/tmp") else ""),
                       "raise resolution_level (each step needs 4x less), use fewer keyframes, "
                       "the low-disk preset, or a workspace on a bigger disk"))
    mem = mem_available()
    ram = 1.0 * GB + need * 0.6  # empirical: ~4.7 GB peak for 5.3 GB of depth maps
    if mem is not None and ram > mem:
        d.append(warn("low-memory", f"densification may need ~{ram / GB:.1f} GB RAM, {mem / GB:.1f} GB available",
                      "raise resolution_level or close other programs"))
    return d


def _pre_vggt(ws, o, video):
    if not ws.frame_paths():
        return [error("no-frames", "no keyframes yet", "run frames first")]
    d = []
    if ws.cameras_from() == "sfm":
        # VGGT writes its own cameras over colmap/ and dense/: the SfM model and the undistorted
        # images the mvs mesh and texturing rely on would be gone for good.
        what = "SfM and MVS results" if ws.output_mtime("mvs") else "an SfM result"
        d.append(error("replaces-cameras", f"this workspace has {what}; VGGT would replace its cameras, "
                       "so they could no longer be cleaned or textured",
                       "duplicate the workspace and run VGGT in the copy (UI: Duplicate), or force to replace them"))
    # vggt is installed with --no-deps (its own deps pin numpy<2), so check its runtime deps too.
    missing = [m for m in ("torch", "vggt", "huggingface_hub", "einops", "safetensors") if not has_module(m)]
    env = vggt_env() if missing else None  # the step may run in its own environment (Docker VGGT layer)
    if missing and env and "error" not in env:
        cuda = env["cuda"] and o["device"] != "cpu"
    elif missing:
        return d + [error("missing-dependency", f"VGGT needs {', '.join(missing)}",
                          env["error"] if env else "Docker: docker/install.sh --with-vggt; directly: "
                          "uv pip install torch torchvision huggingface_hub einops safetensors && "
                          "uv pip install --no-deps git+https://github.com/facebookresearch/vggt")]
    else:
        import torch

        cuda = torch.cuda.is_available() and o["device"] != "cpu"
    if not cuda:
        mem = mem_available()
        if mem is not None and mem < 10 * GB:
            d.append(warn("low-memory", f"VGGT-1B on CPU needs ~8-10 GB RAM, {mem / GB:.1f} GB available",
                          "lower max_frames (e.g. 16) or use a GPU"))
        if (o["max_frames"] or 24) > 32:
            d.append(warn("slow", "VGGT on CPU with more than 32 frames is very slow"))
    return d


def _raw_mesh(ws: Workspace, o: dict) -> Path | None:
    if o.get("mesh"):
        return Path(o["mesh"])
    return ws.raw_mesh()


def _pre_clean(ws, o, video):
    mesh = _raw_mesh(ws, o)
    if mesh is None or not mesh.exists():
        return [error("no-mesh", "no raw mesh to clean", "run mvs or vggt first, or pass mesh=<path>")]
    d = []
    if not (ws.sparse / "images.bin").exists():
        d.append(error("no-sfm", "no camera model (needed for carving and orientation)", "run sfm first"))
    producer = ws.mesh_producer(mesh)
    d += _stale_input(ws, "clean", ["sfm"] + ([producer] if producer else []))
    if not o["no_masks"]:
        if not ws.has_masks():
            d.append(warn("no-masks", "no masks: only the main piece (the one with the most faces) will be kept",
                          "run mask for proper background removal"))
        elif "mask" in ws.stale_stages():
            d.append(error("stale-masks", "masks are older than the keyframes; carving with them would cut "
                           "the subject at the wrong places", "re-run mask, or set no_masks"))
        else:
            missing = [f.name for f in ws.frame_paths() if not ws.mask_for(f.name).exists()]
            if missing:
                d.append(warn("masks-incomplete", f"{len(missing)} keyframes have no mask", "re-run mask"))
    return d


def _broken_faces(mesh: Path) -> int:
    """How many faces of the mesh TextureMesh can't take (clean.broken_faces); 0 if unreadable."""
    import numpy as np
    import open3d as o3d

    from .clean import broken_faces

    try:
        return int(broken_faces(np.asarray(o3d.io.read_triangle_mesh(str(mesh)).triangles)).sum())
    except Exception:
        return 0


def _pre_texture(ws, o, video):
    if not (ws.mesh / "mesh_clean.ply").exists():
        return [error("no-clean-mesh", "no clean mesh yet", "run clean first")]
    if not (ws.mvs / "scene_dense.mvs").exists():
        return [error("no-mvs-scene", "texturing uses the OpenMVS scene, which the VGGT path does not create",
                      "export the clean mesh (vertex colours), carry a texture over in the editor, or run sfm + mvs")]
    d = _stale_input(ws, "texture", ["clean", "mvs"])
    from .clean import texture_source

    custom = ws.mesh / "custom" / "mesh_clean.ply"
    broken = _broken_faces(texture_source(ws)[0])
    if broken:
        d.append(info("broken-faces", f"the mesh has {broken:,} broken faces (duplicates or degenerate, left by an "
                      "older save): texture leaves them out", "saving the mesh in the editor removes them for good"))
    if texture_source(ws)[1] == "custom":
        d.append(info("custom-mesh", "texturing your custom (edited) mesh: the result is the custom textured model",
                      "the pipeline's own textured model is left as it is"))
    elif custom.exists():
        d.append(warn("custom-mesh-outdated", "the custom (edited) mesh was made from an older clean mesh, "
                      "so texture uses the new clean mesh", "edit and save the mesh again to texture your edits"))
    problem = openmvs_problem()
    if problem:
        d.append(problem)
    return d


# ---- quality gates --------------------------------------------------------------------------
def gate(stage: str, ws: Workspace, result: dict, o: dict) -> list[Diag]:
    fn = {"frames": _gate_frames, "mask": _gate_mask, "sfm": _gate_sfm, "mvs": _gate_mesh,
          "vggt": _gate_mesh, "clean": _gate_clean, "texture": _gate_texture}[stage]
    return fn(ws, result, o)


def _gate_frames(ws, r, o):
    d = []
    k = r["keyframes"]
    if k < 8:
        d.append(error("too-few-frames", f"only {k} usable keyframes", "longer video, higher num or sample_fps"))
    elif k < 30:
        d.append(warn("few-frames", f"only {k} keyframes", "raise num or sample_fps"))
    segs = r.get("segments") or 0
    if segs and r.get("blur_rejected", 0) / segs > 0.25:
        d.append(warn("blurry-video", f"{r['blur_rejected']}/{segs} viewpoints had only blurry frames",
                      "more light / faster shutter, move slower, or the blurry preset"))
    if r.get("total_motion_widths", 99) < 1.0:
        d.append(warn("little-motion", "the view changes little across the video",
                      "fine for a distant subject (a drone orbit of a building: the cameras step will tell); close up, "
                      "walk all the way round the subject; for a fixed camera use the turntable preset"))
    return d


def _gate_mask(ws, r, o):
    d = []
    n = max(r["frames"], 1)
    if r["mean_coverage"] < 0.005:
        d.append(error("subject-not-found", "the masks are (almost) empty",
                       "try mask_model object-hq / person, or skip masks (no_masks)"))
    elif r["empty_masks"] / n > 0.2:
        d.append(warn("masks-empty", f"{r['empty_masks']}/{n} masks are empty", "keep the subject in frame"))
    if r["mean_coverage"] > 0.6:
        d.append(warn("mask-too-large", f"masks cover {r['mean_coverage']:.0%} of the frame on average: the "
                      "model probably took background as subject", "try object-hq, or no_masks for clean"))
    if r.get("coverage_cv", 0) > 0.6:
        d.append(warn("masks-unstable", "mask size varies a lot between frames (subject picked inconsistently)",
                      "carving votes across views, but consider a different mask_model or higher bg_tolerance"))
    return d


def _gate_sfm(ws, r, o):
    d = []
    ratio = r["registered"] / max(r["frames"], 1)
    hints = "try the low-texture preset, matcher exhaustive, mapper incremental, or more keyframes"
    if ratio < 0.5:
        d.append(error("poor-registration", f"only {r['registered']}/{r['frames']} frames got a camera pose", hints))
    elif ratio < 0.9:
        d.append(warn("partial-registration", f"{r['frames'] - r['registered']} of {r['frames']} frames have no pose "
                      "(those views won't contribute)", hints))
    if r["mean_reproj_error_px"] > 2.0:
        d.append(warn("high-reprojection", f"mean reprojection error {r['mean_reproj_error_px']:.2f} px",
                      "check for rolling-shutter / motion blur; try camera_model OPENCV only if lenses are wide"))
    if r["points3D"] < 300:
        d.append(warn("sparse-model", f"only {r['points3D']} 3D points", "low-texture preset"))
    cam = r.get("camera") or {}
    params = cam.get("params") or []
    if params and r.get("image_width"):
        f = params[0]
        fov = math.degrees(2 * math.atan(r["image_width"] / (2 * f)))
        if not 15 < fov < 120:
            d.append(warn("implausible-focal", f"estimated horizontal field of view {fov:.0f} deg",
                          "the reconstruction may be distorted; more frames with varied viewpoints help"))
    if r.get("fx_fy_ratio") and abs(r["fx_fy_ratio"] - 1) > 0.03:
        d.append(warn("stretched-model", f"fx/fy = {r['fx_fy_ratio']:.3f}: model likely stretched",
                      "use camera_model RADIAL (single focal)"))
    if r.get("models", 1) > 1:
        d.append(warn("disconnected", f"SfM split into {r['models']} pieces; only the largest is used",
                      "move more slowly through difficult parts; loop_stride 3"))
    d += camera_path_diags(r.get("camera_path") or {})
    return d


CAMERA_PATH_HINT = ("the views did not connect there, so part of the scene is misplaced or built twice, however low "
                    "the reprojection error. Film again: walk slowly through doorways and keep part of the room you "
                    "are leaving in view while you turn; avoid quick pans and close-ups in passing; for an object, "
                    "a steady orbit at an even pace. matcher exhaustive or mapper incremental can move the break "
                    "but rarely remove it; strict stops the pipeline here")


CAMERAS_COLLAPSED_HINT = ("this is not a filming problem like a doorway: the solver did not find how the camera moved. "
                          "Typical causes: mask_sfm on a video where the camera moves round the subject (it is for "
                          "turntables: camera fixed, object turning), too few features inside the masks, the global "
                          "mapper. Run sfm again without mask_sfm, or with mapper incremental and matcher exhaustive "
                          "(the turntable preset's settings); strict stops the pipeline here")


def _listing(items: list[str], total: int, most: int = 8) -> str:
    """The first ``most`` items, then how many of ``total`` are left out."""
    shown = items[:most]
    return ", ".join(shown) + (f" and {total - len(shown)} more" if total > len(shown) else "")


def keyframe_ranges(keyframes: list[int]) -> list[str]:
    """[0, 1, 2, 3, 40] -> ["0–3", "40"]"""
    out, start = [], None
    for i, k in enumerate(keyframes):
        if start is None:
            start = k
        if i + 1 == len(keyframes) or keyframes[i + 1] != k + 1:
            out.append(f"{start}–{k}" if k > start else f"{k}")
            start = None
    return out


def camera_path_diags(p: dict) -> list[Diag]:
    """Warnings for the camera-path check of sfm.camera_path_check (the sfm gate, and mg check-cameras)."""
    d = []
    if p.get("collapsed"):
        n, k = p["collapsed_cameras"], p["distinct_positions"]
        return [warn("cameras-collapsed", f"{n} of {p['cameras']} cameras sit on top of each other (only {k} distinct "
                     f"position{'s' if k != 1 else ''}; {p['still_steps']} of {p['steps']} steps between keyframes are "
                     "~0): SfM did not recover the motion", CAMERAS_COLLAPSED_HINT)]
    jumps = sorted(p.get("jumps") or [], key=lambda j: j["from"])
    if jumps:
        n = p.get("jump_count", len(jumps))
        d.append(warn("camera-path-jump", f"the camera path breaks {'once' if n == 1 else f'{n} times'}, where it moved "
                      "many times the median step from one keyframe to the next: keyframes "
                      + _listing([f"{j['from']}→{j['to']} ×{j['ratio']:g}" for j in jumps], n), CAMERA_PATH_HINT))
    far = p.get("far") or []
    if far:
        n = p.get("far_count", len(far))
        d.append(warn("cameras-far-away", f"{n} camera{'s' if n > 1 else ''} far from all the others (keyframes "
                      + ", ".join(keyframe_ranges([f["keyframe"] for f in far])) + ("…" if n > len(far) else "") +
                      f", up to ×{max(f['ratio'] for f in far):g} the usual distance from the centre)",
                      CAMERA_PATH_HINT))
    return d


def _gate_mesh(ws, r, o):
    d = []
    faces = r.get("mesh_faces")
    if faces is not None and faces < 1000:
        d.append(error("mesh-empty", f"raw mesh has only {faces} faces", "check SfM quality; try resolution_level 0"))
    pts = r.get("dense_points")
    if pts is not None and pts < 20000:
        d.append(warn("dense-sparse", f"dense cloud has only {pts} points",
                      "low texture or too low resolution; try resolution_level 0 or the vggt path"))
    return d


def _gate_clean(ws, r, o):
    d = []
    kept = r["faces_out"] / max(r["faces_in"], 1)
    if r["faces_out"] < 500 or kept < 0.01:
        d.append(error("clean-removed-everything", f"cleanup kept {r['faces_out']} of {r['faces_in']} faces",
                       "masks probably don't match the cameras: re-run mask, raise bg_tolerance, or no_masks"))
    elif r.get("carved_vertices") and r.get("vertices_in") and r["carved_vertices"] / r["vertices_in"] > 0.9:
        d.append(warn("heavy-carving", "carving removed over 90% of the mesh", "check masks in the keyframe strip"))
    if not r.get("support_plane") and not o["keep_ground"]:
        d.append(info("no-support-plane", "no trustworthy desk/floor plane found: none was cut, "
                      "orientation falls back to the camera up-vector"))
    return d


def _gate_texture(ws, r, o):
    ply = Path(r.get("ply") or ws.mesh / "textured.ply")
    if not ply.exists():
        return [error("no-output", f"texturing produced no {ply.name}")]
    return []
