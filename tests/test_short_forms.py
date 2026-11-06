"""Every warning, error and suggestion the web UI shows has a short form (owner, PR #65): a title of at most
5 words and one line of at most 12, and buttons of at most 3 words. The long message and hint stay for
the Details, the CLI and the logs. These tests fail when a new code or advice item comes without one."""

import re
from pathlib import Path

import numpy as np
import pytest

from motiongrammetry import advice, checks
from motiongrammetry.advice import ACTION_WORDS, LINE_WORDS, TITLE_WORDS, advise
from motiongrammetry.diagnostics import SHORT, Diag, short_of
from motiongrammetry.workspace import Workspace
from test_camera_path import BREAKS, COLLAPSED, FAR

SRC = Path(advice.__file__).parent
words = lambda s: len(s.split())  # noqa: E731


def _codes_in_the_source() -> set[str]:
    codes = set()
    for p in list(SRC.glob("*.py")) + [SRC / "web" / "server.py"]:
        text = p.read_text()
        codes |= set(re.findall(r'\b(?:warn|error|info)\(\s*"([a-z0-9-]+)"', text))
        codes |= set(re.findall(r'"level": "(?:error|warning|info)", "code": "([a-z0-9-]+)"', text))
    return codes


def test_every_diagnostic_code_has_a_short_form_within_the_limits():
    codes = _codes_in_the_source()
    assert len(codes) > 50  # the scan found them
    missing = sorted(codes - set(SHORT) - {"print-shell"})  # the print report (Export window) has its own list
    assert not missing, f"no short form (diagnostics.SHORT) for: {missing}"
    for code, (title, line) in SHORT.items():
        assert 1 <= words(title) <= TITLE_WORDS, (code, title)
        assert 1 <= words(line) <= LINE_WORDS and line.endswith((".", "?")), (code, line)


def test_a_diagnostic_carries_its_short_form_and_an_older_one_gets_it():
    d = checks.warn("few-frames", "only 18 keyframes", "raise num", line="Only 18; 80 to 200 is typical.").as_dict()
    assert d["title"] == "Few keyframes" and d["line"] == "Only 18; 80 to 200 is typical." and d["message"] == "only 18 keyframes"
    assert Diag("warning", "disconnected", "m").as_dict()["line"] == "Only the largest group is used."
    old = short_of({"level": "warning", "code": "masks-empty", "message": "30/143 masks are empty", "hint": ""})
    assert old["title"] == "Subject missed in some frames" and old["line"] == "Some masks are empty."


BAD_RESULTS = {  # results that trip every quality gate, so every numbered short line is built
    "frames": [{"keyframes": 5}, {"keyframes": 20, "segments": 100, "blur_rejected": 40, "total_motion_widths": 0.4}],
    "mask": [{"frames": 143, "mean_coverage": 0.001, "empty_masks": 0, "coverage_cv": 0.1},
             {"frames": 143, "mean_coverage": 0.7, "empty_masks": 40, "coverage_cv": 0.9}],
    "sfm": [{"frames": 143, "registered": 40, "mean_reproj_error_px": 2.4, "points3D": 250, "models": 3,
             "camera": {"params": [100]}, "image_width": 720, "fx_fy_ratio": 1.1, "camera_path": BREAKS},
            {"frames": 143, "registered": 120, "mean_reproj_error_px": 0.5, "points3D": 9000,
             "camera_path": {**BREAKS, "far": FAR["far"], "far_count": 4}},
            {"frames": 60, "registered": 60, "mean_reproj_error_px": 0.5, "points3D": 9000, "camera_path": COLLAPSED}],
    "mvs": [{"mesh_faces": 500, "dense_points": 15000}],
}


@pytest.mark.parametrize("stage", BAD_RESULTS)
def test_the_quality_gates_short_lines_stay_short(stage):
    seen = 0
    for r in BAD_RESULTS[stage]:
        for d in checks.gate(stage, None, r, {}):
            s = d.as_dict()
            assert words(s["title"]) <= TITLE_WORDS and 1 <= words(s["line"]) <= LINE_WORDS, s
            assert "×" not in s["line"] and "median" not in s["line"]
            seen += 1
    assert seen >= 2


def test_camera_path_lines_count_the_breaks():
    lines = {d.code: d.as_dict()["line"] for d in checks.camera_path_diags({**BREAKS, "far": FAR["far"], "far_count": 4})}
    assert lines == {"camera-path-jump": "Views don't connect at 4 places.", "cameras-far-away": "4 photos far from the others."}
    assert checks.camera_path_diags(COLLAPSED)[0].as_dict()["line"] == "38 of 60 photos sit on top of each other."


def _scans(tmp_path):
    """Manifests that make every advice rule speak (each branch at least once)."""
    def ws(name, **m):
        w = Workspace(tmp_path / name)
        for stage, r in m.items():
            w.record(stage, r)
        return w
    base = {"points3D": 9000, "mean_reproj_error_px": 0.6}
    yield ws("bad-video", frames={"keyframes": 40, "segments": 100, "blur_rejected": 40, "total_motion_widths": 0.4},
             mask={"frames": 40, "mean_coverage": 0.02, "coverage_cv": 0.1, "empty_masks": 5})
    yield ws("good", frames={"keyframes": 150}, mask={"frames": 150, "mean_coverage": 0.3, "coverage_cv": 0.1, "empty_masks": 0},
             sfm={"frames": 150, "registered": 150, **base}, mvs={"resolution_level": 1, "mesh_faces": 10**6, "dense_points": 10**6},
             clean={"faces_out": 9000, "carved_vertices": 20, "vertices_in": 100, "carved_uncertain": 0.1, "support_plane": False,
                    "print": {"mode": "shell", "lid_share": 0.3}}, texture={"faces": 9000})
    yield ws("unstable", mask={"frames": 150, "mean_coverage": 0.3, "coverage_cv": 0.9, "empty_masks": 0},
             sfm={"frames": 150, "registered": 100, **base}, mvs={"resolution_level": 2},
             clean={"faces_out": 9000, "carved_vertices": 20, "vertices_in": 100, "carved_uncertain": 0.9})
    yield ws("approx", sfm={"frames": 150, "registered": 150, "points3D": 9000, "mean_reproj_error_px": 2.5},
             clean={"faces_out": 5000, "support_plane": True})
    for i, p in enumerate((BREAKS, FAR, COLLAPSED)):
        yield ws(f"path{i}", sfm={"frames": 150, "registered": 150, **base, "camera_path": p, "masked_features": i == 2})


def test_every_advice_item_has_a_short_form_within_the_limits(tmp_path):
    items = [a for w in _scans(tmp_path) for a in advise(w, "object")]
    kinds = {(a["stage"], a["short"]) for a in items}
    assert len(kinds) >= 22  # every rule spoke
    for a in items:
        assert 1 <= words(a["short"]) <= TITLE_WORDS, a["short"]
        assert 1 <= words(a["line"]) <= LINE_WORDS and a["line"].endswith((".", "?")), a["line"]
        assert a["level"] in ("fix", "tip", "ok")
        if a["apply"]:
            assert 1 <= words(a["action"]) <= ACTION_WORDS and "…" not in a["action"], a["action"]
            assert a["changes"] and a["rerun"], a
            for c in a["changes"]:
                assert c["from"] != c["to"] and words(c["label"]) <= 3
        else:
            assert a["action"] == ""
        for code in a["codes"]:
            assert code in SHORT


def test_the_subject_step_speaks_like_the_others(tmp_path):
    """The masks' advice: same levels and layout as the other steps, and the gate's warnings folded in."""
    w = Workspace(tmp_path / "m")
    w.record("mask", {"frames": 143, "mean_coverage": 0.3, "coverage_cv": 0.9, "empty_masks": 30})
    items = [a for a in advise(w) if a["stage"] == "mask"]
    assert [(a["level"], a["short"], a["line"], a["action"]) for a in items] == [
        ("fix", "Subject missed in some frames", "30 of 143 keyframes found no subject.", "Try person masks"),
        ("fix", "Masks vary a lot", "The subject's outline changes between keyframes.", "Try HQ masks")]
    gate = {d.code for d in checks.gate("mask", None, w.load_manifest()["mask"], {})}
    assert gate <= set(items[0]["codes"]) | set(items[1]["codes"])  # nothing shown twice
    assert items[0]["changes"] == [{"label": "Mask model", "from": "object", "to": "person"}]
