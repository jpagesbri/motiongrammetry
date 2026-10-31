"""Run stages with pre-flight checks, quality gates, full logs and clean failure handling.

Every stage, whether started by `mg <stage>`, `mg run` or the web UI, goes through
:func:`run_stage`, which

1. tees *all* process output (Python logging and native COLMAP/OpenMVS output) to
   ``work/logs/<time>-<stage>.log``;
2. runs the pre-flight checks and refuses to start on errors (unless ``force``);
3. runs the stage; known failures become a diagnostic with a hint, anything else is
   logged with its traceback and classified (out of memory, disk full, cancelled, ...);
4. runs the quality gates on the result (warnings fail the stage with ``strict``);
5. records diagnostics in the manifest and one line in ``logs/history.jsonl``;
6. returns an exit code from :mod:`diagnostics` so callers can stop the pipeline.
"""

from __future__ import annotations

import contextlib
import errno
import json
import logging
import os
import sys
import threading
import time
import traceback
from pathlib import Path

from . import checks
from .diagnostics import (EXIT_CANCELLED, EXIT_CRASH, EXIT_GATE, EXIT_OK, EXIT_PREFLIGHT, Diag, StageError,
                          error)
from .options import for_stage
from .workspace import STAGES, Workspace

log = logging.getLogger("motiongrammetry.pipeline")


# ---- stage bodies ---------------------------------------------------------------------------
def _frames(ws: Workspace, o: dict, video: str | None) -> dict:
    from .frames import extract_keyframes

    return extract_keyframes(Path(video), ws.frames, o["num"], o["sample_fps"], o["max_side"], o["blur_reject"])


def _mask(ws, o, video):
    from .mask import compute_masks

    return compute_masks(ws.frame_paths(), ws.masks, o["mask_model"], o["mask_threshold"], o["mask_dilate"])


def _sfm(ws, o, video):
    from .sfm import run_sfm

    return run_sfm(ws, o["mapper"], o["mask_sfm"], o["camera_model"], o["max_image_size"], o["sift_peak"],
                   o["overlap"], o["loop_stride"], o["threads"] or -1, o["max_features"], o["matcher"])


def _mvs(ws, o, video):
    from .mvs import densify_and_mesh

    mesh = densify_and_mesh(ws, o["resolution_level"], o["threads"], o["keep_depth"], o["idle_timeout"])
    dense = ws.mvs / "scene_dense.ply"
    return {"mesh": str(mesh), "mesh_faces": checks.ply_counts(mesh).get("face"),
            "dense_points": checks.ply_counts(dense).get("vertex") if dense.exists() else None,
            "resolution_level": o["resolution_level"]}


def _vggt(ws, o, video):
    from .feedforward import run_vggt

    info = run_vggt(ws, o["max_frames"], device=o["device"])
    info["mesh_faces"] = checks.ply_counts(Path(info["mesh"])).get("face")
    return info


def _clean(ws, o, video):
    from .clean import clean_workspace_mesh

    mesh = checks._raw_mesh(ws, o)
    return clean_workspace_mesh(ws, mesh, use_masks=not o["no_masks"], remove_ground=not o["keep_ground"],
                                up=o["up"], bg_tolerance=o["bg_tolerance"], ground_margin=o["ground_margin"],
                                target_faces=o["target_faces"], fill_holes=o["fill_holes"],
                                print_height=o["print_height"], print_base=o["print_base"],
                                print_opts={k: o[k] for k in PRINT_OPTS})


PRINT_OPTS = ("print_mode", "print_stand", "print_min_wall", "print_shell", "print_shell_side")


def _texture(ws, o, video):
    from .clean import export_glb, texture_source, workspace_alignment
    from .mvs import texture

    src, source = texture_source(ws)
    out = ws.mesh / ("custom" if source == "custom" else "")
    if source == "custom":  # the editor's mesh: its textured model goes next to it, pipeline files untouched
        log.info("texturing the custom (edited) mesh")
        for old in out.glob("textured*"):
            old.unlink()
    ply = texture(ws, src, out_name="custom/textured" if source == "custom" else "textured",
                  resolution_level=o["texture_resolution"], threads=o["threads"], idle_timeout_min=o["idle_timeout"])
    glb = export_glb(ply, out / "model.glb", workspace_alignment(ws, src, up=o["up"]))
    if source == "custom":
        info_path = out / "info.json"
        meta = json.loads(info_path.read_text()) if info_path.exists() else {}
        info_path.write_text(json.dumps({**meta, "textured_from": "custom"}))
    return {"ply": str(ply), "glb": str(glb), "source": source}


BODIES = {"frames": _frames, "mask": _mask, "sfm": _sfm, "mvs": _mvs, "vggt": _vggt, "clean": _clean,
          "texture": _texture}


# ---- output capture -------------------------------------------------------------------------
@contextlib.contextmanager
def tee_output(path: Path):
    """Duplicate everything written to fds 1/2 (Python and native libraries alike) into ``path``."""
    sys.stdout.flush()
    sys.stderr.flush()
    saved_out, saved_err = os.dup(1), os.dup(2)
    r, w = os.pipe()
    os.dup2(w, 1)
    os.dup2(w, 2)
    os.close(w)
    f = open(path, "ab", buffering=0)

    def pump() -> None:
        while chunk := os.read(r, 65536):
            f.write(chunk)
            try:
                os.write(saved_out, chunk)
            except OSError:  # parent went away (UI closed): keep logging to the file
                pass

    t = threading.Thread(target=pump, daemon=True)
    t.start()
    try:
        yield
    finally:
        sys.stdout.flush()
        sys.stderr.flush()
        os.dup2(saved_out, 1)  # drops the last write ends of the pipe -> pump sees EOF
        os.dup2(saved_err, 2)
        t.join(timeout=10)
        os.close(r)
        os.close(saved_out)
        os.close(saved_err)
        f.close()


# ---- the wrapper ----------------------------------------------------------------------------
def _classify(exc: BaseException) -> tuple[Diag, int]:
    if isinstance(exc, StageError):
        return exc.diag, exc.exit_code
    if isinstance(exc, KeyboardInterrupt):
        return error("cancelled", "stopped by user"), EXIT_CANCELLED
    if isinstance(exc, MemoryError):
        return error("out-of-memory", "ran out of memory",
                     "raise resolution_level, use fewer keyframes / max_frames, or the low-disk preset"), EXIT_CRASH
    if isinstance(exc, OSError) and exc.errno == errno.ENOSPC:
        return error("disk-full", f"disk full: {exc}", "free space or use another workspace root"), EXIT_CRASH
    if isinstance(exc, RuntimeError):
        return error("stage-failed", str(exc)), EXIT_CRASH
    return error("internal-error", f"{type(exc).__name__}: {exc}",
                 "unexpected error (likely a bug); the traceback is in the stage log"), EXIT_CRASH


def _cancel_signal(signum, _frame):
    raise KeyboardInterrupt(f"signal {signum}")


def install_signal_handlers() -> None:
    """Turn SIGTERM/SIGHUP into KeyboardInterrupt so stages unwind (and kill OpenMVS).

    pycolmap's glog installs a fatal-signal handler on import that dumps a stack trace
    and kills the process on SIGTERM, skipping all cleanup; importing it first and then
    installing ours keeps cancellation clean."""
    import signal
    import threading

    import pycolmap  # noqa: F401  (side effect: glog handlers, which we override below)

    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGTERM, signal.SIGHUP):
            signal.signal(sig, _cancel_signal)


def run_stage(ws: Workspace, stage: str, o: dict, video: str | None = None, capture: bool = True) -> int:
    if stage not in BODIES:
        raise ValueError(f"unknown stage {stage!r}")
    install_signal_handlers()
    log_path = ws.new_log_path(stage)
    started = time.time()
    status, rc, diags, result = "ok", EXIT_OK, [], None
    ctx = tee_output(log_path) if capture else contextlib.nullcontext()
    file_handler = None
    if not _logging_reaches_fds():
        # Embedded use (tests, other apps): logging doesn't go through fds 1/2, so the
        # tee would miss it; write it to the stage log directly.
        file_handler = logging.FileHandler(log_path)
        file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", "%H:%M:%S"))
        logging.getLogger().addHandler(file_handler)
        if logging.getLogger().level > logging.INFO or logging.getLogger().level == logging.NOTSET:
            logging.getLogger("motiongrammetry").setLevel(logging.INFO)
    with ctx:
        log.info("=== %s  (log: %s)", stage, log_path)
        relevant = {opt.name: o[opt.name] for opt in for_stage(stage)}
        log.info("options: %s", ", ".join(f"{k}={v}" for k, v in relevant.items()))
        try:
            diags = checks.preflight(stage, ws, o, video)
            _report(diags, "pre-flight")
            if any(d.level == "error" for d in diags):
                if not o.get("force"):
                    raise StageError(error("preflight-failed", f"{stage} did not start: pre-flight check failed",
                                           "fix the errors above, or pass force to try anyway"), EXIT_PREFLIGHT)
                log.warning("force: starting despite pre-flight errors")
            # From here outputs change: drop the completion stamp until the stage succeeds again.
            (ws.root / ".stamps" / stage).unlink(missing_ok=True)
            result = BODIES[stage](ws, o, video)
            ws.record(stage, result)
            gates = checks.gate(stage, ws, result, o)
            _report(gates, "quality")
            diags += gates
            if any(d.level == "error" for d in gates):
                status, rc = "gate-failed", EXIT_GATE
            elif o.get("strict") and any(d.level == "warning" for d in gates):
                status, rc = "gate-failed", EXIT_GATE
                log.error("strict: failing on quality warnings")
            else:
                status = "warning" if any(d.level == "warning" for d in gates) else "ok"
                ws.stamp(stage)
                if stage == "vggt":
                    ws.stamp("sfm", by="vggt")  # VGGT wrote the camera model too
        except BaseException as exc:  # noqa: BLE001 - every exit path must be recorded
            diag, rc = _classify(exc)
            status = {EXIT_CANCELLED: "cancelled", EXIT_PREFLIGHT: "preflight-failed",
                      EXIT_GATE: "gate-failed"}.get(rc, "failed")
            if diag.code != "preflight-failed":
                diags.append(diag)
            if rc == EXIT_CRASH:
                log.error("%s failed: %s\n%s", stage, diag.message, traceback.format_exc())
            if diag.hint:
                log.error("hint: %s", diag.hint)
        elapsed = time.time() - started
        log.log(logging.INFO if rc == EXIT_OK else logging.ERROR,
                "=== %s %s in %.0fs", stage, status.upper(), elapsed)
    if file_handler is not None:
        logging.getLogger().removeHandler(file_handler)
        file_handler.close()
    ws.record_diagnostics(stage, [d.as_dict() for d in diags])
    ws.append_history({
        "stage": stage, "status": status, "exit_code": rc, "started": started, "duration": round(elapsed, 1),
        "log": str(log_path.relative_to(ws.root)), "diagnostics": [d.as_dict() for d in diags],
        "options": {opt.name: o[opt.name] for opt in for_stage(stage)},
    })
    return rc


def _logging_reaches_fds() -> bool:
    for h in logging.getLogger().handlers:
        stream = getattr(h, "stream", None)
        try:
            if stream is not None and stream.fileno() in (1, 2):
                return True
        except (AttributeError, OSError, ValueError):
            continue
    return False


def _report(diags: list[Diag], kind: str) -> None:
    for d in diags:
        level = {"error": logging.ERROR, "warning": logging.WARNING}.get(d.level, logging.INFO)
        log.log(level, "%s %s: %s%s", kind, d.code, d.message, f" -- hint: {d.hint}" if d.hint else "")


def run_pipeline(ws: Workspace, stages: list[str], o: dict, video: str | None = None) -> int:
    stages = [s for s in STAGES if s in stages]
    for i, stage in enumerate(stages):
        rc = run_stage(ws, stage, o, video)
        if rc != EXIT_OK:
            for skipped in stages[i + 1:]:
                ws.append_history({"stage": skipped, "status": "skipped", "exit_code": None, "started": time.time(),
                                   "duration": 0, "log": None, "reason": f"{stage} did not succeed"})
            log.error("pipeline stopped at %s; skipped: %s", stage, ", ".join(stages[i + 1:]) or "nothing")
            return rc
    return EXIT_OK
