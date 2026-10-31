"""VGGT post-processing (no network / weights needed): resize rule, unprojection, COLMAP export, TSDF."""

import cv2
import numpy as np
import pycolmap

from motiongrammetry.feedforward import _target_size, _tsdf_fuse, _unproject, _write_colmap
from motiongrammetry.workspace import Workspace


def test_target_size_is_patch_aligned():
    for w, h in [(1920, 1080), (1080, 1920), (640, 480), (518, 518)]:
        tw, th = _target_size(w, h)
        assert tw % 14 == 0 and th % 14 == 0
        assert max(tw, th) == 518
        assert abs(tw / th - w / h) < 0.05


def _plane_scene(n=4, w=56, h=42):
    """Cameras looking down -z at a plane z=0 from z=2, offset along x."""
    K = np.array([[50.0, 0, w / 2], [0, 50.0, h / 2], [0, 0, 1]])
    extr, depth = [], []
    for i in range(n):
        R = np.diag([1.0, -1.0, -1.0])  # camera z points to world -z
        c = np.array([0.1 * i, 0, 2.0])
        extr.append(np.hstack([R, (-R @ c)[:, None]]))
        depth.append(np.full((h, w), 2.0, np.float32))
    return np.stack(extr), np.stack([K] * n), np.stack(depth)


def test_unproject_lands_on_plane():
    extr, intr, depth = _plane_scene()
    rgb = [np.zeros(depth.shape[1:] + (3,), np.uint8)] * len(depth)
    pts, _ = _unproject(depth, np.ones_like(depth, bool), extr, intr, rgb)
    assert np.abs(pts[:, 2]).max() < 1e-9


def test_write_colmap_scales_intrinsics_to_full_frames(tmp_path):
    ws = Workspace(tmp_path / "w")
    ws.frames.mkdir()
    extr, intr, depth = _plane_scene(n=2)
    frames = []
    for i in range(2):
        p = ws.frames / f"frame_{i:05d}.jpg"
        cv2.imwrite(str(p), np.zeros((420, 560, 3), np.uint8))
        frames.append(p)
    pts = np.zeros((8, 3))
    _write_colmap(ws, frames, extr, intr, (560, 420), (56, 42), pts, np.zeros((8, 3), np.uint8))
    rec = pycolmap.Reconstruction(ws.sparse)
    cam = rec.images[1].camera
    assert (cam.width, cam.height) == (560, 420)
    np.testing.assert_allclose(cam.params, [500, 500, 280, 210])
    np.testing.assert_allclose(rec.images[2].cam_from_world().matrix(), extr[1], atol=1e-9)
    assert sorted(p.name for p in (ws.dense / "images").iterdir()) == [f.name for f in frames]


def test_tsdf_fuse_produces_mesh(tmp_path):
    extr, intr, depth = _plane_scene(n=6)
    valid = np.ones_like(depth, bool)
    rgb = [np.full(depth.shape[1:] + (3,), 128, np.uint8)] * len(depth)
    pts, _ = _unproject(depth, valid, extr, intr, rgb)
    out = _tsdf_fuse(tmp_path / "m.ply", depth, valid, extr, intr, rgb, pts, resolution=64)
    import open3d as o3d

    mesh = o3d.io.read_triangle_mesh(str(out))
    assert len(mesh.triangles) > 100
    assert np.abs(np.asarray(mesh.vertices)[:, 2]).max() < 0.1
