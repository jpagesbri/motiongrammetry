"""Structured problems reported by pre-flight checks, quality gates and failures.

A Diag carries a stable ``code`` (for the UI and tests), a human message and a
concrete hint, usually naming the option or preset that addresses it. The web UI shows a short
form first: a title (``SHORT``, per code) and one short line (``line``, given where the numbers
matter, else the code's default); the message and hint are its Details, and what the CLI and the
logs print.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

# Process exit codes of `mg <stage>` / `mg run`.
EXIT_OK = 0
EXIT_CRASH = 1        # unexpected error (bug, missing tool, external tool crashed)
EXIT_GATE = 2         # stage ran but its result failed a quality gate
EXIT_PREFLIGHT = 3    # stage refused to start (missing/stale inputs, no disk, ...)
EXIT_CANCELLED = 130  # interrupted (Ctrl-C / cancel in the UI)


# The short form of every code: (title, line). Title at most 5 words, line at most 12 (a test
# checks every code the checks can produce). A Diag may give its own line, with its numbers.
TITLE_WORDS, LINE_WORDS = 5, 12
SHORT = {
    # environment and inputs (pre-flight)
    "openmvs-missing": ("OpenMVS not installed", "Install OpenMVS, or Docker, to build the mesh."),
    "docker-unresponsive": ("Docker not answering", "Restart Docker, then run again."),
    "docker-down": ("Docker not running", "Start the Docker service, then run again."),
    "openmvs-image-missing": ("OpenMVS image missing", "Build the OpenMVS Docker image first."),
    "stale-input": ("Earlier step out of date", "Re-run the earlier step first."),
    "no-video": ("No video", "Add a video to this scan first."),
    "video-unreadable": ("Video can't be read", "Convert it to MP4 (H.264) and try again."),
    "short-video": ("Short video", "20 to 60 seconds of video works best."),
    "low-resolution": ("Low video resolution", "Film at 1080p or more for detail."),
    "num-exceeds-frames": ("Too many keyframes asked", "The video has fewer frames than that."),
    "disk-low": ("Low disk space", "Free some space before running this."),
    "no-frames": ("No keyframes yet", "Run Pick keyframes first."),
    "missing-dependency": ("Missing software", "A required package is not installed."),
    "low-memory": ("Low memory", "This step may run out of memory."),
    "few-frames": ("Few keyframes", "80 to 200 keyframes is typical."),
    "no-masks": ("No masks", "Run Find the subject first."),
    "no-sfm": ("No camera positions", "Run Place the cameras first."),
    "replaces-cameras": ("Would replace the cameras", "Try VGGT on a duplicate of the scan."),
    "slow": ("Very slow without GPU", "Use 32 frames or fewer, or a GPU."),
    "no-mesh": ("No raw mesh", "Build the raw mesh first."),
    "stale-masks": ("Masks out of date", "Re-run Find the subject before cleaning."),
    "masks-incomplete": ("Some masks missing", "Re-run Find the subject."),
    "no-clean-mesh": ("No clean mesh", "Run Isolate the subject first."),
    "no-mvs-scene": ("Can't texture this mesh", "The VGGT path has no scene to texture from."),
    "broken-faces": ("Broken faces left out", "Saving the mesh in the editor removes them."),
    "custom-mesh": ("Texturing your edited mesh", "The pipeline's own model stays as it is."),
    "custom-mesh-outdated": ("Edited mesh is outdated", "Texture uses the new clean mesh instead."),
    # results (quality gates)
    "too-few-frames": ("Too few keyframes", "Film for longer, or move more slowly."),
    "blurry-video": ("Blurry video", "Many viewpoints had only blurry frames."),
    "little-motion": ("Camera barely moved", "Walk around the subject for different viewpoints."),
    "subject-not-found": ("Subject not found", "The masks are almost empty."),
    "masks-empty": ("Subject missed in some frames", "Some masks are empty."),
    "mask-too-large": ("Masks include background", "The masks cover most of the frame."),
    "masks-unstable": ("Masks vary a lot", "The subject's outline changes between keyframes."),
    "poor-registration": ("Most cameras not placed", "Most keyframes got no position."),
    "partial-registration": ("Some cameras not placed", "Some keyframes got no position."),
    "high-reprojection": ("Camera positions approximate", "Motion blur or a moving subject?"),
    "sparse-model": ("Few 3D points", "Plain or shiny surfaces give few points."),
    "implausible-focal": ("Odd lens estimate", "The model may be distorted."),
    "stretched-model": ("Model may be stretched", "The lens estimate differs across and down."),
    "disconnected": ("Cameras in separate groups", "Only the largest group is used."),
    "camera-path-jump": ("Camera path broken", "Some views don't connect."),
    "cameras-far-away": ("Photos placed far away", "Some photos are far from the others."),
    "cameras-collapsed": ("Camera movement not found", "Many photos sit on top of each other."),
    "mesh-empty": ("Raw mesh almost empty", "Very few faces were built."),
    "dense-sparse": ("Few dense points", "Plain surfaces, or a low resolution?"),
    "clean-removed-everything": ("Clean removed everything", "The masks probably don't match the cameras."),
    "heavy-carving": ("Most of the mesh carved", "Over 90% removed: check the masks."),
    "no-support-plane": ("No table or floor found", "Up comes from the camera path instead."),
    "no-output": ("No textured model", "Texturing made no file: see the log."),
    # failures (the stages' tools, the pipeline, the server)
    "sfm-no-model": ("No camera model built", "Too little overlap or texture between keyframes."),
    "openmvs-hung": ("OpenMVS stopped responding", "It printed nothing for a long time."),
    "openmvs-degenerate-mesh": ("Mesh has broken geometry", "Re-run clean, or fix the mesh in the editor."),
    "openmvs-crashed": ("OpenMVS crashed", "Often a broken mesh: re-run clean."),
    "openmvs-aborted": ("OpenMVS aborted", "Often a broken mesh: re-run clean."),
    "openmvs-failed": ("OpenMVS failed", "The log says what went wrong."),
    "preflight-failed": ("Did not start", "Fix the problems found first."),
    "cancelled": ("Stopped", "You stopped this step."),
    "out-of-memory": ("Out of memory", "Use fewer keyframes or a lower resolution."),
    "disk-full": ("Disk full", "Free some space and run again."),
    "stage-failed": ("Step failed", "The log says what went wrong."),
    "internal-error": ("Unexpected error", "Likely a bug: the log has the details."),
    "check-crashed": ("Check failed", "The check itself crashed."),
}


@dataclass
class Diag:
    level: str      # "info" | "warning" | "error"
    code: str
    message: str
    hint: str = ""
    line: str = ""  # the short line, with this case's numbers (default: SHORT's)

    def as_dict(self) -> dict:
        title, line = SHORT.get(self.code, (self.code.replace("-", " ").capitalize(), ""))
        return {**asdict(self), "title": title, "line": self.line or line}

    def __str__(self) -> str:
        return f"[{self.level}] {self.code}: {self.message}" + (f"\n    hint: {self.hint}" if self.hint else "")


def info(code: str, message: str, hint: str = "", line: str = "") -> Diag:
    return Diag("info", code, message, hint, line)


def warn(code: str, message: str, hint: str = "", line: str = "") -> Diag:
    return Diag("warning", code, message, hint, line)


def error(code: str, message: str, hint: str = "", line: str = "") -> Diag:
    return Diag("error", code, message, hint, line)


def short_of(d: dict) -> dict:
    """A stored diagnostic (an older one may lack its short form) with its title and line."""
    title, line = SHORT.get(d.get("code", ""), ((d.get("code") or "problem").replace("-", " ").capitalize(), ""))
    return {**d, "title": d.get("title") or title, "line": d.get("line") or line}


class StageError(RuntimeError):
    """A failure we understand: carries a diagnostic instead of a bare traceback."""

    def __init__(self, diag: Diag, exit_code: int = EXIT_CRASH):
        super().__init__(diag.message)
        self.diag = diag
        self.exit_code = exit_code
