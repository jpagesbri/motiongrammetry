"""OpenMVS wrapper safeguards that don't need OpenMVS itself."""

import shutil
from collections import namedtuple

import pytest

from motiongrammetry import mvs
from motiongrammetry.workspace import Workspace
from test_geometry import H, W, _orbit_reconstruction

Usage = namedtuple("Usage", "total used free")


def _ws_with_dense(tmp_path) -> Workspace:
    ws = Workspace(tmp_path / "w")
    (ws.dense / "sparse").mkdir(parents=True)
    _orbit_reconstruction(n=10).write(ws.dense / "sparse")
    return ws


def test_depth_map_estimate_scales_with_resolution_level(tmp_path):
    ws = _ws_with_dense(tmp_path)
    full = mvs.depth_map_bytes(ws, 0)
    assert full == 10 * W * H * mvs.DMAP_BYTES_PER_PIXEL
    assert mvs.depth_map_bytes(ws, 1) == 10 * (W // 2) * (H // 2) * mvs.DMAP_BYTES_PER_PIXEL


def test_densify_refuses_when_disk_too_small_and_clears_stale_dmaps(tmp_path, monkeypatch):
    ws = _ws_with_dense(tmp_path)
    ws.mvs.mkdir()
    (ws.mvs / "depth0003.dmap").write_bytes(b"old poses")
    (ws.mvs / "depth0004.geo.dmap.tmp").write_bytes(b"")
    monkeypatch.setattr(shutil, "disk_usage", lambda _: Usage(1, 1, 1000))
    with pytest.raises(RuntimeError, match="not enough disk space"):
        mvs.densify_and_mesh(ws, resolution_level=0)
    assert not list(ws.mvs.glob("depth*"))
