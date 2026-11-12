"""The 3D preview's Layers box (#26, #27): the display spheres at its bottom (click one or W, the shown
one ringed), the Orientation & origin icon button next to them, and View options, a line that folds
and opens on Point size (only while a point cloud shows) and Highlight holes (whose "on" still shows
folded)."""

import re

import pytest

pytest.importorskip("playwright", reason="needs Playwright: uv pip install -e '.[dev]'")

from playwright.sync_api import expect  # noqa: E402

from test_navigation import open_preview  # noqa: E402

NAMES = {"textured": "Colours", "solid": "Solid grey", "wire": "Wireframe", "solidwire": "Solid + wires"}
ORIENT_TITLE = "Orientation & origin: set which way is up and where the origin is, for every export and for printing"


def sphere(page, d):
    return page.locator(f"#dispRow .sph[data-display={d}]")


def shown(page, d):
    """`d` is the one ringed (and pressed), alone, and kept for the next visit."""
    expect(page.locator("#dispRow .sph.on")).to_have_count(1)
    expect(sphere(page, d)).to_have_class(re.compile(r"\bon\b"))
    expect(sphere(page, d)).to_have_attribute("aria-pressed", "true")
    assert page.evaluate("() => localStorage.getItem('mg.display')") == d


def toggle(page):
    return page.locator("#viewOptsTog")


def test_the_spheres_are_drawn_named_and_a_click_shows_that_display(page):
    open_preview(page)
    expect(page.locator("#dispRow .sph")).to_have_count(4)
    expect(page.locator("#dispMode")).to_have_count(0)  # the old drop-down is gone
    expect(page.locator("#dispBox")).to_have_count(0)   # and its box: the row is the Layers box's last
    expect(page.locator("#layers #dispRow")).to_be_visible()
    for d, name in NAMES.items():
        expect(sphere(page, d)).to_have_attribute("title", f"{name} (W)")
        expect(sphere(page, d)).to_have_attribute("aria-label", name)
        assert sphere(page, d).locator("img").get_attribute("src").startswith("data:image/png")  # drawn
    expect(sphere(page, "textured")).to_have_class(re.compile(r"\bon\b"))  # the default
    sphere(page, "solid").click()
    shown(page, "solid")
    expect(sphere(page, "textured")).to_have_attribute("aria-pressed", "false")
    sphere(page, "wire").click()
    shown(page, "wire")
    page.reload()
    expect(page.locator("#loading")).to_be_hidden(timeout=30_000)
    expect(sphere(page, "wire")).to_have_class(re.compile(r"\bon\b"))  # mg.display still works


def test_w_cycles_the_displays_and_the_ring_follows(page):
    open_preview(page)
    page.locator("#view > canvas").click(position={"x": 400, "y": 300})
    for d in ("solid", "wire", "solidwire", "textured"):
        page.keyboard.press("w")
        shown(page, d)
    sphere(page, "solidwire").click()  # W from a clicked sphere (a button lets the key through)
    page.keyboard.press("w")
    shown(page, "textured")


def test_view_options_fold_open_and_remember_it(page):
    open_preview(page)
    expect(toggle(page)).to_have_text(re.compile("View options"))
    expect(toggle(page)).to_have_attribute("aria-expanded", "false")  # folded by default
    expect(page.locator("#viewOptsBody")).to_be_hidden()
    toggle(page).click()
    expect(toggle(page)).to_have_attribute("aria-expanded", "true")
    expect(page.locator("#holesChk")).to_be_visible()
    page.reload()
    expect(page.locator("#loading")).to_be_hidden(timeout=30_000)
    expect(toggle(page)).to_have_attribute("aria-expanded", "true")
    expect(page.locator("#holesChk")).to_be_visible()
    toggle(page).click()
    expect(page.locator("#viewOptsBody")).to_be_hidden()
    page.reload()
    expect(page.locator("#loading")).to_be_hidden(timeout=30_000)
    expect(toggle(page)).to_have_attribute("aria-expanded", "false")
    expect(page.locator("#viewOptsBody")).to_be_hidden()


def test_point_size_shows_only_with_a_point_cloud_and_takes_the_arrows(page):
    open_preview(page)
    toggle(page).click()
    sparse = page.locator("#layers input[data-layer=sparse]")
    expect(sparse).to_be_checked()  # the box scan has sparse points, on by default
    expect(page.locator("#ptRow")).to_be_visible()
    sparse.uncheck()
    expect(page.locator("#ptRow")).to_be_hidden()
    sparse.check()
    expect(page.locator("#ptRow")).to_be_visible()
    expect(page.locator("#ptVal")).to_have_text("1.0×")
    page.locator("#ptSize").focus()
    for _ in range(3):
        page.keyboard.press("ArrowRight")
    expect(page.locator("#ptVal")).to_have_text("1.3×")
    page.keyboard.press("ArrowLeft")
    expect(page.locator("#ptVal")).to_have_text("1.2×")
    expect(page.locator("#filmPos")).to_have_text(re.compile(r"^– /"))  # the keys were the slider's


def test_highlight_holes_from_view_options_with_its_count_and_its_on_state_while_folded(page):
    open_preview(page)
    expect(page.locator("#holesOn")).to_be_hidden()
    toggle(page).click()
    expect(page.locator("#holesInfo")).to_be_hidden()  # empty while off
    page.locator("#holesChk").check()
    expect(page.locator("#holesInfo")).to_have_text(re.compile(r"textured mesh: (\d+ opening|closed)"))
    expect(page.locator("#holesOn")).to_be_hidden()  # open: the checkbox says it
    toggle(page).click()
    expect(page.locator("#holesOn")).to_be_visible()  # folded: the line says it
    expect(page.locator("#holesOn")).to_have_attribute("title", "Highlight holes is on")
    toggle(page).click()
    page.locator("#holesChk").uncheck()
    expect(page.locator("#holesInfo")).to_be_hidden()
    toggle(page).click()
    expect(page.locator("#holesOn")).to_be_hidden()


def test_the_orientation_icon_button_names_itself_and_opens_the_orientation(page):
    open_preview(page)
    b = page.locator("#layers #dispRow #orientBtn")
    expect(b).to_have_attribute("title", ORIENT_TITLE)
    expect(b).to_have_attribute("aria-label", "Orientation and origin")
    expect(b.locator("svg.icon-rotate-3d")).to_have_count(1)
    expect(b).to_have_text("")  # icon only
    b.click()
    expect(page.locator("#pvSide")).to_be_visible()
    expect(page.locator("#orientPanel")).to_be_visible()
    expect(b).to_have_class(re.compile(r"\bprimary\b"))
