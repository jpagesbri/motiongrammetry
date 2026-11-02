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


def test_openmvs_in_docker_logs_in_the_host_time_zone(tmp_path, monkeypatch):
    """Its log stamps are compared with ours on the run screen: the image alone runs in UTC."""
    import subprocess
    from pathlib import Path

    seen = {}

    class Started(Exception):
        pass

    def popen(cmd, **kw):
        seen["cmd"] = cmd
        raise Started

    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/docker" if name == "docker" else None)
    monkeypatch.setattr(subprocess, "Popen", popen)
    with pytest.raises(Started):
        mvs.OpenMVS(Workspace(tmp_path / "w")).run("TextureMesh", "-i", "x")
    assert seen["cmd"][:2] == ["docker", "run"]
    assert ("/etc/localtime:/etc/localtime:ro" in seen["cmd"]) == Path("/etc/localtime").exists()


def _pillow_mesh(path):
    """Two triangles of a square plus a "pillow" (the first again, reversed) and a degenerate face:
    what an editor save from before the fix could leave."""
    import numpy as np
    import open3d as o3d

    v = np.array([[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0]], float)
    f = np.array([[0, 1, 2], [0, 2, 3], [2, 1, 0], [1, 1, 3]])
    path.parent.mkdir(parents=True, exist_ok=True)
    o3d.io.write_triangle_mesh(str(path), o3d.geometry.TriangleMesh(o3d.utility.Vector3dVector(v),
                                                                    o3d.utility.Vector3iVector(f)))
    return path


def test_texture_leaves_out_the_faces_texturemesh_aborts_on(tmp_path, monkeypatch):
    """An old custom mesh with a pillow made TextureMesh abort (exit -6, std::out_of_range); texture
    now gives it a cleaned copy and leaves the mesh itself alone (#1)."""
    import numpy as np
    import open3d as o3d

    ws = Workspace(tmp_path / "w")
    (ws.mvs).mkdir(parents=True)
    (ws.mvs / "scene_dense.mvs").write_bytes(b"x")
    mesh = _pillow_mesh(ws.mesh / "custom" / "mesh_clean.ply")
    before = mesh.read_bytes()
    seen = {}

    def run(self, tool, *args):
        given = ws.root / args[args.index("-m") + 1].removeprefix("/work/")
        seen["faces"] = len(np.asarray(o3d.io.read_triangle_mesh(str(given)).triangles))
        (ws.mesh / "custom" / "textured.ply").write_bytes(b"ply")

    monkeypatch.setattr(mvs.OpenMVS, "__init__", lambda self, *a, **k: setattr(self, "native", True) or setattr(self, "root", ws.root))
    monkeypatch.setattr(mvs.OpenMVS, "p", lambda self, rel: str(ws.root / rel))
    monkeypatch.setattr(mvs.OpenMVS, "run", run)
    mvs.texture(ws, mesh, out_name="custom/textured")
    assert seen["faces"] == 2                              # the square, without the pillow and the degenerate face
    assert mesh.read_bytes() == before                     # the user's mesh is untouched
    assert not (ws.mesh / mvs.TEXTURE_INPUT).exists()      # the copy is gone afterwards


def test_a_clean_mesh_is_textured_as_it_is(tmp_path, monkeypatch):
    ws = Workspace(tmp_path / "w")
    (ws.mvs).mkdir(parents=True)
    (ws.mvs / "scene_dense.mvs").write_bytes(b"x")
    mesh = ws.mesh / "mesh_clean.ply"
    import numpy as np
    import open3d as o3d

    ws.mesh.mkdir(parents=True)
    o3d.io.write_triangle_mesh(str(mesh), o3d.geometry.TriangleMesh(
        o3d.utility.Vector3dVector(np.eye(3)), o3d.utility.Vector3iVector(np.array([[0, 1, 2]]))))
    seen = {}

    def run(self, tool, *args):
        seen["m"] = args[args.index("-m") + 1]
        (ws.mesh / "textured.ply").write_bytes(b"ply")

    monkeypatch.setattr(mvs.OpenMVS, "__init__", lambda self, *a, **k: setattr(self, "native", True) or setattr(self, "root", ws.root))
    monkeypatch.setattr(mvs.OpenMVS, "p", lambda self, rel: str(ws.root / rel))
    monkeypatch.setattr(mvs.OpenMVS, "run", run)
    mvs.texture(ws, mesh)
    assert seen["m"].endswith("mesh/mesh_clean.ply")


@pytest.mark.parametrize("rc", [-6, 134, -11, 139])
def test_an_abort_on_broken_geometry_is_named(rc, tmp_path):
    """std::out_of_range used to end as the generic failure or a bare segfault (#2): an abort (-6,
    134 through docker) or, with several threads throwing at once, a segfault."""
    lines = ["Assigning the best view to each face completed: 51970 faces",
             "terminate called after throwing an instance of 'std::out_of_range'", "  what():  unordered_map::at"]
    d = mvs._explain_exit("TextureMesh", rc, lines, tmp_path / "TextureMesh.log")
    assert d.code == "openmvs-degenerate-mesh" and "save it again" in d.hint
    plain = mvs._explain_exit("TextureMesh", rc, ["terminate called"], tmp_path / "x.log").code
    assert plain == ("openmvs-aborted" if rc in (-6, 134) else "openmvs-crashed")


def test_preflight_says_when_texture_will_leave_faces_out(tmp_path):
    from motiongrammetry import checks

    ws = Workspace(tmp_path / "w")
    (ws.mvs).mkdir(parents=True)
    (ws.mvs / "scene_dense.mvs").write_bytes(b"x")
    _pillow_mesh(ws.mesh / "mesh_clean.ply")
    codes = {d.code: d for d in checks._pre_texture(ws, {}, None)}
    assert "broken-faces" in codes and codes["broken-faces"].level == "info" and "2 broken faces" in codes["broken-faces"].message
