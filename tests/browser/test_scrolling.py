"""Scrolling: the document never scrolls (the top bar stays), every scroll area has no overscroll bounce
and none of the browser's scrollbars, and the app's own bar (scrollbar.js) reaches the end of content
that grew."""

import pytest

pytest.importorskip("playwright", reason="needs Playwright: uv pip install -e '.[dev]'")

from playwright.sync_api import expect  # noqa: E402

from ui import editor_ready, remember_scan, scan_ready  # noqa: E402

VIEWS = {  # each page, and the views of Scan and Edit
    "scan-run": "/?tab=run#box", "scan-preview": "/?tab=preview#box", "edit-sculpt": "/edit#box",
    "edit-paint": "/edit?mode=paint#box", "help": "/help", "about": "/about",
}
# Down to a small laptop window. (Below about 660 px wide the top bar itself is wider than the window:
# the app is not made for phones.)
SIZES = [(1440, 900), (1280, 720), (1024, 640), (800, 480)]
# Known bug, found by these tests: under 1100 px wide the Scan page stacks its parts (index.html's
# @media (max-width: 1100px): .app gets height: auto), so the document scrolls and the top bar goes
# up with it. strict: once fixed, these pass and the mark has to go.
NARROW_SCAN = pytest.mark.xfail(strict=True, reason="Scan page under 1100 px wide: the stacked layout scrolls the document")
CASES = [pytest.param(view, w, h, id=f"{view}-{w}x{h}", marks=[NARROW_SCAN] if view.startswith("scan") and w <= 1100 else [])
         for view in VIEWS for w, h in SIZES]

# Every element the browser scrolls (overflow auto, scroll or overlay), with what matters here: its
# overscroll behaviour, and the room the browser's own scrollbar takes (0 when it is hidden). Text
# fields and data-scroll="native" keep the browser's bar on purpose (scrollbar.css).
SCROLL_AREAS = """() => [...document.querySelectorAll('*')].filter((el) => {
  if (el.matches('textarea, select, input') || el.closest('[data-scroll="native"]')) return false;
  const s = getComputedStyle(el);
  return el === document.documentElement || el === document.body || /auto|scroll|overlay/.test(s.overflowX + s.overflowY);
}).map((el) => {
  const s = getComputedStyle(el), px = (v) => parseFloat(v) || 0;
  const name = el.tagName.toLowerCase() + (el.id ? '#' + el.id : '') + [...el.classList].map((c) => '.' + c).join('');
  const root = el === document.documentElement || el === document.body;
  return { name, overscroll: [s.overscrollBehaviorX, s.overscrollBehaviorY], scrolls: el.scrollHeight > el.clientHeight || el.scrollWidth > el.clientWidth,
           gutter: root ? 0 : Math.max(el.offsetWidth - el.clientWidth - px(s.borderLeftWidth) - px(s.borderRightWidth),
                                       el.offsetHeight - el.clientHeight - px(s.borderTopWidth) - px(s.borderBottomWidth)) };
})"""


def open_view(page, view):
    remember_scan(page)
    page.goto(VIEWS[view])
    if view.startswith("scan"):
        scan_ready(page)
    elif view.startswith("edit"):
        editor_ready(page)
    else:
        expect(page.locator("#page")).to_be_visible()


def check_scroll_areas(page, where):
    bad = [a for a in page.evaluate(SCROLL_AREAS) if a["overscroll"] != ["none", "none"] or a["gutter"] > 0.5]
    assert not bad, f"{where}: scroll areas with a bounce or the browser's scrollbar: {bad}"


def check_document_still(page, where):
    doc = page.evaluate("""() => { const d = document.scrollingElement;
      return { x: scrollX, y: scrollY, over: [d.scrollWidth - d.clientWidth, d.scrollHeight - d.clientHeight],
               bar: document.querySelector('header.topbar').getBoundingClientRect().top } }""")
    assert doc["over"] == [0, 0], f"{where}: the document is bigger than the window by {doc['over']} px, so it can scroll"
    assert (doc["x"], doc["y"], doc["bar"]) == (0, 0, 0), f"{where}: the document scrolled: {doc}"


@pytest.mark.parametrize("view, width, height", CASES)
def test_the_document_never_scrolls_and_no_area_bounces_or_shows_the_browser_bar(page, view, width, height):
    page.set_viewport_size({"width": width, "height": height})
    open_view(page, view)
    where = f"{view} at {width}x{height}"
    check_document_still(page, where)
    check_scroll_areas(page, where)
    # wheel, keys and a programmatic scroll on the document: it stays where it is
    for y in (22, height - 20):  # over the top bar, and near the bottom
        page.mouse.move(width / 2, y)
        page.mouse.wheel(0, 2000)
        page.mouse.wheel(600, 0)
    page.locator("body").press("End")
    page.evaluate("scrollTo(1e5, 1e5)")
    check_document_still(page, where + ", after wheel, End and scrollTo")


@pytest.mark.parametrize("view", ["scan-run", "edit-sculpt", "help", "about"])
def test_windows_and_lists_have_no_bounce_or_browser_bar_either(page, view):
    page.set_viewport_size({"width": 1280, "height": 480})  # short: the lists and windows scroll
    open_view(page, view)
    page.locator("#topScan").click()
    expect(page.locator("#scanPanel")).to_be_visible()
    check_scroll_areas(page, f"{view}, scan chooser open")
    page.keyboard.press("Escape")
    page.locator("#topExport").click()
    expect(page.locator("#exItems .ex-item").first).to_be_visible()
    check_scroll_areas(page, f"{view}, Export window open")
    check_document_still(page, f"{view}, Export window open")


def drag_thumb_to_the_end(page, area, grow=None):
    """Drag the app's bar of `area` from its thumb to past the end of its track; `grow()` runs
    between pressing the thumb and moving it (content that grows during the drag)."""
    thumb = page.locator(f"{area} > .mgsb .mgsb-track.v .mgsb-thumb")
    expect(thumb).to_be_visible()
    t = thumb.bounding_box()
    track = page.locator(f"{area} > .mgsb .mgsb-track.v").bounding_box()
    page.mouse.move(t["x"] + t["width"] / 2, t["y"] + t["height"] / 2)
    page.mouse.down()
    if grow:
        grow()
    page.mouse.move(t["x"] + t["width"] / 2, track["y"] + track["height"] + 40, steps=8)
    page.mouse.up()
    return page.evaluate(f"(() => {{ const el = document.querySelector('{area}'); return el.scrollHeight - el.clientHeight - el.scrollTop; }})()")


def test_help_bar_reaches_the_end_of_text_that_grows_while_dragging(page):
    """Help's screenshots load after the page opens and push the end down, even during a drag."""
    page.set_viewport_size({"width": 1280, "height": 720})
    page.goto("/help")
    expect(page.locator("#page")).to_be_visible()
    grow = lambda: page.evaluate(  # noqa: E731
        "document.querySelector('#page').lastElementChild.insertAdjacentHTML('beforeend', '<div style=\"height:4000px\"></div>')")
    assert drag_thumb_to_the_end(page, "#page", grow) <= 1


def test_log_bar_reaches_the_last_line_after_the_log_grows(page):
    page.set_viewport_size({"width": 1280, "height": 720})
    page.goto("/?tab=run#box")
    scan_ready(page)
    page.locator("#drawerToggle").click()
    # lines come in as the page adds them (appendLog): before the drag, and during it
    add = "(w) => document.getElementById('log').insertAdjacentHTML('beforeend', Array.from({ length: 400 }, (_, i) => '\\n' + w + ' ' + i).join(''))"
    page.evaluate(add, "line")
    left = drag_thumb_to_the_end(page, "#log", lambda: page.evaluate(add, "more"))
    assert left <= 1
    expect(page.locator("#log")).to_contain_text("more 399")
