"""Feed-forward reconstruction with VGGT (Meta, CVPR 2025): cameras + depth in one pass.

Replaces SfM + MVS with a single network forward pass, then fuses the predicted
depth maps into a mesh with a TSDF volume. It is far more tolerant of blur,
low texture and small baselines than SIFT-based SfM, at the cost of resolution
(the network sees ~518 px images) and an arbitrary global scale.

The poses are also written as a regular COLMAP model in ``work/colmap/sparse/0``
so the rest of the pipeline (silhouette carving, 2DGS export) works unchanged.
Install: ``uv pip install torch torchvision huggingface_hub einops safetensors && uv pip install --no-deps git+https://github.com/facebookresearch/vggt``
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

import cv2
import numpy as np
import open3d as o3d
import pycolmap

from .mask import load_mask
from .workspace import Workspace

log = logging.getLogger(__name__)

PATCH = 14
LONG_SIDE = 518


def _target_size(w: int, h: int) -> tuple[int, int]:
    """Resize so the long side is 518 and both sides are multiples of the 14 px patch."""
    s = LONG_SIDE / max(w, h)
    return max(PATCH, round(w * s / PATCH) * PATCH), max(PATCH, round(h * s / PATCH) * PATCH)


def _subsample(paths: list[Path], n: int) -> list[Path]:
    if len(paths) <= n:
        return paths
    return [paths[i] for i in np.linspace(0, len(paths) - 1, n).round().astype(int)]


def run_vggt(
    ws: Workspace,
    max_frames: int | None = None,
    conf_percentile: float = 40.0,
    tsdf_resolution: int = 384,
    device: str = "auto",
) -> dict:
    try:
        import torch
        from vggt.models.vggt import VGGT
        from vggt.utils.pose_enc import pose_encoding_to_extri_intri
    except ImportError as e:  # pragma: no cover - optional heavy dependency
        raise RuntimeError("VGGT path needs torch + vggt; see `mg doctor` for install commands") from e

    if device == "auto":
        device = "cuda" if torch.cuda.is_available() else "cpu"
    if max_frames is None:
        max_frames = 120 if device == "cuda" else 24  # VGGT memory grows ~quadratically with frames
    frames = _subsample(ws.frame_paths(), max_frames)
    if len(frames) < 2:
        raise RuntimeError("need at least 2 frames")

    first = cv2.imread(str(frames[0]))
    H0, W0 = first.shape[:2]
    tw, th = _target_size(W0, H0)
    rgb_small = []
    for f in frames:
        img = cv2.cvtColor(cv2.imread(str(f)), cv2.COLOR_BGR2RGB)
        if img.shape[:2] != (H0, W0):
            raise RuntimeError("all frames must share one resolution")
        rgb_small.append(cv2.resize(img, (tw, th), interpolation=cv2.INTER_AREA))
    batch = torch.from_numpy(np.stack(rgb_small)).permute(0, 3, 1, 2).float().div(255.0)[None].to(device)

    log.info("VGGT on %d frames at %dx%d (%s)", len(frames), tw, th, device)
    model = VGGT.from_pretrained("facebook/VGGT-1B").eval()
    # Only the aggregator and the camera/depth heads are used: dropping the point and track
    # heads saves ~0.4 GB of VRAM.
    model.point_head = model.track_head = None
    if device == "cuda":
        dtype = torch.bfloat16 if torch.cuda.get_device_capability()[0] >= 8 else torch.float16
        if dtype == torch.bfloat16:
            # The 909M-parameter aggregator runs under bf16 autocast anyway; storing it in bf16
            # halves it (3.6 -> 1.8 GB) so the model fits 8 GB GPUs. The heads stay fp32.
            model.aggregator.to(dtype)
        autocast = torch.autocast("cuda", dtype=dtype)
    else:
        autocast = torch.autocast("cpu", enabled=False)
    model.to(device)
    with torch.inference_mode(), autocast:
        tokens, ps_idx = model.aggregator(batch)
        pose_enc = model.camera_head(tokens)[-1]
        depth, depth_conf = model.depth_head(tokens, batch, ps_idx)
    extrinsic, intrinsic = pose_encoding_to_extri_intri(pose_enc.float(), batch.shape[-2:])
    extrinsic = extrinsic[0].cpu().numpy().astype(np.float64)  # (S,3,4) cam_from_world, OpenCV
    intrinsic = intrinsic[0].cpu().numpy().astype(np.float64)  # (S,3,3) at (tw, th)
    depth = depth[0, ..., 0].float().cpu().numpy()  # (S,th,tw)
    conf = depth_conf[0].float().cpu().numpy()
    del model, tokens
    if device == "cuda":
        torch.cuda.empty_cache()

    ws.vggt.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(ws.vggt / "predictions.npz", extrinsic=extrinsic, intrinsic=intrinsic,
                        depth=depth, conf=conf, names=np.array([f.name for f in frames]))

    valid = conf >= np.percentile(conf, conf_percentile)
    masks_used = ws.has_masks()
    if masks_used:
        for i, f in enumerate(frames):
            m = load_mask(ws.mask_for(f.name))
            if m is not None:
                valid[i] &= cv2.resize(m.astype(np.uint8), (tw, th), interpolation=cv2.INTER_NEAREST) > 0

    points, colors = _unproject(depth, valid, extrinsic, intrinsic, rgb_small)
    _write_colmap(ws, frames, extrinsic, intrinsic, (W0, H0), (tw, th), points, colors)
    mesh_path = _tsdf_fuse(ws.vggt / "tsdf_mesh.ply", depth, valid, extrinsic, intrinsic, rgb_small,
                           points, tsdf_resolution)
    return {"frames": len(frames), "device": device, "input_size": [tw, th], "points": len(points),
            "masks_used": masks_used, "mesh": str(mesh_path)}


def _unproject(depth, valid, extrinsic, intrinsic, rgb, max_points: int = 200_000):
    pts, cols = [], []
    for i in range(len(depth)):
        v, u = np.nonzero(valid[i])
        z = depth[i, v, u]
        K = intrinsic[i]
        cam = np.stack([(u + 0.5 - K[0, 2]) * z / K[0, 0], (v + 0.5 - K[1, 2]) * z / K[1, 1], z], 1)
        R, t = extrinsic[i, :, :3], extrinsic[i, :, 3]
        pts.append((cam - t) @ R)  # world = R^T (cam - t)
        cols.append(rgb[i][v, u])
    pts, cols = np.concatenate(pts), np.concatenate(cols)
    if len(pts) > max_points:
        sel = np.random.default_rng(0).choice(len(pts), max_points, replace=False)
        pts, cols = pts[sel], cols[sel]
    return pts, cols


def _write_colmap(ws, frames, extrinsic, intrinsic, full_size, small_size, points, colors):
    """Write VGGT cameras as a COLMAP model at full frame resolution (PINHOLE, per-image intrinsics)."""
    (W0, H0), (tw, th) = full_size, small_size
    sx, sy = W0 / tw, H0 / th
    rec = pycolmap.Reconstruction()
    for i, f in enumerate(frames, start=1):
        K = intrinsic[i - 1]
        cam = pycolmap.Camera.create_from_model_name(i, "PINHOLE", float(K[0, 0] * sx), W0, H0)
        cam.params = [K[0, 0] * sx, K[1, 1] * sy, K[0, 2] * sx, K[1, 2] * sy]
        rec.add_camera_with_trivial_rig(cam)
        R, t = extrinsic[i - 1, :, :3], extrinsic[i - 1, :, 3]
        pose = pycolmap.Rigid3d(pycolmap.Rotation3d(R), t)
        rec.add_image_with_trivial_frame(pycolmap.Image(name=f.name, camera_id=i, image_id=i), pose)
    for p, c in zip(points[::4], colors[::4]):
        rec.add_point3D(p, pycolmap.Track(), c.astype(np.uint8))
    if ws.colmap.exists():
        shutil.rmtree(ws.colmap)
    ws.sparse.mkdir(parents=True)
    rec.write(ws.sparse)
    # PINHOLE frames need no undistortion: lay out the dense/ folder 2DGS expects.
    if ws.dense.exists():
        shutil.rmtree(ws.dense)
    (ws.dense / "sparse").mkdir(parents=True)
    rec.write(ws.dense / "sparse")
    (ws.dense / "images").mkdir()
    for f in frames:
        shutil.copy2(f, ws.dense / "images" / f.name)


def _tsdf_fuse(out: Path, depth, valid, extrinsic, intrinsic, rgb, points, resolution: int) -> Path:
    """Dense TSDF fusion + marching cubes.

    Implemented directly in numpy: Open3D 0.20's TSDF integration (legacy and tensor
    API) silently integrates nothing on some platforms. The grid is processed in
    z-slabs so memory stays at a few hundred MB for resolution 256-384.
    """
    from skimage.measure import marching_cubes

    lo, hi = np.percentile(points, 1, axis=0), np.percentile(points, 99, axis=0)
    pad = 0.05 * (hi - lo).max()
    lo, hi = lo - pad, hi + pad
    voxel = float((hi - lo).max()) / resolution
    trunc = 4 * voxel
    dims = np.ceil((hi - lo) / voxel).astype(int) + 1
    tsdf = np.ones(dims, np.float32)
    weight = np.zeros(dims, np.float32)
    color = np.zeros((*dims, 3), np.float32)
    h, w = depth.shape[1:]
    gx, gy = np.meshgrid(np.arange(dims[0]), np.arange(dims[1]), indexing="ij")
    for z in range(dims[2]):
        X = lo + voxel * np.stack([gx.ravel(), gy.ravel(), np.full(gx.size, z)], 1)
        t_s, w_s, c_s = tsdf[:, :, z].ravel(), weight[:, :, z].ravel(), color[:, :, z].reshape(-1, 3)
        for i in range(len(depth)):
            pc = X @ extrinsic[i, :, :3].T + extrinsic[i, :, 3]
            K = intrinsic[i]
            zc = pc[:, 2]
            with np.errstate(divide="ignore", invalid="ignore"):
                u = np.floor(K[0, 0] * pc[:, 0] / zc + K[0, 2]).astype(np.int64)
                v = np.floor(K[1, 1] * pc[:, 1] / zc + K[1, 2]).astype(np.int64)
            ok = (zc > 0) & (u >= 0) & (u < w) & (v >= 0) & (v < h)
            idx = np.flatnonzero(ok)
            ok_px = valid[i][v[idx], u[idx]]
            idx, uu, vv = idx[ok_px], u[idx][ok_px], v[idx][ok_px]
            sdf = depth[i][vv, uu] - zc[idx]
            near = sdf > -trunc  # voxels far behind the surface are unobserved
            idx, uu, vv, sdf = idx[near], uu[near], vv[near], sdf[near]
            s = np.minimum(1.0, sdf / trunc)
            wn = w_s[idx] + 1
            t_s[idx] = (t_s[idx] * w_s[idx] + s) / wn
            c_s[idx] = (c_s[idx] * w_s[idx, None] + rgb[i][vv, uu]) / wn[:, None]
            w_s[idx] = wn
        tsdf[:, :, z], weight[:, :, z], color[:, :, z] = (
            t_s.reshape(dims[:2]), w_s.reshape(dims[:2]), c_s.reshape(*dims[:2], 3))

    observed = weight > 0
    if not ((tsdf < 0) & observed).any():
        raise RuntimeError("TSDF fusion found no surface (depth maps empty after confidence/mask filtering?)")
    # skimage tests the mask at one corner per cube; require all 8 corners observed,
    # otherwise unobserved voxels (tsdf=1) create skirts along the frustum borders.
    cube_ok = np.zeros_like(observed)
    cube_ok[:-1, :-1, :-1] = observed[:-1, :-1, :-1]
    for dx in (0, 1):
        for dy in (0, 1):
            for dz in (0, 1):
                cube_ok[:-1, :-1, :-1] &= observed[dx : dims[0] - 1 + dx, dy : dims[1] - 1 + dy, dz : dims[2] - 1 + dz]
    verts, faces, _, _ = marching_cubes(tsdf, level=0.0, mask=cube_ok)
    vi = np.clip(np.round(verts).astype(int), 0, dims - 1)
    mesh = o3d.geometry.TriangleMesh(o3d.utility.Vector3dVector(lo + verts * voxel),
                                     o3d.utility.Vector3iVector(faces[:, ::-1].copy()))
    mesh.vertex_colors = o3d.utility.Vector3dVector(color[vi[:, 0], vi[:, 1], vi[:, 2]] / 255.0)
    mesh.compute_vertex_normals()
    o3d.io.write_triangle_mesh(str(out), mesh)
    log.info("TSDF mesh: %d faces (voxel %.4g, grid %s) -> %s", len(faces), voxel, dims.tolist(), out)
    return out
