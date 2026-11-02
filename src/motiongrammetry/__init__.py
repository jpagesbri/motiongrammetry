"""motiongrammetry: video -> keyframes -> masks -> camera poses -> dense mesh -> cleaned mesh."""

import re as _re
import subprocess as _sp
from importlib import metadata as _md
from pathlib import Path as _Path

try:  # the version lives in pyproject.toml only
    __version__ = _md.version("motiongrammetry")
except _md.PackageNotFoundError:
    __version__ = "dev"


def _branch_suffix(root: _Path | None = None, version: str = __version__) -> str:
    """Run from a git checkout (an editable install), the version says which branch the code is
    from: 0.2.0-fix-some-bug. Nothing on the release's own commit (tag v<version>), and nothing for
    an installed copy such as the Docker image, which has no checkout."""
    root = root or _Path(__file__).resolve().parents[2]
    if not (root / ".git").exists() or not (root / "pyproject.toml").exists():
        return ""

    def git(*args: str) -> str:
        return _sp.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=5).stdout.strip()

    try:
        if f"v{version}" in git("tag", "--points-at", "HEAD").split():
            return ""
        branch = git("rev-parse", "--abbrev-ref", "HEAD")
        if branch == "HEAD":  # detached: the commit
            branch = git("rev-parse", "--short", "HEAD")
    except (OSError, _sp.SubprocessError):
        return ""
    return "-" + _re.sub(r"[^A-Za-z0-9._]+", "-", branch).strip("-") if branch else ""


__version__ += _branch_suffix()
