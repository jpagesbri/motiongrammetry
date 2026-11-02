"""Web UI server: workspace lifecycle, preview endpoints, validation and path safety."""

import json
import os
import re
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer

import numpy as np
import pycolmap
import pytest

from motiongrammetry.web.server import App, make_handler
from motiongrammetry.workspace import Workspace
from test_geometry import _orbit_reconstruction


@pytest.fixture()
def server(tmp_path):
    app = App(tmp_path / "root")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", app
    httpd.shutdown()


def call(url, method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    try:
        with urllib.request.urlopen(req) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if r.headers.get_content_type() == "application/json" else raw)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_workspace_lifecycle_and_previews(server, tmp_path):
    base, app = server
    assert call(base + "/")[0] == 200
    status, _ = call(base + "/api/workspaces", "POST", {"name": "obj"})
    assert status == 201
    assert call(base + "/api/workspaces", "POST", {"name": "obj"})[0] == 400  # duplicate
    assert call(base + "/api/workspaces", "POST", {"name": "../x"})[0] == 400
    # stray folders are not listed as workspaces
    (app.root / "not-a-workspace").mkdir()
    assert [w["name"] for w in call(base + "/api/workspaces")[1]] == ["obj"]

    ws = Workspace(app.root / "obj")
    rec = _orbit_reconstruction()
    rng = np.random.default_rng(0)
    for p in rng.normal(size=(300, 3)) * 0.3 + [0, 0, 0.5]:
        rec.add_point3D(p, pycolmap.Track(), np.array([10, 20, 30], np.uint8))
    ws.sparse.mkdir(parents=True)
    rec.write(ws.sparse)

    state = call(base + "/api/workspaces/obj/state")[1]
    assert state["layers"]["cameras"] and state["layers"]["sparse"] and not state["layers"]["textured"]

    scene = call(base + "/api/workspaces/obj/scene")[1]
    assert len(scene["cameras"]) == 12 and np.array(scene["transform"]).shape == (4, 4)

    status, buf = call(base + "/api/workspaces/obj/points/sparse")
    (n,) = struct.unpack_from("<I", buf)
    xyz = np.frombuffer(buf, "<f4", n * 3, 4).reshape(n, 3)
    rgb = np.frombuffer(buf, np.uint8, n * 3, 4 + 12 * n).reshape(n, 3)
    assert n == 300 and len(buf) == 4 + 15 * n
    assert np.allclose(rgb, [10, 20, 30]) and abs(np.median(xyz[:, 2]) - 0.5) < 0.1

    assert call(base + "/api/workspaces/obj/mesh/clean.glb")[0] == 404  # not produced yet
    assert call(base + "/api/workspaces/obj/thumb/frames/..%2F..%2Fmanifest.json")[0] == 404
    assert call(base + "/api/workspaces/nope/state")[0] == 404


def test_run_validation(server):
    base, _ = server
    call(base + "/api/workspaces", "POST", {"name": "w"})
    status, body = call(base + "/api/workspaces/w/run", "POST", {"stages": ["frames"]})
    assert status == 400 and "no video" in body["error"]
    status, body = call(base + "/api/workspaces/w/run", "POST", {"stages": ["bogus"]})
    assert status == 400


def test_upload_sets_video(server):
    base, _ = server
    call(base + "/api/workspaces", "POST", {"name": "u"})
    req = urllib.request.Request(base + "/api/workspaces/u/video?name=clip.mp4", data=b"x" * 3_000_000, method="PUT")
    with urllib.request.urlopen(req) as r:
        meta = json.loads(r.read())["meta"]
    assert meta["video"].endswith("clip.mp4")
    from pathlib import Path

    assert Path(meta["video"]).stat().st_size == 3_000_000


def test_outputs_older_than_their_inputs_are_stale(tmp_path):
    import os

    app = App(tmp_path / "root")
    ws = Workspace(app.root / "s")
    ws.sparse.mkdir(parents=True)
    _orbit_reconstruction().write(ws.sparse)
    ws.mesh.mkdir()
    for name in ("mesh_clean.ply", "textured.ply"):
        (ws.mesh / name).write_text("ply")
    old = (ws.sparse / "images.bin").stat().st_mtime - 60
    os.utime(ws.mesh / "mesh_clean.ply", (old, old))  # clean ran before the latest SfM
    assert app.stale(ws) == ["clean", "texture"]  # texture inherits staleness from clean
    layers = app.layers(ws)
    assert layers["cameras"] and not layers["clean"] and not layers["textured"]


def test_exports_are_made_when_asked_and_reused_until_their_inputs_change(server):
    import os

    import open3d as o3d
    import trimesh

    base, app = server
    call(base + "/api/workspaces", "POST", {"name": "p"})
    ws = Workspace(app.root / "p")
    ws.mesh.mkdir()
    box = o3d.geometry.TriangleMesh.create_box(1, 2, 3).subdivide_midpoint(2)
    box.remove_triangles_by_index([0, 1])  # a hole: the print must still be closed
    o3d.io.write_triangle_mesh(str(ws.mesh / "mesh_clean.ply"), box)
    state = lambda: call(base + "/api/workspaces/p/state")[1]  # noqa: E731
    assert "outputs" not in state() and state()["print_check"] == {}  # nothing is made until asked
    assert state()["export_defaults"]["height_mm"] == 100 and state()["export_defaults"]["up"] == "auto"
    body = {"items": [{"content": "print", "format": "stl"}, {"content": "mesh", "format": "glb"}]}
    status = lambda: [i["state"] for i in call(base + "/api/workspaces/p/export/status", "POST", body)[1]["items"]]  # noqa: E731
    assert status() == ["none", "none"]

    r = call(base + "/api/workspaces/p/export", "POST", body)[1]
    assert not r["cached"] and [f["cached"] for f in r["files"]] == [False, False]
    with urllib.request.urlopen(base + "/api/workspaces/p/exports/p-print.stl") as resp:
        mesh = trimesh.load(trimesh.util.wrap_as_stream(resp.read()), file_type="stl")
    assert mesh.is_watertight and abs(mesh.extents[2] - 100) < 1e-6  # the scan's print height by default
    assert status() == ["ready", "ready"]
    stamp = (ws.root / ".ui" / "exports" / "p-print.stl").stat().st_mtime_ns
    r = call(base + "/api/workspaces/p/export", "POST", body)[1]  # again: the same files, at once
    assert r["cached"] and (ws.root / ".ui" / "exports" / "p-print.stl").stat().st_mtime_ns == stamp
    pc = state()["print_check"]["pipeline"]
    assert pc["current"] and pc["watertight"] and abs(pc["size_mm"][2] - 100) < 0.5

    # other settings or a changed source: outdated, made again when asked
    other = {**body, "options": {"height_mm": 50}}
    assert [i["state"] for i in call(base + "/api/workspaces/p/export/status", "POST", other)[1]["items"]] == ["outdated", "outdated"]
    t = (ws.mesh / "mesh_clean.ply").stat().st_mtime_ns + 10**9
    os.utime(ws.mesh / "mesh_clean.ply", ns=(t, t))
    assert status() == ["outdated", "outdated"] and not state()["print_check"]["pipeline"]["current"]
    r = call(base + "/api/workspaces/p/export", "POST", body)[1]
    assert not r["cached"] and status() == ["ready", "ready"] and state()["print_check"]["pipeline"]["current"]
    # the records next to the files are not served
    assert call(base + "/api/workspaces/p/exports/p-print.stl.json")[0] == 404
    assert call(base + "/api/workspaces/p/download?path=mesh/mesh_clean.ply")[0] == 404  # the old file list is gone


def test_clean_follows_the_raw_mesh_it_used(tmp_path):
    """A vggt run in an mvs workspace replaces the cameras: the old mvs mesh is stale, but a
    clean made from the vggt mesh is not, and the raw layer shows the mesh clean used."""
    import os

    app = App(tmp_path / "root")
    ws = Workspace(app.root / "v")
    (ws.mvs).mkdir()
    (ws.mvs / "scene_mesh.ply").write_text("ply")
    ws.sparse.mkdir(parents=True)
    _orbit_reconstruction().write(ws.sparse)  # vggt rewrote the cameras after mvs
    ws.vggt.mkdir()
    (ws.vggt / "tsdf_mesh.ply").write_text("ply")
    ws.mesh.mkdir()
    (ws.mesh / "mesh_clean.ply").write_text("ply")
    ws.record("clean", {"input": str(ws.vggt / "tsdf_mesh.ply")})
    old = (ws.sparse / "images.bin").stat().st_mtime - 60
    os.utime(ws.mvs / "scene_mesh.ply", (old, old))

    assert ws.raw_mesh() == ws.vggt / "tsdf_mesh.ply"
    assert app.stale(ws) == ["mvs"]
    layers = app.layers(ws)
    assert layers["clean"] and layers["raw"]

    ws.record("clean", {"input": str(ws.mvs / "scene_mesh.ply")})  # cleaned the stale mvs mesh instead
    assert app.stale(ws) == ["mvs", "clean"]


def test_terminal_runs_replace_a_finished_ui_job(tmp_path):
    import time

    from motiongrammetry.web.server import Job

    app = App(tmp_path / "root")
    ws = Workspace(app.root / "j")
    job = Job.__new__(Job)  # a finished UI run, without starting a thread
    job.status, job.ended = "failed", time.time() - 10
    app.jobs["j"] = job
    assert app.job(ws) is job
    ws.append_history({"stage": "clean", "status": "ok", "started": time.time()})
    assert app.job(ws) is None


def _sfm_workspace(root, name="src"):
    """Frames + masks + SfM + MVS, each with its completion stamp, as the pipeline leaves them."""
    ws = Workspace(root / name)
    ws.frames.mkdir()
    ws.masks.mkdir()
    for i in range(3):
        (ws.frames / f"f{i}.jpg").write_bytes(b"jpg")
        (ws.masks / f"f{i}.jpg.png").write_bytes(b"png")
    ws.sparse.mkdir(parents=True)
    _orbit_reconstruction().write(ws.sparse)
    (ws.dense / "images").mkdir(parents=True)
    ws.mvs.mkdir()
    (ws.mvs / "scene_mesh.ply").write_text("ply")
    for stage in ("frames", "mask", "sfm", "mvs"):
        ws.stamp(stage)
        ws.record(stage, {"stage": stage})
    return ws


def test_vggt_refuses_to_replace_sfm_cameras(tmp_path):
    from motiongrammetry import checks
    from motiongrammetry.options import resolve

    ws = _sfm_workspace(tmp_path)
    assert ws.cameras_from() == "sfm"
    codes = [d.code for d in checks.preflight("vggt", ws, resolve({}))]
    assert "replaces-cameras" in codes
    # after a vggt run the cameras are its own: running it again is fine
    ws.vggt.mkdir()
    (ws.vggt / "tsdf_mesh.ply").write_text("ply")
    ws.stamp("vggt")
    ws.stamp("sfm", by="vggt")
    assert ws.cameras_from() == "vggt"
    assert "replaces-cameras" not in [d.code for d in checks.preflight("vggt", ws, resolve({}))]


def test_duplicate_workspace(server):
    base, app = server
    src = _sfm_workspace(app.root)
    app.set_meta(src, video=".ui/clip.mp4")  # an uploaded video, stored relative to the workspace
    (src.root / ".ui" / "clip.mp4").write_bytes(b"mp4")

    status, state = call(base + "/api/workspaces/src/duplicate", "POST", {"name": "try-vggt"})
    assert status == 201 and state["done"]["frames"] and state["done"]["mask"] and not state["done"]["sfm"]
    dst = Workspace(app.root / "try-vggt")
    assert sorted(p.name for p in dst.frames.iterdir()) == ["f0.jpg", "f1.jpg", "f2.jpg"]
    assert state["meta"]["video"] == str(src.root / ".ui" / "clip.mp4")  # still points at the original
    assert set(dst.load_manifest()) == {"frames", "mask", "diagnostics"}
    assert dst.stale_stages() == []

    status, state = call(base + "/api/workspaces/src/duplicate", "POST", {"name": "full", "reconstruction": True})
    assert status == 201 and state["done"]["sfm"] and state["done"]["mvs"] and state["cameras"] == "sfm"
    assert Workspace(app.root / "full").stale_stages() == []

    (src.root / ".stamps" / "mvs").unlink()  # mvs running: not copied half-written
    call(base + "/api/workspaces/src/duplicate", "POST", {"name": "mid", "reconstruction": True})
    assert not (app.root / "mid" / "mvs").exists() and (app.root / "mid" / "colmap").exists()

    assert call(base + "/api/workspaces/src/duplicate", "POST", {"name": "full"})[0] == 400  # exists
    assert call(base + "/api/workspaces/src/duplicate", "POST", {"name": "../x"})[0] == 400
    assert (src.root / "mvs" / "scene_mesh.ply").read_text() == "ply"  # original untouched


def test_state_reports_mesh_sources(server):
    base, app = server
    ws = _sfm_workspace(app.root, "m")
    ws.vggt.mkdir()
    (ws.vggt / "tsdf_mesh.ply").write_text("ply")
    ws.mesh.mkdir()
    (ws.mesh / "mesh_clean.ply").write_text("ply")
    ws.record("clean", {"input": str(ws.mvs / "scene_mesh.ply")})
    state = call(base + "/api/workspaces/m/state")[1]
    assert state["raw_meshes"] == ["mvs", "vggt"] and state["raw_source"] == "vggt"
    assert state["clean_source"] == "mvs"
    assert "upstream" in call(base + "/api/options")[1]


def _post_bytes(url, data):
    req = urllib.request.Request(url, data=data, method="POST", headers={"Content-Type": "application/octet-stream"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_editor_saves_a_custom_mesh_and_leaves_the_pipeline_files_alone(server):
    import open3d as o3d

    base, app = server
    call(base + "/api/workspaces", "POST", {"name": "e"})
    ws = Workspace(app.root / "e")
    ws.mesh.mkdir()
    box = o3d.geometry.TriangleMesh.create_box(1, 1, 1).subdivide_midpoint(2)
    box.vertex_colors = o3d.utility.Vector3dVector(np.full((len(box.vertices), 3), 0.5))
    o3d.io.write_triangle_mesh(str(ws.mesh / "mesh_clean.ply"), box)
    pipeline_bytes = (ws.mesh / "mesh_clean.ply").read_bytes()
    with urllib.request.urlopen(base + "/edit") as r:
        assert b"Sculpt mesh" in r.read()

    v = np.asarray(box.vertices, np.float32).copy()
    v[:, 2] *= 2  # "sculpt": stretch it
    f = np.asarray(box.triangles, np.uint32)[:-4]  # and cut 4 faces away
    col = np.full((len(v), 3), 128, np.uint8)
    body = struct.pack("<II", len(v), len(f)) + v.tobytes() + col.tobytes() + f.tobytes()
    status, r = _post_bytes(base + "/api/workspaces/e/edit/mesh", body)
    assert status == 200, r
    assert r["faces"] == len(f)
    assert (ws.mesh / "mesh_clean.ply").read_bytes() == pipeline_bytes  # untouched
    c = ws.mesh / "custom"
    out = o3d.io.read_triangle_mesh(str(c / "mesh_clean.ply"))
    assert np.isclose(np.asarray(out.vertices)[:, 2].max(), 2.0) and len(out.triangles) == len(f)
    assert not (c / "mesh_clean.glb").exists() and not (c / "print.stl").exists()  # exports: when asked
    state = call(base + "/api/workspaces/e/state")[1]
    assert state["custom"]["mesh"] and not state["custom"]["mesh_outdated"] and state["layers"]["custom"]
    assert state["exportable"]["custom"] == ["mesh", "print"]

    from motiongrammetry.clean import texture_source
    assert texture_source(ws)[1] == "custom"  # texture would project onto the edit
    old = time.time() - 60
    os.utime(c / "mesh_clean.ply", (old, old))  # ...unless clean has run again since
    assert texture_source(ws)[1] == "clean"
    assert call(base + "/api/workspaces/e/state")[1]["custom"]["mesh_outdated"]

    assert _post_bytes(base + "/api/workspaces/e/edit/mesh", body[:-4])[0] == 400  # truncated

    # two boxes touching at one corner: a bow-tie vertex, which crashes OpenMVS TextureMesh
    pair = o3d.geometry.TriangleMesh.create_box(1, 1, 1) + o3d.geometry.TriangleMesh.create_box(1, 1, 1).translate((1, 1, 1))
    pair.remove_duplicated_vertices()
    bv, bf = np.asarray(pair.vertices, np.float32), np.asarray(pair.triangles, np.uint32)
    bow = struct.pack("<II", len(bv), len(bf)) + bv.tobytes() + np.zeros((len(bv), 3), np.uint8).tobytes() + bf.tobytes()
    status, r = _post_bytes(base + "/api/workspaces/e/edit/mesh", bow)
    assert status == 200 and r["nonmanifold_vertices_removed"] >= 1, (status, r)
    saved = o3d.io.read_triangle_mesh(str(c / "mesh_clean.ply"))
    assert len(saved.get_non_manifold_vertices()) == 0
    empty = struct.pack("<II", 3, 1) + np.eye(3, dtype=np.float32).tobytes() + bytes(9) + np.array([0, 0, 0], np.uint32).tobytes()
    assert _post_bytes(base + "/api/workspaces/e/edit/mesh", empty)[0] == 400

    status, r = call(base + "/api/workspaces/e/edit/delete", "POST", {"what": "mesh"})
    assert status == 200 and "mesh_clean.ply" in r["deleted"]
    assert not (c / "mesh_clean.ply").exists() and (ws.mesh / "mesh_clean.ply").read_bytes() == pipeline_bytes
    assert call(base + "/api/workspaces/e/edit/delete", "POST", {"what": "mesh"})[0] == 404


def test_editor_saves_a_custom_texture(server):
    import open3d as o3d

    base, app = server
    call(base + "/api/workspaces", "POST", {"name": "t"})
    ws = Workspace(app.root / "t")
    ws.mesh.mkdir()
    box = o3d.geometry.TriangleMesh.create_box(1, 1, 1)
    o3d.io.write_triangle_mesh(str(ws.mesh / "mesh_clean.ply"), box)
    o3d.io.write_triangle_mesh(str(ws.mesh / "textured.ply"), box)
    png = b"\x89PNG\r\n\x1a\n" + b"original"
    (ws.mesh / "textured0.png").write_bytes(png)

    assert _post_bytes(base + "/api/workspaces/t/edit/texture?i=0", b"not a png")[0] == 400
    assert _post_bytes(base + "/api/workspaces/t/edit/texture?i=3", png)[0] == 404
    painted = b"\x89PNG\r\n\x1a\n" + b"painted"
    status, r = _post_bytes(base + "/api/workspaces/t/edit/texture?i=0&from=pipeline", painted)
    assert status == 200, r
    c = ws.mesh / "custom"
    assert (c / "textured0.png").read_bytes() == painted and (c / "textured.ply").exists()
    assert (ws.mesh / "textured0.png").read_bytes() == png  # pipeline texture untouched
    assert not (c / "model.glb").exists()  # its exports are made when asked
    again = b"\x89PNG\r\n\x1a\n" + b"painted twice"
    assert _post_bytes(base + "/api/workspaces/t/edit/texture?i=0&from=custom", again)[0] == 200
    assert (c / "textured0.png").read_bytes() == again
    state = call(base + "/api/workspaces/t/state")[1]
    assert state["custom"]["texture"] and state["custom"]["texture_from"] == "clean"
    status, r = call(base + "/api/workspaces/t/edit/delete", "POST", {"what": "texture"})
    assert status == 200 and "textured0.png" in r["deleted"] and not (c / "textured.ply").exists()


def test_scan_previews(server):
    from PIL import Image

    base, app = server
    call(base + "/api/workspaces", "POST", {"name": "p"})
    ws = Workspace(app.root / "p")
    assert call(base + "/api/workspaces/p/preview.jpg")[0] == 404  # nothing to show yet
    ws.frames.mkdir()
    for i in range(3):
        Image.new("RGB", (64, 48), (i * 80, 0, 0)).save(ws.frames / f"frame_{i:04d}.jpg")
    with urllib.request.urlopen(base + "/api/workspaces/p/preview.jpg") as r:  # a keyframe meanwhile
        assert r.read()[:2] == b"\xff\xd8"
    ws.mesh.mkdir()
    (ws.mesh / "mesh_clean.ply").write_text("ply")
    assert call(base + "/api/workspaces/p/state")[1]["preview_stale"]
    assert _post_bytes(base + "/api/workspaces/p/preview", b"not a jpeg")[0] == 400
    import io
    buf = io.BytesIO()
    Image.new("RGB", (32, 24), (0, 200, 0)).save(buf, "JPEG")
    assert _post_bytes(base + "/api/workspaces/p/preview", buf.getvalue())[0] == 200
    state = call(base + "/api/workspaces/p/state")[1]
    assert not state["preview_stale"]
    with urllib.request.urlopen(base + "/api/workspaces/p/preview.jpg") as r:
        assert r.read() == buf.getvalue()
    item = next(w for w in call(base + "/api/workspaces")[1] if w["name"] == "p")
    assert item["preview"] and "frames" in item and "last_run" in item


def test_editor_refuses_to_save_during_a_run(server):
    base, app = server
    call(base + "/api/workspaces", "POST", {"name": "busy"})
    ws = Workspace(app.root / "busy")

    class Running:
        status = "running"
        ended = None
    app.jobs["busy"] = Running()
    status, r = _post_bytes(base + "/api/workspaces/busy/edit/mesh", struct.pack("<II", 0, 0))
    assert status == 400 and "pipeline run" in r["error"]


def test_export_window_builds_files_with_their_own_settings(server):
    import io
    import zipfile

    import open3d as o3d
    import trimesh
    from PIL import Image

    base, app = server
    call(base + "/api/workspaces", "POST", {"name": "x"})
    ws = Workspace(app.root / "x")
    ws.mesh.mkdir()
    box = o3d.geometry.TriangleMesh.create_box(1, 2, 3).subdivide_midpoint(2)
    box.vertex_colors = o3d.utility.Vector3dVector(np.full((len(box.vertices), 3), 0.4))
    o3d.io.write_triangle_mesh(str(ws.mesh / "mesh_clean.ply"), box)
    # a textured mesh: box with UVs and a PNG, written by trimesh as OpenMVS would
    tm = trimesh.creation.box((1, 2, 3))
    tm.visual = trimesh.visual.TextureVisuals(uv=np.random.default_rng(0).random((len(tm.vertices), 2)),
                                              image=Image.new("RGB", (8, 8), (200, 0, 0)))
    tm.export(str(ws.mesh / "textured.ply"))

    def export(fmt, **kw):
        status, r = call(base + "/api/workspaces/x/export", "POST", {"format": fmt, **kw})
        assert status == 200, r
        with urllib.request.urlopen(base + "/api/workspaces/x/exports/" + r["file"]) as resp:
            return r, resp.read()

    r, data = export("stl", options={"height_mm": 50, "mode": "shell", "shell": 2, "shell_side": "inward", "stand": "none"})
    stl = trimesh.load(trimesh.util.wrap_as_stream(data), file_type="stl")
    assert stl.is_watertight and abs(stl.extents[2] - 50) < 1e-3 and r["info"]["print"]["mode"] == "shell"
    r, data = export("glb_mesh", options={"height_mm": 200})  # glTF in metres: 0.2 m tall
    glb = trimesh.load(trimesh.util.wrap_as_stream(data), file_type="glb", force="mesh")
    assert abs(glb.extents[1] - 0.2) < 1e-3
    r, data = export("ply", options={"height_mm": 30, "up": "none"})
    assert abs(trimesh.load(trimesh.util.wrap_as_stream(data), file_type="ply").extents[1] - 30) < 1e-3
    r, data = export("obj")
    assert {"model.obj", "model.mtl"} <= set(zipfile.ZipFile(io.BytesIO(data)).namelist())
    r, data = export("glb_textured")
    assert data[:4] == b"glTF"
    # bad input
    assert call(base + "/api/workspaces/x/export", "POST", {"format": "stl", "options": {"mode": "hollow"}})[0] == 400
    assert call(base + "/api/workspaces/x/export", "POST", {"format": "glb_mesh", "source": "custom"})[0] == 404
    assert call(base + "/api/workspaces/x/exports/..%2Fmanifest.json")[0] == 404

    # pick the contents, each in its format: several come as one zip, a folder each
    assert call(base + "/api/workspaces/x/state")[1]["exportable"] == {"pipeline": ["textured", "mesh", "print"],
                                                                       "custom": []}
    items = [{"content": "print", "format": "3mf"}, {"content": "mesh", "format": "obj"},
             {"content": "textured", "format": "glb"}]
    status, r = call(base + "/api/workspaces/x/export", "POST", {"items": items, "options": {"height_mm": 40}})
    assert status == 200 and r["bundle"] and [f["content"] for f in r["files"]] == ["print", "mesh", "textured"]
    with urllib.request.urlopen(base + "/api/workspaces/x/exports/" + r["file"]) as resp:
        z = zipfile.ZipFile(io.BytesIO(resp.read()))
    assert {"print/x-print.3mf", "mesh/model.obj", "textured/x-textured.glb"} <= set(z.namelist())
    model = z.read("print/x-print.3mf")
    three = zipfile.ZipFile(io.BytesIO(model))  # a 3MF is a zip of XML, with its unit
    xml = three.read("3D/3dmodel.model").decode()
    assert 'unit="millimeter"' in xml and xml.count("<triangle ") > 12 and "[Content_Types].xml" in three.namelist()
    assert r["files"][0]["preview"].endswith(".stl")
    assert len(z.read("mesh/model.obj").decode().split("\nv ")[1].split()) == 6  # vertex colours on each v line
    # contents the scan does not have, unknown formats, the same content twice
    for bad, code in [([{"content": "cloud", "format": "ply"}], 404), ([{"content": "print", "format": "glb"}], 400),
                      ([{"content": "mesh", "format": "ply"}, {"content": "mesh", "format": "glb"}], 400), ([], 400)]:
        assert call(base + "/api/workspaces/x/export", "POST", {"items": bad})[0] == code


def test_texture_layout_is_rebuilt_into_the_custom_texture(server):
    import trimesh
    from test_atlas import scattered_box

    base, app = server
    call(base + "/api/workspaces", "POST", {"name": "lay"})
    ws = Workspace(app.root / "lay")
    ws.mesh.mkdir()
    scattered_box(ws.mesh)
    trimesh.load(str(ws.mesh / "textured.ply"), process=True).export(str(ws.mesh / "mesh_clean.ply"))
    url = base + "/api/workspaces/lay/edit/layout"
    status, r = call(url, "POST", {"angle": 45, "min_size": 0.5, "preview": True})
    assert status == 200 and (r["pieces_now"], r["pieces"], r["source"]) == (54, 6, "pipeline")
    assert not (ws.mesh / "custom").exists()  # counting writes nothing
    status, r = call(url, "POST", {"angle": 45, "min_size": 0.5})
    assert status == 200 and r["pieces"] == 6
    c = ws.mesh / "custom"
    assert {"textured.ply", "textured0.png"} <= {p.name for p in c.iterdir()} and not (c / ".layout").exists()
    state = call(base + "/api/workspaces/lay/state")[1]
    assert state["custom"]["texture"] and state["custom"]["texture_from"] == "clean"
    # a second rebuild starts from the custom texture; the pipeline's files never change
    status, r = call(url, "POST", {"angle": 30})
    assert status == 200 and r["source"] == "custom" and r["pieces_now"] == 6
    assert trimesh.load(str(ws.mesh / "textured.ply"), process=False).visual.material.image.size == (1024, 1024)
    assert call(url, "POST", {"angle": 5})[0] == 400
    # seams drawn in the editor: one byte per face of the model it showed (here: its own seams again)
    from test_atlas import seam_mask

    mask = seam_mask(trimesh.load(str(c / "textured.ply"), process=False)).tobytes()

    def seams(source):
        req = urllib.request.Request(url + f"?seams=1&from={source}", data=mask, method="POST")
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    status, r = seams("custom")
    assert status == 200 and r["pieces"] == r["pieces_now"] and r["recut"] == 0
    assert seams("pipeline")[0] == 400  # the editor showed another model: refused

    # a preview: an image of the new texture, nothing saved
    before = {p.name: p.stat().st_mtime_ns for p in c.iterdir() if p.is_file()}
    status, r = call(url, "POST", {"angle": 60, "preview": "image"})
    assert status == 200 and r["image"].startswith("data:image/png;base64,") and max(r["size"]) <= 1024
    assert {p.name: p.stat().st_mtime_ns for p in c.iterdir() if p.is_file()} == before
    assert not (ws.root / ".ui" / "layout-preview").exists()

    # undo: back to the layout the last rebuild replaced, one step
    state = lambda: call(base + "/api/workspaces/lay/state")[1]["custom"]  # noqa: E731
    first = (c / "textured0.png").stat().st_ino  # the very file set aside comes back
    assert call(url, "POST", {"angle": 45})[0] == 200 and state()["layout_undo"]
    assert (c / "textured0.png").stat().st_ino != first
    assert call(url, "POST", {"undo": True})[1] == {"restored": "custom"}
    assert (c / "textured0.png").stat().st_ino == first and not state()["layout_undo"]
    assert call(url, "POST", {"undo": True})[0] == 404  # nothing more to go back to
    # painting after a rebuild ends its undo (it would throw the paint away)
    assert call(url, "POST", {"angle": 45})[0] == 200
    png = (c / "textured0.png").read_bytes()
    req = urllib.request.Request(base + "/api/workspaces/lay/edit/texture?i=0&from=custom", data=png, method="POST")
    urllib.request.urlopen(req).read()
    assert not state()["layout_undo"]
    # a rebuild from the pipeline's texture goes back to it: no custom texture left
    assert call(base + "/api/workspaces/lay/edit/delete", "POST", {"what": "texture"})[0] == 200
    assert call(url, "POST", {"angle": 45})[0] == 200
    assert call(url, "POST", {"undo": True})[1] == {"restored": "pipeline"}
    assert not state()["texture"] and not list(c.glob("textured*"))


def test_texture_is_carried_over_to_the_sculpted_mesh(server):
    import trimesh
    from test_atlas import scattered_box

    base, app = server
    call(base + "/api/workspaces", "POST", {"name": "tr"})
    ws = Workspace(app.root / "tr")
    (ws.mesh / "custom").mkdir(parents=True)
    scattered_box(ws.mesh)
    welded = trimesh.load(str(ws.mesh / "textured.ply"), process=True)
    welded.export(str(ws.mesh / "mesh_clean.ply"))
    welded.update_faces(welded.triangles_center[:, 1] < 0.5 - 1e-6)  # sculpted: the top removed
    welded.remove_unreferenced_vertices()
    welded.export(str(ws.mesh / "custom" / "mesh_clean.ply"))
    state = lambda: call(base + "/api/workspaces/tr/state")[1]  # noqa: E731
    assert state()["can_texture"] is False  # no OpenMVS scene: only carrying over is possible
    status, r = call(base + "/api/workspaces/tr/edit/transfer", "POST", {})
    assert status == 200 and r["pieces"] == 5 and r["source"] == "pipeline"
    c = state()["custom"]
    assert c["texture"] and c["texture_from"] == "custom" and not c["texture_stale"] and c["layout_undo"]
    assert len(trimesh.load(str(ws.mesh / "custom" / "textured.ply"), process=False).faces) == len(welded.faces)
    assert call(base + "/api/workspaces/tr/edit/layout", "POST", {"undo": True})[1] == {"restored": "pipeline"}
    assert not state()["custom"]["texture"]
    (ws.mesh / "custom" / "mesh_clean.ply").unlink()
    assert call(base + "/api/workspaces/tr/edit/transfer", "POST", {})[0] == 404  # nothing sculpted


def test_advice_reads_the_results_and_suggests_what_to_change(server):
    from motiongrammetry.advice import advise

    base, app = server
    call(base + "/api/workspaces", "POST", {"name": "a"})
    ws = Workspace(app.root / "a")
    ws.record("frames", {"keyframes": 132, "segments": 133, "blur_rejected": 1, "total_motion_widths": 15})
    ws.record("mask", {"frames": 132, "mean_coverage": 0.57, "coverage_cv": 0.34, "empty_masks": 0})
    ws.record("sfm", {"frames": 132, "registered": 90, "points3D": 8000, "mean_reproj_error_px": 1.5})
    # face before the fix: 21% carved, almost all of it where the masks were unsure
    ws.record("clean", {"vertices_in": 373376, "carved_vertices": 78065, "carved_uncertain": 0.99,
                        "faces_out": 585819, "support_plane": False})
    ws.append_history({"stage": "clean", "status": "ok", "options": {"bg_tolerance": 0.15}})
    items = {(i["stage"], i["level"]): i for i in advise(ws, "person")}
    assert items[("clean", "fix")]["apply"] == {"bg_tolerance": 0.35}
    assert items[("clean", "fix")]["rerun"] == ["clean", "texture"]
    assert items[("sfm", "fix")]["apply"]["mapper"] == "incremental"
    assert ("frames", "ok") in items and ("mask", "ok") in items
    # a gentler run already: no more suggestion to carve gently
    ws.append_history({"stage": "clean", "status": "ok", "options": {"bg_tolerance": 0.35}})
    assert ("clean", "fix") not in {(i["stage"], i["level"]) for i in advise(ws)}
    # over HTTP, and the assisted mode's choices
    status, r = call(base + "/api/workspaces/a/advice?subject=person")
    assert status == 200 and any(i["stage"] == "sfm" for i in r)
    status, r = call(base + "/api/assist")
    assert status == 200 and "person" in r["subjects"] and "partial" in r["coverage"]


def test_help_page_is_served(server):
    base, _ = server
    with urllib.request.urlopen(base + "/help") as r:
        assert b"How to" in r.read()
    assert call(base + "/help/..%2Fserver.py")[0] == 404


def test_orientation_set_by_hand_is_used_by_every_export(server):
    import open3d as o3d
    import trimesh

    base, app = server
    call(base + "/api/workspaces", "POST", {"name": "o"})
    ws = Workspace(app.root / "o")
    ws.sparse.mkdir(parents=True)
    _orbit_reconstruction().write(ws.sparse)
    ws.mesh.mkdir()
    box = o3d.geometry.TriangleMesh.create_box(1, 2, 3)
    o3d.io.write_triangle_mesh(str(ws.mesh / "mesh_clean.ply"), box)
    assert call(base + "/api/workspaces/o/state")[1]["orientation"] == {"user": False, "lapsed": False}
    auto = call(base + "/api/workspaces/o/scene")[1]["auto"]
    glb_body = {"items": [{"content": "mesh", "format": "glb"}]}
    assert call(base + "/api/workspaces/o/export", "POST", glb_body)[0] == 200  # made with the automatic one

    # its 2 m side up (the automatic orientation stands it on its 1 x 2 face), then moved
    T = np.array([[0, 0, 1, 0.5], [0, 1, 0, 0], [-1, 0, 0, -2.0], [0, 0, 0, 1]])
    status, r = call(base + "/api/workspaces/o/orientation", "POST", {"transform": T.tolist()})
    assert status == 200 and r["user"]  # saved at once: nothing is rebuilt
    scene = call(base + "/api/workspaces/o/scene")[1]
    assert scene["user"] and np.allclose(scene["transform"], T) and np.allclose(scene["auto"], auto)
    # the file made before is outdated, and the next export uses the new orientation
    assert call(base + "/api/workspaces/o/export/status", "POST", glb_body)[1]["items"][0]["state"] == "outdated"
    v = np.asarray(box.vertices) @ T[:3, :3].T + T[:3, 3]
    expect = np.array([v.min(0), v.max(0)])
    status, r = call(base + "/api/workspaces/o/export", "POST", glb_body)
    assert not r["cached"]
    with urllib.request.urlopen(base + "/api/workspaces/o/exports/" + r["file"]) as resp:
        out = trimesh.load(trimesh.util.wrap_as_stream(resp.read()), file_type="glb", force="mesh")
    assert np.allclose(out.bounds, expect, atol=1e-5)
    status, r = call(base + "/api/workspaces/o/export", "POST", {"items": [{"content": "print", "format": "stl"}]})
    with urllib.request.urlopen(base + "/api/workspaces/o/exports/" + r["file"]) as resp:
        stl = trimesh.load(trimesh.util.wrap_as_stream(resp.read()), file_type="stl")
    assert abs(stl.extents[2] - 100) < 0.5 and abs(stl.extents[0] / stl.extents[2] - 1.5) < 0.05  # Z-up, 2 m side up

    # only rotations and moves
    bad = np.diag([2.0, 1, 1, 1])
    assert call(base + "/api/workspaces/o/orientation", "POST", {"transform": bad.tolist()})[0] == 400
    assert call(base + "/api/workspaces/o/orientation", "POST", {"transform": [[1, 0], [0, 1]]})[0] == 400
    # re-running SfM changes the frame: the orientation lapses rather than turning the model at random
    os.utime(ws.sparse / "images.bin", ns=(time.time_ns(), time.time_ns() + 10**9))
    assert call(base + "/api/workspaces/o/state")[1]["orientation"]["lapsed"]
    assert not call(base + "/api/workspaces/o/scene")[1]["user"]
    status, r = call(base + "/api/workspaces/o/orientation", "POST", {"transform": None})
    assert status == 200 and not r["user"] and not (ws.root / "orientation.json").exists()


def test_frame_viewer_gets_full_frames_and_per_frame_stats(server):
    import cv2

    base, app = server
    call(base + "/api/workspaces", "POST", {"name": "f"})
    ws = Workspace(app.root / "f")
    ws.frames.mkdir()
    ws.masks.mkdir()
    rng = np.random.default_rng(0)
    for i in range(4):
        img = (rng.random((120, 160, 3)) * 255).astype(np.uint8)
        if i == 2:
            img = cv2.GaussianBlur(img, (0, 0), 6)  # blurred
        cv2.imwrite(str(ws.frames / f"frame_{i:05d}.jpg"), img)
        mask = np.zeros((120, 160), np.uint8)
        mask[30:90, 40:120 if i != 3 else 41] = 255  # the last one barely finds the subject
        cv2.imwrite(str(ws.mask_for(f"frame_{i:05d}.jpg")), mask)
    status, rows = call(base + "/api/workspaces/f/frame-stats")
    assert status == 200 and [r["name"] for r in rows] == [f"frame_{i:05d}.jpg" for i in range(4)]
    assert abs(rows[0]["coverage"] - 0.25) < 0.02 and rows[3]["coverage"] < 0.01
    assert rows[2]["sharp_rel"] < 0.3 < rows[0]["sharp_rel"] and rows[0]["posed"] is None  # no SfM yet
    with urllib.request.urlopen(base + "/api/workspaces/f/image/frames/frame_00001.jpg") as r:
        assert r.headers["Content-Type"] == "image/jpeg" and cv2.imdecode(np.frombuffer(r.read(), np.uint8), 1).shape == (120, 160, 3)
    with urllib.request.urlopen(base + "/api/workspaces/f/image/masks/frame_00001.jpg") as r:
        assert r.headers["Content-Type"] == "image/png"
    assert call(base + "/api/workspaces/f/image/frames/..%2Fmanifest.json")[0] == 404
    assert call(base + "/api/workspaces/f/image/frames/missing.jpg")[0] == 404


def test_shared_page_modules_are_served(server):
    base, _ = server
    with urllib.request.urlopen(base + "/viewcube.js") as r:
        assert r.headers["Content-Type"].startswith("text/javascript") and b"export function addViewCube" in r.read()
    assert call(base + "/nope.js")[0] == 404
    with urllib.request.urlopen(base + "/vendor/three/build/three.module.js") as r:  # bundled, no CDN
        assert r.headers["Content-Type"].startswith("text/javascript") and b"REVISION" in r.read()
    assert call(base + "/vendor/..%2F..%2Fserver.py")[0] == 404
    assert call(base + "/..%2Fserver.py")[0] == 404


def test_every_page_links_to_the_repository(server):
    from motiongrammetry.credits import PROJECT

    base, _ = server
    for page in ["/", "/edit", "/help", "/about"]:
        with urllib.request.urlopen(base + page) as r:
            html = r.read().decode()
        assert re.search(r'<a class="gh[^"]*"[^>]* href="' + re.escape(PROJECT["url"]) + '"', html), page


def test_about_lists_third_party_software_with_licenses(server):
    base, _ = server
    with urllib.request.urlopen(base + "/about") as r:
        assert b"About motiongrammetry" in r.read()
    status, a = call(base + "/api/about")
    assert status == 200
    names = {x["id"] for x in a["external"]} | {x["id"] for x in a["models"]}
    assert {"colmap", "openmvs", "three", "vggt-1b", "birefnet"} <= names
    py = {x["id"]: x for x in a["python"]}
    assert py["numpy"]["group"] == "required" and py["numpy"]["license"]
    assert py["rembg"]["group"].startswith("optional")
    for kind, ident, needle in [("external", "openmvs", "AFFERO"), ("model", "vggt-1b", "NonCommercial"),
                                ("python", "numpy", "Copyright")]:
        with urllib.request.urlopen(f"{base}/api/about/license?kind={kind}&id={ident}") as r:
            assert needle.lower() in r.read().decode().lower()
    assert call(base + "/api/about/license?kind=external&id=nope")[0] == 404
    assert a["license"] == "Apache-2.0" and a["content"] and all("present" in x for x in a["external"])
    with urllib.request.urlopen(f"{base}/api/about/license?kind=project&id=motiongrammetry") as r:
        assert "Apache License" in r.read().decode()
    with urllib.request.urlopen(base + "/api/about/notices.txt") as r:
        assert 'THIRD_PARTY_NOTICES.txt' in r.headers["Content-Disposition"]
        text = r.read().decode()
    assert "OpenMVS" in text and "three.js" in text and "pycolmap" in text


def test_delete_a_scan_needs_its_name_and_no_run(server, tmp_path):
    base, app = server
    call(base + "/api/workspaces", "POST", {"name": "gone"})
    video = tmp_path / "outside.mp4"
    video.write_bytes(b"v")
    call(base + "/api/workspaces", "POST", {"name": "keepvid", "video_path": str(video)})
    ws = Workspace(app.root / "gone")
    ws.frames.mkdir()
    (ws.frames / "f.jpg").write_bytes(b"x" * 1000)
    status, z = call(base + "/api/workspaces/gone/size")
    assert status == 200 and z["bytes"] >= 1000 and z["video_outside"] is None
    assert z["path"] == str(ws.root) and z["frees"] >= 0 and isinstance(z["frees_exact"], bool)
    assert call(base + "/api/workspaces/keepvid/size")[1]["video_outside"] == str(video.resolve())
    assert call(base + "/api/workspaces/gone/delete", "POST", {"confirm": "gon"})[0] == 400  # not its name
    assert ws.root.exists()

    class Running:
        status = "running"
    app.jobs["gone"] = Running()
    assert call(base + "/api/workspaces/gone/delete", "POST", {"confirm": "gone"})[0] == 400  # in use
    app.jobs["gone"].status = "done"
    status, r = call(base + "/api/workspaces/gone/delete", "POST", {"confirm": "gone"})
    assert status == 200 and r["deleted"] == "gone" and not ws.root.exists()
    assert "gone" not in {w["name"] for w in call(base + "/api/workspaces")[1]}
    status, r = call(base + "/api/workspaces/keepvid/delete", "POST", {"confirm": "keepvid"})
    assert status == 200 and video.exists()  # the video outside the scan stays


def _put_video(base, scan, name, data, replace=False):
    url = f"{base}/api/workspaces/{scan}/video?name={name}" + ("&replace=1" if replace else "")
    req = urllib.request.Request(url, data=data, method="PUT")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_a_video_is_replaced_only_when_confirmed(server):
    """An upload used to replace a scan's video without asking (#25)."""
    from pathlib import Path

    base, app = server
    call(base + "/api/workspaces", "POST", {"name": "r"})
    status, body = _put_video(base, "r", "first.mp4", b"a" * 1000)
    assert status == 200
    first = Path(body["meta"]["video"])
    status, body = _put_video(base, "r", "second.mp4", b"b" * 1000)
    assert status == 409 and "already has a video" in body["error"]
    _, state = call(base + "/api/workspaces/r/state")
    assert Path(state["meta"]["video"]) == first and first.read_bytes() == b"a" * 1000
    status, body = _put_video(base, "r", "second.mp4", b"b" * 1000, replace=True)
    assert status == 200 and body["meta"]["video"].endswith("second.mp4")
    assert not first.exists()  # the upload it replaced is gone, not left taking space


def test_replacing_a_video_never_deletes_one_outside_the_scan(server, tmp_path):
    base, app = server
    outside = tmp_path / "mine.mp4"
    outside.write_bytes(b"keep")
    call(base + "/api/workspaces", "POST", {"name": "o", "video_path": str(outside)})
    status, _ = _put_video(base, "o", "new.mp4", b"n" * 100, replace=True)
    assert status == 200 and outside.read_bytes() == b"keep"


def test_job_polls_carry_the_server_clock(server):
    """The run screen times steps from the log lines' stamps against the server's clock, not the
    browser's (another time zone made every running step start at 1h 00)."""
    base, _ = server
    call(base + "/api/workspaces", "POST", {"name": "c"})
    t = time.localtime()
    status, body = call(base + "/api/workspaces/c/job?since=0")
    assert status == 200
    now = t.tm_hour * 3600 + t.tm_min * 60 + t.tm_sec
    assert abs((body["clock"] - now + 43200) % 86400 - 43200) < 5
