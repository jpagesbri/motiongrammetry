"""Export a dataset for 2D Gaussian Splatting (2DGS) surface reconstruction on a CUDA host.

2DGS (SIGGRAPH 2024) trains view-consistent Gaussian surfels and extracts a mesh
by TSDF-fusing their rendered depth. It copes with shiny, thin and low-texture
regions much better than classic MVS, but needs an NVIDIA GPU (~10-30 min).

The exported folder is self-contained: copy it to the GPU machine, run
``run_2dgs.sh`` and bring back ``out/train/ours_30000/fuse_post.ply``, then
finish locally with ``mg clean <work> --mesh fuse_post.ply``.
"""

from __future__ import annotations

import shutil
import stat
from pathlib import Path

import cv2
import numpy as np
import pycolmap

from .mask import load_mask
from .workspace import Workspace

RUN_SCRIPT = """#!/usr/bin/env bash
# Train 2DGS on this folder and extract a mesh. Needs CUDA + the 2DGS environment:
#   git clone --recursive https://github.com/hbb1/2d-gaussian-splatting && cd 2d-gaussian-splatting
#   conda env create --file environment.yml && conda activate surfel_splatting
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="${{TWODGS_REPO:-$HOME/2d-gaussian-splatting}}"
cd "$REPO"
python train.py -s "$HERE" -m "$HERE/out" -r {resolution} --depth_ratio {depth_ratio} \\
    --lambda_normal 0.05 --lambda_dist {lambda_dist}{extra_train}
python render.py -s "$HERE" -m "$HERE/out" -r {resolution} --depth_ratio {depth_ratio} \\
    --skip_train --skip_test --num_cluster 1{extra_render}
echo "mesh: $HERE/out/train/ours_30000/{mesh_name}"
"""


def export_2dgs(ws: Workspace, out: Path | None = None, masked: bool = False, resolution: int = 2) -> Path:
    src_sparse = ws.dense / "sparse"
    if not src_sparse.exists():
        raise RuntimeError(f"no undistorted model in {ws.dense}; run `mg sfm` or `mg vggt` first")
    out = out or ws.root / "splat"
    if out.exists():
        shutil.rmtree(out)
    (out / "images").mkdir(parents=True)
    (out / "sparse" / "0").mkdir(parents=True)

    rec = pycolmap.Reconstruction(src_sparse)
    if masked:
        if not ws.has_masks():
            raise RuntimeError("--masked needs masks; run `mg mask` first")
        # 2DGS (like its DTU loader) multiplies the image by its alpha channel, so the
        # subject is trained alone and the fused mesh contains no background.
        undist_masks = _undistorted_masks(ws, rec)
        for image in rec.images.values():
            rgb = cv2.imread(str(ws.dense / "images" / image.name))
            alpha = undist_masks.get(image.name)
            if alpha is None:
                alpha = np.full(rgb.shape[:2], 255, np.uint8)
            new_name = Path(image.name).with_suffix(".png").name
            cv2.imwrite(str(out / "images" / new_name), np.dstack([rgb, alpha]))
            image.name = new_name
    else:
        for image in rec.images.values():
            shutil.copy2(ws.dense / "images" / image.name, out / "images" / image.name)
    rec.write(out / "sparse" / "0")

    script = out / "run_2dgs.sh"
    script.write_text(RUN_SCRIPT.format(
        resolution=resolution,
        depth_ratio=1 if masked else 0,  # median depth suits bounded objects, mean depth for scenes
        lambda_dist=1000 if masked else 0,
        extra_train="",
        extra_render="" if masked else " --unbounded --mesh_res 1024",
        mesh_name="fuse_post.ply" if masked else "fuse_unbounded_post.ply",
    ))
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)
    return out


def _undistorted_masks(ws: Workspace, undistorted: pycolmap.Reconstruction) -> dict[str, np.ndarray]:
    """Warp the masks (drawn on the original, distorted frames) into the undistorted cameras."""
    from .sfm import load_reconstruction

    distorted = load_reconstruction(ws)
    by_name = {img.name: img for img in distorted.images.values()}
    result = {}
    for image in undistorted.images.values():
        src = by_name.get(image.name)
        mask = load_mask(ws.mask_for(image.name))
        if src is None or mask is None:
            continue
        cam_u, cam_d = image.camera, src.camera
        u, v = np.meshgrid(np.arange(cam_u.width) + 0.5, np.arange(cam_u.height) + 0.5)
        rays = cam_u.cam_from_img(np.stack([u.ravel(), v.ravel()], 1))
        rays = np.hstack([rays, np.ones((len(rays), 1))])
        xy = cam_d.img_from_cam(rays, check_cheirality=False) - 0.5
        map_x = xy[:, 0].reshape(u.shape).astype(np.float32)
        map_y = xy[:, 1].reshape(u.shape).astype(np.float32)
        result[image.name] = cv2.remap(mask.astype(np.uint8) * 255, map_x, map_y, cv2.INTER_NEAREST,
                                       borderValue=0)
    return result
