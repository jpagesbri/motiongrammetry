"""`mg` command line: run the whole pipeline or any single stage on a workspace folder."""

from __future__ import annotations

import argparse
import importlib.util
import logging
import os
import shutil
import sys
import time
from pathlib import Path

from .workspace import Workspace

log = logging.getLogger("motiongrametry")


def cmd_doctor(_: argparse.Namespace) -> int:
    from . import mvs

    def has(mod: str) -> bool:
        return importlib.util.find_spec(mod) is not None

    rows = [("ffmpeg (optional)", shutil.which("ffmpeg") is not None, "")]
    import pycolmap

    rows.append((f"pycolmap {pycolmap.__version__}", True,
                 "CUDA" if getattr(pycolmap, "has_cuda", False) else "CPU only (SIFT + GLOMAP are fine on CPU)"))
    backend = mvs.available()
    rows.append(("OpenMVS", backend is not None, backend or
                 f"docker build -f docker/openmvs.Dockerfile -t {mvs.DOCKER_IMAGE} docker/"))
    rows.append(("rembg (masks)", has("rembg"), "" if has("rembg") else "uv pip install 'motiongrametry[mask]'"))
    cuda = False
    if has("torch"):
        import torch

        cuda = torch.cuda.is_available()
        rows.append((f"torch {torch.__version__}", True, "CUDA" if cuda else "CPU"))
    else:
        rows.append(("torch (VGGT)", False, "uv pip install torch torchvision"))
    vggt_deps = [m for m in ("huggingface_hub", "einops", "safetensors") if not has(m)]
    from .checks import vggt_env

    env = vggt_env()
    if env and "error" not in env:  # the Docker image's separate VGGT environment
        rows.append((f"vggt (in {env['python'].rsplit('/bin', 1)[0]})", True, "CUDA" if env["cuda"] else "CPU"))
        cuda = cuda or env["cuda"]
        vggt_deps = []
    rows.append(("vggt", (has("vggt") or bool(env and "error" not in env)) and not vggt_deps,
                 "uv pip install --no-deps git+https://github.com/facebookresearch/vggt" if not has("vggt") else
                 f"uv pip install {' '.join(vggt_deps)}" if vggt_deps else ""))
    for name, ok, note in rows:
        print(f"  {'ok ' if ok else '-- '} {name:<28} {note}")
    from .checks import accel

    acc = accel()
    print(f"\nGPU: {acc['gpu'] or 'none usable'}")
    for step, (dev, note) in acc["steps"].items():
        print(f"  {step:<8} {dev:<8} {note}")
    print("\nAvailable paths:")
    print(f"  mvs   (SfM + OpenMVS, textured): {'yes' if backend else 'no'}")
    vggt_ok = has("vggt") or bool(env and "error" not in env)
    print(f"  vggt  (feed-forward + TSDF):     {'yes' if vggt_ok else 'no'}{'' if cuda else ' (CPU: <=24 frames, slow)'}")
    print(f"  2dgs  (Gaussian surfels):        {'run here' if cuda else 'export with `mg export-2dgs`, run on a CUDA host'}")
    return 0


def _options(a: argparse.Namespace) -> dict:
    import json

    from .options import OPTIONS, resolve

    explicit = {o.name: getattr(a, o.name, None) for o in OPTIONS}
    if getattr(a, "options_json", None):  # used by the web UI
        explicit.update(json.loads(a.options_json))
    return resolve(explicit, getattr(a, "preset", None))


def cmd_stage(a: argparse.Namespace) -> int:
    from .pipeline import run_stage

    ws = Workspace(a.work)
    return run_stage(ws, a.cmd, _options(a), getattr(a, "video", None))


def cmd_check(a: argparse.Namespace) -> int:
    from . import checks
    from .diagnostics import EXIT_OK, EXIT_PREFLIGHT
    from .workspace import STAGES

    ws = Workspace(a.work)
    o = _options(a)
    worst = EXIT_OK
    for stage in [s for s in STAGES if s in (a.stages or STAGES)]:
        diags = checks.preflight(stage, ws, o, a.video)
        mark = "FAIL" if any(d.level == "error" for d in diags) else "warn" if diags else " ok "
        print(f"[{mark}] {stage}")
        for d in diags:
            print(f"        {d.level}: {d.message}" + (f"\n          -> {d.hint}" if d.hint else ""))
        if mark == "FAIL":
            worst = EXIT_PREFLIGHT
    stale = ws.stale_stages()
    if stale:
        print(f"\nstale outputs (older than their inputs): {', '.join(stale)}")
    return worst


def cmd_logs(a: argparse.Namespace) -> int:
    ws = Workspace(a.work)
    hist = ws.history()
    if a.stage:
        hist = [h for h in hist if h["stage"] == a.stage]
    if a.list or not hist:
        for h in hist[-(a.n or 30):]:
            when = time.strftime("%m-%d %H:%M:%S", time.localtime(h["started"]))
            ndiag = len(h.get("diagnostics") or [])
            print(f"{when}  {h['stage']:<8} {h['status']:<17} {h.get('duration', 0):>7.0f}s  "
                  f"{ndiag} diag  {h.get('log') or h.get('reason', '')}")
        if not hist:
            print("no runs recorded yet")
        return 0
    last = next((h for h in reversed(hist) if h.get("log")), None)
    if last is None:
        print("no log files recorded")
        return 1
    lines = (ws.root / last["log"]).read_text(errors="replace").splitlines()
    if not a.all:
        lines = [ln for ln in lines if not _is_glog(ln)]
    print(f"# {last['stage']} {last['status']} -- {ws.root / last['log']}")
    print("\n".join(lines[-(a.n or 80):]))
    for d in last.get("diagnostics") or []:
        print(f"! {d['level']} {d['code']}: {d['message']}" + (f"\n    -> {d['hint']}" if d.get("hint") else ""))
    return 0


def _is_glog(line: str) -> bool:
    return len(line) > 9 and line[0] in "IW" and line[1:9].isdigit()


def cmd_notices(args: argparse.Namespace) -> int:
    from .credits import notices

    text = notices()
    if args.output:
        Path(args.output).write_text(text)
        print(f"wrote {args.output} ({len(text) // 1024} kB)")
    else:
        sys.stdout.write(text)
    return 0


def cmd_presets(_: argparse.Namespace) -> int:
    from .presets import PRESETS

    for name, p in PRESETS.items():
        opts = ", ".join(f"{k}={v}" for k, v in p["options"].items())
        print(f"{name}\n    {p['description']}\n    sets: {opts}\n")
    return 0


def cmd_export_2dgs(a: argparse.Namespace) -> int:
    from .splat import export_2dgs

    ws = Workspace(a.work)
    out = export_2dgs(ws, Path(a.out) if a.out else None, a.masked)
    print(f"2DGS dataset -> {out}\n  run: {out / 'run_2dgs.sh'} on a CUDA machine, then\n"
          f"  mg clean {ws.root} --mesh <out/train/ours_30000/fuse_post.ply>")
    return 0


def cmd_ui(a: argparse.Namespace) -> int:
    from .web.server import serve

    serve(Path(a.root), a.host, a.port)
    return 0


def cmd_run(a: argparse.Namespace) -> int:
    from .pipeline import run_pipeline

    ws = Workspace(a.work)
    if a.stages:
        stages = a.stages
    else:
        stages = ["frames"] + ([] if a.no_mask else ["mask"])
        stages += ["sfm", "mvs", "clean"] + ([] if a.no_texture else ["texture"]) if a.method == "mvs" \
            else ["vggt", "clean"]
    rc = run_pipeline(ws, stages, _options(a), a.video)
    if rc == 0:
        print(f"\ndone. outputs in {ws.mesh}")
    else:
        print(f"\nfailed (exit {rc}). details: mg logs {ws.root}", file=sys.stderr)
    return rc


def _add_options(sp: argparse.ArgumentParser, stages: list[str]) -> None:
    from .options import OPTIONS
    from .presets import PRESETS

    seen = set()
    for o in OPTIONS:
        if o.name in seen or not any(st in o.stages or "*" in o.stages for st in stages):
            continue
        seen.add(o.name)
        flag = "--" + o.name.replace("_", "-")
        if o.type is bool:
            sp.add_argument(flag, action="store_true", default=None, help=o.help)
        else:
            sp.add_argument(flag, type=o.type, default=None, help=f"{o.help} (default {o.default})",
                            choices=list(o.choices) if o.choices else None)
    sp.add_argument("--preset", choices=list(PRESETS), help="option set for a known capture problem (mg presets)")
    sp.add_argument("--options-json", help=argparse.SUPPRESS)


def build_parser() -> argparse.ArgumentParser:
    from .workspace import STAGES

    p = argparse.ArgumentParser(prog="mg", description="Video -> 3D mesh (motiongrametry). "
                                "Exit codes: 0 ok, 1 failed, 2 quality gate failed, 3 pre-flight failed, 130 cancelled.")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("doctor", help="check installed backends").set_defaults(fn=cmd_doctor)
    sub.add_parser("presets", help="list option presets for common capture problems").set_defaults(fn=cmd_presets)
    sp = sub.add_parser("notices", help="third-party licenses and notices (THIRD_PARTY_NOTICES) for redistribution")
    sp.add_argument("-o", "--output", help="write to this file instead of stdout")
    sp.set_defaults(fn=cmd_notices)

    helps = {"frames": "select sharp, evenly spaced keyframes from a video",
             "mask": "segment the subject in every keyframe",
             "sfm": "camera poses with COLMAP/GLOMAP",
             "mvs": "dense reconstruction + meshing with OpenMVS",
             "vggt": "feed-forward cameras + depth (VGGT) fused into a mesh",
             "clean": "carve background with masks, remove the desk, floaters; export GLB",
             "texture": "texture the cleaned mesh with OpenMVS"}
    for stage in STAGES:
        sp = sub.add_parser(stage, help=helps[stage])
        if stage == "frames":
            sp.add_argument("video")
        sp.add_argument("work", help="workspace folder")
        _add_options(sp, [stage])
        sp.set_defaults(fn=cmd_stage)

    sp = sub.add_parser("run", help="full pipeline with checks; stops at the first failing stage")
    sp.add_argument("video")
    sp.add_argument("work")
    sp.add_argument("--method", choices=["mvs", "vggt"], default="mvs")
    sp.add_argument("--no-mask", action="store_true", help="skip segmentation (no background carving)")
    sp.add_argument("--no-texture", action="store_true")
    sp.add_argument("--stages", nargs="+", choices=STAGES, help="run exactly these stages")
    _add_options(sp, STAGES)
    sp.set_defaults(fn=cmd_run)

    sp = sub.add_parser("check", help="pre-flight checks only: what would fail, and why")
    sp.add_argument("work")
    sp.add_argument("--video")
    sp.add_argument("--stages", nargs="+", choices=STAGES)
    _add_options(sp, STAGES)
    sp.set_defaults(fn=cmd_check)

    sp = sub.add_parser("logs", help="show the last stage log, or the run history")
    sp.add_argument("work")
    sp.add_argument("--stage", choices=STAGES)
    sp.add_argument("--list", action="store_true", help="run history instead of a log")
    sp.add_argument("--all", action="store_true", help="include COLMAP internals")
    sp.add_argument("-n", type=int, help="number of lines / entries")
    sp.set_defaults(fn=cmd_logs)

    sp = sub.add_parser("export-2dgs", help="package a 2D Gaussian Splatting dataset for a GPU host")
    sp.add_argument("work")
    sp.add_argument("--out")
    sp.add_argument("--masked", action="store_true", help="train on the subject only (masks in alpha)")
    sp.set_defaults(fn=cmd_export_2dgs)

    sp = sub.add_parser("ui", help="local web UI: run stages, follow progress, preview results in 3D")
    sp.add_argument("--root", default="workspaces", help="folder holding one sub-folder per workspace")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8765)
    sp.set_defaults(fn=cmd_ui)
    return p


def main(argv: list[str] | None = None) -> int:
    import signal

    args = build_parser().parse_args(argv)
    sys.stdout.reconfigure(line_buffering=True)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S")
    from .pipeline import _cancel_signal

    for sig in (signal.SIGTERM, signal.SIGHUP):  # re-installed after native imports in run_stage
        signal.signal(sig, _cancel_signal)
    try:
        return args.fn(args)
    except KeyboardInterrupt:
        log.error("cancelled")
        return 130
    except BrokenPipeError:  # output piped into `head` etc.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 0
    except (RuntimeError, ValueError) as e:
        log.error("%s", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
