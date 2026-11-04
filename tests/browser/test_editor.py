"""The editor (edit.html) driven with the mouse on its WebGL view, as the issue lists: a sculpt stroke and
its undo, Adaptive detail and Decimate, the clone tool, seams drawn by hand, and the texture layout's
preview, rebuild and undo, and carrying the texture over to a sculpted mesh.

The model is framed in the middle of the view, so strokes across the middle land on it. Each check
reads what the editor says (its status line, messages and buttons) and, after a save or a rebuild,
what the server has."""

import json
import re

import pytest

pytest.importorskip("playwright", reason="needs Playwright: uv pip install -e '.[dev]'")

from playwright.sync_api import expect  # noqa: E402

from scan_fixture import SCAN  # noqa: E402
from ui import click_on_view, drag_on_view, editor_ready, faces, frames_drawn, open_page, undo_count  # noqa: E402

FACES = 6912  # the test scan's clean mesh


def state(app):
    return json.loads(app.get(f"/api/workspaces/{SCAN}/state"))


def to_paint(page, layout="3d"):
    page.locator("#modeTabs [data-value=paint]").click()
    editor_ready(page)
    page.locator(f'#layoutTabs [data-value="{layout}"]').click()  # 3D only: the model in the middle of the view


# ---------------------------------------------------------------- sculpt
def test_sculpt_stroke_undo_redo_and_save(page, app):
    open_page(page, "edit")
    expect(page.locator("#status")).to_have_text(f"{FACES:,} faces · 3,458 vertices · 0 undo")
    expect(page.locator("#undoBtn")).to_be_disabled()
    expect(page.locator("#saveBtn")).to_be_disabled()
    drag_on_view(page)
    expect(page.locator("#status")).to_contain_text("1 undo")
    expect(page.locator("#topStatus")).to_have_text("unsaved sculpt edits")
    expect(page.locator("#undoBtn")).to_be_enabled()
    expect(page.locator("#saveBtn")).to_be_enabled()
    page.keyboard.press("Control+z")
    expect(page.locator("#status")).to_contain_text("0 undo")
    expect(page.locator("#redoBtn")).to_be_enabled()
    page.locator("#redoBtn").click()
    expect(page.locator("#status")).to_contain_text("1 undo")
    assert faces(page) == FACES  # a stroke moves vertices, no more
    page.keyboard.press("Control+s")
    expect(page.locator("#okmsg")).to_contain_text(f"saved as your custom mesh: {FACES:,} faces")
    expect(page.locator("#topStatus")).to_have_text("")
    expect(page.locator("#modeNote")).to_have_text(re.compile("^editing your custom mesh"))
    assert state(app)["custom"]["mesh"]


def test_adaptive_detail_adds_vertices_under_the_stroke_and_undo_takes_them_back(page):
    open_page(page, "edit")
    page.locator("#adaptive").check()
    page.locator("#adDensity").fill("8")  # ×8: many new vertices under one stroke
    drag_on_view(page)
    msg = page.locator("#okmsg")
    expect(msg).to_have_text(re.compile(r"^Adaptive detail added [\d,]+ vertices under the stroke"))
    added = int(re.search(r"added ([\d,]+) vertices", msg.inner_text()).group(1).replace(",", ""))
    assert added > 0 and faces(page) > FACES
    assert undo_count(page) == 1  # one undo step for the stroke and its new vertices
    # the button, not Ctrl+Z: the focus is still on the density slider (a stroke doesn't take it),
    # and the editor's keys ignore every <input>, sliders and checkboxes included (see the PR)
    page.locator("#undoBtn").click()
    expect(page.locator("#status")).to_have_text(f"{FACES:,} faces · 3,458 vertices · 0 undo")


def test_decimate_merges_edges_where_you_brush_and_undo_takes_it_back(page):
    open_page(page, "edit")
    page.keyboard.press("e")
    expect(page.locator('#tools [data-tool="decimate"]')).to_have_class(re.compile(r"\bon\b"))
    page.locator("#adDensity").fill("4")  # ÷4
    drag_on_view(page, dy=0.05)
    msg = page.locator("#okmsg")
    expect(msg).to_have_text(re.compile(r"^decimated: [\d,]+ → [\d,]+ faces$"))
    before, after = (int(n.replace(",", "")) for n in re.findall(r"[\d,]+", msg.inner_text()))
    assert before == FACES and after < before and faces(page) == after
    page.locator("#undoBtn").click()
    expect(page.locator("#status")).to_contain_text(f"{FACES:,} faces")
    page.locator("#redoBtn").click()
    expect(page.locator("#status")).to_contain_text(f"{after:,} faces")


# ---------------------------------------------------------------- paint
def test_clone_takes_its_source_with_ctrl_click_then_paints_a_copy(page):
    open_page(page, "edit")
    to_paint(page)
    expect(page.locator("#status")).to_contain_text("texture 0: 1024×1024 · 0/")
    click_on_view(page, modifiers=["Control"], fx=0.4, fy=0.4)
    expect(page.locator("#cloneInfo")).to_have_text(re.compile(r"^clone source 0\.\d{3}, 0\.\d{3}$"))
    expect(page.locator('#tools [data-tool="clone"]')).to_have_class(re.compile(r"\bon\b"))
    drag_on_view(page, dx=0.08, dy=0.05)
    expect(page.locator("#status")).to_contain_text("· 1/")
    expect(page.locator("#topStatus")).to_have_text("unsaved paint")
    page.keyboard.press("Control+z")
    expect(page.locator("#status")).to_contain_text("· 0/")
    expect(page.locator("#topStatus")).to_have_text("")


def test_seams_drawn_by_hand_undo_and_apply_then_undo_layout(page, app):
    open_page(page, "edit")
    to_paint(page)
    page.locator("#seamBtn").click()
    info = page.locator("#seamInfo")
    expect(page.locator("#seamBar")).to_be_visible()
    expect(info).to_have_text(re.compile(r"^54 pieces · [\d,]+ seam edges"))  # the pipeline's layout: 54 pieces
    seams = lambda: int(re.search(r"([\d,]+) seam edges", info.inner_text()).group(1).replace(",", ""))  # noqa: E731
    before = seams()
    # Join: brush over seams to remove them. (Cut follows the mesh's edges, and on this model every
    # edge but the pieces' diagonals is a seam already, so it is Join that changes something here.)
    page.locator('#seamBar [data-seam="join"]').click()
    expect(page.locator("#seamHelp")).to_have_text(re.compile("^Join: brush over seams"))
    drag_on_view(page, dx=0.15, steps=20)
    expect(info).not_to_have_text(re.compile(rf" {before:,} seam edges"))
    assert seams() < before
    page.keyboard.press("Control+z")  # a stroke at a time
    frames_drawn(page)  # the seams are redrawn on the next frame: the line under them must still count the pieces
    expect(info).to_have_text(re.compile(rf"^54 pieces · {before:,} seam edges"))
    drag_on_view(page, dx=0.15, steps=20)
    frames_drawn(page)
    expect(info).to_have_text(re.compile(r"^\d+ pieces · [\d,]+ seam edges"))
    expect(info).not_to_have_text(re.compile(rf" {before:,} seam edges"))
    pieces = int(info.inner_text().split(" ")[0])
    assert pieces < 54
    page.locator("#seamApply").click()
    msg = page.locator("#okmsg")
    expect(msg).to_have_text(re.compile(rf"^texture rebuilt with your seams: {pieces} pieces"), timeout=60_000)
    expect(page.locator("#seamBar")).to_be_hidden()
    assert state(app)["custom"]["texture"]
    undo = page.locator("#lyUndo")
    expect(undo).to_be_visible()
    undo.click()
    expect(msg).to_have_text("back to the pipeline's texture layout")
    expect(undo).to_be_hidden()
    assert not state(app)["custom"]["texture"]


def test_texture_layout_count_preview_rebuild_and_undo(page, app):
    open_page(page, "edit")
    to_paint(page, layout="split")
    page.locator("#lyCount").click()
    info = page.locator("#lyInfo")
    expect(info).to_have_text(re.compile(r"^now 54 pieces · rebuilt: [\d,]+$"))
    rebuilt = int(info.inner_text().rsplit(" ", 1)[1].replace(",", ""))
    assert rebuilt < 54
    # a preview in the flat view: nothing saved
    page.locator("#lyLook").click()
    expect(info).to_have_text(f"preview in the flat view: {rebuilt:,} pieces (not applied)")
    expect(page.locator("#label2d")).to_have_text(re.compile(rf"^preview · {rebuilt:,} pieces.* · not applied$"))
    assert not state(app)["custom"]["texture"]
    # rebuilt: the custom texture, with an undo
    page.locator("#lyGo").click()
    msg = page.locator("#okmsg")
    expect(msg).to_have_text(re.compile(rf"^texture rebuilt: 54 → {rebuilt:,} pieces, \d+ × \d+ px"), timeout=60_000)
    custom = state(app)["custom"]
    assert custom["texture"] and custom["layout_undo"]
    expect(page.locator("#modeNote")).to_have_text("painting your custom textured model")
    page.locator("#lyUndo").click()
    expect(msg).to_have_text("back to the pipeline's texture layout")
    expect(page.locator("#lyUndo")).to_be_hidden()
    expect(page.locator("#modeNote")).to_have_text(re.compile("^the pipeline textured model"))
    assert not state(app)["custom"]["texture"]


def test_carry_the_texture_over_to_a_sculpted_mesh(page, app):
    """After a saved sculpt, Paint offers to give the new shape its own texture. Without OpenMVS's
    scene (the test scan has none) Texture my edits is hidden and carrying over is the way."""
    open_page(page, "edit")
    drag_on_view(page)
    page.keyboard.press("Control+s")
    expect(page.locator("#okmsg")).to_contain_text("saved as your custom mesh")
    to_paint(page)
    expect(page.locator("#modeWarn")).to_contain_text("This textured model is from before your sculpt edits")
    expect(page.locator("#editTex")).to_be_visible()
    expect(page.locator("#texEdits")).to_be_hidden()
    page.locator("#carryTex").click()
    msg = page.locator("#okmsg")
    expect(msg).to_have_text(re.compile(r"^texture carried over to your edited mesh: [\d,]+ pieces"), timeout=60_000)
    custom = state(app)["custom"]
    assert custom["texture"] and custom["texture_from"] == "custom" and not custom["texture_stale"]
    expect(page.locator("#editTex")).to_be_hidden()
    expect(page.locator("#modeWarn")).to_be_hidden()
