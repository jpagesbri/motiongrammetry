"""The one control for tabs and modes (modes.js): clicks, arrow keys, and the choice kept after a reload."""

import re

import pytest

pytest.importorskip("playwright", reason="needs Playwright: uv pip install -e '.[dev]'")

from playwright.sync_api import expect  # noqa: E402

from ui import editor_ready, keyboard_focus, open_page, scan_ready  # noqa: E402

SELECTED = "[aria-selected=true]"


def selected(page, control):
    return page.locator(f"#{control} {SELECTED}")


def test_run_and_preview_switch_by_click_and_keys_and_stay_after_a_reload(page):
    open_page(page, "scan")
    expect(selected(page, "gTabs")).to_have_text("Run")
    expect(page.locator("#paneRun")).to_be_visible()
    page.locator("#tabPreview").click()
    expect(selected(page, "gTabs")).to_have_text("Preview")
    expect(page.locator("#panePreview")).to_be_visible()
    expect(page.locator("#paneRun")).to_be_hidden()
    page.reload()
    scan_ready(page)
    expect(selected(page, "gTabs")).to_have_text("Preview")
    # keys: ← → on the focused tab; one Tab stop per control (roving tabindex)
    keyboard_focus(page, page.locator("#tabPreview"))
    page.keyboard.press("ArrowLeft")
    expect(selected(page, "gTabs")).to_have_text("Run")
    expect(page.locator("#tabRun")).to_be_focused()
    expect(page.locator("#tabRun")).to_have_attribute("tabindex", "0")
    expect(page.locator("#tabPreview")).to_have_attribute("tabindex", "-1")
    page.keyboard.press("ArrowRight")
    expect(selected(page, "gTabs")).to_have_text("Preview")
    page.keyboard.press("Home")
    expect(selected(page, "gTabs")).to_have_text("Run")
    page.reload()
    scan_ready(page)
    expect(selected(page, "gTabs")).to_have_text("Run")


def test_assisted_and_manual_are_kept_per_scan(page):
    open_page(page, "scan")
    expect(selected(page, "modeTabs")).to_have_text("Manual")  # box has keyframes: Manual by default
    expect(page.locator("#runBtn")).to_be_visible()
    page.locator("#modeTabs [data-value=assisted]").click()
    expect(selected(page, "modeTabs")).to_have_text("Assisted")
    expect(page.locator("#runBtn")).to_be_hidden()
    assert page.evaluate("localStorage.getItem('mg.mode.box')") == "assisted"
    keyboard_focus(page, page.locator("#modeTabs [data-value=assisted]"))
    page.keyboard.press("ArrowRight")
    expect(selected(page, "modeTabs")).to_have_text("Manual")
    page.keyboard.press("ArrowRight")  # wraps around
    expect(selected(page, "modeTabs")).to_have_text("Assisted")
    # the other scan keeps its own: blank opens in Assisted (nothing run yet); Manual chosen there
    # is what it shows after a reload (the page shows Assisted until it has read the scan, so only
    # Manual tells that the choice came back)
    page.locator("#topScan").click()
    page.locator('#scanPanel .sc-pick[data-name="blank"]').click()
    expect(page.locator("#topScanName")).to_have_text("blank")
    page.locator("#modeTabs [data-value=manual]").click()
    expect(selected(page, "modeTabs")).to_have_text("Manual")
    page.reload()
    expect(page.locator("#topScanName")).to_have_text("blank")
    expect(selected(page, "modeTabs")).to_have_text("Manual")
    assert page.evaluate("localStorage.getItem('mg.mode.box')") == "assisted"


def test_frames_keys_do_not_fire_while_a_tab_is_focused(page):
    """On Frames & masks, ← → Home End step through the keyframes; on a focused tab they switch it instead."""
    page.goto("/?tab=preview#box")
    scan_ready(page)
    page.locator("#pvTabs [data-value=frames]").click()
    expect(selected(page, "pvTabs")).to_have_text("Frames & masks")
    pos = page.locator("#fvPos")
    expect(pos).to_have_text("1 / 12")
    page.locator("#fvCanvas").click()  # the focus off the tabs: the keys step through the keyframes
    page.keyboard.press("ArrowRight")
    expect(pos).to_have_text("2 / 12")
    page.keyboard.press("End")
    expect(pos).to_have_text("12 / 12")
    page.keyboard.press("Home")
    expect(pos).to_have_text("1 / 12")
    # on the focused tab: End stays on Frames & masks (its last option), the keyframe doesn't move
    keyboard_focus(page, page.locator("#pvTabs [data-value=frames]"))
    page.keyboard.press("End")
    expect(selected(page, "pvTabs")).to_have_text("Frames & masks")
    expect(pos).to_have_text("1 / 12")
    page.keyboard.press("ArrowLeft")
    expect(selected(page, "pvTabs")).to_have_text("3D model")
    expect(page.locator("#pvFrames")).to_be_hidden()
    page.keyboard.press("ArrowRight")
    expect(selected(page, "pvTabs")).to_have_text("Frames & masks")
    expect(pos).to_have_text("1 / 12")
    page.reload()
    scan_ready(page)
    expect(selected(page, "pvTabs")).to_have_text("Frames & masks")


def test_log_live_and_history(page):
    open_page(page, "scan")
    page.locator("#drawerToggle").click()
    expect(selected(page, "logTabs")).to_have_text("Live")
    keyboard_focus(page, page.locator("#logTabs [data-value=live]"))
    page.keyboard.press("ArrowRight")
    expect(selected(page, "logTabs")).to_have_text("History")
    expect(page.locator("#history")).to_be_visible()
    page.keyboard.press("ArrowLeft")
    expect(selected(page, "logTabs")).to_have_text("Live")


def test_sculpt_and_paint_switch_and_the_address_keeps_paint(page):
    open_page(page, "edit")
    expect(selected(page, "modeTabs")).to_have_text("Sculpt mesh")
    expect(page.locator("#meshOps")).to_be_visible()
    page.locator("#modeTabs [data-value=paint]").click()
    expect(selected(page, "modeTabs")).to_have_text("Paint texture")
    expect(page).to_have_url(re.compile(r"/edit\?mode=paint#box$"))
    editor_ready(page)
    expect(page.locator("#status")).to_contain_text("texture 0: 1024×1024")
    expect(page.locator("#layoutOps")).to_be_visible()
    page.reload()
    editor_ready(page)
    expect(selected(page, "modeTabs")).to_have_text("Paint texture")
    # the view's layout control, in Paint: 3D / 3D + texture / Texture
    expect(selected(page, "layoutTabs")).to_have_text("3D + texture")
    keyboard_focus(page, page.locator("#layoutTabs [data-value=split]"))
    page.keyboard.press("ArrowRight")
    expect(selected(page, "layoutTabs")).to_have_text("Texture")
    # keys on the mode tabs: ← back to Sculpt, and the address drops ?mode
    keyboard_focus(page, page.locator("#modeTabs [data-value=paint]"))
    page.keyboard.press("ArrowLeft")
    expect(selected(page, "modeTabs")).to_have_text("Sculpt mesh")
    expect(page).to_have_url(re.compile(r"/edit#box$"))
    editor_ready(page)
    expect(page.locator("#meshOps")).to_be_visible()
    page.reload()
    editor_ready(page)
    expect(selected(page, "modeTabs")).to_have_text("Sculpt mesh")


def test_a_sculpt_edit_is_kept_across_a_switch_to_paint(page):
    open_page(page, "edit")
    from ui import drag_on_view, undo_count

    drag_on_view(page)
    expect(page.locator("#status")).to_contain_text("1 undo")
    page.locator("#modeTabs [data-value=paint]").click()
    editor_ready(page)
    page.locator("#modeTabs [data-value=sculpt]").click()
    expect(page.locator("#status")).to_contain_text("1 undo")
    expect(page.locator("#topStatus")).to_have_text("unsaved sculpt edits")
    assert undo_count(page) == 1
