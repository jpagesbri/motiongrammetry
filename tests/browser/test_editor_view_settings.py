"""The editor's 3D view settings (#26), the same box as the Scan page's preview (static/viewsettings.js),
top left over the 3D view: in Sculpt, View options holds Highlight holes and the display spheres follow
(a click or W, the shown one ringed); in Paint, View options holds Show texture unlit and there are no
spheres. One fixed width, the preview's. Its own keys: mg.editDisplay, mg.editViewopts."""

import re

import pytest

pytest.importorskip("playwright", reason="needs Playwright: uv pip install -e '.[dev]'")

from playwright.sync_api import expect  # noqa: E402

from test_editor import to_paint  # noqa: E402
from test_navigation import open_preview  # noqa: E402
from ui import frames_drawn, open_page  # noqa: E402


def sphere(page, d):
    return page.locator(f"#viewBox .sph[data-display={d}]")


def ringed(page, d):
    expect(page.locator("#viewBox .sph.on")).to_have_count(1)
    expect(sphere(page, d)).to_have_class(re.compile(r"\bon\b"))
    expect(sphere(page, d)).to_have_attribute("aria-pressed", "true")
    assert page.evaluate("() => localStorage.getItem('mg.editDisplay')") == d


def shot(page):
    frames_drawn(page)
    return page.locator("#view > canvas").screenshot()


def test_the_spheres_change_the_editors_display_without_touching_the_mesh(page):
    open_page(page, "edit")
    expect(page.locator("#dispMode")).to_have_count(0)  # the side panel's drop-down is gone
    expect(page.locator("#sculptOpts #holesChk")).to_have_count(0)
    box = page.locator("#viewBox")
    expect(box).to_be_visible()
    expect(box.locator(".sph")).to_have_count(4)
    for d, name in {"colours": "Colours", "solid": "Solid grey", "wire": "Wireframe", "solidwire": "Solid + wires"}.items():
        expect(sphere(page, d)).to_have_attribute("title", f"{name} (W)")
    expect(box.locator("#orientBtn")).to_have_count(0)  # no orientation in the editor
    expect(sphere(page, "colours")).to_have_class(re.compile(r"\bon\b"))
    before = shot(page)
    sphere(page, "wire").click()
    ringed(page, "wire")
    assert shot(page) != before  # the model is drawn otherwise
    expect(page.locator("#status")).to_contain_text("0 undo")  # a click on the box is not a stroke
    assert page.evaluate("() => localStorage.getItem('mg.display')") is None  # the preview's own choice is apart
    page.reload()
    expect(page.locator("#center")).to_be_hidden(timeout=30_000)
    expect(sphere(page, "wire")).to_have_class(re.compile(r"\bon\b"))


def test_w_cycles_the_editors_display_and_the_ring_follows(page):
    open_page(page, "edit")
    sphere(page, "colours").click()
    for d in ("solid", "wire", "solidwire", "colours"):
        page.keyboard.press("w")
        ringed(page, d)


def test_highlight_holes_from_view_options_with_its_count_and_the_folded_note(page):
    open_page(page, "edit")
    tog = page.locator("#viewBox #viewOptsTog")
    expect(tog).to_have_attribute("aria-expanded", "false")
    tog.click()
    page.locator("#holesChk").check()
    expect(page.locator("#holesInfo")).to_have_text(re.compile(r"^(\d+ openings? · \d+ edges|closed, no holes)$"))
    tog.click()
    expect(page.locator("#viewOptsNote")).to_have_text("holes")
    assert page.evaluate("() => localStorage.getItem('mg.editViewopts')") == "0"
    tog.click()
    page.locator("#holesChk").uncheck()
    expect(page.locator("#holesInfo")).to_be_hidden()
    page.reload()
    expect(page.locator("#center")).to_be_hidden(timeout=30_000)
    expect(page.locator("#viewBox #viewOptsTog")).to_have_attribute("aria-expanded", "true")  # remembered


def test_the_editors_box_has_the_previews_fixed_width_open_or_folded(page):
    open_page(page, "edit")
    width = lambda: page.locator("#viewBox").evaluate("(b) => b.getBoundingClientRect().width")  # noqa: E731
    folded = width()
    page.locator("#viewOptsTog").click()
    page.locator("#holesChk").check()
    expect(page.locator("#holesInfo")).not_to_be_empty()
    assert abs(width() - folded) <= 1, (folded, width())
    open_preview(page)
    assert abs(page.locator("#layers").evaluate("(b) => b.getBoundingClientRect().width") - folded) <= 1  # the same as the preview's


def test_paint_shows_texture_unlit_in_view_options_and_no_spheres(page):
    open_page(page, "edit")
    to_paint(page)
    box = page.locator("#viewBox")
    expect(box).to_be_visible()
    expect(box.locator("#dispRow")).to_be_hidden()
    page.locator("#viewOptsTog").click()
    expect(page.locator("#holesChk")).to_be_hidden()
    unlit = page.locator("#viewBox #unlit")
    expect(unlit).to_be_visible()
    expect(unlit).to_be_checked()
    before = shot(page)
    unlit.uncheck()  # lit: the view's light and shade on the texture
    assert shot(page) != before
    kept = page.evaluate("() => localStorage.getItem('mg.editDisplay')")
    page.keyboard.press("w")  # W is Sculpt's
    assert page.evaluate("() => localStorage.getItem('mg.editDisplay')") == kept
    page.locator('#layoutTabs [data-value="uv"]').click()  # the flat texture only: no 3D view, no box
    expect(box).to_be_hidden()
    page.locator('#layoutTabs [data-value="split"]').click()
    expect(box).to_be_visible()
    page.locator("#modeTabs [data-value=sculpt]").click()
    expect(box.locator("#dispRow")).to_be_visible()
    expect(page.locator("#holesChk")).to_be_visible()
    expect(page.locator("#unlit")).to_be_hidden()
