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

import pycolmap

from .workspace import Workspace

log = logging.getLogger(__name__)


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
    # Hand-held video: one physical camera, so share intrinsics across all frames.
    log.info("extracting SIFT features from %d frames", len(frames))
    pycolmap.extract_features(
        db, ws.frames, image_names=names, camera_mode=pycolmap.CameraMode.SINGLE,
        reader_options=reader, extraction_options=extraction,
    )

    if matcher == "exhaustive":
        log.info("exhaustive matching (%d pairs)", len(names) * (len(names) - 1) // 2)
        pycolmap.match_exhaustive(db)
        pairs = []
    else:
        log.info("sequential matching (overlap=%d)", overlap)
        seq = pycolmap.SequentialPairingOptions()
        seq.overlap = overlap
        seq.quadratic_overlap = True
        pycolmap.match_sequential(db, pairing_options=seq)
        pairs = _loop_pairs(names, loop_stride, overlap)
    if pairs:
        pair_file = ws.colmap / "loop_pairs.txt"
        pair_file.write_text("".join(f"{a} {b}\n" for a, b in pairs))
        imported = pycolmap.ImportedPairingOptions()
        imported.match_list_path = str(pair_file)
        log.info("loop-closure matching on %d sparse pairs", len(pairs))
        pycolmap.match_image_pairs(db, pairing_options=imported)

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
