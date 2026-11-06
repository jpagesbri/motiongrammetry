"""Assisted mode says plainly when SfM's camera path is broken (issue #49): what happened, what going on
leads to, and the choices (the slower setting, filming again, continuing anyway). Assisted runs stop
after SfM on these warnings (pause_on); Manual keeps the technical message and runs every step.

No reconstruction runs: the run requests are caught in the browser and checked for what they ask."""

import json

import pytest

pytest.importorskip("playwright", reason="needs Playwright: uv pip install -e '.[dev]'")

from playwright.sync_api import expect  # noqa: E402

from motiongrammetry.advice import CAMERA_PATH_CODES  # noqa: E402
from motiongrammetry.workspace import Workspace  # noqa: E402
from scan_fixture import KEYFRAMES, SCAN  # noqa: E402

BROKEN = {"ok": False, "checked": True, "collapsed": False, "jumps": [{"from": 3, "to": 4, "ratio": 711.8},
                                                                     {"from": 8, "to": 9, "ratio": 31.1}],
          "jump_count": 2, "far": [], "far_count": 0}
JUMP_DIAG = {"level": "warning", "code": "camera-path-jump",
             "message": "the camera path breaks 2 times, where it moved many times the median step from one keyframe "
                        "to the next: keyframes 3→4 ×711.8, 8→9 ×31.1", "hint": "film again"}
COLLAPSED = {"ok": False, "checked": True, "collapsed": True, "cameras": KEYFRAMES, "collapsed_cameras": 9,
             "distinct_positions": 4, "still_steps": 7, "steps": KEYFRAMES - 1, "jumps": [], "far": []}
COLLAPSE_DIAG = {"level": "warning", "code": "cameras-collapsed",
                 "message": "9 of 12 cameras sit on top of each other (only 4 distinct positions; 7 of 11 steps "
                            "between keyframes are ~0): SfM did not recover the motion", "hint": "no mask_sfm"}


def _broken_sfm(app, path=BROKEN, diag=JUMP_DIAG, **sfm) -> None:
    ws = Workspace(app.root / SCAN)
    ws.record("sfm", {**ws.load_manifest().get("sfm", {}), "frames": KEYFRAMES, "registered": KEYFRAMES,
                      "points3D": 200, "mean_reproj_error_px": 0.7, "camera_path": path, **sfm})
    ws.record_diagnostics("sfm", [diag])


def _open_sfm_step(page, mode="assisted", values=None, step="sfm", title="Place the cameras") -> None:
    """The scan on its SfM step, in Assisted (or Manual), as if you had got there (with these option values)."""
    page.add_init_script(f"""try {{
      localStorage.setItem('mg.mode.{SCAN}', '{mode}');
      localStorage.setItem('mg.assist.{SCAN}', JSON.stringify({{ step: '{step}', subject: 'object', coverage: 'around' }}));
      localStorage.setItem('mg.run.{SCAN}', JSON.stringify({{ page: '{step}', values: {json.dumps(values or {})} }}));
    }} catch {{}}""")
    page.goto(f"/#{SCAN}")
    expect(page.locator("#topScanName")).to_have_text(SCAN)
    expect(page.locator("#modeTabs [aria-selected=true]")).to_have_text(mode.capitalize())
    expect(page.locator("#asMain h3")).to_contain_text(title)


def _catch_runs(page) -> list[dict]:
    """Run requests are answered here without running anything; returns what they asked for."""
    runs = []

    def answer(route):
        if route.request.method != "POST":
            return route.continue_()
        runs.append(json.loads(route.request.post_data))
        route.fulfill(json={"status": "done", "current": None, "stages": {}, "failure": None, "paused": None,
                            "log": [], "next": 0})
    page.route(f"**/api/workspaces/{SCAN}/run", answer)
    return runs


def _run_sent_by(page, click) -> dict:
    """What the run request sent by `click` asks for (answered by _catch_runs, nothing runs)."""
    with page.expect_request(lambda r: r.url.endswith(f"/api/workspaces/{SCAN}/run") and r.method == "POST") as req:
        click()
    return json.loads(req.value.post_data)


def _paused_job(page, ran=("sfm",), when=lambda: True) -> None:
    """The server's answer while an assisted run waits after SfM, the next steps held back (once `when`)."""
    held = ["mvs", "clean", "texture"]
    snap = {"status": "paused", "current": None, "failure": None, "log": [], "next": 0,
            "stages": {**{s: {"status": "done"} for s in ran}, "sfm": {"status": "warning", "diagnostics": [JUMP_DIAG]},
                       **{s: {"status": "skipped"} for s in held}},
            "paused": {"stage": "sfm", "codes": ["camera-path-jump"], "skipped": held}}
    page.route(f"**/api/workspaces/{SCAN}/job*", lambda route: route.fulfill(json=snap) if when() else route.continue_())


def test_assisted_says_plainly_what_a_broken_camera_path_means(page, app):
    _broken_sfm(app)
    _open_sfm_step(page)
    card = page.locator("#asPlain")
    expect(card).to_contain_text("Some camera positions don't line up")
    expect(card).to_contain_text("Some camera positions don't line up with the others (around keyframes 3 and 8).")
    expect(card).to_contain_text("quick turn, a doorway")
    # what continuing does: the next steps, roughly how long, and the defect in the model
    expect(card).to_contain_text("If you continue, the next steps")
    expect(card).to_contain_text("several minutes (usually 5-30 minutes for the raw mesh alone)")
    expect(card).to_contain_text("appear twice")
    # the three choices
    expect(card.locator("#asSlower")).to_have_text("Try the slower setting")
    expect(card.locator("#asFilm")).to_have_text("Stop here and film again")
    expect(card.locator("#asAnyway")).to_have_text("Continue anyway")
    # no jargon in sight; the technical message only under "technical details"
    tech = card.locator("details.tech")
    expect(tech).not_to_have_attribute("open", "")
    for word in ("×711.8", "median step"):
        expect(card.locator(f"p:has-text('{word}')")).to_have_count(0)
        expect(page.locator(f"#asResult > .as-advice:not(.as-plain) :text('{word}')")).to_have_count(0)
    tech.locator("summary").click()
    expect(tech).to_contain_text("camera-path-jump: the camera path breaks 2 times")
    expect(tech).to_contain_text("mapper = incremental")
    # the usual "Continue: next step" is replaced by the choices
    expect(page.locator("#asNext")).to_have_count(0)
    # film again: nothing runs, it says what to do with the new video
    expect(page.locator("#asStopped")).to_be_hidden()
    page.locator("#asFilm").click()
    expect(page.locator("#asStopped")).to_be_visible()
    expect(page.locator("#asStopped")).to_contain_text("New scan…")


@pytest.mark.parametrize("sfm, values, label, sent", [
    ({}, {}, "Try the slower setting", {"mapper": "incremental", "matcher": "exhaustive"}),
    # mask_sfm back to its default (off): no longer sent
    ({"path": COLLAPSED, "diag": COLLAPSE_DIAG, "masked_features": True}, {"mask_sfm": True}, "Try without the masks",
     {"mask_sfm": None}),
])
def test_the_slower_setting_reruns_from_sfm_with_its_options_and_pauses_again(page, app, sfm, values, label, sent):
    _broken_sfm(app, **sfm)
    _catch_runs(page)
    _open_sfm_step(page, values=values)
    expect(page.locator("#asSlower")).to_have_text(label)
    run = _run_sent_by(page, page.locator("#asSlower").click)
    assert run["stages"] == ["sfm", "mvs", "clean", "texture"]
    assert {k: run["options"].get(k) for k in sent} == sent
    assert run["pause_on"] == list(CAMERA_PATH_CODES)  # and stops after SfM again if it still breaks


def test_the_collapse_label_names_the_masks(page, app):
    _broken_sfm(app, path=COLLAPSED, diag=COLLAPSE_DIAG, masked_features=True)
    _open_sfm_step(page)
    card = page.locator("#asPlain")
    expect(card).to_contain_text("The camera's movement could not be worked out")
    expect(card).to_contain_text("9 of 12 photos were placed at the same spot")
    expect(card).to_contain_text("mostly empty or garbled")
    expect(card.locator("#asSlower")).to_have_text("Try without the masks")


def test_no_slower_setting_once_it_was_tried(page, app):
    _broken_sfm(app, mapper="incremental", matcher="exhaustive")
    _open_sfm_step(page)
    card = page.locator("#asPlain")
    expect(card).to_contain_text("The slower setting was already used for this run")
    expect(card.locator("#asSlower")).to_have_count(0)
    expect(card.locator("#asFilm")).to_be_visible()
    expect(card.locator("#asAnyway")).to_be_visible()


def test_continue_anyway_goes_to_the_next_step_without_running(page, app):
    _broken_sfm(app)
    runs = _catch_runs(page)
    _open_sfm_step(page)
    expect(page.locator("#asPlain")).to_contain_text("Goes on to the next step (Build the raw mesh)")
    page.locator("#asAnyway").click()
    expect(page.locator("#asMain h3")).to_contain_text("Build the raw mesh")
    assert runs == []


def test_continue_anyway_after_a_pause_runs_what_was_held_back(page, app):
    _broken_sfm(app)
    _catch_runs(page)
    _paused_job(page)
    _open_sfm_step(page)
    card = page.locator("#asPlain")
    expect(card).to_contain_text("I stopped after this step: Build the raw mesh, Isolate the subject, Paint the photos on did not run yet")
    expect(card).to_contain_text("Runs what was held back (Build the raw mesh → Isolate the subject → Paint the photos on)")
    expect(page.locator("#runStatus")).not_to_have_class("runstatus bad")  # paused is not a failure
    run = _run_sent_by(page, page.locator("#asAnyway").click)
    assert run["stages"] == ["mvs", "clean", "texture"]


def test_manual_keeps_the_technical_message_and_does_not_pause(page, app):
    _broken_sfm(app)
    _catch_runs(page)
    _open_sfm_step(page, "manual")
    expect(page.locator("#asResult")).to_contain_text("keyframes 3→4 ×711.8")
    expect(page.locator("#asResult")).to_contain_text("the camera path breaks near keyframes 3, 8")
    expect(page.locator("#asPlain")).to_have_count(0)
    run = _run_sent_by(page, page.locator("#asGo").click)  # Re-run this step
    assert run["stages"] == ["sfm"] and run["pause_on"] == []


def test_a_run_from_an_earlier_step_that_stops_after_sfm_shows_its_question(page, app):
    """Apply & re-run on the masks re-runs SfM too (it used them): when SfM's path breaks, the run stops
    there and the page moves to SfM's step, with the plain warning and the choices."""
    _broken_sfm(app, masked_features=True)
    ws = Workspace(app.root / SCAN)
    ws.record("mask", {"frames": KEYFRAMES, "mean_coverage": 0.3, "coverage_cv": 0.1, "empty_masks": 3})
    runs = _catch_runs(page)
    _paused_job(page, ran=("mask",), when=lambda: bool(runs))
    _open_sfm_step(page, step="mask", title="Find the subject")
    fix = page.locator(".as-advice.fix", has_text="keyframes found no subject")
    run = _run_sent_by(page, fix.locator("[data-asfix]").click)
    assert run["stages"] == ["mask", "sfm", "mvs", "clean", "texture"] and run["pause_on"] == list(CAMERA_PATH_CODES)
    expect(page.locator("#asMain h3")).to_contain_text("Place the cameras")
    expect(page.locator("#asPlain")).to_contain_text("I stopped after this step")
