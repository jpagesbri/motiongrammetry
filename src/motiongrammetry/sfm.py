"""Camera poses with COLMAP 4 (pycolmap): SIFT -> sequential + loop pairs -> GLOMAP / incremental.

Video frames come in order, so sequential matching covers most of the overlap
cheaply. An orbit, however, returns to its start; without matching the end to
the beginning the trajectory drifts and the model does not close. Rather than
needing a vocabulary tree, we additionally match a sparse all-pairs subset
(every ``loop_stride``-th frame against each other), which is cheap for the
100-300 keyframes a capture produces and reliably closes the loop.
"""

from __future__ import annotations

import logging
import shutil
from itertools import combinations
from pathlib import Path

import numpy as np
import pycolmap

from .workspace import Workspace

log = logging.getLogger(__name__)

# The camera-path check (issue #49). A model can place every frame at sub-pixel reprojection error
# and still be wrong as a whole: where neighbouring views share too little (a fast pan through a
# doorway), SfM may put the next stretch of the walk somewhere else, or build a room twice. Video
# keyframes are close together in time, so the camera moves by similar amounts from one to the
# next; a step many times the usual one, or cameras far from all the others, mean a break.
# Normal object scans peak at x2-x5 the median step (uneven walking pace) and x1-x2 the usual
# distance; the broken flat walk of issue #49 at x10-x712, its misplaced entrance cameras at x19-x23.
PATH_JUMP_FACTOR = 10.0  # a step this many times the median step between keyframes is a break
PATH_FAR_FACTOR = 5.0    # a camera this many times farther from the median centre than most (p75) is misplaced
PATH_MIN_CAMERAS = 8     # fewer placed cameras: no meaningful median, no check
PATH_MAX_LISTED = 20     # entries of each kind kept in the manifest


def _loop_pairs(names: list[str], stride: int, overlap: int) -> list[tuple[str, str]]:
    picked = list(range(0, len(names), stride))
    return [(names[i], names[j]) for i, j in combinations(picked, 2) if j - i > overlap]


def run_sfm(
    ws: Workspace,
    mapper: str = "global",
    use_masks: bool = False,
    # One focal length: with separate fx/fy (OPENCV) short video orbits can converge to a
    # stretched solution (fx/fy off by >10%) that still reprojects well but is wrong.
    camera_model: str = "RADIAL",
    max_image_size: int = 1600,
    # COLMAP's default (0.0067) finds only ~200-300 keypoints on a dark object on a plain
    # desk; 0.0015 gives ~5k and keeps consecutive frames connected. Costs a few seconds.
    peak_threshold: float = 0.0015,
    overlap: int = 12,
    loop_stride: int = 5,
    num_threads: int = -1,
    max_features: int = 8192,
    matcher: str = "sequential",
) -> dict:
    frames = ws.frame_paths()
    if len(frames) < 3:
        raise RuntimeError(f"need at least 3 frames in {ws.frames}, found {len(frames)}")
    names = [f.name for f in frames]

    if ws.colmap.exists():
        shutil.rmtree(ws.colmap)
    ws.colmap.mkdir(parents=True)
    db = ws.colmap / "database.db"

    reader = pycolmap.ImageReaderOptions()
    reader.camera_model = camera_model
    if use_masks:
        if not ws.has_masks():
            raise RuntimeError("--mask-sfm given but no masks found; run `mg mask` first")
        reader.mask_path = str(ws.masks)

    extraction = pycolmap.FeatureExtractionOptions()
    extraction.max_image_size = max_image_size
    extraction.num_threads = num_threads
    extraction.sift.peak_threshold = peak_threshold
    extraction.sift.max_num_features = max_features
    # The CUDA build of pycolmap stops with "num_cuda_devices > 0" on Device.auto when no GPU is
    # passed in (e.g. the Docker image started without one): choose explicitly.
    gpu = bool(getattr(pycolmap, "has_cuda", False)) and getattr(pycolmap, "get_num_cuda_devices", lambda: 0)() > 0
    device = pycolmap.Device.cuda if gpu else pycolmap.Device.cpu
    # Hand-held video: one physical camera, so share intrinsics across all frames.
    log.info("extracting SIFT features from %d frames (%s)", len(frames), "GPU" if gpu else "CPU")
    pycolmap.extract_features(
        db, ws.frames, image_names=names, camera_mode=pycolmap.CameraMode.SINGLE,
        reader_options=reader, extraction_options=extraction, device=device,
    )

    if matcher == "exhaustive":
        log.info("exhaustive matching (%d pairs)", len(names) * (len(names) - 1) // 2)
        pycolmap.match_exhaustive(db, device=device)
        pairs = []
    else:
        log.info("sequential matching (overlap=%d)", overlap)
        seq = pycolmap.SequentialPairingOptions()
        seq.overlap = overlap
        seq.quadratic_overlap = True
        pycolmap.match_sequential(db, pairing_options=seq, device=device)
        pairs = _loop_pairs(names, loop_stride, overlap)
    if pairs:
        pair_file = ws.colmap / "loop_pairs.txt"
        pair_file.write_text("".join(f"{a} {b}\n" for a, b in pairs))
        imported = pycolmap.ImportedPairingOptions()
        imported.match_list_path = str(pair_file)
        log.info("loop-closure matching on %d sparse pairs", len(pairs))
        pycolmap.match_image_pairs(db, pairing_options=imported, device=device)

    sparse_root = ws.colmap / "sparse"
    sparse_root.mkdir(exist_ok=True)
    recon, n_models = _map(db, ws.frames, sparse_root, mapper)
    if mapper == "global" and recon.num_reg_images() < 0.6 * len(frames):
        log.warning("global mapper registered only %d/%d frames, trying incremental too",
                    recon.num_reg_images(), len(frames))
        try:
            alt, alt_models = _map(db, ws.frames, ws.colmap / "sparse_incremental", "incremental")
        except RuntimeError:
            alt = None
        if alt is not None and alt.num_reg_images() > recon.num_reg_images():
            recon, mapper, n_models = alt, "incremental", alt_models
        log.info("keeping %s model with %d frames", mapper, recon.num_reg_images())

    ws.sparse.mkdir(parents=True, exist_ok=True)
    recon.write(ws.sparse)
    recon.export_PLY(str(ws.colmap / "sparse_points.ply"))

    log.info("undistorting images for dense reconstruction")
    if ws.dense.exists():
        shutil.rmtree(ws.dense)
    pycolmap.undistort_images(ws.dense, ws.sparse, ws.frames)

    cam = next(iter(recon.cameras.values()))
    fx, fy = cam.focal_length_x, cam.focal_length_y
    info = {
        "mapper": mapper,
        "matcher": matcher,
        "models": n_models,
        "image_width": cam.width,
        "fx_fy_ratio": fx / fy,
        "camera": {"model": cam.model_name, "params": [round(p, 5) for p in cam.params]},
        "frames": len(frames),
        "registered": recon.num_reg_images(),
        "points3D": recon.num_points3D(),
        "mean_reproj_error_px": recon.compute_mean_reprojection_error(),
        "mean_track_length": recon.compute_mean_track_length(),
        "masked_features": use_masks,
        "unregistered": sorted(set(names) - {img.name for img in recon.images.values() if img.has_pose}),
        "camera_path": camera_path_from_reconstruction(recon, names),
    }
    log.info("SfM: %s", info)
    return info


def _map(db: Path, image_dir: Path, out: Path, mapper: str) -> pycolmap.Reconstruction:
    out.mkdir(parents=True, exist_ok=True)
    if mapper == "global":
        recs = pycolmap.global_mapping(db, image_dir, out)
    elif mapper == "incremental":
        recs = pycolmap.incremental_mapping(db, image_dir, out)
    else:
        raise ValueError(f"unknown mapper {mapper!r}")
    if not recs:
        from .diagnostics import EXIT_GATE, StageError, error

        raise StageError(error("sfm-no-model", "SfM could not build any model (too little overlap or texture)",
                               "try the low-texture preset, matcher exhaustive, or more keyframes"), EXIT_GATE)
    # Several disconnected models can come out; the biggest is the subject orbit.
    best = max(recs.values(), key=lambda r: r.num_reg_images())
    significant = sum(r.num_reg_images() >= 0.1 * best.num_reg_images() for r in recs.values())
    return best, significant


def load_reconstruction(ws: Workspace) -> pycolmap.Reconstruction:
    if not (ws.sparse / "images.bin").exists() and not (ws.sparse / "images.txt").exists():
        raise RuntimeError(f"no SfM model at {ws.sparse}; run `mg sfm` (or `mg vggt`) first")
    return pycolmap.Reconstruction(ws.sparse)


def camera_path_from_reconstruction(recon: pycolmap.Reconstruction, names: list[str] | None = None) -> dict:
    """:func:`camera_path_check` on a model: ``names`` are all keyframes in order (unplaced ones
    included); by default the model's own images, sorted by name."""
    centres = {img.name: img.projection_center() for img in recon.images.values() if img.has_pose}
    names = names if names is not None else sorted(centres)
    return camera_path_check([centres.get(n) for n in names], names)


def camera_path_check(centres: list, names: list[str] | None = None, jump_factor: float = PATH_JUMP_FACTOR,
                      far_factor: float = PATH_FAR_FACTOR) -> dict:
    """Breaks in the camera path: a pure function of the camera centres, one per keyframe in keyframe
    order (``None`` for a keyframe SfM could not place).

    - **jumps**: consecutive placed keyframes whose distance is over ``jump_factor`` times the median
      step. Across unplaced keyframes the distance is divided by the number of steps it spans (a
      lower bound on the motion per keyframe), so a gap alone is never a jump, but a part placed
      far off after one still is.
    - **far**: cameras over ``far_factor`` times farther from the median centre than the 75th
      percentile of those distances (robust to up to a quarter of the cameras being off).

    Keyframes are given by their position in the list (``frame_00062.jpg`` is 62). Ratios are
    rounded; ``ok`` is False when anything was flagged."""
    idx = [i for i, c in enumerate(centres) if c is not None]
    out = {"cameras": len(idx), "checked": False, "ok": True, "median_step": None, "max_step_ratio": None,
           "usual_distance": None, "max_far_ratio": None, "jump_factor": jump_factor, "far_factor": far_factor,
           "jumps": [], "far": [], "jump_count": 0, "far_count": 0}
    if len(idx) < PATH_MIN_CAMERAS:
        return out
    C = np.asarray([centres[i] for i in idx], dtype=float)
    span = np.diff(idx)
    steps = np.linalg.norm(np.diff(C, axis=0), axis=1) / span
    median = float(np.median(steps))
    dist = np.linalg.norm(C - np.median(C, axis=0), axis=1)
    usual = float(np.percentile(dist, 75))
    if median <= 0 or usual <= 0:  # the camera did not move: nothing to compare against
        return out
    ratio, far_ratio = steps / median, dist / usual
    out.update(checked=True, median_step=round(median, 6), max_step_ratio=round(float(ratio.max()), 1),
               usual_distance=round(usual, 6), max_far_ratio=round(float(far_ratio.max()), 1))

    def name(i):
        return names[i] if names else None

    jumps = [{"from": idx[k], "to": idx[k + 1], "ratio": round(float(ratio[k]), 1)}
             for k in np.flatnonzero(ratio > jump_factor)]
    far = [{"keyframe": idx[k], "ratio": round(float(far_ratio[k]), 1)} for k in np.flatnonzero(far_ratio > far_factor)]
    for j in jumps:
        j.update(from_name=name(j["from"]), to_name=name(j["to"]))
    for f in far:
        f["name"] = name(f["keyframe"])
    out.update(ok=not jumps and not far, jump_count=len(jumps), far_count=len(far),
               jumps=sorted(jumps, key=lambda j: -j["ratio"])[:PATH_MAX_LISTED],
               far=sorted(sorted(far, key=lambda f: -f["ratio"])[:PATH_MAX_LISTED], key=lambda f: f["keyframe"]))
    return out
