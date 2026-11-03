"""Shared test setup."""

from pathlib import Path

import pytest

import motiongrammetry

_SRC = Path(__file__).resolve().parents[1] / "src"


def pytest_sessionstart(session):
    """The tests must import the code next to them, not another installed copy (an editable install
    of another checkout, or a wheel in site-packages): otherwise they pass or fail for code that
    isn't the code under review."""
    here = Path(motiongrammetry.__file__).resolve()
    if not here.is_relative_to(_SRC.resolve()):
        pytest.exit(f"the tests import motiongrammetry from {here}, not from this checkout ({_SRC}). "
                    "Install it editable from here: uv pip install -e '.[mask,dev]'", returncode=4)
