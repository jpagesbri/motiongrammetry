"""The editor's browser code that runs without a browser (static/sculpt-mesh.js), tested in Node."""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _node_ok() -> bool:  # module.registerHooks (tests/js/vendor-hooks.mjs) came in Node 22.15
    if not shutil.which("node"):
        return False
    v = subprocess.run(["node", "--version"], capture_output=True, text=True).stdout.strip().lstrip("v")
    return tuple(int(x) for x in v.split(".")[:2]) >= (22, 15)


@pytest.mark.skipif(not _node_ok(), reason="needs Node.js 22.15 or later")
def test_js_suites():
    r = subprocess.run(["node", "--import", "./tests/js/vendor-hooks.mjs", "--test", "tests/js/"],
                       cwd=ROOT, capture_output=True, text=True, timeout=600)
    assert r.returncode == 0, r.stdout[-4000:] + r.stderr[-2000:]
