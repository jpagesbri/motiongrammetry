"""The camera-path check of sfm (issue #49): breaks in the path that reprojection error can't see."""

import re
from types import SimpleNamespace

import numpy as np
import pytest

from motiongrammetry import checks, cli
from motiongrammetry.advice import CAMERA_PATH_CODES, advise, camera_path_plain
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
    # the defaults ran: first the slower solver (it fixed orbit's break), with a re-run from SfM
    assert sfm[0]["apply"] == {"mapper": "incremental", "matcher": "exhaustive"}
    assert sfm[0]["rerun"] == ["sfm", "mvs", "clean", "texture"] and "First try" in sfm[0]["detail"]
    # already tried: only filming again is left
    ws.record("sfm", {**ws.load_manifest()["sfm"], "mapper": "incremental", "matcher": "exhaustive"})
    sfm = [a for a in advise(ws) if a["stage"] == "sfm"]
    assert sfm[0]["apply"] == {} and sfm[0]["rerun"] == [] and sfm[0]["detail"].endswith("even pace")


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


# ---------------------------------------------------------------- the assisted run's plain-language warning
JARGON = re.compile(r"×|median|reproj|mapper|matcher|SfM|step between|\bpx\b")  # none of these in the plain texts


def _plain_texts(p):
    return [p["headline"], p["what"], p["impact"], p["film"]] + ([p["slower"]["label"]] if p["slower"] else [])


BREAKS = {"ok": False, "collapsed": False, "jumps": [{"from": 3, "to": 4, "ratio": 711.8},
                                                      {"from": 62, "to": 63, "ratio": 31.1},
                                                      {"from": 81, "to": 82, "ratio": 12.1}],
          "jump_count": 3, "far": [], "far_count": 0}
FAR = {**BREAKS, "jumps": [], "jump_count": 0, "far": [{"keyframe": k, "ratio": 22.8} for k in range(4)], "far_count": 4}
COLLAPSED = {"ok": False, "collapsed": True, "cameras": 60, "collapsed_cameras": 38, "distinct_positions": 23,
             "still_steps": 37, "steps": 59, "jumps": [], "far": []}


def test_plain_warning_for_a_broken_path():
    p = camera_path_plain(BREAKS)
    assert p["kind"] == "jump" and p["headline"] == "Some camera positions don't line up"
    assert p["what"].startswith("Some camera positions don't line up with the others (around keyframes 3, 62 and 81).")
    assert "quick turn, a doorway" in p["what"]
    # what continuing does: the next steps run, for how long, and the defect it leaves in the model
    assert p["impact"].startswith("If you continue, the next steps") and "several minutes" in p["impact"]
    assert p["time"] == "several minutes"
    assert "wrong place" in p["impact"] and "appear twice" in p["impact"] and "can't repair" in p["impact"]
    assert p["slower"]["label"] == "Try the slower setting" and "takes a few times longer" in p["slower"]["text"]
    assert p["film"].startswith("Film again: walk slowly through doorways")
    assert not any(JARGON.search(t) for t in _plain_texts(p))


def test_plain_warning_when_the_slower_setting_was_tried():
    p = camera_path_plain(BREAKS, tried=True)
    assert p["slower"] is None and p["film"].startswith("The slower setting was already used for this run")


def test_plain_warning_for_cameras_far_away_and_both():
    p = camera_path_plain(FAR)
    assert p["kind"] == "far" and p["what"].startswith("Four photos were placed far away from all the others (keyframes 0–3).")
    assert "stray pieces of surface" in p["impact"] and "appear twice" not in p["impact"]
    both = camera_path_plain({**BREAKS, "far": FAR["far"], "far_count": 4})
    assert "(around keyframes 3, 62 and 81), and four photos were placed far away" in both["what"]
    assert "appear twice" in both["impact"] and "stray pieces" in both["impact"]
    one = camera_path_plain({**FAR, "far": [{"keyframe": 7, "ratio": 9}], "far_count": 1})
    assert one["what"].startswith("One photo was placed far away from all the others (keyframe 7).")
    # more breaks than listed in the manifest (it keeps 20)
    many = camera_path_plain({**BREAKS, "jump_count": 30})
    assert "(around keyframes 3, 62, 81 and others)" in many["what"]
    assert not any(JARGON.search(t) for x in (p, both, one) for t in _plain_texts(x))


def test_plain_warning_for_collapsed_cameras():
    masked = camera_path_plain(COLLAPSED, masked=True)
    assert masked["kind"] == "collapsed" and masked["headline"] == "The camera's movement could not be worked out"
    assert masked["what"].startswith("The program could not work out how the camera moved: 38 of 60 photos were "
                                     "placed at the same spot")
    assert "turntable" in masked["what"]
    assert "mostly empty or garbled" in masked["impact"] and masked["impact"].startswith("If you continue")
    assert masked["slower"]["label"] == "Try without the masks"
    assert masked["film"].startswith("Filming is not the cause here, so try the other setting first")
    plain = camera_path_plain(COLLAPSED)
    assert "calculation problem, not a filming one" in plain["what"] and plain["slower"]["label"] == "Try the slower setting"
    tried = camera_path_plain(COLLAPSED, tried=True)
    assert tried["slower"] is None and "did not help either" in tried["film"]
    # without mask_sfm and already tried, the masks are still offered when SfM used them
    assert camera_path_plain(COLLAPSED, masked=True, tried=True)["slower"]["label"] == "Try without the masks"
    assert not any(JARGON.search(t) for x in (masked, plain, tried) for t in _plain_texts(x))


@pytest.mark.parametrize("sfm, apply, slower", [
    ({"camera_path": BREAKS}, {"mapper": "incremental", "matcher": "exhaustive"}, "Try the slower setting"),
    ({"camera_path": BREAKS, "mapper": "incremental", "matcher": "exhaustive"}, {}, None),
    ({"camera_path": COLLAPSED, "masked_features": True}, {"mask_sfm": False}, "Try without the masks"),
    ({"camera_path": COLLAPSED, "masked_features": True, "mapper": "incremental", "matcher": "exhaustive"},
     {"mask_sfm": False}, "Try without the masks"),
    ({"camera_path": COLLAPSED}, {"mapper": "incremental", "matcher": "exhaustive"}, "Try the slower setting"),
    ({"camera_path": COLLAPSED, "mapper": "incremental", "matcher": "exhaustive"}, {}, None),
])
def test_the_advice_item_carries_the_plain_warning(tmp_path, sfm, apply, slower):
    ws = Workspace(tmp_path / "w")
    ws.record("sfm", {"frames": 143, "registered": 143, "points3D": 9000, "mean_reproj_error_px": 0.68, **sfm})
    [a] = [a for a in advise(ws) if a["stage"] == "sfm"]
    assert a["level"] == "fix" and a["apply"] == apply and a["rerun"] == (["sfm", "mvs", "clean", "texture"] if apply else [])
    assert a["plain"]["kind"] == ("collapsed" if sfm["camera_path"]["collapsed"] else "jump")
    assert (a["plain"]["slower"] or {}).get("label") == slower


def test_only_camera_path_warnings_carry_a_plain_warning(tmp_path):
    ws = Workspace(tmp_path / "w")
    ws.record("sfm", {"frames": 143, "registered": 143, "points3D": 9000, "mean_reproj_error_px": 0.68,
                      "camera_path": {"ok": True, "collapsed": False}})
    assert all("plain" not in a for a in advise(ws))
    assert set(CAMERA_PATH_CODES) == {"camera-path-jump", "cameras-far-away", "cameras-collapsed"}
