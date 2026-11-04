"""The Export window (export.js): on every page, a real download, the eye of a ready file, a scan with
nothing to export, and the note about unsaved edits on Edit."""

import re
import struct

import pytest

pytest.importorskip("playwright", reason="needs Playwright: uv pip install -e '.[dev]'")

from playwright.sync_api import expect  # noqa: E402

from scan_fixture import EMPTY, SCAN  # noqa: E402
from ui import PAGES, drag_on_view, editor_ready, open_page, remember_scan, scan_ready  # noqa: E402


def only(page, content, fmt):
    """Tick only `content`, in format `fmt` (once the window has read the scan and listed its contents)."""
    expect(page.locator("#exItems .ex-item").first).to_be_visible()
    for box in page.locator("#exItems input[type=checkbox]:not([disabled])").all():
        box.set_checked(box.get_attribute("id") == f"exOn-{content}")
    page.locator(f"#exFmt-{content}").select_option(fmt)
    expect(page.locator("#exItems input[type=checkbox]:checked")).to_have_count(1)


@pytest.mark.parametrize("which", list(PAGES))
def test_export_window_opens_from_the_top_bar_on_every_page(page, which):
    remember_scan(page)
    open_page(page, which)
    page.locator("#topExport").click()
    win = page.locator("#exportModal")
    expect(win).to_be_visible()
    expect(page.locator("#exScan")).to_have_text(f"· {SCAN}")
    # box's contents: the textured model, the clean mesh, a 3D print and the cameras
    expect(page.locator("#exItems .ex-item:not(.off) label")).to_contain_text(["Textured model", "Clean mesh", "3D print", "Cameras"])
    expect(page.locator("#exDir")).to_contain_text(f"/{SCAN}/mesh/")
    page.keyboard.press("Escape")
    expect(win).to_be_hidden()
    expect(page.locator("#topExport")).to_be_focused()


@pytest.mark.parametrize("which", ["scan", "about"])
def test_export_window_opens_from_a_scans_menu(page, which):
    remember_scan(page)
    open_page(page, which)
    page.locator("#topScan").click()
    page.locator(f'#scanPanel .sc-more[data-more="{SCAN}"]').click()
    page.locator("#scanPanel .sc-menu").get_by_role("menuitem", name="Export…").click()
    expect(page.locator("#exportModal")).to_be_visible()
    expect(page.locator("#exItems .ex-item").first).to_be_visible()
    page.keyboard.press("Escape")
    expect(page.locator("#exportModal")).to_be_hidden()


def test_glb_export_downloads_a_valid_file_and_the_eye_shows_it_again_without_exporting(page, tmp_path):
    open_page(page, "scan")
    page.locator("#topExport").click()
    only(page, "mesh", "glb")
    expect(page.locator("#exBuild")).to_have_text("Export")
    page.locator("#exBuild").click()
    download_link = page.locator("#exDownload")
    expect(download_link).to_have_text(f"Download {SCAN}-mesh.glb", timeout=60_000)
    expect(page.locator("#exShown")).to_contain_text("made just now")
    expect(page.locator("#exPreview canvas[data-engine]")).to_be_visible()  # shown in 3D (three.js)
    with page.expect_download() as dl:
        download_link.click()
    path = tmp_path / dl.value.suggested_filename
    dl.value.save_as(path)
    assert path.name == f"{SCAN}-mesh.glb"
    data = path.read_bytes()
    magic, version, length = struct.unpack_from("<4sII", data)
    assert (magic, version, length) == (b"glTF", 2, len(data))
    import trimesh

    mesh = trimesh.load(path, force="mesh")
    assert len(mesh.faces) == 6912

    # closed and opened again: the row is ready, with an eye
    page.keyboard.press("Escape")
    page.locator("#topExport").click()
    expect(page.locator("#exOn-mesh")).to_be_checked()
    expect(page.locator("#exSt-mesh")).to_have_text(re.compile(r"^ready · "))
    eye = page.locator("#exEye-mesh .ex-eye")
    expect(eye).to_have_attribute("title", "preview this file")
    exports = []
    page.on("request", lambda r: r.method == "POST" and r.url.endswith("/export") and exports.append(r.url))
    eye.click()
    expect(page.locator("#exShown")).to_have_text(re.compile(rf"Clean mesh {SCAN}-mesh\.glb .*made earlier, shown as it is"))
    expect(page.locator("#exOn-mesh")).to_be_checked()  # the eye's click doesn't untick the row
    assert exports == []  # shown as it is: not made again
    # a different height makes it outdated: no eye
    page.locator("#exHeight").fill("50")
    page.locator("#exHeight").dispatch_event("change")
    expect(page.locator("#exSt-mesh")).to_have_text("outdated: made again")
    expect(eye).to_have_count(0)


def test_a_scan_with_nothing_to_export_says_so_and_goes_to_run(page):
    page.goto("/?tab=preview#box")
    scan_ready(page)
    page.locator("#topScan").click()
    page.locator(f'#scanPanel .sc-more[data-more="{EMPTY}"]').click()
    page.locator("#scanPanel .sc-menu").get_by_role("menuitem", name="Export…").click()
    none = page.locator("#exNone")
    expect(none).to_contain_text(f"{EMPTY} has nothing to export yet.")
    expect(page.locator("#exBody")).to_be_hidden()
    page.locator("#exToRun").click()
    expect(page.locator("#exportModal")).to_be_hidden()
    expect(page.locator("#topScanName")).to_have_text(EMPTY)
    expect(page.locator("#gTabs [aria-selected=true]")).to_have_text("Run")


def test_edit_says_unsaved_edits_are_left_out_and_keeps_its_keys_out(page):
    open_page(page, "edit")
    page.locator("#topExport").click()
    expect(page.locator("#exItems .ex-item").first).to_be_visible()
    expect(page.locator("#exUnsaved")).to_be_hidden()  # nothing changed yet
    page.keyboard.press("Escape")
    drag_on_view(page)
    expect(page.locator("#topStatus")).to_have_text("unsaved sculpt edits")
    page.locator("#topExport").click()
    note = page.locator("#exUnsaved")
    expect(note).to_be_visible()
    expect(note).to_contain_text("You have unsaved sculpt edits.")
    expect(note).to_contain_text("Export uses the saved files only")
    # the editor gets no keys while the window is open
    page.keyboard.press("e")          # Decimate's key
    page.keyboard.press("Control+s")  # save
    page.keyboard.press("Escape")
    expect(page.locator("#exportModal")).to_be_hidden()
    expect(page.locator('#tools [data-tool="draw"]')).to_have_class(re.compile(r"\bon\b"))
    expect(page.locator("#topStatus")).to_have_text("unsaved sculpt edits")
    editor_ready(page)


def test_closing_the_window_opened_from_a_menu_gives_the_page_its_keys_back(page):
    """Opened from a scan's ⋯ menu (whose button is gone once it is picked), then closed with Esc: the
    editor's keys work again at once, without a click."""
    open_page(page, "edit")
    page.locator("#topScan").click()
    page.locator(f'#scanPanel .sc-more[data-more="{SCAN}"]').click()
    page.locator("#scanPanel .sc-menu").get_by_role("menuitem", name="Export…").click()
    expect(page.locator("#exItems .ex-item").first).to_be_visible()
    page.keyboard.press("Escape")
    expect(page.locator("#exportModal")).to_be_hidden()
    page.keyboard.press("f")  # Flatten
    expect(page.locator('#tools [data-tool="flatten"]')).to_have_class(re.compile(r"\bon\b"))
