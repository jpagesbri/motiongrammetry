"""'re-run from here' on the All steps page ticks the stage and the ones that depend on it, as this
scan ran: after an SfM that used the masks (mask_sfm), re-running the masks re-runs SfM and MVS too."""

import pytest

pytest.importorskip("playwright", reason="needs Playwright: uv pip install -e '.[dev]'")

from playwright.sync_api import expect  # noqa: E402

from motiongrammetry.workspace import Workspace  # noqa: E402
from scan_fixture import SCAN  # noqa: E402
from ui import open_page  # noqa: E402


def _sfm_used_masks(app, used: bool) -> None:
    ws = Workspace(app.root / SCAN)
    ws.record("sfm", {**ws.load_manifest().get("sfm", {}), "masked_features": used})


def _rerun_masks(page) -> None:
    open_page(page, "scan")  # box opens in Manual, on All steps
    page.locator("#stages [data-rerun=mask]").click()


@pytest.mark.parametrize("used, ticked", [
    (True, "mask → sfm → mvs → clean → texture"),
    (False, "mask → clean → texture"),
])
def test_rerun_from_the_masks_follows_how_sfm_ran(page, app, used, ticked):
    _sfm_used_masks(app, used)
    _rerun_masks(page)
    expect(page.locator("#rerunInfo b").first).to_have_text(ticked)
