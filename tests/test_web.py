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
    status, body = call(base + "/api/workspaces/w/run", "POST", {"stages": ["sfm"], "pause_on": "camera-path-jump"})
    assert status == 400 and "pause_on" in body["error"]


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
    assert layers["cameras"] and layers["clean"] and layers["textured"]  # still there to look at
    assert app.layers_outdated(ws) == {"clean": "made before the cameras changed: re-run from Clean",
                                       "textured": "made before the cameras changed: re-run from Clean"}


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
    # a ready file is named, so the Export window can show it without making it again (its eye button)
    rows = call(base + "/api/workspaces/p/export/status", "POST", body)[1]["items"]
    assert [(i["file"], i["preview"]) for i in rows] == [("p-print.stl", None), ("p-mesh.glb", None)]
    rows = call(base + "/api/workspaces/p/export/status", "POST", {"items": [{"content": "print", "format": "3mf"}]})[1]["items"]
    assert rows[0]["state"] == "none" and "file" not in rows[0]

    # other settings or a changed source: outdated, made again when asked
    other = {**body, "options": {"height_mm": 50}}
    rows = call(base + "/api/workspaces/p/export/status", "POST", other)[1]["items"]
    assert [i["state"] for i in rows] == ["outdated", "outdated"] and not any("file" in i for i in rows)  # no preview of a stale file
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
    assert app.layers_outdated(ws) == {}  # the raw layer is VGGT's mesh, which matches the cameras

    ws.record("clean", {"input": str(ws.mvs / "scene_mesh.ply")})  # cleaned the stale mvs mesh instead
    assert app.stale(ws) == ["mvs", "clean"]


def _masks_rerun_after_everything(tmp_path, masked_features):
    """A scan run through texture, with SfM run with or without the masks, then the masks re-run."""
    ws = Workspace(tmp_path / "root" / "m")
    ws.sparse.mkdir(parents=True)
    _orbit_reconstruction().write(ws.sparse)
    t = time.time() - 600
    for i, stage in enumerate(("frames", "mask", "sfm", "mvs", "clean", "texture")):
        ws.stamp(stage)
        os.utime(ws.root / ".stamps" / stage, (t + 60 * i, t + 60 * i))
    ws.record("sfm", {"registered": 3, "masked_features": masked_features})
    ws.stamp("mask")  # mg mask again (new model, or edited masks): newer than everything
    return ws


def test_new_masks_outdate_an_sfm_that_used_them(tmp_path):
    """mask_sfm: SfM matched features inside the masks only, so new masks outdate the cameras and
    everything made from them, and every place that asks agrees."""
    from motiongrammetry import advice, checks

    ws = _masks_rerun_after_everything(tmp_path, masked_features=True)
    assert ws.sfm_upstream() == ["frames", "mask"]
    assert ws.stale_stages() == ["sfm", "mvs", "clean", "texture"]
    app = App(tmp_path / "root")
    assert app.stale(ws) == ws.stale_stages()
    assert app.state(ws)["upstream"]["sfm"] == ["frames", "mask"]  # the UI's "re-run from here" ticks sfm, mvs
    # every stage after it is outdated because of the masks, and is brought up to date from SfM
    assert ws.stale_causes() == {s: {"changed": "mask", "rerun": "sfm"} for s in ("sfm", "mvs", "clean", "texture")}
    # pre-flight says why: the masks changed, not just "older than its inputs"
    msg = [d.message for d in checks._stale_input(ws, "mvs", ["sfm"])]
    assert msg and "masks changed after sfm ran with them" in msg[0]
    # the assisted advice for a new mask model re-runs SfM and MVS too
    ws.record("mask", {"frames": 100, "mean_coverage": 0.3, "coverage_cv": 0.1, "empty_masks": 20})
    fix = next(a for a in advice.advise(ws) if a["stage"] == "mask" and a["level"] == "fix")
    assert fix["rerun"] == ["mask", "sfm", "mvs", "clean", "texture"]
    # cameras VGGT wrote since used no masks
    ws.stamp("sfm", by="vggt")
    assert ws.sfm_upstream() == ["frames"]


def test_new_masks_leave_an_sfm_without_masks_alone(tmp_path):
    """Without mask_sfm the masks only matter from clean on, as before."""
    from motiongrammetry import advice, checks

    ws = _masks_rerun_after_everything(tmp_path, masked_features=False)
    assert ws.sfm_upstream() == ["frames"]
    assert ws.stale_stages() == ["clean", "texture"]
    assert ws.stale_causes() == {s: {"changed": "mask", "rerun": "clean"} for s in ("clean", "texture")}
    assert App(tmp_path / "root").state(ws)["upstream"]["sfm"] == ["frames"]
    assert not checks._stale_input(ws, "mvs", ["sfm"])
    ws.record("mask", {"frames": 100, "mean_coverage": 0.3, "coverage_cv": 0.1, "empty_masks": 20})
    fix = next(a for a in advice.advise(ws) if a["stage"] == "mask" and a["level"] == "fix")
    assert fix["rerun"] == ["mask", "clean", "texture"]
    # an SfM from before masked_features was recorded counts as one without masks
    ws.record("sfm", {"registered": 3})
    assert ws.stale_stages() == ["clean", "texture"]


def test_new_masks_keep_every_layer_viewable_and_say_why_it_is_outdated(tmp_path):
    """Nothing on disk moved after new masks: every layer stays in the preview, each marked with the
    step that changed and where to re-run from; an up-to-date layer carries no mark."""
    ws = _masks_rerun_after_everything(tmp_path, masked_features=True)
    ws.mesh.mkdir()
    for name in ("mesh_clean.ply", "textured.ply"):
        (ws.mesh / name).write_text("ply")
    app = App(tmp_path / "root")
    state = app.state(ws)
    assert {k for k, v in state["layers"].items() if v} == {"cameras", "sparse", "clean", "textured"}
    why = "made before the masks changed: re-run from SfM"
    assert state["layers_outdated"] == {"cameras": why, "sparse": why, "clean": why, "textured": why}

    # SfM without the masks: the cameras are up to date, the meshes are re-made from Clean
    ws.record("sfm", {"registered": 3, "masked_features": False})
    why = "made before the masks changed: re-run from Clean"
    assert app.layers_outdated(ws) == {"clean": why, "textured": why}

    # re-running from Clean clears the marks
    ws.stamp("clean")
    ws.stamp("texture")
    assert app.layers_outdated(ws) == {}


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


_FAKE_STAGE = """
import sys, time
from motiongrammetry.workspace import Workspace
stage, root = sys.argv[1], sys.argv[2]
diags = [{"level": "warning", "code": "camera-path-jump", "message": "m", "hint": ""}] if stage == "sfm" else []
Workspace(root).append_history({"stage": stage, "status": "warning" if diags else "ok", "diagnostics": diags,
                                "started": time.time()})
print("ran", stage)
"""


@pytest.mark.parametrize("stages, pause_on, status, ran", [
    (["sfm", "mvs", "clean"], ["camera-path-jump", "cameras-collapsed"], "paused", ["sfm"]),
    (["sfm", "mvs", "clean"], None, "done", ["sfm", "mvs", "clean"]),          # Manual: carries on, as before
    (["sfm", "mvs", "clean"], ["cameras-collapsed"], "done", ["sfm", "mvs", "clean"]),  # another warning
    (["sfm"], ["camera-path-jump"], "done", ["sfm"]),                          # nothing left to hold back
])
def test_a_run_pauses_after_a_stage_with_a_chosen_warning(tmp_path, monkeypatch, stages, pause_on, status, ran):
    """The assisted run passes sfm's camera-path warnings as pause_on: the run stops after SfM, the
    next stages skipped, and waits for the user's choice instead of building a broken model."""
    import sys

    from motiongrammetry.web.server import Job

    monkeypatch.setattr(Job, "_cmd", lambda self, stage: [sys.executable, "-c", _FAKE_STAGE, stage, str(self.ws.root)])
    ws = Workspace(tmp_path / "w")
    job = Job(ws, stages, {}, None, pause_on)
    job.thread.join(30)
    snap = job.snapshot(0)
    assert snap["status"] == status
    assert [h["stage"] for h in ws.history()] == ran
    assert snap["stages"]["sfm"]["status"] == "warning"
    if status == "paused":
        assert snap["paused"] == {"stage": "sfm", "codes": ["camera-path-jump"], "skipped": ["mvs", "clean"]}
        assert [snap["stages"][s]["status"] for s in ("mvs", "clean")] == ["skipped", "skipped"]
        assert snap["failure"] is None and any("paused after sfm" in line for line in snap["log"])
    else:
        assert snap["paused"] is None


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
    opts = call(base + "/api/options")[1]
    assert "upstream" in opts and opts["short"]["masks-empty"] == ["Subject missed in some frames", "Some masks are empty."]


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


@pytest.mark.parametrize("wrong_first", [True, False])
def test_editor_save_keeps_the_face_of_a_pillow_wound_like_its_neighbours(server, wrong_first):
    """The save drops a pillow's second face; it used to keep the first, which after editing is often
    the copy wound against its neighbours, so the saved mesh had flipped faces."""
    import open3d as o3d

    from test_geometry import assert_wound_like_its_neighbours, pillowed_box

    base, app = server
    call(base + "/api/workspaces", "POST", {"name": "p"})
    ws = Workspace(app.root / "p")
    ws.mesh.mkdir()
    v, f, t = pillowed_box(wrong_first)
    v32, f32 = v.astype(np.float32), f.astype(np.uint32)
    body = struct.pack("<II", len(v32), len(f32)) + v32.tobytes() + np.full((len(v32), 3), 128, np.uint8).tobytes() + f32.tobytes()
    status, r = _post_bytes(base + "/api/workspaces/p/edit/mesh", body)
    assert status == 200 and r["faces"] == len(t), (status, r)
    saved = o3d.io.read_triangle_mesh(str(ws.mesh / "custom" / "mesh_clean.ply"))
    assert np.allclose(np.asarray(saved.vertices), v, atol=1e-6)  # the vertices stay as they were
    assert_wound_like_its_neighbours(np.asarray(saved.vertices), np.asarray(saved.triangles), t)


def test_state_gives_the_scans_typical_edge_from_the_pipeline_mesh(server):
    """The editor's absolute Density is k times the scan's typical density: the median, over the
    pipeline's clean mesh, of each vertex's mean edge averaged with its neighbours'. It stays the
    scan's when a custom mesh is loaded."""
    import open3d as o3d

    base, app = server
    call(base + "/api/workspaces", "POST", {"name": "d"})
    ws = Workspace(app.root / "d")
    assert call(base + "/api/workspaces/d/state")[1]["scan_edge"] is None  # no mesh yet
    ws.mesh.mkdir()
    box = o3d.geometry.TriangleMesh.create_box(1, 1, 1).subdivide_midpoint(2)  # edges 0.25 and 0.35
    o3d.io.write_triangle_mesh(str(ws.mesh / "mesh_clean.ply"), box)
    edge = call(base + "/api/workspaces/d/state")[1]["scan_edge"]
    assert 0.25 < edge < 0.36
    (ws.mesh / "custom").mkdir()
    o3d.io.write_triangle_mesh(str(ws.mesh / "custom" / "mesh_clean.ply"), box.subdivide_midpoint(1))
    assert call(base + "/api/workspaces/d/state")[1]["scan_edge"] == edge  # the scan's, not the edit's
    (ws.mesh / "mesh_clean.ply").write_text("not a mesh")
    assert call(base + "/api/workspaces/d/state")[1]["scan_edge"] is None  # the editor measures its own


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
    assert r["pause_on"] == ["camera-path-jump", "cameras-far-away", "cameras-collapsed"]  # what assisted runs stop on


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


def test_every_page_has_the_one_top_bar(server):
    from motiongrammetry.credits import PROJECT

    base, _ = server
    with urllib.request.urlopen(base + "/topbar.js") as r:
        assert r.headers["Content-Type"].startswith("text/javascript")
        bar = r.read().decode()
    # the pages are named and linked there only, with the repository link
    assert "const REPO = '" + PROJECT["url"] + "'" in bar and '<a class="gh" href="${REPO}"' in bar
    labels = re.findall(r"\{ id: '(\w+)', label: '([^']+)'", bar)
    assert labels == [("scan", "Scan"), ("edit", "Edit"), ("help", "Help"), ("about", "About")]
    for page, pid in [("/", "scan"), ("/edit", "edit"), ("/help", "help"), ("/about", "about")]:
        with urllib.request.urlopen(base + page) as r:
            html = r.read().decode()
        assert '<header class="topbar"></header>' in html and re.search(rf"mountTopbar\('{pid}'[,)]", html), page
        assert 'class="screens"' not in html and "Generate" not in html and "Edit &amp; paint" not in html, page


def test_every_page_draws_the_app_scrollbar(server):
    base, _ = server
    with urllib.request.urlopen(base + "/scrollbar.js") as r:
        assert r.headers["Content-Type"].startswith("text/javascript")
        js = r.read().decode()
    with urllib.request.urlopen(base + "/scrollbar.css") as r:
        assert r.headers["Content-Type"].startswith("text/css")
        css = r.read().decode()
    # scroll areas are found by their computed overflow, also when added later, and keep scrolling natively
    assert "getComputedStyle" in js and "MutationObserver" in js and "ResizeObserver" in js
    assert "data-scroll=\"native\"" in js and "pointerdown" in js and "setPointerCapture" in js
    assert "scrollbar-width: none" in css and "::-webkit-scrollbar" in css and ".mgsb-thumb" in css
    # the browser's bars are hidden by the style sheet itself (no flash before the script), with
    # data-scroll="native" and text fields as the exceptions, and there is no overscroll bounce
    assert ':where(*:not(textarea, select, input, [data-scroll="native"])) { scrollbar-width: none; overscroll-behavior: none; }' in css
    assert "html, body { overscroll-behavior: none; }" in css
    # the thumb is measured again when it is used, and a log growing by text is a change too
    assert "pointerenter" in js and "update(b);  // measure now" in js
    with urllib.request.urlopen(base + "/topbar.css") as r:  # the document never scrolls: Help and About scroll a .page box
        assert "body.paged > .page { flex: 1; min-height: 0; overflow: auto;" in r.read().decode()
    # the top bar brings the code to every page; each page links the styles
    with urllib.request.urlopen(base + "/topbar.js") as r:
        assert "import '/scrollbar.js';" in r.read().decode()
    for page in ["/", "/edit", "/help", "/about"]:
        with urllib.request.urlopen(base + page) as r:
            html = r.read().decode()
        assert '<link rel="stylesheet" href="/scrollbar.css">' in html and "from '/topbar.js'" in html, page
        head = html[: html.index("<body")]
        assert '<link rel="stylesheet" href="/scrollbar.css">' in head, page  # styled from the first paint
        if page in ("/help", "/about"):
            assert '<body class="paged">' in html and '<div class="page" id="page" tabindex="-1">' in html, page
            assert "position: sticky; top: 0; z-index" not in html, page  # no sticky header over a scrolling document


def test_tabs_and_modes_are_the_one_shared_control(server):
    base, _ = server
    with urllib.request.urlopen(base + "/modes.js") as r:
        assert r.headers["Content-Type"].startswith("text/javascript")
        js = r.read().decode()
    with urllib.request.urlopen(base + "/modes.css") as r:
        assert r.headers["Content-Type"].startswith("text/css")
        css = r.read().decode()
    # a tab list with roving tabindex, the arrow keys, Home / End, and a clear selected state
    assert "export function modeControl" in js
    for text in ["'tablist'", "'tab'", "aria-selected", "tabIndex", "ArrowRight", "ArrowLeft", "Home", "End", "stopPropagation"]:
        assert text in js, text
    # one control, two looks: a page's own tabs (underlined, as Run / Preview were) and a segmented mode inside a page
    assert '.modes > button[aria-selected="true"]' in css and ".modes.tabs > button" in css and ":focus-visible" in css
    assert ".pagebar {" in css  # the bar that holds a page's tabs, the same on every page
    pages = {}
    for page in ["/", "/edit"]:
        with urllib.request.urlopen(base + page) as r:
            pages[page] = html = r.read().decode()
        head = html[: html.index("<body")]
        assert '<link rel="stylesheet" href="/modes.css">' in head, page
        assert "import { modeControl } from '/modes.js';" in html, page
    scan, edit = pages["/"], pages["/edit"]
    # Scan: Run / Preview (the page's tabs), Assisted / Manual, 3D / Frames, the log's Live / History
    assert re.search(r'<div class="modes tabs" id="gTabs"[^>]*>\s*<button data-value="run"', scan)
    assert '<div class="modes" id="modeTabs"' in scan and '<button data-value="assisted"' in scan and '<button data-value="manual"' in scan
    assert '<div class="modes" id="pvTabs"' in scan and '<div class="modes" id="logTabs"' in scan
    assert 'role="tabpanel" aria-labelledby="tabRun"' in scan and 'role="tabpanel" aria-labelledby="tabPreview"' in scan
    for ctl in ["gTabs", "modeTabs", "pvTabs", "logTabs"]:
        assert f"const {ctl} = modeControl($('{ctl}')" in scan, ctl
    # Edit: Sculpt mesh / Paint texture, the page's tabs in the same bar under the top bar as on Scan (not in the
    # side panel, which scrolls), and the view's layout
    for html in (scan, edit):
        assert re.search(r'<div class="pagebar">.*?\s*<div class="modes tabs" id="(gTabs|modeTabs)"', html)
    assert re.search(r'<div class="pagebar">.*?\s*<div class="modes tabs" id="modeTabs"[^>]*>\s*<button data-value="sculpt"', edit)
    assert edit.index('id="modeTabs"') < edit.index("<aside") and '"tabs tabs"' in edit and ".gbar" not in scan
    assert '<button data-value="paint"' in edit and '<div class="modes" id="layoutTabs"' in edit
    for ctl in ["modeTabs", "layoutTabs"]:
        assert f"const {ctl} = modeControl($('{ctl}')" in edit, ctl
    assert "u.searchParams.set('mode', 'paint')" in edit  # the mode is remembered in the URL
    # the old per-page tab markup and styles are gone
    for gone in ['class="gtabs"', 'class="gtab"', ".gtab {", 'id="modeSeg"', 'data-mode="', 'id="pvSeg"', 'class="tabs"',
                 'class="tab ', ".tab {", "data-g=\"run\" title", "data-pv=", "data-tab="]:
        assert gone not in scan, gone
    for gone in ['<div class="modes">', ".modes button", "data-mode=", 'id="layoutSeg"', "data-layout=", "<h2>Mode</h2>"]:
        assert gone not in edit, gone


def test_every_page_offers_export_through_the_one_shared_module(server):
    base, _ = server
    with urllib.request.urlopen(base + "/export.js") as r:
        assert r.headers["Content-Type"].startswith("text/javascript")
        js = r.read().decode()
    with urllib.request.urlopen(base + "/export.css") as r:
        assert r.headers["Content-Type"].startswith("text/css")
        css = r.read().decode()
    with urllib.request.urlopen(base + "/projection.js") as r:
        assert "export function projection(" in r.read().decode()
    with urllib.request.urlopen(base + "/topbar.js") as r:
        bar = r.read().decode()
    # the window and its logic: the contents and formats, ready / outdated, the downloads, the saved choices
    for text in ["export const EX_CONTENTS = [", "export async function openExport(name)", "export function exportHooks(",
                 "/export/status`", "/export`", "/exports/${enc(file)}", "'mg.export'", "'mg.export.' + scan",
                 "export_defaults", 'id="exDownload"', "import('/projection.js')", "import('/viewcube.js')"]:
        assert text in js, text
    # nothing to export yet: it says so and points to Run; unsaved edits: it says it exports the saved files only
    assert "has nothing to export yet" in js and 'href="/?tab=run#${enc(scan)}">Go to Run</a>' in js
    assert "Export uses the saved" in js and 'id="exUnsaved"' in js
    # what the scan has, its folder and the 3D print check are in the window, on every page
    for text in ['id="exSummary"', 'id="exDir"', 'id="exCheck"', "'on disk: '", "print_check", "outdated: export the print again"]:
        assert text in js, text
    assert ".ex-modal {" in css and ".ex-unsaved {" in css
    # a ready file's eye shows it in the preview as it is: only ready (not outdated), only what the preview can show,
    # a real button with a tooltip and a label, kept from ticking the row, and the preview names the file
    assert "if (it.state === 'ready' && it.file && showable(it))" in js
    assert "const showable = (f) => !!f.preview || (['glb', 'stl', 'ply'].includes(f.format) && f.size < 150e6);" in js
    assert '<button type="button" class="ex-eye"' in js and 'title="preview this file"' in js and 'aria-label="preview this file: ' in js
    # the eye is Tabler's, from the one icon helper and the vendored set (with its license), not drawn here
    assert "import { icon } from '/icons.js';" in js and "${icon('eye')}" in js and "<svg" not in js
    with urllib.request.urlopen(base + "/icons.js") as r:
        assert r.headers["Content-Type"].startswith("text/javascript")
        icons = r.read().decode()
    assert "export function icon(name)" in icons and "const NAMES = ['eye', 'help-circle', 'clock-exclamation'];" in icons and "/vendor/tabler-icons/" in icons
    with urllib.request.urlopen(base + "/vendor/tabler-icons/eye.svg") as r:
        assert b"icon-tabler-eye" in r.read()
    with urllib.request.urlopen(base + "/vendor/tabler-icons/LICENSE") as r:
        assert b"MIT License" in r.read()
    assert "Tabler Icons" in {x["name"] for x in call(base + "/api/about")[1]["external"]}
    assert "e.preventDefault(); e.stopPropagation();" in js and 'id="exShown"' in js and ".ex-eye:focus-visible" in css
    # the top bar: Export… beside the scan chip, and in every scan's ⋯ menu, on every page
    assert "import { openExport } from '/export.js';" in bar
    assert '<button class="topexport" id="topExport" disabled' in bar and "openExport(scan)" in bar
    assert "label: 'Export…'" in bar and "run: () => openExport(w.name)" in bar
    pages = {}
    for page in ["/", "/edit", "/help", "/about"]:
        with urllib.request.urlopen(base + page) as r:
            pages[page] = html = r.read().decode()
        head = html[: html.index("<body")]
        assert '<link rel="stylesheet" href="/export.css">' in head and '<script type="importmap">' in head, page
        assert "from '/topbar.js'" in html, page
    scan, edit = pages["/"], pages["/edit"]
    # the Scan page uses the module for the assisted run's last step: no copy of its own, and no Export side
    # panel on the Preview (the 3D view takes the width; a side column shows only while orienting)
    assert "import { openExport, exportHooks } from '/export.js';" in scan
    assert '<div class="pvside" id="pvSide" hidden>' in scan and "$('pv3d').classList.toggle('side', on);" in scan
    for gone in ['id="outputsPanel"', 'id="exportBtn"', 'id="editBtn"', 'id="outSummary"', 'id="outDir"', 'id="printNotes"',
                 'id="noOutputs"', "function renderOutputs", "Open in editor", "exported:"]:
        assert gone not in scan, gone
    assert "params.get('pane') === 'outputs') {" in scan  # the old deep link opens the Export window
    assert "$('asExport').addEventListener('click', () => { showG('preview'); showPv('3d'); openExport(current); });" in scan
    for gone in ['id="exportModal"', "const EX_CONTENTS", "function exRenderItems", "const exUrl", "const exSize",
                 "async function exPreview", 'id="exSource"', ".ex-item {", "function exSave"]:
        assert gone not in scan, gone
    # the editor says whether there are unsaved edits; projection() is shared, not copied
    assert "import { exportHooks } from '/export.js';" in edit and "S.dirty && 'sculpt edits', P.dirty && 'paint'" in edit
    for html in (scan, edit):
        assert "import { projection } from '/projection.js';" in html and "function projection(" not in html


def test_every_page_but_help_has_its_help_window_from_the_one_shared_module(server):
    base, _ = server
    with urllib.request.urlopen(base + "/pagehelp.js") as r:
        assert r.headers["Content-Type"].startswith("text/javascript")
        js = r.read().decode()
    with urllib.request.urlopen(base + "/pagehelp.css") as r:
        assert r.headers["Content-Type"].startswith("text/css")
        css = r.read().decode()
    pages = {}
    for page in ["/", "/edit", "/help", "/about"]:
        with urllib.request.urlopen(base + page) as r:
            pages[page] = r.read().decode()
    # every page but Help loads the module and its styles, and says which views it shows; Help is the help
    for page, call_ in [("/", "pageHelp('scan', () => ["), ("/edit", "pageHelp('edit', () => ["), ("/about", "pageHelp('about');")]:
        html = pages[page]
        assert '<link rel="stylesheet" href="/pagehelp.css">' in html[: html.index("<body")], page
        assert "from '/pagehelp.js';" in html and call_ in html, page
        assert html.index("mountTopbar(") < html.index(call_), page  # the button goes into the bar
    assert "pagehelp" not in pages["/help"]
    # the views each page names exist, with a few items and a link to a Help section that exists
    views = {v: page for v, page, _ in re.findall(r"^  '?([\w-]+)'?: \{ page: '(\w+)', label: '[^']+', more: '(#[\w-]+)'", js, re.M)}
    assert set(re.findall(r"'((?:scan|edit)-\w+)'", pages["/"] + pages["/edit"])) == {
        "scan-assisted", "scan-manual", "scan-3d", "scan-frames", "edit-sculpt", "edit-paint"}
    assert len(views) == 7 and views["about"] == "about" and all(views[v] == v.split("-")[0] for v in views)
    hrefs = re.findall(r"more: '(#[\w-]+)'", js)
    assert len(hrefs) == 7
    anchors = set(re.findall(r'id="([\w-]+)"', pages["/help"]))
    for h in hrefs:
        assert h[1:] in anchors, h
    assert 'href="/help${h.more}"' in js and "Read more in Help →" in js
    # the icon is Tabler's, from the one icon helper (no SVG drawn here), with a tooltip and a label
    assert "import { icon } from '/icons.js';" in js and "icon('help-circle')" in js and "<svg" not in js
    assert "title: 'help on this page (?)'" in js and "setAttribute('aria-label', 'help on this page')" in js
    with urllib.request.urlopen(base + "/vendor/tabler-icons/help-circle.svg") as r:
        assert b"icon-tabler-help-circle" in r.read()
    # ? opens it (not from a text field, nor over another window); Esc closes it; the page gets no keys meanwhile
    assert "e.key !== '?'" in js and "inText(e.target)" in js and "otherWindow()" in js
    assert "if (e.key === 'Escape') closeHelp(); e.stopPropagation();" in js
    assert "e.stopPropagation(); if (e.key === 'Escape') closeHelp();" in js and "}, true);" in js
    assert "role', 'dialog'" in js and "aria-modal', 'true'" in js
    # ? was free: no page binds it
    for html in pages.values():
        assert "e.key === '?'" not in html
    # hints: one look, closable ones remembered and brought back from the window
    assert ".pghint {" in css and ".pghint.off {" in css and "header.topbar .tophelp" in css
    assert "'mg.hints.off'" in js and "export function hint(id, html" in js and "you closed" in js
    scan, edit = pages["/"], pages["/edit"]
    for h in ['data-hint="pv-3d"', 'data-hint="pv-frames"', 'data-hint="film"', "hint('all-steps'", "hint('run-stop'"]:
        assert h in scan, h
    assert 'id="pvHint"' not in scan and '<div class="pghint" id="orHelp">' in scan
    assert 'data-hint="layout"' in edit and '<div class="pghint" id="toolHelp">' in edit and '<div class="pghint" id="seamHelp">' in edit
    # the editor's long mouse and keys text moved into the window
    assert 'id="help"' not in edit and "const HELP = {" not in edit
    assert "Fill</b> places spheres at the depth of the surface" in js and "<kbd>Alt</kbd>+click: pick a colour" in js


def test_the_scan_is_chosen_from_the_top_bar_on_every_page(server):
    base, _ = server
    with urllib.request.urlopen(base + "/scanchooser.js") as r:
        assert r.headers["Content-Type"].startswith("text/javascript")
        chooser = r.read().decode()
    with urllib.request.urlopen(base + "/topbar.js") as r:
        bar = r.read().decode()
    # the chip is a button that opens the chooser, written once for all pages
    assert '<button class="scanchip' in bar and "import { initChooser, chooserScan } from '/scanchooser.js'" in bar
    chip = re.search(r'<button class="scanchip.*?</button>', bar).group(0)
    assert "<img" not in chip  # the chip names the scan; the thumbnails are in the list
    with urllib.request.urlopen(base + "/topbar.css") as r:  # a scan's ⋯ menu is not clipped by the list's scrolling
        assert re.search(r"\.sc-menu \{ position: fixed;", r.read().decode())
    # what the Scan page's left bar offered lives in the chooser: the list, New scan, Duplicate, Delete
    for text in ["+ New scan…", "'Duplicate…'", "'Delete…'", "Upload video…", "include SfM / MVS results", "Delete for good"]:
        assert text in chooser, text
    for call_ in ["post('/api/workspaces', body)", "/duplicate`", "/delete`", "/size`", "/video?name="]:
        assert call_ in chooser, call_
    assert "Escape" in chooser and "ArrowDown" in chooser  # keyboard
    pages = {}
    for page in ["/", "/edit", "/help", "/about"]:
        with urllib.request.urlopen(base + page) as r:
            pages[page] = r.read().decode()
    # the old selectors are gone: the Scan page's left bar and the editor's own dropdown
    scan, edit = pages["/"], pages["/edit"]
    for gone in ["<aside", 'id="wsList"', 'id="newName"', 'id="createBtn"', 'id="dupBtn"', 'id="delModal"', 'id="scanCard"']:
        assert gone not in scan, gone
    assert 'id="scanPick"' not in edit and 'id="scanPanel"' not in edit
    # each page says what picking a scan does there; the Scan page adds the video upload to a scan's menu
    assert "mountTopbar('scan', {" in scan and "open: (name) => select(name)" in scan and "Replace the video…" in scan
    assert 'id="upload"' in scan and 'id="repModal"' in scan
    assert "mountTopbar('edit', {" in edit and "location.hash = '#' + encodeURIComponent(name)" in edit
    assert "Discard unsaved edits?" in edit  # switching scans still asks first
    assert "mountTopbar('help');" in pages["/help"] and "New scan…" in pages["/help"]


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


def test_editor_checks_and_sanitizes_meshes(server, monkeypatch):
    """#12: the saved meshes texturing starts from are checked; #29: the editor's mesh is checked
    (with time estimates) and sanitized, mesh in, mesh out, nothing saved."""
    import open3d as o3d

    from motiongrammetry import meshfix
    from motiongrammetry.meshops import decode_mesh, encode_mesh

    monkeypatch.setattr(meshfix, "BENCH_FACES", 4000)
    monkeypatch.setattr(meshfix, "_rates", {})
    base, app = server
    call(base + "/api/workspaces", "POST", {"name": "k"})
    ws = Workspace(app.root / "k")
    assert call(base + "/api/workspaces/k/edit/check?what=edits")[0] == 404  # no edited mesh
    assert call(base + "/api/workspaces/k/edit/check?what=other")[0] == 400

    # an edited mesh saved by an older version: a pillow and a bow-tie vertex
    v, f, c = meshfix.test_mesh(4000)
    (ws.mesh / "custom").mkdir(parents=True)
    m = o3d.geometry.TriangleMesh(o3d.utility.Vector3dVector(v), o3d.utility.Vector3iVector(f.astype(np.int32)))
    o3d.io.write_triangle_mesh(str(ws.mesh / "custom" / "mesh_clean.ply"), m)
    status, r = call(base + "/api/workspaces/k/edit/check?what=edits")
    assert status == 200 and r["mesh"] == "your edited mesh"
    codes = {p["code"]: p for p in r["problems"]}
    assert {"pillows", "bowtie", "degenerate"} <= set(codes) and codes["pillows"]["count"] == 1
    # the textured model painted: its vertices are split at texture seams, the check joins them
    sphere = o3d.geometry.TriangleMesh.create_sphere(1.0, 12)
    sv, sf = np.asarray(sphere.vertices), np.asarray(sphere.triangles)
    split = o3d.geometry.TriangleMesh(o3d.utility.Vector3dVector(sv[sf.ravel()]),
                                      o3d.utility.Vector3iVector(np.arange(sf.size).reshape(-1, 3).astype(np.int32)))
    o3d.io.write_triangle_mesh(str(ws.mesh / "textured.ply"), split)
    status, r = call(base + "/api/workspaces/k/edit/check?what=paint")
    assert status == 200 and r["problems"] == [] and r["mesh"] == "the textured model"

    params = json.dumps({}).encode()
    body = struct.pack("<I", len(params)) + params + encode_mesh(v, f, c)
    status, r = _post_bytes(base + "/api/workspaces/k/edit/op?kind=inspect", body)
    assert status == 200 and r["report"]["bowtie_vertices"] == 1 and r["report"]["intersecting_pairs"] >= 1
    assert set(r["seconds"]) >= set(meshfix.FIXES) and r["machine"]["cpu"]

    params = json.dumps({"fixes": ["duplicates", "manifold", "pieces", "orient", "holes"], "hole_size": 0.5}).encode()
    body = struct.pack("<I", len(params)) + params + encode_mesh(v, f, c)
    req = urllib.request.Request(base + "/api/workspaces/k/edit/op?kind=sanitize", data=body, method="POST")
    with urllib.request.urlopen(req) as resp:
        info = json.loads(resp.headers["X-Op-Info"])
        V, F, _ = decode_mesh(resp.read())
    assert [s["fix"] for s in info["steps"]] == ["duplicates", "manifold", "pieces", "orient", "holes"]
    assert info["after"]["pillow_faces"] == 0 and info["after"]["bowtie_vertices"] == 0 and info["after"]["holes"] == 0
    assert len(F) == info["faces_out"]
    assert len(o3d.io.read_triangle_mesh(str(ws.mesh / "custom" / "mesh_clean.ply")).triangles) == len(f)  # not saved
