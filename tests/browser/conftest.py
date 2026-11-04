"""Real-browser tests of the web UI's pages (Playwright, headless Chromium).

The app runs as ``mg ui`` itself, in a subprocess, on a free port of its own (never 8765 or 8766,
which can be real instances), over a temp folder holding the test scans (scan_fixture.py). Each
test starts from fresh copies of them, in a fresh browser context (empty browser storage).

Every test here is marked ``browser`` and skips, saying why, when Playwright or its browser is not
installed, so ``pytest`` still runs everywhere. With MG_BROWSER_TESTS=required (CI) they fail
instead of skipping. A test fails on any uncaught page error or console.error (see ``page`` below).

    .venv/bin/playwright install chromium-headless-shell     # once (CI: --with-deps)
    .venv/bin/python -m pytest -m browser                     # only these
    .venv/bin/python -m pytest -m browser --tracing retain-on-failure   # + a trace per failure
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import pytest

from scan_fixture import SCAN, build_scans

HERE = Path(__file__).resolve().parent
REQUIRED = os.environ.get("MG_BROWSER_TESTS") == "required"  # CI: a missing browser is a failure, not a skip
if REQUIRED:
    import playwright  # noqa: F401  (an ImportError here stops the run)
REAL_PORTS = {8765, 8766}  # the container's and the usual local instance's: never these
INSTALL = ".venv/bin/playwright install chromium-headless-shell"


def pytest_collection_modifyitems(items):
    for item in items:
        if Path(str(item.fspath)).resolve().is_relative_to(HERE):
            item.add_marker(pytest.mark.browser)


# ---------------------------------------------------------------- the app
@dataclass
class Server:
    url: str
    root: Path       # the scans it serves
    template: Path   # pristine copies, put back before each test

    def reset(self) -> None:
        for p in self.root.iterdir():
            shutil.rmtree(p)
        for p in sorted(self.template.iterdir(), key=lambda p: p.stat().st_mtime):  # the same order of "last changed"
            shutil.copytree(p, self.root / p.name, symlinks=True)

    def get(self, path: str) -> bytes:
        with urllib.request.urlopen(self.url + path, timeout=30) as r:
            return r.read()


def _free_port() -> int:
    while True:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        if port not in REAL_PORTS:
            return port


@pytest.fixture(scope="session")
def mg_server(tmp_path_factory):
    """`mg ui` over a temp root, for the whole session; checked to serve that root before use."""
    base = tmp_path_factory.mktemp("mg-ui")
    template, root = base / "template", base / "root"
    build_scans(template)
    root.mkdir()
    port = _free_port()
    log = open(base / "server.log", "w")
    proc = subprocess.Popen([sys.executable, "-m", "motiongrammetry.cli", "ui", "--root", str(root), "--port", str(port)],
                            stdout=log, stderr=subprocess.STDOUT, env={**os.environ, "PYTHONUNBUFFERED": "1"})
    server = Server(f"http://127.0.0.1:{port}", root, template)
    server.reset()
    try:
        deadline = time.monotonic() + 60
        while True:  # up when it answers; the start takes about a second (imports)
            if proc.poll() is not None:
                raise RuntimeError(f"mg ui exited ({proc.returncode}):\n{(base / 'server.log').read_text()}")
            try:
                server.get("/api/workspaces")
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.1)
        # the server on that port is ours, on the temp root, and not some other instance
        state = json.loads(server.get(f"/api/workspaces/{SCAN}/state"))
        assert Path(state["root"]) == (root / SCAN).resolve(), f"port {port} serves {state['root']}, not {root}"
        yield server
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()


@pytest.fixture()
def app(mg_server):
    """The server, with the test scans as built (`box` and `blank`): changes from the test before are gone."""
    mg_server.reset()
    return mg_server


# ---------------------------------------------------------------- the browser (pytest-playwright)
@pytest.fixture(scope="session")
def browser_type_launch_args(browser_type_launch_args):
    # WebGL (three.js) without a GPU: Chromium's software renderer
    args = ["--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist"]
    return {**browser_type_launch_args, "args": [*browser_type_launch_args.get("args", []), *args]}


@pytest.fixture(scope="session")
def browser(launch_browser):
    from playwright.sync_api import Error

    try:
        b = launch_browser()
    except Error as e:
        if not REQUIRED and ("Executable doesn't exist" in str(e) or "playwright install" in str(e)):
            pytest.skip(f"no browser for Playwright: install it once with `{INSTALL}`")
        raise
    yield b
    b.close()


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args, mg_server):
    return {**browser_context_args, "base_url": mg_server.url, "viewport": {"width": 1440, "height": 900},
            "accept_downloads": True}


@pytest.fixture()
def page(context, app, request):
    """A page in a fresh context, on fresh scans. The test fails if the page throws, or logs an error."""
    p = context.new_page()
    p.set_default_timeout(15_000)
    errors: list[str] = []
    p.on("pageerror", lambda e: errors.append(f"uncaught: {e}"))
    p.on("console", lambda m: m.type == "error" and errors.append(f"console.error: {m.text} at {m.location.get('url')}"))
    yield p
    allowed = [m.args[0] for m in request.node.iter_markers("console_error")]  # expected, said by the test
    left = [e for e in errors if not any(re.search(a, e) for a in allowed)]
    if left:
        pytest.fail("the page logged errors:\n  " + "\n  ".join(left), pytrace=False)


@pytest.fixture(autouse=True, scope="session")
def _expect_timeout():
    from playwright.sync_api import expect

    expect.set_options(timeout=15_000)
