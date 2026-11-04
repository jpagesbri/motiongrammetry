"""What the browser tests do often: open a page and wait until it is ready, draw on the editor's model.

Everything waits on what the page shows (Playwright's auto-waiting `expect`), never on a fixed time.
"""

from __future__ import annotations

import re

from playwright.sync_api import Page, expect

from scan_fixture import SCAN

# Each page: its path with the scan in it, and its label in the top bar
PAGES = {"scan": ("/#{}", "Scan"), "edit": ("/edit#{}", "Edit"), "help": ("/help", "Help"), "about": ("/about", "About")}


def remember_scan(page: Page, name: str = SCAN) -> None:
    """Help and About show the scan last opened on Scan or Edit (browser storage): as if it was."""
    page.add_init_script(f"try {{ localStorage.setItem('mg.scan', {name!r}) }} catch {{}}")


def open_page(page: Page, which: str, scan: str = SCAN) -> None:
    """Open one of the four pages on `scan` and wait until it has loaded it."""
    path, label = PAGES[which]
    page.goto(path.format(scan))
    expect(page.locator("header.topbar nav a.on")).to_have_text(label)
    if which == "scan":
        scan_ready(page, scan)
    elif which == "edit":
        expect(page.locator("#topScanName")).to_have_text(scan)
        editor_ready(page)


def scan_ready(page: Page, scan: str = SCAN) -> None:
    """The Scan page has read the scan. For `box`: its run mode is set last, from what the scan has
    (keyframes: Manual); the page shows Assisted until then. (An empty scan opens in Assisted, the
    same as before it is read: only the chip tells.)"""
    expect(page.locator("#topScanName")).to_have_text(scan)
    if scan == SCAN:
        expect(page.locator("#modeTabs [aria-selected=true]")).to_have_text("Manual")


def editor_ready(page: Page) -> None:
    """The editor shows its model: the status line has its size (sculpt) or its texture (paint)."""
    expect(page.locator("#center")).to_be_hidden(timeout=30_000)
    expect(page.locator("#status")).to_have_text(re.compile(r"faces|texture \d"))


def frames_drawn(page: Page, n: int = 2) -> None:
    """Wait until the page has drawn `n` more frames: what it does on the next frame (requestAnimationFrame)
    is done. A condition, not a time: as fast as the browser draws."""
    page.evaluate("(n) => new Promise((done) => { const f = () => (--n ? requestAnimationFrame(f) : done()); requestAnimationFrame(f); })", n)


def undo_count(page: Page) -> int:
    return int(re.search(r"(\d+)(?:/\d+)? undo", page.locator("#status").inner_text()).group(1))


def faces(page: Page) -> int:
    return int(re.match(r"([\d,]+) faces", page.locator("#status").inner_text()).group(1).replace(",", ""))


def drag_on_view(page: Page, dx: float = 0.18, dy: float = 0.0, *, steps: int = 12, modifiers: str | None = None) -> None:
    """A stroke across the middle of the editor's view (the model is framed there): from the centre
    minus (dx, dy) to the centre plus (dx, dy), as fractions of the view's size."""
    box = page.locator("#view canvas").first.bounding_box()
    cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    x0, y0 = cx - dx * box["width"], cy - dy * box["height"]
    x1, y1 = cx + dx * box["width"], cy + dy * box["height"]
    if modifiers:
        page.keyboard.down(modifiers)
    page.mouse.move(x0, y0)
    page.mouse.down()
    page.mouse.move(x1, y1, steps=steps)
    page.mouse.up()
    if modifiers:
        page.keyboard.up(modifiers)


def click_on_view(page: Page, *, modifiers: list[str] | None = None, fx: float = 0.5, fy: float = 0.5) -> None:
    box = page.locator("#view canvas").first.bounding_box()
    page.mouse.move(box["x"] + fx * box["width"], box["y"] + fy * box["height"])
    for m in modifiers or []:
        page.keyboard.down(m)
    page.mouse.down()
    page.mouse.up()
    for m in modifiers or []:
        page.keyboard.up(m)
