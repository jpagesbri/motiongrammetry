"""The camera-path check of sfm (issue #49): breaks in the path that reprojection error can't see."""

from types import SimpleNamespace

import numpy as np
import pytest

from motiongrammetry import checks, cli
from motiongrammetry.advice import advise
from motiongrammetry.sfm import camera_path_check, camera_path_from_reconstruction
from motiongrammetry.workspace import Workspace


def _orbit(n=150, radius=3.5, seed=0):
    """Two loops round an object at an uneven pace, the height and distance wandering, a little noise."""
    rng = np.random.default_rng(seed)
    speed = 1 + 0.6 * np.sin(np.linspace(0, 9, n))  # 0.4x to 1.6x the mean pace
    angle = np.cumsum(speed) / speed.sum() * 4 * np.pi
    r = radius * (1 + 0.15 * np.sin(np.linspace(0, 5, n)))
    h = 1.0 * np.sin(np.linspace(0, 3, n))
    pts = np.stack([r * np.cos(angle), h, r * np.sin(angle)], axis=1)
    return list(pts + rng.normal(0, 0.02, pts.shape))


def _walk(n=120):
    """A walk along a corridor with a turn: steady steps."""
    t = np.arange(n, dtype=float) * 0.1
    return [np.array([x, 0.0, 0.0]) if x < 6 else np.array([6.0, 0.0, x - 6]) for x in t]


def test_normal_orbit_is_not_flagged():
    p = camera_path_check(_orbit())
    assert p["checked"] and p["ok"], p
    assert p["jumps"] == [] and p["far"] == [] and p["max_step_ratio"] < 5


def test_gaps_of_unplaced_keyframes_are_not_jumps():
    centres = _orbit()
    for i in [*range(40, 52), 90, 91, 120]:  # a run of 12 frames SfM could not place, and a few more
        centres[i] = None
    p = camera_path_check(centres)
    assert p["ok"], p
    assert p["cameras"] == 150 - 15


def test_a_misplaced_stretch_is_a_jump():
    centres = _walk()
    for i in range(70, 120):  # the rest of the walk was placed elsewhere
        centres[i] = centres[i] + np.array([0.0, 0.0, 4.0])
    p = camera_path_check(centres, [f"frame_{i:05d}.jpg" for i in range(120)])
    assert not p["ok"]
    assert [(j["from"], j["to"]) for j in p["jumps"]] == [(69, 70)]
    assert p["jumps"][0]["ratio"] == pytest.approx(40, rel=0.05)
    assert p["jumps"][0]["to_name"] == "frame_00070.jpg"
    assert p["far"] == []  # shifted, but not far from the rest


def test_a_jump_after_a_gap_is_still_found():
    centres = _walk()
    for i in range(70, 120):
        centres[i] = centres[i] + np.array([0.0, 0.0, 6.0])
    for i in range(66, 70):
        centres[i] = None
    p = camera_path_check(centres)
    assert [(j["from"], j["to"]) for j in p["jumps"]] == [(65, 70)]  # 6 units over 5 steps: x12


def test_a_far_cluster_is_flagged():
    centres = _orbit(n=140)
    for i in range(4):  # the entrance cameras of issue #49: ~100 units off in a scene ~8 across
        centres[i] = np.array([100.0, 5.0, 3.0]) + i * 0.2
    p = camera_path_check(centres)
    assert not p["ok"]
    assert [f["keyframe"] for f in p["far"]] == [0, 1, 2, 3] and p["far_count"] == 4
    assert min(f["ratio"] for f in p["far"]) > 20
    assert (3, 4) in [(j["from"], j["to"]) for j in p["jumps"]]


def test_too_few_or_still_cameras_are_not_checked():
    assert not camera_path_check(_orbit()[:5])["checked"]
    still = camera_path_check([np.zeros(3)] * 30)
    assert not still["checked"] and still["ok"]


def test_the_sfm_gate_warns_with_capture_advice():
    centres = _walk()
    for i in range(70, 120):
        centres[i] = centres[i] + np.array([0.0, 0.0, 4.0])
    centres[:3] = [np.array([90.0, 0, 0])] * 3
    r = {"frames": 120, "registered": 120, "points3D": 20000, "mean_reproj_error_px": 0.7,
         "camera_path": camera_path_check(centres)}
    d = {x.code: x for x in checks.gate("sfm", None, r, {})}
    assert d["camera-path-jump"].level == "warning" and "69→70 ×" in d["camera-path-jump"].message
    assert "keyframes 0–2" in d["cameras-far-away"].message and "3 cameras" in d["cameras-far-away"].message
    assert "doorways" in d["camera-path-jump"].hint and "orbit" in d["camera-path-jump"].hint
    ok = {**r, "camera_path": camera_path_check(_orbit())}
    assert not checks.gate("sfm", None, ok, {})
    assert not checks.gate("sfm", None, {k: v for k, v in r.items() if k != "camera_path"}, {})  # older manifests


def test_keyframe_ranges():
    assert checks.keyframe_ranges([0, 1, 2, 3, 40, 42, 43]) == ["0–3", "40", "42–43"]


def test_advice_points_to_the_capture(tmp_path):
    ws = Workspace(tmp_path / "w")
    centres = _walk()
    centres[81:] = [c + np.array([0, 0, 5.0]) for c in centres[81:]]
    ws.record("sfm", {"frames": 120, "registered": 120, "points3D": 20000, "mean_reproj_error_px": 0.7,
                      "camera_path": camera_path_check(centres)})
    sfm = [a for a in advise(ws) if a["stage"] == "sfm"]
    assert len(sfm) == 1 and sfm[0]["level"] == "fix"
    assert sfm[0]["title"] == "the camera path breaks near keyframe 80" and "doorways" in sfm[0]["detail"]


def _fake_model(centres):
    imgs = {i: SimpleNamespace(name=f"frame_{i:05d}.jpg", has_pose=c is not None,
                               projection_center=lambda c=c: c) for i, c in enumerate(centres)}
    return SimpleNamespace(images=imgs)


def test_from_a_model_and_mg_check_cameras(tmp_path, monkeypatch, capsys):
    centres = _orbit(n=60)
    centres[10] = None  # not placed: a gap, not a jump
    p = camera_path_from_reconstruction(_fake_model(centres))
    assert p["cameras"] == 59 and p["ok"]

    centres[:4] = [np.array([80.0, 0, 0]) + k for k in range(4)]
    monkeypatch.setattr("motiongrammetry.sfm.load_reconstruction", lambda ws: _fake_model(centres))
    assert cli.main(["check-cameras", str(tmp_path / "w")]) == 2
    out = capsys.readouterr().out
    assert "4 cameras far from all the others (keyframes 0–3" in out and "walk slowly through doorways" in out

    monkeypatch.setattr("motiongrammetry.sfm.load_reconstruction", lambda ws: _fake_model(_orbit(n=60)))
    assert cli.main(["check-cameras", str(tmp_path / "w")]) == 0
    assert "ok: no breaks" in capsys.readouterr().out


def test_a_broken_path_warns_and_strict_stops(tmp_path, monkeypatch):
    from motiongrammetry import pipeline
    from motiongrammetry.diagnostics import EXIT_GATE, EXIT_OK
    from motiongrammetry.options import resolve

    ws = Workspace(tmp_path / "w")
    ws.frames.mkdir()
    for i in range(30):
        (ws.frames / f"frame_{i:05d}.jpg").write_bytes(b"")
    centres = _walk()
    centres[62:] = [c + np.array([0, 0, 5.0]) for c in centres[62:]]
    result = {"frames": 120, "registered": 120, "points3D": 20000, "mean_reproj_error_px": 0.7,
              "camera_path": camera_path_check(centres)}
    monkeypatch.setitem(pipeline.BODIES, "sfm", lambda ws, o, video: result)

    assert pipeline.run_stage(ws, "sfm", resolve({}), capture=False) == EXIT_OK
    last = ws.history()[-1]
    assert last["status"] == "warning" and [d["code"] for d in last["diagnostics"]] == ["camera-path-jump"]
    assert ws.load_manifest()["diagnostics"]["sfm"][0]["code"] == "camera-path-jump"  # what the web UI shows

    assert pipeline.run_stage(ws, "sfm", resolve({"strict": True}), capture=False) == EXIT_GATE
    assert ws.history()[-1]["status"] == "gate-failed"


def _collapsed(n=60, positions=23, seed=1):
    """The mask_sfm orbit of the verification: 60 cameras put at 23 positions on an orbit,
    ~1e-5 of the scene apart within one (like the real one: 1e-6 to 1.3e-4)."""
    rng = np.random.default_rng(seed)
    spots = _orbit(n=positions, seed=seed)
    at = np.sort(np.concatenate([np.arange(positions), rng.integers(0, positions, n - positions)]))
    return [spots[k] + rng.normal(0, 3e-5, 3) for k in at]


def test_a_collapsed_path_is_reported_as_such():
    p = camera_path_check(_collapsed())
    assert p["checked"] and not p["ok"] and p["collapsed"]
    assert p["distinct_positions"] == 23 and 38 <= p["collapsed_cameras"] < 60 and p["still_steps"] == 37
    assert p["jumps"] == [] and p["far"] == [] and p["max_step_ratio"] is None  # no ratios against a ~0 median
    d = checks.camera_path_diags(p)
    assert [x.code for x in d] == ["cameras-collapsed"]
    assert "23 distinct positions" in d[0].message and "37 of 59 steps" in d[0].message
    assert "×" not in d[0].message and "doorway" not in d[0].message.lower()
    assert "mask_sfm" in d[0].hint and "turntable" in d[0].hint and "mapper incremental" in d[0].hint
    assert not camera_path_check(_orbit())["collapsed"]


def test_a_few_repeated_cameras_or_most_at_one_point():
    centres = _orbit()
    for i in range(10, 20, 2):  # five cameras on top of their neighbour: not a collapse, nor a jump
        centres[i + 1] = centres[i]
    p = camera_path_check(centres)
    assert not p["collapsed"] and p["ok"]
    most = [np.zeros(3)] * 40 + _orbit(n=10)  # over three quarters at one point: the usual distance is 0
    p = camera_path_check(most)
    assert p["collapsed"] and p["distinct_positions"] == 11 and p["collapsed_cameras"] == 40


def test_collapse_in_the_gate_advice_and_mg_check_cameras(tmp_path, monkeypatch, capsys):
    r = {"frames": 60, "registered": 60, "points3D": 3700, "mean_reproj_error_px": 0.57, "masked_features": True,
         "camera_path": camera_path_check(_collapsed())}
    assert [x.code for x in checks.gate("sfm", None, r, {})] == ["cameras-collapsed"]

    ws = Workspace(tmp_path / "w")
    ws.record("sfm", r)
    sfm = [a for a in advise(ws) if a["stage"] == "sfm"]
    assert len(sfm) == 1 and sfm[0]["level"] == "fix" and sfm[0]["apply"] == {"mask_sfm": False}
    assert sfm[0]["title"].endswith("of 60 cameras sit on top of each other") and "turntable" in sfm[0]["detail"]
    ws.record("sfm", {**r, "masked_features": False})
    sfm = [a for a in advise(ws) if a["stage"] == "sfm"]
    assert sfm[0]["apply"] == {"mapper": "incremental", "matcher": "exhaustive"}

    monkeypatch.setattr("motiongrammetry.sfm.load_reconstruction", lambda ws: _fake_model(_collapsed()))
    assert cli.main(["check-cameras", str(tmp_path / "w2")]) == 2
    out = capsys.readouterr().out
    assert "37 of 59 steps are ~0" in out and "cameras-collapsed" not in out and "sit on top of each other" in out
