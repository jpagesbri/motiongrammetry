"""One navigation for every 3D view (#26): the Scan page's preview moves as the editor does (left or right
drag turns, middle drag pans, Space + left drag pans, the wheel zooms), and the clipping planes follow the
camera in both, so a model approached closely is not cut (#22).

The camera is read from the view's canvas (`canvas.navigation`, set by static/navigation.js)."""

import math

import pytest

pytest.importorskip("playwright", reason="needs Playwright: uv pip install -e '.[dev]'")

from playwright.sync_api import expect  # noqa: E402

from ui import drag_on_view, editor_ready, frames_drawn, open_page, undo_count  # noqa: E402

CAMERA = """() => {
  const n = document.querySelector('#view canvas').navigation, c = n.controls.object, t = n.controls.target;
  return { pos: c.position.toArray(), target: t.toArray(), near: c.near, far: c.far, size: n.size,
           dist: c.position.distanceTo(t), left: n.controls.mouseButtons.LEFT };
}"""
ROTATE, PAN = 0, 2  # THREE.MOUSE


def camera(page):
    return page.evaluate(CAMERA)


def direction(cam):
    d = [p - t for p, t in zip(cam["pos"], cam["target"])]
    n = math.hypot(*d)
    return [x / n for x in d]


def moved(a, b, tol=1e-6):
    return math.dist(a, b) > tol


def turned(before, after):
    """The view turned about the target: the target stays, the direction changes."""
    return not moved(before["target"], after["target"]) and moved(direction(before), direction(after), 1e-3)


def panned(before, after):
    """The view slid sideways: the target moves, the direction and the distance stay."""
    return (moved(before["target"], after["target"]) and not moved(direction(before), direction(after), 1e-4)
            and math.isclose(before["dist"], after["dist"], rel_tol=1e-4))


def view_centre(page):
    box = page.locator("#view canvas").first.bounding_box()
    return box["x"] + box["width"] / 2, box["y"] + box["height"] / 2, box["width"]


def drag(page, button="left", dx=0.12, key=None):
    """A drag across the middle of the view with one mouse button, holding `key` (e.g. Space) if given."""
    cx, cy, w = view_centre(page)
    page.mouse.move(cx - dx * w, cy)
    if key:
        page.keyboard.down(key)
    page.mouse.down(button=button)
    page.mouse.move(cx + dx * w, cy + 0.03 * w, steps=10)
    page.mouse.up(button=button)
    if key:
        page.keyboard.up(key)


def zoom_in(page, steps=10):
    cx, cy, _ = view_centre(page)
    page.mouse.move(cx, cy)
    for _ in range(steps):
        page.mouse.wheel(0, -500)
    frames_drawn(page)


def open_preview(page):
    open_page(page, "scan")
    page.locator("#tabPreview").click()
    expect(page.locator("#panePreview")).to_be_visible()
    expect(page.locator("#pv3d")).to_be_visible()
    expect(page.locator("#loading")).to_be_hidden(timeout=30_000)
    expect(page.locator("#layers input[data-layer=textured]")).to_be_checked()
    frames_drawn(page)


# ---------------------------------------------------------------- the Scan page's preview
def test_preview_right_drag_turns_middle_drag_and_space_drag_pan_and_the_wheel_zooms(page):
    open_preview(page)
    a = camera(page)
    assert a["left"] == ROTATE
    drag(page, "right")
    b = camera(page)
    assert turned(a, b), (a, b)  # before: right drag panned
    drag(page, "middle")
    c = camera(page)
    assert panned(b, c), (b, c)  # before: middle drag zoomed
    drag(page, "left", key="Space")
    d = camera(page)
    assert panned(c, d), (c, d)
    assert camera(page)["left"] == ROTATE  # Space up: left drag turns again
    drag(page, "left")
    e = camera(page)
    assert turned(d, e), (d, e)
    zoom_in(page, 3)
    f = camera(page)
    assert f["dist"] < e["dist"] * 0.8 and not moved(e["target"], f["target"])
    # the near plane follows the camera's distance (#22)
    assert f["near"] < e["near"] and f["near"] <= f["dist"] / 1000 * 1.001


def test_space_over_the_view_neither_scrolls_nor_presses_a_button(page):
    open_preview(page)
    page.evaluate("() => addEventListener('keydown', (e) => { if (e.code === 'Space') window.spaceKept = e.defaultPrevented; })")
    cx, cy, _ = view_centre(page)
    page.mouse.move(cx, cy)
    page.keyboard.down("Space")
    assert camera(page)["left"] == PAN
    assert page.evaluate("window.spaceKept") is True  # no page scroll, no button press
    page.keyboard.up("Space")
    assert camera(page)["left"] == ROTATE


def test_editor_space_in_a_field_is_the_field_s_and_left_drag_still_turns(page):
    open_page(page, "edit")
    page.evaluate("() => addEventListener('keydown', (e) => { if (e.code === 'Space') window.spaceKept = e.defaultPrevented; })")
    page.locator("#radius").focus()  # the brush radius slider
    page.keyboard.down("Space")
    assert camera(page)["left"] == ROTATE
    assert page.evaluate("window.spaceKept") is False
    page.keyboard.up("Space")


# ---------------------------------------------------------------- the editor
def test_editor_space_drag_pans_right_drag_turns_and_a_stroke_still_sculpts(page):
    open_page(page, "edit")
    frames_drawn(page)
    a = camera(page)
    drag(page, "left", key="Space")
    b = camera(page)
    assert panned(a, b), (a, b)
    assert undo_count(page) == 0  # Space: the left button pans, no stroke
    drag(page, "right")
    c = camera(page)
    assert turned(b, c), (b, c)
    assert undo_count(page) == 0
    page.locator(".vcube .vfit").click()  # the frame button: the model back in the middle
    frames_drawn(page)
    drag_on_view(page)
    expect(page.locator("#status")).to_contain_text("1 undo")


def test_editor_paint_space_drag_pans_and_a_stroke_still_paints(page):
    open_page(page, "edit")
    page.locator("#modeTabs [data-value=paint]").click()
    editor_ready(page)
    page.locator('#layoutTabs [data-value="3d"]').click()
    expect(page.locator("#status")).to_contain_text("· 0/")
    frames_drawn(page)
    a = camera(page)
    drag(page, "left", key="Space")
    b = camera(page)
    assert panned(a, b), (a, b)
    drag(page, "middle")
    c = camera(page)
    assert panned(b, c), (b, c)
    expect(page.locator("#status")).to_contain_text("· 0/")  # no paint while panning
    page.locator(".vcube .vfit").click()
    frames_drawn(page)
    drag_on_view(page, dx=0.08, dy=0.05)
    expect(page.locator("#status")).to_contain_text("· 1/")


def test_editor_near_plane_follows_the_camera_so_a_close_look_is_not_clipped(page):
    open_page(page, "edit")
    frames_drawn(page)
    a = camera(page)
    assert a["near"] <= a["dist"] / 1000 * 1.001  # set from the distance, not size / 500 once
    zoom_in(page, 10)
    b = camera(page)
    assert b["dist"] < a["dist"] * 0.2
    # before: near stayed at size / 500, a large share of a close distance, and cut the surface
    assert b["near"] < a["near"] / 4 and b["near"] <= max(b["dist"] / 1000, b["size"] / 1e5) * 1.001


# ---------------------------------------------------------------- the Export window's preview
def test_export_preview_moves_the_same_way(page):
    open_page(page, "scan")
    page.locator("#topExport").click()
    expect(page.locator("#exItems .ex-item").first).to_be_visible()
    for box in page.locator("#exItems input[type=checkbox]:not([disabled])").all():
        box.set_checked(box.get_attribute("id") == "exOn-mesh")
    page.locator("#exFmt-mesh").select_option("glb")
    page.locator("#exBuild").click()
    expect(page.locator("#exShown")).to_contain_text("made just now", timeout=60_000)
    canvas = page.locator("#exPreview canvas[data-engine]")
    expect(canvas).to_be_visible()
    frames_drawn(page)
    cam = CAMERA.replace("#view canvas", "#exPreview canvas[data-engine]")
    box = canvas.bounding_box()
    cx, cy, w = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2, box["width"]

    def drag_here(button, key=None):
        page.mouse.move(cx - 0.12 * w, cy)
        if key:
            page.keyboard.down(key)
        page.mouse.down(button=button)
        page.mouse.move(cx + 0.12 * w, cy + 0.03 * w, steps=10)
        page.mouse.up(button=button)
        if key:
            page.keyboard.up(key)

    a = page.evaluate(cam)
    assert a["near"] <= a["dist"] / 1000 * 1.001  # the clipping follows the camera here too
    drag_here("right")
    b = page.evaluate(cam)
    assert turned(a, b), (a, b)
    drag_here("middle")
    c = page.evaluate(cam)
    assert panned(b, c), (b, c)
    drag_here("left", key="Space")
    d = page.evaluate(cam)
    assert panned(c, d), (c, d)
    expect(page.locator("#exportModal")).to_be_visible()  # Space pressed no button of the window
