"""Assisted mode shows SfM's camera-path warning (issue #49) as a short card: a headline, a figure drawn
from the scan's cameras, two sentences, one line per problem, and three buttons (the fix, Continue, Stop).
The fix and Continue first say in the page what they will do; Stop runs nothing. Assisted runs stop
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
    page.add_init_script(f"""try {{ if (!sessionStorage.getItem('mg.test.set')) {{
      sessionStorage.setItem('mg.test.set', '1');
      localStorage.setItem('mg.mode.{SCAN}', '{mode}');
      localStorage.setItem('mg.assist.{SCAN}', JSON.stringify({{ step: '{step}', subject: 'object', coverage: 'around' }}));
      localStorage.setItem('mg.run.{SCAN}', JSON.stringify({{ page: '{step}', values: {json.dumps(values or {})} }}));
    }} }} catch {{}}""")  # once: a reload keeps what the page stored
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


FAR = {**BROKEN, "jumps": [], "jump_count": 0, "far": [{"keyframe": 0, "ratio": 20}, {"keyframe": 1, "ratio": 21}],
       "far_count": 2}
FAR_DIAG = {"level": "warning", "code": "cameras-far-away",
            "message": "2 cameras far from all the others (keyframes 0–1, up to ×21 the usual distance from the centre)",
            "hint": "film again"}
NEXT = "Build the raw mesh → Isolate the subject → Paint the photos on"


def _stored_options(page) -> dict:
    return page.evaluate("JSON.parse(localStorage.getItem('mg.run.box') || '{}').values || {}")


def test_the_card_is_short_with_a_figure_and_three_buttons(page, app):
    _broken_sfm(app)
    runs = _catch_runs(page)
    _open_sfm_step(page)
    card = page.locator("#asPlain")
    expect(card.locator(".cp-head")).to_have_text("⚠ Camera path broken")
    expect(card.locator(".cp-sum")).to_have_text("Some views didn't connect, so the camera positions don't line up. "
                                                  "The model will likely have misplaced or doubled parts.")
    expect(card.locator(".cp-flags li")).to_have_text(["Views don't connect at keyframe 3", "Views don't connect at keyframe 8"])
    expect(card.locator(".cp-tip")).to_have_text("Tip: Quick turns and doorways often cause this.")
    # the figure, from the scan's cameras: one broken-link marker per break, the path drawn from above
    fig = card.locator("svg.cp-figure")
    expect(fig.locator(".cp-break")).to_have_count(2)
    expect(fig.locator("text", has_text="cameras seen from above")).to_have_count(1)
    expect(fig.locator(".cp-jump")).to_have_count(2)
    expect(fig.locator("text").filter(has_text="8")).not_to_have_count(0)
    # three buttons: the fix, Continue, Stop; no "film again", no "Continue: next step" underneath
    expect(card.locator(".btns button")).to_have_text(["Use the thorough camera solver…", "Continue…", "Stop"])
    expect(page.get_by_role("button", name="film again")).to_have_count(0)
    expect(page.locator("#asNext")).to_have_count(0)
    # no jargon on show: the Result box has one line per warning, the full text folded
    result = page.locator("#asResult .as-advice").first
    expect(result.locator(":scope > .diag")).to_have_text(["camera path breaks at 2 places"])
    tech = result.locator("details.tech")
    expect(tech).not_to_have_attribute("open", "")
    tech.locator("summary").click()
    expect(tech).to_contain_text("camera-path-jump: the camera path breaks 2 times")
    for word in ("×711.8", "median step", "mapper"):
        expect(page.locator(".as-plain .cp-sum, .as-plain .cp-flags, .as-plain .btns", has_text=word)).to_have_count(0)
    assert runs == []


def test_far_photos_are_drawn_lifted_off_the_strip(page, app):
    _broken_sfm(app, path=FAR, diag=FAR_DIAG)
    _open_sfm_step(page)
    card = page.locator("#asPlain")
    expect(card.locator(".cp-head")).to_have_text("⚠ Photos placed far away")
    expect(card.locator(".cp-flags li")).to_have_text(["Two photos placed far away (keyframes 0–1)"])
    expect(card.locator(".cp-figure .cp-far")).to_have_count(1)
    expect(card.locator(".cp-figure .cp-far-cam")).to_have_count(2)
    expect(card.locator(".cp-figure .cp-break")).to_have_count(0)
    expect(page.locator("#asResult .as-advice").first.locator(":scope > .diag")).to_have_text(["2 photos placed far away"])


@pytest.mark.parametrize("sfm, values, label, title, change, sent, field", [
    ({}, {}, "Use the thorough camera solver…", "Re-run with the thorough camera solver?",
     ["Camera solver", "fast → thorough"], {"mapper": "incremental", "matcher": "exhaustive"}, ("select", "mapper", "incremental")),
    # mask_sfm back to its default (off): no longer sent
    ({"path": COLLAPSED, "diag": COLLAPSE_DIAG, "masked_features": True}, {"mask_sfm": True},
     "Place the cameras without the masks…", "Re-run the cameras without the masks?",
     ["Place cameras from the subject's outline", "yes → no"], {"mask_sfm": None}, ("checkbox", "mask_sfm", False)),
])
def test_the_fix_says_what_it_will_do_then_sets_the_form_and_reruns(page, app, sfm, values, label, title, change, sent, field):
    _broken_sfm(app, **sfm)
    runs = _catch_runs(page)
    _open_sfm_step(page, values=values)
    kind, name, after = field
    control = page.locator(f"#asMain [data-opt={name}]")
    before = control.input_value() if kind == "select" else control.is_checked()
    fix = page.locator("#cpFix")
    expect(fix).to_have_text(label)
    # first, in the page, what will happen; nothing changed yet
    fix.click()
    confirm = page.locator("#cpConfirm")
    expect(confirm).to_be_visible()
    expect(confirm.locator("b").first).to_have_text(title)
    expect(confirm.locator(".row").first).to_have_text(change[0] + change[1])
    expect(confirm).to_contain_text("Re-runs" + "Place the cameras → " + NEXT)
    expect(confirm).to_contain_text("Time")
    expect(confirm).to_contain_text("Replacesthe current camera positions")
    assert _stored_options(page) == values and runs == []
    assert (control.input_value() if kind == "select" else control.is_checked()) == before
    # cancel: nothing changes
    page.locator("#cpNo").click()
    expect(confirm).to_be_hidden()
    assert _stored_options(page) == values and runs == []
    # again, and confirm: the form shows the new values and the re-run starts from SfM
    fix.click()
    run = _run_sent_by(page, page.locator("#cpYes").click)
    assert run["stages"] == ["sfm", "mvs", "clean", "texture"]
    assert {k: run["options"].get(k) for k in sent} == sent
    assert run["pause_on"] == list(CAMERA_PATH_CODES)  # and stops after SfM again if it still breaks
    if kind == "select":
        expect(page.locator(f"#asMain [data-opt={name}]")).to_have_value(after)
    else:
        expect(page.locator(f"#asMain [data-opt={name}]")).not_to_be_checked()


def test_the_collapse_card(page, app):
    _broken_sfm(app, path=COLLAPSED, diag=COLLAPSE_DIAG, masked_features=True)
    _open_sfm_step(page)
    card = page.locator("#asPlain")
    expect(card.locator(".cp-head")).to_have_text("⚠ Camera movement not found")
    expect(card.locator(".cp-flags li")).to_have_text(["9 of 12 photos placed on top of each other",
                                                       "Cameras placed from the subject's outline (a turntable setting)"])
    expect(card.locator("svg.cp-figure")).to_have_attribute("aria-label", "cameras stacked on the same spots")
    expect(page.locator("#asResult .as-advice").first.locator(":scope > .diag")).to_have_text(["9 of 12 cameras on top of each other"])


def test_no_fix_once_it_was_tried(page, app):
    _broken_sfm(app, mapper="incremental", matcher="exhaustive")
    _open_sfm_step(page)
    expect(page.locator("#asPlain .btns button")).to_have_text(["Continue…", "Stop"])


def test_continue_warns_first_then_runs_the_next_steps(page, app):
    _broken_sfm(app)
    runs = _catch_runs(page)
    _open_sfm_step(page)
    page.locator("#cpContinue").click()
    confirm = page.locator("#cpConfirm")
    expect(confirm.locator("b").first).to_have_text("Continue with these camera positions?")
    expect(confirm).to_contain_text("Runs next" + NEXT)
    expect(confirm).to_contain_text("usually 5-30 minutes for the raw mesh")
    expect(confirm.locator(".impact")).to_have_text("⚠ The model will likely have misplaced or doubled parts.")
    assert runs == []
    page.locator("#cpNo").click()
    expect(confirm).to_be_hidden()
    page.locator("#cpContinue").click()
    run = _run_sent_by(page, page.locator("#cpYes").click)
    assert run["stages"] == ["mvs", "clean", "texture"]
    expect(page.locator("#asMain h3")).to_contain_text("Build the raw mesh")


def test_continue_after_a_pause_runs_what_was_held_back(page, app):
    _broken_sfm(app)
    _catch_runs(page)
    _paused_job(page)
    _open_sfm_step(page)
    expect(page.locator("#asPlain .cp-paused")).to_have_text("The run stopped here to ask you before going on.")
    expect(page.locator("#runStatus")).not_to_have_class("runstatus bad")  # paused is not a failure
    page.locator("#cpContinue").click()
    run = _run_sent_by(page, page.locator("#cpYes").click)
    assert run["stages"] == ["mvs", "clean", "texture"]


def test_stop_runs_nothing_and_the_card_stays(page, app):
    _broken_sfm(app)
    runs = _catch_runs(page)
    _open_sfm_step(page)
    page.locator("#cpStop").click()
    stopped = page.locator("#cpStopped")
    expect(stopped).to_have_text("Stopped. Nothing else runs: the scan stays as it is after Place the cameras. "
                                 "You can come back here to re-run or continue.")
    expect(page.locator("#asPlain .btns button")).to_have_count(3)
    page.reload()
    expect(page.locator("#cpStopped")).to_be_visible()  # remembered for this scan
    expect(page.locator("#cpContinue")).to_be_enabled()
    assert runs == []


def test_a_run_from_an_earlier_step_that_stops_after_sfm_shows_its_question(page, app):
    """Apply & re-run on the masks re-runs SfM too (it used them): when SfM's path breaks, the run stops
    there and the page moves to SfM's step, with the card."""
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
    expect(page.locator("#asPlain .cp-paused")).to_be_visible()


def test_manual_keeps_the_technical_message_and_does_not_pause(page, app):
    _broken_sfm(app)
    _catch_runs(page)
    _open_sfm_step(page, "manual")
    expect(page.locator("#asResult")).to_contain_text("keyframes 3→4 ×711.8")
    expect(page.locator("#asResult")).to_contain_text("the camera path breaks near keyframes 3, 8")
    expect(page.locator("#asPlain")).to_have_count(0)
    run = _run_sent_by(page, page.locator("#asGo").click)  # Re-run this step
    assert run["stages"] == ["sfm"] and run["pause_on"] == []
