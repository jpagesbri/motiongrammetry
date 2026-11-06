"""One style for warnings on the Scan page (owner, PR #65): a note with an icon, a short title, one short
line, an optional figure, buttons of 1-3 words, and one Details toggle, closed. The same at every step
of Assisted and in Manual (stage cards, step pages). SfM's camera-path warning is such a note with a
figure of the scan's cameras and Continue / Stop; Assisted runs stop after SfM on it (pause_on). Every
fix first shows a compact confirmation in the page.

No reconstruction runs: the run requests are caught in the browser and checked for what they ask."""

import json

import pytest

pytest.importorskip("playwright", reason="needs Playwright: uv pip install -e '.[dev]'")

from playwright.sync_api import expect  # noqa: E402

from motiongrammetry import checks  # noqa: E402
from motiongrammetry.advice import CAMERA_PATH_CODES  # noqa: E402
from motiongrammetry.workspace import Workspace  # noqa: E402
from scan_fixture import KEYFRAMES, SCAN  # noqa: E402

BROKEN = {"ok": False, "checked": True, "collapsed": False, "jumps": [{"from": 3, "to": 4, "ratio": 711.8},
                                                                     {"from": 8, "to": 9, "ratio": 31.1}],
          "jump_count": 2, "far": [], "far_count": 0}
FAR = {**BROKEN, "jumps": [], "jump_count": 0, "far": [{"keyframe": 0, "ratio": 20}, {"keyframe": 1, "ratio": 21}],
       "far_count": 2}
COLLAPSED = {"ok": False, "checked": True, "collapsed": True, "cameras": KEYFRAMES, "collapsed_cameras": 9,
             "distinct_positions": 4, "still_steps": 7, "steps": KEYFRAMES - 1, "jumps": [], "far": []}


def _broken_sfm(app, path=BROKEN, **sfm) -> None:
    """The box scan's SfM with this camera path, and the warnings the sfm gate gives for it."""
    ws = Workspace(app.root / SCAN)
    ws.record("sfm", {**ws.load_manifest().get("sfm", {}), "frames": KEYFRAMES, "registered": KEYFRAMES,
                      "points3D": 200, "mean_reproj_error_px": 0.7, "camera_path": path, **sfm})
    ws.record_diagnostics("sfm", [d.as_dict() for d in checks.camera_path_diags(path)])


def _subject_missed(app) -> None:
    ws = Workspace(app.root / SCAN)
    r = {"frames": KEYFRAMES, "mean_coverage": 0.3, "coverage_cv": 0.9, "empty_masks": 4}
    ws.record("mask", r)
    ws.record_diagnostics("mask", [d.as_dict() for d in checks.gate("mask", ws, r, {})])


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
            "stages": {**{s: {"status": "done"} for s in ran}, "sfm": {"status": "warning", "diagnostics": [d.as_dict() for d in checks.camera_path_diags(BROKEN)]},
                       **{s: {"status": "skipped"} for s in held}},
            "paused": {"stage": "sfm", "codes": ["camera-path-jump"], "skipped": held}}
    page.route(f"**/api/workspaces/{SCAN}/job*", lambda route: route.fulfill(json=snap) if when() else route.continue_())


def _stored_options(page) -> dict:
    return page.evaluate("JSON.parse(localStorage.getItem('mg.run.box') || '{}').values || {}")


def _buttons_are_short(page) -> None:
    labels = page.locator("#asMain .note .btns button").all_inner_texts()
    assert labels and all(1 <= len(t.split()) <= 3 and "…" not in t for t in labels), labels


def _details(note):
    """A note's one Details toggle: closed, opens on a click."""
    d = note.locator("details.note-more")
    expect(d).to_have_count(1)
    expect(d).not_to_have_attribute("open", "")
    d.locator("summary").click()
    expect(d).to_have_attribute("open", "")
    return d


def test_the_camera_card_is_a_note_with_a_figure(page, app):
    _broken_sfm(app)
    runs = _catch_runs(page)
    _open_sfm_step(page)
    card = page.locator("#asPlain")
    expect(card).to_have_class("note warn")
    expect(card.locator(".note-title")).to_have_text("Camera path broken")
    expect(card.locator(".note-line")).to_have_text("Views don't connect at 2 places.")
    fig = card.locator("svg.cp-figure")  # from the scan's cameras: one broken-link marker per break
    expect(fig.locator(".cp-break")).to_have_count(2)
    expect(fig.locator(".cp-jump")).to_have_count(2)
    expect(card.locator(":scope > .btns button")).to_have_text(["Thorough solver", "Continue", "Stop"])
    _buttons_are_short(page)
    # the warning is said once: the gate's diagnostic is in this card's Details, not a note of its own
    expect(page.locator("#asResult .note")).to_have_count(1)
    expect(page.locator("#asNext")).to_have_count(0)
    d = _details(card)
    expect(d).to_contain_text("Views don't connect at keyframe 3")
    expect(d).to_contain_text("the camera path breaks 2 times")  # the full message, as Manual's Details and the CLI
    for word in ("×711.8", "median", "mapper"):
        expect(card.locator(".note-line, .note-title, :scope > .btns", has_text=word)).to_have_count(0)
    assert runs == []


def test_far_photos_are_drawn_lifted_off_the_strip(page, app):
    _broken_sfm(app, path=FAR)
    _open_sfm_step(page)
    card = page.locator("#asPlain")
    expect(card.locator(".note-title")).to_have_text("Photos placed far away")
    expect(card.locator(".note-line")).to_have_text("2 photos far from the others.")
    expect(card.locator(".cp-figure .cp-far")).to_have_count(1)
    expect(card.locator(".cp-figure .cp-far-cam")).to_have_count(2)
    expect(card.locator(".cp-figure .cp-break")).to_have_count(0)


@pytest.mark.parametrize("sfm, values, label, change, sent, field", [
    ({}, {}, "Thorough solver", "Solver fast → thorough, Matching neighbours → all pairs",
     {"mapper": "incremental", "matcher": "exhaustive"}, ("select", "mapper", "incremental")),
    # mask_sfm back to its default (off): no longer sent
    ({"path": COLLAPSED, "masked_features": True}, {"mask_sfm": True}, "Retry without masks",
     "Masks for cameras on → off", {"mask_sfm": None}, ("checkbox", "mask_sfm", False)),
])
def test_a_fix_confirms_compactly_then_sets_the_form_and_reruns(page, app, sfm, values, label, change, sent, field):
    _broken_sfm(app, **sfm)
    runs = _catch_runs(page)
    _open_sfm_step(page, values=values)
    kind, name, after = field
    control = page.locator(f"#asMain [data-opt={name}]")
    before = control.input_value() if kind == "select" else control.is_checked()
    fix = page.locator("#asPlain [data-fix]")
    expect(fix).to_have_text(label)
    fix.click()
    confirm = page.locator("#asPlain .note-confirm")
    expect(confirm).to_be_visible()
    expect(confirm.locator(".nc-title")).to_have_text("Re-run with these changes?")
    expect(confirm.locator(".nc-row")).to_have_count(3)
    expect(confirm.locator(".nc-row").nth(0)).to_have_text("Changes" + change)
    expect(confirm.locator(".nc-row").nth(1)).to_have_text("Re-runsCameras → Mesh → Clean → Texture")
    expect(confirm.locator(".nc-row").nth(2)).to_contain_text("Time~")
    expect(confirm.locator(".nc-warn")).to_have_text("⚠ Replaces the cameras; stops again if still broken.")
    expect(confirm.locator(".btns button")).to_have_text(["Re-run", "Cancel"])
    assert len(confirm.locator(".nc-title").inner_text().split()) <= 6
    # nothing changed yet; Cancel changes nothing
    assert _stored_options(page) == values and runs == []
    assert (control.input_value() if kind == "select" else control.is_checked()) == before
    confirm.locator("[data-nc=no]").click()
    expect(confirm).to_be_hidden()
    assert _stored_options(page) == values and runs == []
    # again, and confirm: the form shows the new values and the re-run starts from SfM
    fix.click()
    run = _run_sent_by(page, page.locator("#asPlain [data-nc=yes]").click)
    assert run["stages"] == ["sfm", "mvs", "clean", "texture"]
    assert {k: run["options"].get(k) for k in sent} == sent
    assert run["pause_on"] == list(CAMERA_PATH_CODES)  # and stops after SfM again if it still breaks
    if kind == "select":
        expect(page.locator(f"#asMain [data-opt={name}]")).to_have_value(after)
    else:
        expect(page.locator(f"#asMain [data-opt={name}]")).not_to_be_checked()


def test_the_collapse_card(page, app):
    _broken_sfm(app, path=COLLAPSED, masked_features=True)
    _open_sfm_step(page)
    card = page.locator("#asPlain")
    expect(card.locator(".note-title")).to_have_text("Camera movement not found")
    expect(card.locator(".note-line")).to_have_text("The camera's movement couldn't be worked out.")
    expect(card.locator("svg.cp-figure")).to_have_attribute("aria-label", "cameras stacked on the same spots")
    expect(card.locator(":scope > .btns button")).to_have_text(["Retry without masks", "Continue", "Stop"])
    expect(_details(card)).to_contain_text("9 of 12 photos placed on top of each other")


def test_no_fix_once_it_was_tried(page, app):
    _broken_sfm(app, mapper="incremental", matcher="exhaustive")
    _open_sfm_step(page)
    expect(page.locator("#asPlain > .btns button")).to_have_text(["Continue", "Stop"])


def test_continue_warns_first_then_runs_the_next_steps(page, app):
    _broken_sfm(app)
    runs = _catch_runs(page)
    _open_sfm_step(page)
    page.locator("#cpContinue").click()
    confirm = page.locator("#asPlain .note-confirm")
    expect(confirm.locator(".nc-title")).to_have_text("Continue with these cameras?")
    expect(confirm.locator(".nc-row")).to_have_text(["RunsMesh → Clean → Texture", "Time~7–36 min"])
    expect(confirm.locator(".nc-warn")).to_have_text("⚠ Likely misplaced or doubled parts.")
    expect(confirm.locator(".btns button")).to_have_text(["Continue", "Cancel"])
    assert runs == []
    confirm.locator("[data-nc=no]").click()
    expect(confirm).to_be_hidden()
    page.locator("#cpContinue").click()
    run = _run_sent_by(page, page.locator("#asPlain [data-nc=yes]").click)
    assert run["stages"] == ["mvs", "clean", "texture"]
    expect(page.locator("#asMain h3")).to_contain_text("Build the raw mesh")


def test_continue_after_a_pause_runs_what_was_held_back(page, app):
    _broken_sfm(app)
    _catch_runs(page)
    _paused_job(page)
    _open_sfm_step(page)
    expect(page.locator("#asPlain .note-line")).to_have_text("Views don't connect at 2 places. The run stopped here.")
    expect(page.locator("#runStatus")).not_to_have_class("runstatus bad")  # paused is not a failure
    page.locator("#cpContinue").click()
    run = _run_sent_by(page, page.locator("#asPlain [data-nc=yes]").click)
    assert run["stages"] == ["mvs", "clean", "texture"]


def test_stop_runs_nothing_and_the_card_stays(page, app):
    _broken_sfm(app)
    runs = _catch_runs(page)
    _open_sfm_step(page)
    page.locator("#cpStop").click()
    expect(page.locator("#cpStopped")).to_have_text("Stopped. Nothing else runs; come back any time.")
    expect(page.locator("#asPlain > .btns button")).to_have_count(3)
    page.reload()
    expect(page.locator("#cpStopped")).to_be_visible()  # remembered for this scan
    expect(page.locator("#cpContinue")).to_be_enabled()
    assert runs == []


def test_the_subject_step_uses_the_same_notes(page, app):
    """Find the subject: one note per problem, the gate's warnings folded into them, same buttons and Details."""
    _subject_missed(app)
    runs = _catch_runs(page)
    _open_sfm_step(page, step="mask", title="Find the subject")
    notes = page.locator("#asResult .note")
    expect(notes.locator(".note-title")).to_have_text(["Subject missed in some frames", "Masks vary a lot"])
    expect(notes.locator(".note-line")).to_have_text(["4 of 12 keyframes found no subject.",
                                                      "The subject's outline changes between keyframes."])
    expect(notes.nth(0).locator(".btns button")).to_have_text(["Try person masks", "Show keyframes"])
    expect(notes.nth(1).locator(".btns button")).to_have_text(["Try HQ masks", "Show keyframes"])
    _buttons_are_short(page)
    expect(_details(notes.nth(0))).to_contain_text("4/12 masks are empty")  # the gate's message, folded in
    notes.nth(0).locator("[data-fix]").click()
    confirm = notes.nth(0).locator(".note-confirm")
    expect(confirm.locator(".nc-row").nth(0)).to_have_text("ChangesMask model object → person")
    expect(confirm.locator(".nc-row").nth(1)).to_have_text("Re-runsMasks → Clean → Texture")
    assert runs == []


def test_a_step_with_nothing_to_say_shows_one_done_note(page, app):
    ws = Workspace(app.root / SCAN)
    ws.record("sfm", {"frames": KEYFRAMES, "registered": KEYFRAMES, "points3D": 9000, "mean_reproj_error_px": 0.5})
    _open_sfm_step(page)
    notes = page.locator("#asResult .note")
    expect(notes).to_have_count(1)
    expect(notes).to_have_class("note ok")
    expect(notes.locator(".note-title")).to_have_text("Cameras placed")
    expect(notes.locator(".note-line")).to_have_text("All 12 keyframes, 0.50 px error.")


def test_a_run_from_an_earlier_step_that_stops_after_sfm_shows_its_question(page, app):
    """A mask fix re-runs SfM too (it used the masks): when SfM's path breaks, the run stops there and the
    page moves to SfM's step, with the card."""
    _broken_sfm(app, masked_features=True)
    _subject_missed(app)
    runs = _catch_runs(page)
    _paused_job(page, ran=("mask",), when=lambda: bool(runs))
    _open_sfm_step(page, step="mask", title="Find the subject")
    note = page.locator("#asResult .note", has_text="Subject missed in some frames")
    note.locator("[data-fix]").click()
    run = _run_sent_by(page, note.locator("[data-nc=yes]").click)
    assert run["stages"] == ["mask", "sfm", "mvs", "clean", "texture"] and run["pause_on"] == list(CAMERA_PATH_CODES)
    expect(page.locator("#asMain h3")).to_contain_text("Place the cameras")
    expect(page.locator("#asPlain .note-line")).to_contain_text("The run stopped here.")


def test_manual_shows_the_same_notes_short_with_details_on_request(page, app):
    _broken_sfm(app)
    _catch_runs(page)
    # All steps: the stage card's warning is a compact note
    _open_sfm_step(page, "manual", step="overview", title="All steps")
    card = page.locator("#stages .stage").filter(has=page.locator(".name", has_text="sfm"))
    note = card.locator(".note.compact")
    expect(note.locator(".note-title")).to_have_text("Camera path broken")
    expect(note.locator(".note-line")).to_have_text("Views don't connect at 2 places.")
    assert "×" not in note.locator(".note-head").inner_text() + note.locator(".note-line").inner_text()
    expect(_details(note)).to_contain_text("keyframes 3→4 ×711.8")
    # the step page: the same note as Assisted, without the figure and Continue / Stop; no pause
    page.locator("#rail [data-page=sfm]").click()
    n = page.locator("#asResult .note")
    expect(n).to_have_count(1)
    expect(n.locator(".note-title")).to_have_text("Camera path broken")
    expect(n.locator(".btns button")).to_have_text(["Thorough solver", "Show keyframes"])
    expect(page.locator("#asPlain, .cp-figure")).to_have_count(0)
    run = _run_sent_by(page, page.locator("#asGo").click)  # Re-run this step
    assert run["stages"] == ["sfm"] and run["pause_on"] == []
