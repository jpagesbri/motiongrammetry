"""The 3D preview's layers: the most advanced result shows by default, and a layer made before an
earlier step changed stays viewable, dimmed and marked with why, instead of being hidden (#26)."""

import os
import re
import time

import pytest

pytest.importorskip("playwright", reason="needs Playwright: uv pip install -e '.[dev]'")

from playwright.sync_api import expect  # noqa: E402

from motiongrammetry.workspace import Workspace  # noqa: E402
from scan_fixture import SCAN  # noqa: E402
from ui import open_page  # noqa: E402

WHY = "made before the masks changed: re-run from SfM"


def _open_preview(page):
    open_page(page, "scan")
    page.locator("#tabPreview").click()
    expect(page.locator("#panePreview")).to_be_visible()


def _new_masks_after_an_sfm_that_used_them(app) -> None:
    """mg mask again on a finished scan whose SfM matched inside the masks: every stage after the masks
    is outdated, although no file of theirs moved."""
    ws = Workspace(app.root / SCAN)
    ws.record("sfm", {**ws.load_manifest().get("sfm", {}), "masked_features": True})
    ws.stamp("mask")
    later = time.time() + 60
    os.utime(ws.root / ".stamps" / "mask", (later, later))


def test_an_up_to_date_scan_shows_its_textured_model_unmarked(page):
    _open_preview(page)
    layers = page.locator("#layers")
    expect(layers.locator("input[data-layer=textured]")).to_be_checked()
    expect(layers.locator("input[data-layer=clean]")).not_to_be_checked()
    expect(layers.locator("label.old")).to_have_count(0)
    expect(layers.locator("[data-outdated]")).to_have_count(0)
    expect(page.locator("#layersOutdated")).to_have_count(0)


def test_outdated_layers_stay_dimmed_and_marked_and_the_most_advanced_one_shows(page, app):
    _new_masks_after_an_sfm_that_used_them(app)
    _open_preview(page)
    layers = page.locator("#layers")
    # the textured model still shows, not hidden and not replaced by an earlier step
    expect(layers.locator("input[data-layer=textured]")).to_be_checked()
    expect(layers.locator("input[data-layer=textured]")).to_be_enabled()
    expect(layers.locator("input[data-layer=clean]")).not_to_be_checked()
    for k in ("cameras", "sparse", "clean", "textured"):
        row = layers.locator(f"label[data-row={k}]")
        expect(row).to_have_class(re.compile(r"\bold\b"))  # dimmed
        expect(row).to_have_attribute("title", f"Outdated: {WHY}")
        expect(row.locator(f"[data-outdated={k}] svg.icon-clock-exclamation")).to_be_visible()
        expect(row.locator("input")).to_be_enabled()  # still viewable
    expect(layers.locator("label[data-row=dense]")).to_have_class("na")  # not made: not marked
    expect(layers.locator("label[data-row=dense] [data-outdated]")).to_have_count(0)
    # the shown model is outdated: said without hovering, the reason in its tooltip
    note = page.locator("#layersOutdated")
    expect(note).to_be_visible()
    expect(note).to_have_text("Textured model outdated")
    expect(note).to_have_attribute("title", WHY)
    # the box keeps its fixed width with the marks and the warning line: nothing sticks out (#26)
    assert layers.evaluate("(b) => b.scrollWidth <= b.clientWidth && Math.round(b.getBoundingClientRect().width) === 220")
    # an outdated layer can still be turned on
    layers.locator("input[data-layer=clean]").check()
    expect(layers.locator("input[data-layer=clean]")).to_be_checked()
