"""The top bar on every page: the page links, the scan chip and its chooser, Export… and the ? help."""

import json
import re

import pytest

pytest.importorskip("playwright", reason="needs Playwright: uv pip install -e '.[dev]'")

from playwright.sync_api import expect  # noqa: E402

from scan_fixture import EMPTY, SCAN  # noqa: E402
from ui import PAGES, editor_ready, open_page, remember_scan, scan_ready  # noqa: E402

LINKS = ["Scan", "Edit", "Help", "About"]


@pytest.mark.parametrize("which", list(PAGES))
def test_page_links_name_the_active_page_and_carry_the_scan(page, which):
    remember_scan(page)
    open_page(page, which)
    links = page.locator("header.topbar nav.screens a")
    expect(links).to_have_text(LINKS)
    active = page.locator("header.topbar nav a.on")
    expect(active).to_have_count(1)
    expect(active).to_have_text(PAGES[which][1])
    expect(active).to_have_attribute("aria-current", "page")
    expect(active).not_to_have_attribute("href", re.compile(".*"))  # the page you are on is not a link
    # the other pages are links; Scan and Edit open the scan shown in the chip
    expect(page.locator("#topScanName")).to_have_text(SCAN)
    hrefs = {a.inner_text(): a.get_attribute("href") for a in links.all() if a.get_attribute("href")}
    assert set(hrefs) == set(LINKS) - {PAGES[which][1]}
    assert hrefs.get("Scan", f"/#{SCAN}") == f"/#{SCAN}" and hrefs.get("Edit", f"/edit#{SCAN}") == f"/edit#{SCAN}"
    # Scan and Edit hold work open: Help and About open from them in a new tab
    for label in {"Help", "About"} - {PAGES[which][1]}:
        if which in ("scan", "edit"):
            expect(links.filter(has_text=label)).to_have_attribute("target", "_blank")
        else:
            expect(links.filter(has_text=label)).not_to_have_attribute("target", re.compile(".*"))
    # following a link opens that page
    target = {"scan": "Edit", "edit": "Scan", "help": "About", "about": "Help"}[which]
    links.filter(has_text=target).click()
    expect(page.locator("header.topbar nav a.on")).to_have_text(target)
    expect(page.locator("#topScanName")).to_have_text(SCAN)


def test_chooser_opens_picks_with_the_keyboard_and_closes(page):
    open_page(page, "scan")
    chip = page.locator("#topScan")
    expect(chip).to_have_attribute("aria-expanded", "false")
    chip.click()
    panel = page.locator("#scanPanel")
    expect(panel).to_be_visible()
    expect(chip).to_have_attribute("aria-expanded", "true")
    rows = panel.locator(".sc-pick")
    expect(rows).to_have_count(2)
    expect(panel.locator(".sc-row.on .sc-pick")).to_have_attribute("data-name", SCAN)
    expect(panel.locator(f'.sc-pick[data-name="{SCAN}"]')).to_be_focused()   # the open scan
    expect(panel.locator(f'.sc-pick[data-name="{SCAN}"]')).to_contain_text("12 keyframes")
    expect(panel.locator(f'.sc-pick[data-name="{EMPTY}"]')).to_contain_text("no video yet")
    expect(panel.locator(".sc-new")).to_have_text("+ New scan…")
    # Esc closes it, and so does a click on the page
    page.keyboard.press("Escape")
    expect(panel).to_be_hidden()
    expect(chip).to_be_focused()
    chip.click()
    expect(panel).to_be_visible()
    page.locator("#work").click(position={"x": 400, "y": 300})
    expect(panel).to_be_hidden()
    # ↓ from the chip opens it; ↓ Enter picks the next scan
    chip.focus()
    page.keyboard.press("ArrowDown")
    expect(panel).to_be_visible()
    expect(panel.locator(f'.sc-pick[data-name="{SCAN}"]')).to_be_focused()
    page.keyboard.press("ArrowDown")
    expect(panel.locator(f'.sc-pick[data-name="{EMPTY}"]')).to_be_focused()
    page.keyboard.press("Enter")
    expect(panel).to_be_hidden()
    expect(page.locator("#topScanName")).to_have_text(EMPTY)
    expect(page).to_have_url(re.compile(f"/#{EMPTY}$"))
    # the ⋯ menu: → opens it with its first item focused, Esc closes it, then the list
    chip.click()
    expect(panel.locator(f'.sc-pick[data-name="{EMPTY}"]')).to_be_focused()
    page.keyboard.press("ArrowRight")
    menu = panel.locator(".sc-menu")
    expect(menu).to_be_visible()
    expect(menu.get_by_role("menuitem")).to_have_text(["Upload a video…", "Export…", "Duplicate…", "Delete…"])
    expect(menu.get_by_role("menuitem").first).to_be_focused()
    page.keyboard.press("Escape")
    expect(menu).to_have_count(0)
    expect(panel).to_be_visible()
    page.keyboard.press("Escape")
    expect(panel).to_be_hidden()


def test_new_scan_then_delete_it(page, app):
    open_page(page, "scan")
    page.locator("#topScan").click()
    page.locator("#scanPanel .sc-new").click()
    win = page.locator("#scanModal .sc-win")
    expect(win).to_be_visible()
    expect(page.locator("#scNewName")).to_be_focused()
    page.keyboard.type("try-1")
    page.keyboard.press("Enter")
    expect(win).to_be_hidden()
    expect(page.locator("#topScanName")).to_have_text("try-1")
    expect(page).to_have_url(re.compile("/#try-1$"))
    assert (app.root / "try-1").is_dir()
    # its ⋯ menu: nothing to duplicate yet
    page.locator("#topScan").click()
    page.locator('#scanPanel .sc-more[data-more="try-1"]').click()
    expect(page.locator("#scanPanel .sc-menu").get_by_role("menuitem", name="Duplicate…")).to_be_disabled()
    page.locator("#scanPanel .sc-menu").get_by_role("menuitem", name="Delete…").click()
    expect(win).to_be_visible()
    expect(win).to_contain_text(str(app.root / "try-1"))  # its folder, measured
    go = page.locator("#scDelGo")
    expect(go).to_be_disabled()
    page.locator("#scDelConfirm").fill("try")
    expect(go).to_be_disabled()
    page.locator("#scDelConfirm").fill("try-1")
    expect(go).to_be_enabled()
    go.click()
    expect(win).to_be_hidden()
    # the open scan was deleted: the page moves on to another one, and the list no longer has it
    expect(page.locator("#topScanName")).not_to_have_text("try-1")
    assert not (app.root / "try-1").exists()
    assert "try-1" not in [w["name"] for w in json.loads(app.get("/api/workspaces"))]
    page.locator("#topScan").click()
    expect(page.locator("#scanPanel .sc-pick")).to_have_count(2)


@pytest.mark.parametrize("which", ["help", "about"])
def test_export_is_disabled_until_a_scan_is_chosen(page, which):
    open_page(page, which)  # a fresh browser: no scan opened yet
    expect(page.locator("#topScanName")).to_have_text("choose…")
    expect(page.locator("#topExport")).to_be_disabled()
    # Help and About show the scan last opened on Scan and Edit, as the links do
    page.locator("#topScan").click()
    page.locator(f'#scanPanel .sc-pick[data-name="{SCAN}"]').click()
    expect(page.locator("#topScanName")).to_have_text(SCAN)
    expect(page.locator("#topExport")).to_be_enabled()
    expect(page.locator("header.topbar nav a", has_text="Edit")).to_have_attribute("href", f"/edit#{SCAN}")
    page.locator("#topExport").click()
    expect(page.locator("#exportModal")).to_be_visible()
    expect(page.locator("#exScan")).to_have_text(f"· {SCAN}")


@pytest.mark.parametrize("which, url, title, views", [
    ("scan", "/#box", "Help · Scan", ["Run · Manual"]),  # box has keyframes: Manual is its mode
    ("scan", "/?tab=preview#box", "Help · Scan", ["Preview · 3D model"]),
    ("edit", "/edit#box", "Help · Edit", ["Sculpt mesh"]),
    ("edit", "/edit?mode=paint#box", "Help · Edit", ["Paint texture"]),
    ("about", "/about", "Help · About", []),
])
def test_help_window_is_about_the_view_shown_and_esc_closes_it(page, which, url, title, views):
    page.goto(url)
    if which == "edit":
        editor_ready(page)
    elif which == "scan":
        scan_ready(page)
    button = page.locator("#topHelp")
    expect(button).to_be_visible()
    expect(button).to_have_attribute("title", "help on this page (?)")
    button.click()
    win = page.locator("#helpModal")
    expect(win).to_be_visible()
    expect(page.locator("#phTitle")).to_have_text(title)
    expect(win.locator(".ph-view h3")).to_have_text(views)
    expect(win.locator(".ph-more").first).to_have_attribute("href", re.compile(r"^/help#\w+"))
    if which == "edit":
        expect(win.locator(".ph-view")).to_have_attribute("data-view", "edit-paint" if "paint" in url else "edit-sculpt")
    page.keyboard.press("Escape")
    expect(win).to_be_hidden()
    expect(button).to_be_focused()  # back where it was opened from
    # the ? key opens it too (not from a text field)
    page.locator("body").press("?")
    expect(win).to_be_visible()
    page.keyboard.press("Escape")
    expect(win).to_be_hidden()


def test_help_page_has_no_help_button(page):
    open_page(page, "help")
    expect(page.locator("#topHelp")).to_have_count(0)
    page.keyboard.press("?")
    expect(page.locator("#helpModal")).to_have_count(0)


def test_help_window_keeps_the_page_keys(page):
    """While it is open, the editor's tool keys don't reach the editor."""
    open_page(page, "edit")
    expect(page.locator('#tools [data-tool="draw"]')).to_have_class(re.compile(r"\bon\b"))
    page.keyboard.press("?")
    expect(page.locator("#helpModal")).to_be_visible()
    page.keyboard.press("f")  # Flatten's key
    expect(page.locator('#tools [data-tool="draw"]')).to_have_class(re.compile(r"\bon\b"))
    page.keyboard.press("Escape")
    expect(page.locator("#helpModal")).to_be_hidden()
    page.keyboard.press("f")
    expect(page.locator('#tools [data-tool="flatten"]')).to_have_class(re.compile(r"\bon\b"))
