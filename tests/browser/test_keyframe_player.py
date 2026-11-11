"""The keyframe player under the 3D preview (#26): first / previous / play / next / last in the strip's
header with the position, ← → Home End on the 3D model, play at a steady pace that stops on the last
frame and pauses when you move the view. Each step is a click on that thumbnail: selected, flown to.

The camera highlighted in the view is read from `#view[data-cam]` (set by highlightCamera)."""

import pytest

pytest.importorskip("playwright", reason="needs Playwright: uv pip install -e '.[dev]'")

from PIL import Image  # noqa: E402
from playwright.sync_api import expect  # noqa: E402

from motiongrammetry.workspace import Workspace  # noqa: E402
from scan_fixture import KEYFRAMES, SCAN  # noqa: E402
from test_navigation import camera, drag, moved, open_preview  # noqa: E402
from ui import keyboard_focus  # noqa: E402

N = KEYFRAMES


def btn(page, which):
    return page.locator(f"#player [data-play={which}]")


def at(page, i):
    """Frame i (0-based) is the one selected and the position says so; its camera is highlighted."""
    expect(page.locator("#filmPos")).to_have_text(f"{i + 1} / {N}")
    expect(page.locator("#strip .thumb.sel")).to_have_count(1)
    expect(page.locator("#strip .thumb").nth(i)).to_have_class("thumb sel")
    expect(page.locator("#view")).to_have_attribute("data-cam", str(i))  # box: cameras f00 … f11 in order


def test_the_buttons_step_select_highlight_and_fly(page):
    open_preview(page)
    expect(page.locator("#filmPos")).to_have_text(f"– / {N}")
    for b in ("start", "prev", "play", "next", "end"):
        expect(btn(page, b)).to_be_enabled()
        expect(btn(page, b).locator("svg.icon")).to_have_count(1)
        assert btn(page, b).get_attribute("aria-label") and btn(page, b).get_attribute("title")
    start = camera(page)
    btn(page, "next").click()  # before any selection: the first frame
    at(page, 0)
    a = camera(page)
    assert moved(start["pos"], a["pos"])
    btn(page, "next").click()
    at(page, 1)
    b = camera(page)
    assert moved(a["pos"], b["pos"])
    btn(page, "end").click()
    at(page, N - 1)
    btn(page, "next").click()  # stays on the last
    at(page, N - 1)
    btn(page, "prev").click()
    at(page, N - 2)
    btn(page, "start").click()
    at(page, 0)
    btn(page, "prev").click()  # stays on the first
    at(page, 0)
    # a click on a thumbnail is the same step
    page.locator("#strip .thumb").nth(5).click()
    at(page, 5)
    btn(page, "next").click()
    at(page, 6)
    expect(page.locator("#filmNote")).to_be_hidden()


def test_the_last_frame_is_scrolled_into_the_strip_without_scrolling_the_page(page):
    page.set_viewport_size({"width": 700, "height": 800})  # the strip is narrower than its 12 thumbnails
    open_preview(page)
    strip = page.locator("#strip")
    assert strip.evaluate("(s) => s.scrollWidth > s.clientWidth")
    y = page.evaluate("() => scrollY")
    btn(page, "end").click()
    at(page, N - 1)
    last = page.locator("#strip .thumb").nth(N - 1)
    inside = "([s, t]) => { const a = s.getBoundingClientRect(), b = t.getBoundingClientRect(); return b.left >= a.left - 1 && b.right <= a.right + 1; }"
    assert page.evaluate(inside, [strip.element_handle(), last.element_handle()])
    assert page.evaluate("() => scrollY") == y


def test_arrow_keys_home_and_end_step_on_the_3d_model_but_not_while_typing(page):
    open_preview(page)
    page.locator("#view canvas").first.click()  # the focus off the Preview tab, which takes the arrows itself
    page.keyboard.press("ArrowRight")  # before any selection: the first frame
    at(page, 0)
    page.keyboard.press("ArrowRight")
    at(page, 1)
    page.keyboard.press("ArrowLeft")
    at(page, 0)
    page.keyboard.press("End")
    at(page, N - 1)
    page.keyboard.press("Home")
    at(page, 0)
    page.keyboard.press("ArrowLeft")
    at(page, 0)
    # in a field, the keys are the field's
    page.locator("#ptSize").focus()
    page.keyboard.press("End")
    page.keyboard.press("ArrowLeft")
    at(page, 0)


def test_play_steps_through_and_stops_on_the_last_frame_then_restarts_from_the_first(page):
    open_preview(page)
    btn(page, "end").click()
    for _ in range(3):
        btn(page, "prev").click()
    at(page, N - 4)
    btn(page, "play").click()
    expect(btn(page, "play")).to_have_attribute("aria-label", "pause")
    expect(btn(page, "play").locator("svg.icon-player-pause")).to_have_count(1)
    at(page, N - 3)  # it moves on by itself, at 2 a second
    expect(page.locator("#filmPos")).to_have_text(f"{N} / {N}", timeout=10_000)
    expect(btn(page, "play")).to_have_attribute("aria-label", "play")  # stopped there: no loop
    at(page, N - 1)
    btn(page, "play").click()  # at the end: from the first again
    at(page, 0)
    expect(btn(page, "play")).to_have_attribute("aria-label", "pause")
    btn(page, "play").click()  # pause
    expect(btn(page, "play")).to_have_attribute("aria-label", "play")


def test_moving_the_view_a_key_or_another_tab_pauses_play(page):
    open_preview(page)
    btn(page, "play").click()
    at(page, 1)
    drag(page, "right")  # turning the view
    expect(btn(page, "play")).to_have_attribute("aria-label", "play")
    pos = page.locator("#filmPos").inner_text()
    btn(page, "play").click()
    expect(page.locator("#filmPos")).not_to_have_text(pos)
    page.keyboard.press("ArrowLeft")  # a player key
    expect(btn(page, "play")).to_have_attribute("aria-label", "play")
    btn(page, "play").click()
    expect(btn(page, "play")).to_have_attribute("aria-label", "pause")
    page.locator("#pvTabs [data-value=frames]").click()  # leaving the 3D model
    expect(page.locator("#pvFrames")).to_be_visible()
    expect(btn(page, "play")).to_have_attribute("aria-label", "play")


def test_a_frame_sfm_did_not_place_is_selected_with_a_note_and_the_view_stays(page, app):
    ws = Workspace(app.root / SCAN)
    Image.new("RGB", (320, 240), (90, 60, 40)).save(ws.frames / "f99.jpg")  # a keyframe with no camera
    open_preview(page)
    expect(page.locator("#filmPos")).to_have_text(f"– / {N + 1}")
    btn(page, "end").click()
    at_last = page.locator("#strip .thumb").nth(N)
    expect(at_last).to_have_class("thumb sel")
    expect(page.locator("#filmPos")).to_have_text(f"{N + 1} / {N + 1}")
    expect(page.locator("#filmNote")).to_be_visible()
    expect(page.locator("#filmNote")).to_contain_text("no camera for this frame")
    expect(page.locator("#view")).to_have_attribute("data-cam", "-1")
    still = camera(page)
    btn(page, "prev").click()  # back to a placed one: the note goes, the view flies
    expect(page.locator("#filmNote")).to_be_hidden()
    expect(page.locator("#view")).to_have_attribute("data-cam", str(N - 1))
    assert moved(still["pos"], camera(page)["pos"])


def test_frames_and_masks_keep_their_own_arrows_and_double_click_opens_a_frame(page):
    open_preview(page)
    page.locator("#strip .thumb").nth(3).dblclick()
    expect(page.locator("#pvFrames")).to_be_visible()
    expect(page.locator("#fvPos")).to_have_text(f"4 / {N}")
    page.keyboard.press("ArrowRight")
    expect(page.locator("#fvPos")).to_have_text(f"5 / {N}")
    page.keyboard.press("End")
    expect(page.locator("#fvPos")).to_have_text(f"{N} / {N}")
    page.keyboard.press("Home")
    expect(page.locator("#fvPos")).to_have_text(f"1 / {N}")
    expect(page.locator("#filmPos")).to_have_text(f"4 / {N}")  # the player's place is its own


def tab(page, value):
    return page.locator(f"#pvTabs [data-value=\"{value}\"]")


def test_after_dragging_the_strip_s_scrollbar_the_arrows_step_the_frames_not_the_tabs(page):
    page.set_viewport_size({"width": 700, "height": 800})  # the strip scrolls
    open_preview(page)
    keyboard_focus(page, tab(page, "3d"))  # a tab holding the focus, the arrows its own
    thumb = page.locator("#strip > .mgsb .mgsb-track.h .mgsb-thumb")
    expect(thumb).to_be_visible()
    t = thumb.bounding_box()
    page.mouse.move(t["x"] + t["width"] / 2, t["y"] + t["height"] / 2)
    page.mouse.down()
    page.mouse.move(t["x"] + t["width"] / 2 + 80, t["y"] + t["height"] / 2, steps=6)
    page.mouse.up()
    assert page.locator("#strip").evaluate("(s) => s.scrollLeft") > 0
    page.keyboard.press("ArrowRight")
    at(page, 0)
    expect(page.locator("#pv3d")).to_be_visible()
    expect(tab(page, "3d")).to_have_attribute("aria-selected", "true")


def test_after_clicking_the_3d_model_tab_the_arrows_step_the_frames(page):
    open_preview(page)
    tab(page, "3d").click()
    expect(tab(page, "3d")).to_be_focused()
    page.keyboard.press("ArrowRight")
    at(page, 0)
    page.keyboard.press("End")
    at(page, N - 1)
    expect(page.locator("#pv3d")).to_be_visible()


def test_a_tab_reached_with_the_keyboard_still_switches_with_the_arrows(page):
    open_preview(page)
    keyboard_focus(page, tab(page, "3d"))
    page.keyboard.press("ArrowRight")
    expect(tab(page, "frames")).to_have_attribute("aria-selected", "true")
    expect(page.locator("#pvFrames")).to_be_visible()
    expect(page.locator("#filmPos")).to_have_text(f"– / {N}")  # the player didn't move


def test_after_ticking_mask_overlay_the_arrows_step_the_frames_and_space_still_toggles_it(page):
    open_preview(page)
    box = page.locator("#showMasks")
    box.click()
    expect(box).to_be_checked()
    expect(box).to_be_focused()
    page.keyboard.press("ArrowRight")
    at(page, 0)
    page.keyboard.press("ArrowRight")
    at(page, 1)
    page.keyboard.press("Space")
    expect(box).not_to_be_checked()


def test_thumbnails_say_what_a_click_does_and_the_strip_has_no_hint_line(page):
    open_preview(page)
    expect(page.locator("#strip .thumb").first).to_have_attribute(
        "title", "f00.jpg · click: view from its camera · double-click: open it big")
    expect(page.locator('[data-hint="film"]')).to_have_count(0)
    expect(btn(page, "next")).to_have_attribute("title", "next keyframe (→)")
    expect(btn(page, "start")).to_have_attribute("title", "first keyframe (Home)")
