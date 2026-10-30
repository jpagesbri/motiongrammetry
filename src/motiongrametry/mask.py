"""Subject segmentation with salient-object models (BiRefNet by default) via rembg.

Masks are written in COLMAP's convention (``<image name>.png``, 0 = ignore) so the
same files can (a) stop feature extraction on a background that moves relative to
the subject (turntable captures) and (b) carve the background out of the final mesh.
"""

from __future__ import annotations

import logging
from pathlib import Path

import cv2
import numpy as np

log = logging.getLogger(__name__)

# rembg model presets: (with CUDA, CPU only). BiRefNet is the current open state of
# the art for dichotomous segmentation but takes ~1 min/frame and ~7 GB RAM on a
# laptop CPU, so CPU presets use IS-Net / U2Net (~1-2 s/frame) unless "-hq" is asked.
PRESETS = {
    "object": ("birefnet-general-lite", "isnet-general-use"),
    "object-hq": ("birefnet-general", "birefnet-general-lite"),
    "person": ("birefnet-portrait", "u2net_human_seg"),
    "person-hq": ("birefnet-portrait", "birefnet-portrait"),
}


def resolve_model(model: str) -> str:
    if model not in PRESETS:
        return model
    import onnxruntime

    gpu = "CUDAExecutionProvider" in onnxruntime.get_available_providers()
    return PRESETS[model][0 if gpu else 1]


def compute_masks(
    frames: list[Path],
    out_dir: Path,
    model: str = "object",
    threshold: float = 0.5,
    dilate_px: int = 4,
) -> dict:
    try:
        from rembg import new_session, remove
    except ImportError as e:  # pragma: no cover - depends on optional extra
        raise RuntimeError("masking needs the 'mask' extra: uv pip install 'motiongrametry[mask]'") from e
    from PIL import Image

    model_name = resolve_model(model)
    log.info("segmenting %d frames with %s", len(frames), model_name)
    session = new_session(model_name)
    out_dir.mkdir(parents=True, exist_ok=True)
    coverage = []
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * dilate_px + 1,) * 2) if dilate_px > 0 else None
    for i, frame in enumerate(frames):
        with Image.open(frame) as img:
            alpha = np.asarray(remove(img.convert("RGB"), session=session, only_mask=True))
        mask = (alpha >= threshold * 255).astype(np.uint8)
        mask = _keep_largest_blobs(mask)
        if kernel is not None:
            mask = cv2.dilate(mask, kernel)
        cv2.imwrite(str(out_dir / f"{frame.name}.png"), mask * 255)
        coverage.append(float(mask.mean()))
        if (i + 1) % 10 == 0 or i + 1 == len(frames):
            log.info("masked %d/%d frames", i + 1, len(frames))
    if not frames:
        raise RuntimeError("no keyframes to mask")
    cov = np.array(coverage)
    empty = int((cov < 0.002).sum())
    if empty:
        log.warning("%d frames have an (almost) empty mask - check the subject is in view", empty)
    cv = float(cov.std() / cov.mean()) if cov.mean() > 0 else 0.0
    return {"model": model_name, "frames": len(frames), "mean_coverage": float(cov.mean()), "empty_masks": empty,
            "coverage_cv": cv}


def _keep_largest_blobs(mask: np.ndarray, min_ratio: float = 0.1) -> np.ndarray:
    """Drop speckles: keep connected components at least ``min_ratio`` of the largest."""
    n, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    if n <= 2:
        return mask
    areas = stats[1:, cv2.CC_STAT_AREA]
    keep = np.flatnonzero(areas >= min_ratio * areas.max()) + 1
    return np.isin(labels, keep).astype(np.uint8)


def load_mask(path: Path) -> np.ndarray | None:
    if not path.exists():
        return None
    m = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
    return None if m is None else m > 127
