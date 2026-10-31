"""motiongrammetry: video -> keyframes -> masks -> camera poses -> dense mesh -> cleaned mesh."""

from importlib import metadata as _md

try:  # the version lives in pyproject.toml only
    __version__ = _md.version("motiongrammetry")
except _md.PackageNotFoundError:
    __version__ = "dev"
