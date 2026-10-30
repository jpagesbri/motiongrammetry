"""Structured problems reported by pre-flight checks, quality gates and failures.

A Diag carries a stable ``code`` (for the UI and tests), a human message and a
concrete hint, usually naming the option or preset that addresses it.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

# Process exit codes of `mg <stage>` / `mg run`.
EXIT_OK = 0
EXIT_CRASH = 1        # unexpected error (bug, missing tool, external tool crashed)
EXIT_GATE = 2         # stage ran but its result failed a quality gate
EXIT_PREFLIGHT = 3    # stage refused to start (missing/stale inputs, no disk, ...)
EXIT_CANCELLED = 130  # interrupted (Ctrl-C / cancel in the UI)


@dataclass
class Diag:
    level: str      # "info" | "warning" | "error"
    code: str
    message: str
    hint: str = ""

    def as_dict(self) -> dict:
        return asdict(self)

    def __str__(self) -> str:
        return f"[{self.level}] {self.code}: {self.message}" + (f"\n    hint: {self.hint}" if self.hint else "")


def info(code: str, message: str, hint: str = "") -> Diag:
    return Diag("info", code, message, hint)


def warn(code: str, message: str, hint: str = "") -> Diag:
    return Diag("warning", code, message, hint)


def error(code: str, message: str, hint: str = "") -> Diag:
    return Diag("error", code, message, hint)


class StageError(RuntimeError):
    """A failure we understand: carries a diagnostic instead of a bare traceback."""

    def __init__(self, diag: Diag, exit_code: int = EXIT_CRASH):
        super().__init__(diag.message)
        self.diag = diag
        self.exit_code = exit_code
