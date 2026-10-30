"""Tiny stdlib HTTP server behind `mg ui`.

Each workspace is a folder under the UI root. Pipeline stages run as `mg <stage>`
subprocesses (so a run can be cancelled and a crashing native library cannot take the
server down); their output is parsed for progress and kept in memory for polling.

Preview data is served in the raw SfM frame together with one 4x4 "scene" transform
(Y-up, subject on y=0), which the viewer applies to a root node so every layer
(cameras, sparse/dense points, raw/clean/textured mesh) overlays exactly:

  GET  /api/workspaces                      list
  POST /api/workspaces                      {"name", "video_path"} -> create
  PUT  /api/workspaces/<ws>/video?name=f    raw upload body -> video file
  GET  /api/workspaces/<ws>/state           stages, manifest, frames, layers
  POST /api/workspaces/<ws>/run             {"stages": [...], "options": {...}}
  POST /api/workspaces/<ws>/cancel
  GET  /api/workspaces/<ws>/job?since=N     status + new log lines
  GET  /api/workspaces/<ws>/scene           transform, extent, cameras
  GET  /api/workspaces/<ws>/points/<sparse|dense>   uint32 n | f32 xyz*n | u8 rgb*n
  GET  /api/workspaces/<ws>/mesh/<raw|clean|textured>.glb
  GET  /api/workspaces/<ws>/thumb/<frames|masks>/<file>
  GET  /api/workspaces/<ws>/image/<frames|masks>/<file>   full size, for the frame viewer
  GET  /api/workspaces/<ws>/frame-stats     per keyframe: sharpness, mask coverage, has a camera
  POST /api/workspaces/<ws>/orientation     {"transform": 4x4 | null} -> set / reset the orientation
  GET  /api/workspaces/<ws>/download?path=<rel>   an output file (see App.outputs), or mesh.zip
  POST /api/workspaces/<ws>/duplicate       {"name", "reconstruction": bool} -> copy to try other settings
  GET  /edit                                sculpt / paint editor (static/edit.html)
  POST /api/workspaces/<ws>/edit/mesh       sculpted mesh -> mesh/custom/ (see App.save_mesh)
  POST /api/workspaces/<ws>/edit/texture?i=N&from=pipeline|custom  painted texture -> mesh/custom/
  POST /api/workspaces/<ws>/edit/delete     {"what": "mesh"|"texture"} -> drop the custom version
  POST /api/workspaces/<ws>/export          {"format","source","options"} -> one file in .ui/exports/
  GET  /api/workspaces/<ws>/exports/<file>  download it
  GET  /api/workspaces/<ws>/preview.jpg     scan thumbnail (3D snapshot, else a keyframe)
  GET  /api/workspaces/<ws>/advice?subject= what to change next (advice.py), for assisted mode
  GET  /api/assist                          assisted mode's subject / coverage choices
  GET  /<name>.js                           shared page modules (static/viewcube.js)
  GET  /about                               third-party software and licenses (credits.py)
  GET  /api/about[/license?kind=&id=|/notices.txt]   their list, one license text, all notices
  GET  /help                                the how-to page (static/help.html, images in static/help/)
  POST /api/workspaces/<ws>/preview         JPEG snapshot taken by the viewer
  POST /api/workspaces/<ws>/edit/op?kind=cap|plane|remesh  mesh edit (see meshops.run), returns the mesh
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import signal
import struct
import subprocess
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import numpy as np

from ..diagnostics import EXIT_CANCELLED, EXIT_GATE, EXIT_PREFLIGHT
from ..workspace import RAW_MESH, STAGES, UPSTREAM, Workspace

log = logging.getLogger(__name__)

STATIC = Path(__file__).parent / "static"
MAX_POINTS = {"sparse": 200_000, "dense": 600_000}
NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
# What a duplicate copies per stage. Frames and masks always; the reconstruction on request.
STAGE_DIRS = {"frames": ["frames"], "mask": ["masks"], "sfm": ["colmap", "dense"], "mvs": ["mvs"], "vggt": ["vggt"]}
# Downloadable results, most useful first: (path in the workspace, label, stage that writes it).
# Any other file in mesh/ is offered too, under its own name.
OUTPUTS = [
    ("mesh/model.glb", "Textured model (GLB, Y-up)", "texture"),
    ("mesh/mesh_clean.glb", "Clean mesh, vertex colours (GLB, Y-up)", "clean"),
    ("mesh/print.stl", "3D print (STL: watertight, Z-up, sized in mm)", "clean"),
    ("mesh/textured.ply", "Textured mesh (PLY, SfM frame; needs its PNG)", "texture"),
    ("mesh/mesh_clean.ply", "Clean mesh (PLY, SfM frame)", "clean"),
    ("mvs/scene_mesh.ply", "Raw mesh before cleanup (PLY)", "mvs"),
    ("mvs/scene_dense.ply", "Dense point cloud (PLY)", "mvs"),
    ("vggt/tsdf_mesh.ply", "Raw VGGT mesh (PLY)", "vggt"),
    ("mesh/custom/model.glb", "Custom textured model (edited, GLB, Y-up)", "custom"),
    ("mesh/custom/print.stl", "Custom 3D print (edited, STL)", "custom"),
    ("mesh/custom/mesh_clean.glb", "Custom mesh (edited, GLB, Y-up)", "custom"),
    ("mesh/custom/mesh_clean.ply", "Custom mesh (edited, PLY, SfM frame)", "custom"),
    ("mesh/custom/textured.ply", "Custom textured mesh (PLY; needs its PNG)", "custom"),
]
STL = "mesh/print.stl"

# Progress patterns found in stage output (mg logging, COLMAP glog, OpenMVS).
PROGRESS = [
    re.compile(r"masked (\d+)/(\d+) frames"),
    re.compile(r"\((\d+(?:\.\d+)?)%"),
    re.compile(r"Processed file \[(\d+)/(\d+)\]"),
    re.compile(r"Matching block \[(\d+)/\d+, (\d+)/\d+\]"),
]


class Job:
    """One pipeline run on one workspace: stages executed in order as subprocesses."""

    def __init__(self, ws: Workspace, stages: list[str], options: dict, video: str | None):
        self.ws = ws
        self.stages = stages
        self.options = options
        self.video = video
        self.status = "running"
        self.current: str | None = None
        self.info = {s: {"status": "queued", "progress": None, "step": None, "start": None, "end": None}
                     for s in stages}
        self.lines: list[str] = []
        self.lock = threading.Lock()
        self.proc: subprocess.Popen | None = None
        self.cancelled = False
        self.failure: dict | None = None
        self.ended: float | None = None
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _emit(self, line: str) -> None:
        with self.lock:
            self.lines.append(line)
            if len(self.lines) > 20_000:
                del self.lines[:5_000]
            if self.current:
                p = _parse_progress(line)
                if p is not None:
                    st = self.info[self.current]
                    st["progress"] = p
                    # What the percentage refers to, e.g. "DensifyPointCloud: Estimated depth-maps 12".
                    st["step"] = _step_label(line)

    def _cmd(self, stage: str) -> list[str]:
        cmd = [sys.executable, "-m", "motiongrametry.cli", stage]
        if stage == "frames":
            cmd.append(self.video or "")
        cmd.append(str(self.ws.root))
        opts = {k: v for k, v in self.options.items() if k != "preset" and v not in (None, "")}
        if opts:
            cmd += ["--options-json", json.dumps(opts)]
        if self.options.get("preset"):
            cmd += ["--preset", self.options["preset"]]
        return cmd

    def _run(self) -> None:
        try:
            self._run_stages()
        except Exception as e:  # e.g. the interpreter or video vanished
            self._emit(f"=== runner error: {e}")
            self.status = "failed"
        finally:
            self.ended = time.time()
            _persist_log(self)

    def _run_stages(self) -> None:
        for i, stage in enumerate(self.stages):
            if self.cancelled:
                break
            self.current = stage
            st = self.info[stage]
            st.update(status="running", start=time.time())
            self._emit(f"=== {stage}: mg {' '.join(self._cmd(stage)[3:])}")
            self.proc = subprocess.Popen(self._cmd(stage), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                         text=True, bufsize=1, start_new_session=True)
            for line in self.proc.stdout:
                self._emit(line.rstrip())
            rc = self.proc.wait()
            st["end"] = time.time()
            st["exit_code"] = rc
            last = next((h for h in reversed(self.ws.history()) if h["stage"] == stage), {})
            st["log"] = last.get("log")
            st["diagnostics"] = last.get("diagnostics") or []
            status = {0: "done", EXIT_GATE: "gate-failed", EXIT_PREFLIGHT: "preflight-failed",
                      EXIT_CANCELLED: "cancelled"}.get(rc, "failed")
            if status == "done" and any(d["level"] == "warning" for d in st["diagnostics"]):
                status = "warning"
            if self.cancelled:
                status = "cancelled"
            st["status"] = status
            if status not in ("done", "warning"):
                self.failure = {"stage": stage, "status": status, "exit_code": rc, "log": st["log"],
                                "diagnostics": [d for d in st["diagnostics"] if d["level"] == "error"]}
                for rest in self.stages[i + 1:]:
                    self.info[rest]["status"] = "skipped"
                self.status = "cancelled" if status == "cancelled" else "failed"
                self.current = None
                return
            st["progress"] = 1.0
        self.current = None
        self.status = "cancelled" if self.cancelled else "done"

    def cancel(self) -> None:
        self.cancelled = True
        proc = self.proc
        if not proc or proc.poll() is not None:
            return
        # SIGTERM lets the stage unwind (it kills its own OpenMVS container and records the
        # run as cancelled); escalate if it has not exited after a grace period.
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            return

        def escalate() -> None:
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                self._emit("=== cancel: stage did not stop in 20 s, killed")
            # Belt and braces: containers are named mg-<tool>-<stage pid>-..., remove any
            # the stage could not kill itself (e.g. it was SIGKILLed).
            if shutil.which("docker"):
                ids = subprocess.run(["docker", "ps", "-q", "--filter", f"name=-{proc.pid}-"],
                                     capture_output=True, text=True).stdout.split()
                if ids:
                    subprocess.run(["docker", "kill", *ids], capture_output=True)
                    self._emit(f"=== cancel: killed {len(ids)} leftover OpenMVS container(s)")

        threading.Thread(target=escalate, daemon=True).start()

    def snapshot(self, since: int) -> dict:
        with self.lock:
            return {"status": self.status, "current": self.current, "stages": self.info,
                    "failure": self.failure, "log": self.lines[since:], "next": len(self.lines)}


def _parse_progress(line: str) -> float | None:
    for rx in PROGRESS:
        m = rx.search(line)
        if m:
            g = m.groups()
            if len(g) == 1:
                return float(g[0]) / 100.0
            a, b = float(g[0]), float(g[1])
            return a / b if b else None
    return None


def _step_label(line: str) -> str:
    line = re.sub(r"^\d\d:\d\d:\d\d \w+ [\w.]+: ", "", line)  # logging prefix
    line = re.sub(r"\s*\(\d.*$", "", line)                       # "(42.1%, 3s, ETA ...)"
    return line[:80]


def _persist_log(job: Job) -> None:
    ui = job.ws.root / ".ui"
    ui.mkdir(exist_ok=True)
    with job.lock:
        (ui / "last_run.log").write_text("\n".join(job.lines))


class App:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.jobs: dict[str, Job] = {}
        self.cache_lock = threading.Lock()
        self.reorienting: set[str] = set()  # scans whose exports are being rebuilt (set_orientation)

    # ---- workspaces -------------------------------------------------------------------
    def job(self, ws: Workspace) -> Job | None:
        """The UI's last run on this workspace, unless stages ran since (e.g. from the terminal):
        then the history on disk is more current than that run's in-memory status."""
        job = self.jobs.get(ws.root.name)
        if job and job.status != "running" and job.ended is not None \
                and any(h.get("started", 0) > job.ended for h in ws.history()):
            return None
        return job

    def ws(self, name: str) -> Workspace:
        if not NAME_RE.match(name) or not (self.root / name).is_dir():
            raise KeyError(name)
        return Workspace(self.root / name)

    def meta(self, ws: Workspace) -> dict:
        p = ws.root / ".ui" / "meta.json"
        meta = json.loads(p.read_text()) if p.exists() else {}
        # Uploaded videos are stored relative to the workspace so it can be moved.
        if meta.get("video") and not Path(meta["video"]).is_absolute():
            meta["video"] = str(ws.root / meta["video"])
        return meta

    def set_meta(self, ws: Workspace, **kw) -> None:
        (ws.root / ".ui").mkdir(exist_ok=True)
        p = ws.root / ".ui" / "meta.json"
        meta = (json.loads(p.read_text()) if p.exists() else {}) | kw
        (ws.root / ".ui" / "meta.json").write_text(json.dumps(meta, indent=2))

    def list(self) -> list[dict]:
        out = []
        for d in sorted(self.root.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            # Only folders that are workspaces (made by the UI or by `mg` on the command line).
            if d.is_dir() and NAME_RE.match(d.name) and ((d / ".ui").is_dir() or (d / "manifest.json").exists()):
                ws = Workspace(d)
                job = self.job(ws)
                out.append({"name": d.name, "video": self.meta(ws).get("video"),
                            "stages": self.stage_status(ws), "job": job.status if job else None,
                            **self._summary(ws)})
        return out

    def _summary(self, ws: Workspace) -> dict:
        """What the scan picker shows: preview, size of the result, last activity."""
        m = ws.load_manifest()
        frames = m.get("frames", {}).get("keyframes")
        pv = ws.root / ".ui" / "preview.jpg"
        hist = ws.history()
        return {
            "preview": pv.stat().st_mtime if pv.exists() else (0 if ws.frames.is_dir() and any(ws.frames.glob("*.jpg")) else None),
            "frames": frames,
            "faces": m.get("clean", {}).get("faces_out"),
            "textured": (ws.mesh / "model.glb").exists(),
            "custom": (ws.mesh / "custom" / "mesh_clean.ply").exists() or (ws.mesh / "custom" / "textured.ply").exists(),
            "last_run": hist[-1]["started"] + (hist[-1].get("duration") or 0) if hist else None,
            "last_status": hist[-1]["status"] if hist else None,
        }

    def stage_status(self, ws: Workspace) -> dict[str, bool]:
        return {
            "frames": any(ws.frames.glob("*.jpg")) if ws.frames.is_dir() else False,
            "mask": ws.has_masks(),
            "sfm": (ws.sparse / "images.bin").exists(),
            "mvs": (ws.mvs / "scene_mesh.ply").exists(),
            "vggt": (ws.vggt / "tsdf_mesh.ply").exists(),
            "clean": (ws.mesh / "mesh_clean.ply").exists(),
            "texture": (ws.mesh / "textured.ply").exists(),
        }

    def disk(self, ws: Workspace) -> dict:
        from ..mvs import depth_map_bytes

        info = {"free": shutil.disk_usage(ws.root).free, "mvs_need": None}
        if (ws.dense / "sparse" / "cameras.bin").exists():
            try:
                info["mvs_need"] = {lvl: depth_map_bytes(ws, lvl) for lvl in (0, 1, 2)}
            except Exception:  # half-written model while SfM runs
                pass
        return info

    def stale(self, ws: Workspace) -> list[str]:
        return ws.stale_stages()

    def state(self, ws: Workspace) -> dict:
        frames = [p.name for p in ws.frame_paths()]
        return {
            "name": ws.root.name,
            "root": str(ws.root),
            "meta": self.meta(ws),
            "done": self.stage_status(ws),
            "manifest": ws.load_manifest(),
            "frames": frames,
            "masks": [f for f in frames if ws.mask_for(f).exists()],
            "layers": self.layers(ws),
            "stale": self.stale(ws),
            "disk": self.disk(ws),
            "outputs": self.outputs(ws),
            "cameras": ws.cameras_from(),
            "raw_meshes": [s for s, rel in RAW_MESH.items() if (ws.root / rel).exists()],
            "raw_source": ws.mesh_producer(ws.raw_mesh()),
            "clean_source": self._clean_source(ws),
            "custom": self.custom_info(ws),
            "preview_stale": self.preview_stale(ws),
            "orientation": self.orientation(ws),
            "version": self._version(ws),
        }

    @staticmethod
    def _clean_source(ws: Workspace) -> str | None:
        used = ws.load_manifest().get("clean", {}).get("input")
        return (ws.mesh_producer(used) or "custom") if used and (ws.mesh / "mesh_clean.ply").exists() else None

    def duplicate(self, ws: Workspace, name: str, reconstruction: bool = False) -> Workspace:
        """Copy a workspace's finished inputs so other settings or VGGT can be tried without
        touching the original. Only stages with a completion stamp are copied, so a stage that
        is running (in the UI or a terminal) is never copied half-written."""
        if not NAME_RE.match(name):
            raise ValueError("name: letters, digits, '.', '_' or '-' only")
        dst_root = self.root / name
        if dst_root.exists():
            raise ValueError(f"workspace {name!r} already exists")
        stages = ["frames", "mask"] + (["sfm", "mvs", "vggt"] if reconstruction else [])
        done = [s for s in stages if (ws.root / ".stamps" / s).exists()]
        if reconstruction and "sfm" in done and ws.cameras_from() == "sfm":
            done = [s for s in done if s != "vggt"]  # its mesh does not match the SfM cameras
        dst = Workspace(dst_root)
        try:
            (dst.root / ".stamps").mkdir()
            manifest, src_manifest = {}, ws.load_manifest()
            for stage in done:
                for d in STAGE_DIRS[stage]:
                    if (ws.root / d).is_dir():
                        _copy_tree(ws.root / d, dst.root / d)
                shutil.copy2(ws.root / ".stamps" / stage, dst.root / ".stamps" / stage)
                if stage in src_manifest:
                    manifest[stage] = src_manifest[stage]
            manifest["diagnostics"] = {s: d for s, d in src_manifest.get("diagnostics", {}).items() if s in done}
            dst._write_manifest(manifest)
            self.set_meta(dst, video=self.meta(ws).get("video"), duplicated_from=ws.root.name)
        except BaseException:
            shutil.rmtree(dst.root, ignore_errors=True)  # no half-copied workspace in the list
            raise
        return dst

    def outputs(self, ws: Workspace) -> list[dict]:
        stale = set(self.stale(ws))
        known = {rel: (label, stage) for rel, label, stage in OUTPUTS}
        extra = sorted(str(p.relative_to(ws.root)) for p in [*ws.mesh.glob("*"), *ws.mesh.glob("custom/*")]
                       if p.is_file() and p.name != "info.json") if ws.mesh.is_dir() else []
        custom = self.custom_info(ws)
        cinfo = ws.mesh / "custom" / "info.json"
        notes = {"pipeline": ws.load_manifest().get("clean", {}).get("print_issues", []),
                 "custom": json.loads(cinfo.read_text()).get("print_issues", []) if cinfo.exists() else []}
        out = []
        for rel in [r for r, _, _ in OUTPUTS] + [r for r in extra if r not in known]:
            p = ws.root / rel
            label, stage = known.get(rel, (p.name, "texture" if p.name.startswith("textured") else "clean"))
            if rel == STL and self._stl_outdated(ws):  # older workspaces: converted on download
                out.append({"path": rel, "label": label, "size": None, "stale": stage in stale})
                continue
            if not p.is_file():
                continue
            if rel.startswith("mesh/custom/") and rel not in known:
                label = f"Custom {p.name}"
            is_stale = stage in stale or (rel.startswith("mesh/custom/") and custom["texture_stale"]
                                          and ("textured" in p.name or p.name == "model.glb"))
            entry = {"path": rel, "label": label, "size": p.stat().st_size, "stale": is_stale}
            if p.name == "print.stl":  # what may still go wrong when printing (clean.print_report)
                entry["issues"] = notes["custom" if rel.startswith("mesh/custom/") else "pipeline"]
            out.append(entry)
        return out

    @staticmethod
    def _stl_outdated(ws: Workspace) -> bool:
        from ..clean import ORIENTATION

        src, stl, orient = ws.root / "mesh/mesh_clean.ply", ws.root / STL, ws.root / ORIENTATION
        return src.is_file() and (not stl.is_file() or stl.stat().st_mtime < src.stat().st_mtime
                                  or orient.exists() and stl.stat().st_mtime < orient.stat().st_mtime)

    def output_file(self, ws: Workspace, rel: str) -> Path:
        """Only the files listed by outputs() are served, plus a zip of mesh/."""
        if rel in (STL, "mesh.zip") and self._stl_outdated(ws):
            from ..clean import workspace_print_stl

            with self.cache_lock:
                if self._stl_outdated(ws):
                    info = workspace_print_stl(ws, **self._print_opts(ws))
                    manifest = ws.load_manifest()
                    manifest.setdefault("clean", {}).update(info)
                    ws._write_manifest(manifest)
        if rel == "mesh.zip":
            return self._mesh_zip(ws)
        if rel not in {o["path"] for o in self.outputs(ws)}:
            raise FileNotFoundError(rel)
        return ws.root / rel

    def _mesh_zip(self, ws: Workspace) -> Path:
        import zipfile

        files = sorted(p for p in [*ws.mesh.glob("*"), *ws.mesh.glob("custom/*")]
                       if p.is_file() and p.name != "info.json") if ws.mesh.is_dir() else []
        if not files:
            raise FileNotFoundError("mesh/")

        def build(out: Path) -> None:
            # PNG and GLB are already compressed; deflate only pays off for the PLYs.
            with zipfile.ZipFile(out, "w") as z:
                for p in files:
                    z.write(p, f"{ws.root.name}/{p.relative_to(ws.mesh)}",
                            zipfile.ZIP_STORED if p.suffix in (".png", ".jpg", ".glb") else zipfile.ZIP_DEFLATED)

        return self._cached(ws, "mesh.zip", files, build)

    def layers(self, ws: Workspace) -> dict[str, bool]:
        stale = set(self.stale(ws))
        layers = {
            "cameras": (ws.sparse / "images.bin").exists(),
            "sparse": (ws.sparse / "points3D.bin").exists(),
            "dense": (ws.mvs / "scene_dense.ply").exists(),
            "raw": self._raw_mesh(ws) is not None,
            "clean": (ws.mesh / "mesh_clean.ply").exists(),
            "textured": (ws.mesh / "textured.ply").exists(),
            "custom": (ws.mesh / "custom" / "textured.ply").exists() or (ws.mesh / "custom" / "mesh_clean.ply").exists(),
        }
        # Hide layers that no longer line up with the current cameras.
        depends = {"cameras": "sfm", "sparse": "sfm", "dense": "mvs", "clean": "clean", "textured": "texture",
                   "raw": ws.mesh_producer(ws.raw_mesh()) or "mvs", "custom": None}
        return {k: v and depends[k] not in stale for k, v in layers.items()}

    def _version(self, ws: Workspace) -> float:
        # Changes whenever an output changes, so the viewer knows when to reload layers.
        paths = [ws.sparse / "images.bin", ws.mvs / "scene_dense.ply", ws.mesh / "mesh_clean.ply",
                 ws.mesh / "textured.ply", ws.vggt / "tsdf_mesh.ply", ws.mvs / "scene_mesh.ply",
                 ws.mesh / "custom" / "textured.ply", ws.mesh / "custom" / "mesh_clean.ply"]
        return max((p.stat().st_mtime for p in paths if p.exists()), default=0.0)

    @staticmethod
    def _raw_mesh(ws: Workspace) -> Path | None:
        return ws.raw_mesh()  # the one clean uses, so the raw and clean layers match

    def run(self, ws: Workspace, stages: list[str], options: dict) -> Job:
        name = ws.root.name
        if name in self.jobs and self.jobs[name].status == "running":
            raise RuntimeError("a run is already in progress for this scan")
        if name in self.reorienting:
            raise RuntimeError("the export files are being updated for the new orientation: try again in a moment")
        bad = [s for s in stages if s not in STAGES]
        if bad or not stages:
            raise ValueError(f"unknown stages {bad}")
        stages = [s for s in STAGES if s in stages]  # canonical order
        video = self.meta(ws).get("video")
        if "frames" in stages and not (video and Path(video).exists()):
            raise ValueError("this workspace has no video")
        from ..options import resolve

        resolve({k: v for k, v in options.items() if k != "preset"}, options.get("preset"))  # raises on bad input
        job = Job(ws, stages, options, video)
        self.jobs[name] = job
        return job

    def check(self, ws: Workspace, stages: list[str], options: dict) -> dict:
        from .. import checks
        from ..options import resolve

        o = resolve({k: v for k, v in options.items() if k != "preset"}, options.get("preset"))
        video = self.meta(ws).get("video")
        out = {}
        for stage in [s for s in STAGES if s in stages]:
            try:
                out[stage] = [d.as_dict() for d in checks.preflight(stage, ws, o, video)]
            except Exception as e:  # a check itself failing must not hide the others
                out[stage] = [{"level": "error", "code": "check-crashed", "message": str(e), "hint": ""}]
        return {"checks": out, "stale": ws.stale_stages()}

    def log_file(self, ws: Workspace, rel: str) -> Path:
        """Only stage logs and OpenMVS tool logs are served."""
        p = (ws.root / rel).resolve()
        allowed = (ws.logs.resolve(), ws.mvs.resolve())
        if p.parent not in allowed or p.suffix != ".log" or not p.is_file():
            raise FileNotFoundError(rel)
        return p

    # ---- preview data ---------------------------------------------------------------------
    def _cached(self, ws: Workspace, key: str, sources: list[Path], build) -> Path:
        """Build a derived file once per change of its sources."""
        cache = ws.root / ".ui" / "cache"
        cache.mkdir(parents=True, exist_ok=True)
        stamp = "-".join(str(int(p.stat().st_mtime_ns)) for p in sources if p.exists())
        out = cache / key
        tag = cache / f"{key}.stamp"
        with self.cache_lock:
            if out.exists() and tag.exists() and tag.read_text() == stamp:
                return out
            build(out)
            tag.write_text(stamp)
        return out

    def scene(self, ws: Workspace) -> dict:
        import pycolmap

        from ..clean import _ground_for_workspace, gravity_alignment, user_orientation

        rec = pycolmap.Reconstruction(ws.sparse)
        pts = np.array([p.xyz for p in rec.points3D.values()]) if rec.num_points3D() else np.zeros((0, 3))

        def build(out: Path) -> None:
            mesh = ws.mesh / "mesh_clean.ply"
            if mesh.exists():
                import open3d as o3d

                verts = np.asarray(o3d.io.read_triangle_mesh(str(mesh)).vertices)
            else:
                verts = pts
            T = gravity_alignment(rec, verts, _ground_for_workspace(ws, rec)) if len(verts) else np.eye(4)
            out.write_text(json.dumps(T.tolist()))

        srcs = [ws.sparse / "images.bin", ws.mesh / "mesh_clean.ply", ws.mvs / "scene_dense.ply"]
        auto = json.loads(self._cached(ws, "transform.json", srcs, build).read_text())
        user = user_orientation(ws)
        T = user.tolist() if user is not None else auto
        cams = []
        for img in sorted(rec.images.values(), key=lambda i: i.name):
            if not img.has_pose:
                continue
            c = img.camera
            cams.append({
                "name": img.name,
                "center": img.projection_center().tolist(),
                "world_from_cam": img.cam_from_world().rotation.matrix().T.tolist(),
                "fx": c.focal_length_x, "fy": c.focal_length_y, "w": c.width, "h": c.height,
            })
        if len(pts):
            lo, hi = np.percentile(pts, [5, 95], axis=0)
            extent = float(np.linalg.norm(hi - lo))
        else:
            centres = np.array([c["center"] for c in cams]) if cams else np.zeros((1, 3))
            extent = float(np.linalg.norm(np.ptp(centres, axis=0))) or 1.0
        return {"transform": T, "auto": auto, "user": user is not None, "extent": extent, "cameras": cams}

    # ---- orientation: set by hand in the viewer, used by every export -------------------------
    def orientation(self, ws: Workspace) -> dict:
        from ..clean import ORIENTATION, user_orientation

        return {"user": user_orientation(ws) is not None, "updating": ws.root.name in self.reorienting,
                # set against cameras that were re-run since: no longer applies
                "lapsed": (ws.root / ORIENTATION).exists() and user_orientation(ws) is None}

    def set_orientation(self, ws: Workspace, T) -> dict:
        """Save the orientation (SfM frame -> Y-up, like gravity_alignment) or drop it (None), then
        rebuild the files that have an orientation baked in (GLBs, print STLs) in the background."""
        from ..clean import ORIENTATION

        self._editable(ws)
        if ws.root.name in self.reorienting:
            raise RuntimeError("still updating the files for the last orientation: try again in a moment")
        p, cams = ws.root / ORIENTATION, ws.sparse / "images.bin"
        if T is None:
            p.unlink(missing_ok=True)
        else:
            T = np.asarray(T, float)
            if T.shape != (4, 4) or not np.isfinite(T).all() or not np.allclose(T[3], [0, 0, 0, 1]):
                raise ValueError("transform: a 4x4 matrix")
            R = T[:3, :3]
            if not np.allclose(R.T @ R, np.eye(3), atol=1e-4) or np.linalg.det(R) < 0:
                raise ValueError("transform: rotation and translation only")
            if not cams.exists():
                raise FileNotFoundError("cameras (run sfm first)")
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps({"transform": T.tolist(), "cameras": cams.stat().st_mtime_ns}))
            tmp.replace(p)
        self.reorienting.add(ws.root.name)
        threading.Thread(target=self._reexport, args=(ws,), daemon=True).start()
        return self.orientation(ws)

    def _reexport(self, ws: Workspace) -> None:
        from ..clean import export_glb, export_print_stl, workspace_alignment, workspace_print_stl

        m, c = ws.mesh, ws.mesh / "custom"
        try:
            textured_from_custom = self.custom_info(ws)["texture_from"] == "custom" and (c / "mesh_clean.ply").exists()
            # (source, file with the orientation baked in, geometry the automatic orientation is fitted to)
            for src, dst, geometry in [(m / "mesh_clean.ply", m / "mesh_clean.glb", m / "mesh_clean.ply"),
                                       (m / "textured.ply", m / "model.glb", m / "mesh_clean.ply"),
                                       (c / "mesh_clean.ply", c / "mesh_clean.glb", c / "mesh_clean.ply"),
                                       (c / "textured.ply", c / "model.glb",
                                        c / "mesh_clean.ply" if textured_from_custom else m / "mesh_clean.ply")]:
                if src.exists() and dst.exists() and geometry.exists():
                    export_glb(src, dst, workspace_alignment(ws, geometry))
            if (m / "mesh_clean.ply").exists():
                with self.cache_lock:
                    info = workspace_print_stl(ws, **self._print_opts(ws))
                    manifest = ws.load_manifest()
                    manifest.setdefault("clean", {}).update(info)
                    ws._write_manifest(manifest)
            if (c / "mesh_clean.ply").exists() and (c / "print.stl").exists():
                dst = c / "mesh_clean.ply"
                info = export_print_stl(dst, c / "print.stl", workspace_alignment(ws, dst), ws=ws, **self._print_opts(ws))
                self._custom_meta(ws, print_issues=info["print_issues"])
        except Exception:
            log.exception("re-exporting %s for its new orientation", ws.root.name)
        finally:
            self.reorienting.discard(ws.root.name)

    # ---- frame viewer -------------------------------------------------------------------------
    def image(self, ws: Workspace, kind: str, name: str) -> Path:
        src = (ws.frames / name) if kind == "frames" else ws.mask_for(name)
        if not src.exists() or src.resolve().parent not in (ws.frames.resolve(), ws.masks.resolve()):
            raise FileNotFoundError(name)
        return src

    def frame_stats(self, ws: Workspace) -> Path:
        """Per keyframe: sharpness (relative to the median, as the frames stage judges blur), how much
        of it the mask covers, and whether SfM placed its camera."""
        frames = ws.frame_paths()
        if not frames:
            raise FileNotFoundError("frames")

        def build(out: Path) -> None:
            import cv2

            posed = None
            if (ws.sparse / "images.bin").exists():
                import pycolmap

                try:
                    posed = {i.name for i in pycolmap.Reconstruction(ws.sparse).images.values() if i.has_pose}
                except Exception:  # half-written model while SfM runs
                    pass
            rows = []
            for f in frames:
                g = cv2.imread(str(f), cv2.IMREAD_REDUCED_GRAYSCALE_2)
                mk = cv2.imread(str(ws.mask_for(f.name)), cv2.IMREAD_REDUCED_GRAYSCALE_4) if ws.mask_for(f.name).exists() else None
                rows.append({"name": f.name, "sharpness": float(cv2.Laplacian(g, cv2.CV_64F).var()) if g is not None else None,
                             "coverage": round(float((mk > 127).mean()), 4) if mk is not None else None,
                             "posed": None if posed is None else f.name in posed})
            med = float(np.median([r["sharpness"] for r in rows if r["sharpness"]] or [1.0]))
            for r in rows:
                r["sharp_rel"] = round(r["sharpness"] / med, 3) if r["sharpness"] and med else None
            out.write_text(json.dumps(rows))

        return self._cached(ws, "frame_stats.json", [ws.frames, ws.masks, ws.sparse / "images.bin"], build)

    def points(self, ws: Workspace, kind: str) -> Path:
        if kind == "sparse":
            src = ws.sparse / "points3D.bin"

            def load():
                import pycolmap

                rec = pycolmap.Reconstruction(ws.sparse)
                p = list(rec.points3D.values())
                return (np.array([q.xyz for q in p], np.float32).reshape(-1, 3),
                        np.array([q.color for q in p], np.uint8).reshape(-1, 3))
        elif kind == "dense":
            src = ws.mvs / "scene_dense.ply"

            def load():
                import open3d as o3d

                pc = o3d.io.read_point_cloud(str(src))
                xyz = np.asarray(pc.points, np.float32)
                rgb = (np.asarray(pc.colors) * 255).astype(np.uint8) if pc.has_colors() \
                    else np.full_like(xyz, 200, dtype=np.uint8)
                return xyz, rgb
        else:
            raise KeyError(kind)
        if not src.exists():
            raise FileNotFoundError(src)

        def build(out: Path) -> None:
            xyz, rgb = load()
            if len(xyz) > MAX_POINTS[kind]:
                sel = np.random.default_rng(0).choice(len(xyz), MAX_POINTS[kind], replace=False)
                xyz, rgb = xyz[sel], rgb[sel]
            with open(out, "wb") as f:
                f.write(struct.pack("<I", len(xyz)))
                f.write(np.ascontiguousarray(xyz, "<f4").tobytes())
                f.write(np.ascontiguousarray(rgb, np.uint8).tobytes())

        return self._cached(ws, f"{kind}.bin", [src], build)

    def mesh(self, ws: Workspace, kind: str) -> Path:
        c = ws.mesh / "custom"
        src = {"raw": self._raw_mesh(ws), "clean": ws.mesh / "mesh_clean.ply",
               "textured": ws.mesh / "textured.ply", "custom_clean": c / "mesh_clean.ply",
               "custom_textured": c / "textured.ply",
               "custom": c / "textured.ply" if (c / "textured.ply").exists() else c / "mesh_clean.ply"}.get(kind)
        if src is None or not src.exists():
            raise FileNotFoundError(kind)

        def build(out: Path) -> None:
            import trimesh

            # Raw SfM frame, no transform: the viewer applies the shared scene transform.
            scene = trimesh.load(str(src), force="scene", process=False)
            scene.export(str(out), file_type="glb")

        return self._cached(ws, f"{kind}.glb", [src], build)

    # ---- editor: custom (edited) versions ----------------------------------------------------
    # Edits never touch the pipeline's files: they live in mesh/custom/. Texture projects onto the
    # custom mesh when it is newer than the clean mesh (pipeline._texture) and writes the custom
    # textured model there too; painting saves into it.
    @staticmethod
    def custom_info(ws: Workspace) -> dict:
        c, clean = ws.mesh / "custom", ws.mesh / "mesh_clean.ply"
        mesh, tex = c / "mesh_clean.ply", c / "textured.ply"
        mt = lambda p: p.stat().st_mtime if p.exists() else None  # noqa: E731
        info = json.loads((c / "info.json").read_text()) if (c / "info.json").exists() else {}
        return {
            "mesh": mesh.exists(),
            # made from an older clean mesh: texture ignores it until it is edited again
            "mesh_outdated": bool(mesh.exists() and clean.exists() and mt(mesh) < mt(clean)),
            "texture": tex.exists(),
            "texture_from": info.get("textured_from"),  # "custom" mesh or the pipeline's "clean" one
            "texture_stale": bool(tex.exists() and mesh.exists() and info.get("textured_from") != "custom"
                                  or tex.exists() and mesh.exists() and mt(tex) + 1 < mt(mesh)),
        }

    @staticmethod
    def clean_input(ws: Workspace) -> Path | None:
        """The raw mesh the clean mesh was carved from (what the editor's Restore brings back)."""
        used = ws.load_manifest().get("clean", {}).get("input")
        return Path(used) if used and Path(used).exists() else ws.raw_mesh()

    @staticmethod
    def _print_opts(ws: Workspace) -> dict:
        """The print settings of the scan's last clean run (defaults if it predates them)."""
        from ..clean import print_kwargs

        last = next((h for h in reversed(ws.history()) if h["stage"] == "clean" and h["status"] in ("ok", "warning")), {})
        return print_kwargs({k: v for k, v in (last.get("options") or {}).items() if k.startswith("print_")})

    def _editable(self, ws: Workspace) -> None:
        job = self.job(ws)
        if job and job.status == "running":
            raise RuntimeError("a pipeline run is using this scan: save when it has finished")

    @staticmethod
    def _custom_meta(ws: Workspace, **kw) -> None:
        p = ws.mesh / "custom" / "info.json"
        info = json.loads(p.read_text()) if p.exists() else {}
        info.update(kw)
        p.write_text(json.dumps(info))

    def save_mesh(self, ws: Workspace, data: bytes) -> dict:
        """Body: see meshops.decode_mesh. Written as the custom mesh (+ its GLB and print STL)."""
        import open3d as o3d

        from ..clean import _make_manifold, export_glb, export_print_stl, workspace_alignment
        from ..meshops import decode_mesh

        self._editable(ws)
        pos, tri, col = decode_mesh(data)
        c = ws.mesh / "custom"
        c.mkdir(parents=True, exist_ok=True)
        mesh = o3d.geometry.TriangleMesh(o3d.utility.Vector3dVector(pos),
                                         o3d.utility.Vector3iVector(tri.astype(np.int32)))
        mesh.vertex_colors = o3d.utility.Vector3dVector(col / 255.0)
        # As clean does: cuts leave bow-tie vertices, and OpenMVS TextureMesh crashes on them
        # (std::out_of_range in the atlas builder).
        mesh.remove_degenerate_triangles()
        mesh.remove_duplicated_triangles()
        mesh.remove_unreferenced_vertices()
        fixed = _make_manifold(mesh)
        if not mesh.has_triangles():
            raise ValueError("nothing would be left of the mesh: not saved")
        mesh.compute_vertex_normals()
        dst = c / "mesh_clean.ply"
        tmp = dst.with_suffix(".tmp.ply")
        o3d.io.write_triangle_mesh(str(tmp), mesh)
        tmp.replace(dst)
        align = workspace_alignment(ws, dst)
        export_glb(dst, c / "mesh_clean.glb", align)
        info = export_print_stl(dst, c / "print.stl", align, ws=ws, **self._print_opts(ws))
        self._custom_meta(ws, print_issues=info["print_issues"])
        return {"faces": len(mesh.triangles), "nonmanifold_vertices_removed": fixed, **info}

    def save_texture(self, ws: Workspace, index: int, data: bytes, source: str) -> dict:
        """A painted texture image. ``source`` is the model the editor painted: the pipeline's
        textured model (copied into custom/ on the first save) or the custom one."""
        from ..clean import export_glb, workspace_alignment

        self._editable(ws)
        if not data.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("texture upload must be a PNG")
        c = ws.mesh / "custom"
        if source != "custom":
            if not (ws.mesh / "textured.ply").exists():
                raise FileNotFoundError("textured.ply")
            c.mkdir(parents=True, exist_ok=True)
            for old in list(c.glob("textured*")):
                old.unlink()
            for p in [ws.mesh / "textured.ply", *ws.mesh.glob("textured*.png")]:
                shutil.copy2(p, c / p.name)  # the PLY names its PNGs relative to itself
            self._custom_meta(ws, textured_from="clean")
        png, ply = c / f"textured{index}.png", c / "textured.ply"
        if not png.exists() or not ply.exists():
            raise FileNotFoundError(f"custom/{png.name}")
        tmp = png.with_suffix(".tmp.png")
        tmp.write_bytes(data)
        tmp.replace(png)
        os.utime(ply)  # the viewer's cached GLB of textured.ply follows its mtime
        from_custom = self.custom_info(ws)["texture_from"] == "custom" and (c / "mesh_clean.ply").exists()
        geometry = c / "mesh_clean.ply" if from_custom else ws.mesh / "mesh_clean.ply"
        export_glb(ply, c / "model.glb", workspace_alignment(ws, geometry))
        return {"texture": f"custom/{png.name}", "bytes": len(data)}

    def delete_custom(self, ws: Workspace, what: str) -> dict:
        """Drop the custom mesh or texture: the pipeline's version is what remains."""
        self._editable(ws)
        c = ws.mesh / "custom"
        names = {"mesh": ["mesh_clean.ply", "mesh_clean.glb", "print.stl"],
                 "texture": [p.name for p in c.glob("textured*")] + ["model.glb"]}[what]
        gone = [n for n in names if (c / n).exists()]
        for n in gone:
            (c / n).unlink()
        if what == "texture":
            self._custom_meta(ws, textured_from=None)
        if not gone:
            raise FileNotFoundError(f"no custom {what}")
        return {"deleted": gone}

    # ---- export window: one file at a time, with its own settings ------------------------------
    EXPORT_FORMATS = {"stl": "print.stl", "glb_textured": "textured.glb", "glb_mesh": "mesh.glb",
                      "obj": "textured-obj.zip", "ply": "mesh.ply"}
    PRINT_SETTINGS = {"height_mm": float, "base": float, "mode": ("auto", "solid", "shell"), "stand": ("auto", "none"),
                      "min_wall": float, "shell": float, "shell_side": ("inward", "outward", "both")}

    def export(self, ws: Workspace, body: dict) -> dict:
        """Build one export (format, pipeline or custom version, settings) into .ui/exports/."""
        import trimesh

        from ..clean import export_glb, export_print_stl, workspace_alignment

        fmt, source, opts = body.get("format"), body.get("source", "pipeline"), body.get("options") or {}
        if fmt not in self.EXPORT_FORMATS or source not in ("pipeline", "custom"):
            raise ValueError("unknown export")
        c, custom = ws.mesh / "custom", source == "custom"
        mesh = c / "mesh_clean.ply" if custom else ws.mesh / "mesh_clean.ply"
        textured = c / "textured.ply" if custom else ws.mesh / "textured.ply"
        # a custom texture painted on the pipeline model has the pipeline mesh's geometry
        tex_geom = mesh if not custom or self.custom_info(ws)["texture_from"] == "custom" else ws.mesh / "mesh_clean.ply"
        needs = textured if fmt in ("glb_textured", "obj") else mesh
        if not needs.exists():
            raise FileNotFoundError(f"{'custom ' if custom else ''}{needs.name}")
        up = opts.get("up", "auto")
        if up not in ("auto", "plane", "camera", "none"):
            raise ValueError("up: auto, plane, camera or none")
        out_dir = ws.root / ".ui" / "exports"
        out_dir.mkdir(parents=True, exist_ok=True)
        dst = out_dir / f"{ws.root.name}-{'custom-' if custom else ''}{self.EXPORT_FORMATS[fmt]}"
        tmp = dst.with_name("tmp-" + dst.name)
        result: dict = {"issues": []}
        if fmt == "stl":
            kw = {}
            for k, kind in self.PRINT_SETTINGS.items():
                if opts.get(k) in (None, ""):
                    continue
                if isinstance(kind, tuple) and opts[k] not in kind:
                    raise ValueError(f"{k}: one of {', '.join(kind)}")
                kw[k] = opts[k] if isinstance(kind, tuple) else float(opts[k])
            info = export_print_stl(mesh, tmp, workspace_alignment(ws, mesh, up=up), ws=ws, **kw)
            result.update(issues=info.pop("print_issues"), info=info)
        else:
            T = workspace_alignment(ws, tex_geom if fmt in ("glb_textured", "obj") else mesh, up=up)
            T = np.eye(4) if T is None else T
            height = opts.get("height_mm")
            if height not in (None, ""):  # real size: glTF is in metres, OBJ / PLY in millimetres
                v = np.asarray(trimesh.load(str(needs), force="mesh", process=False).vertices)
                ext = float(np.ptp(v @ T[:3, :3].T, axis=0)[1])
                unit = 0.001 if fmt.startswith("glb") else 1.0
                if ext > 0:
                    T = np.diag([*(3 * [float(height) * unit / ext]), 1.0]) @ T
            if fmt.startswith("glb"):
                export_glb(needs, tmp.with_suffix(".glb"), T)
                tmp.with_suffix(".glb").replace(tmp)
            elif fmt == "ply":
                m = trimesh.load(str(needs), force="mesh", process=False)
                m.apply_transform(T)
                m.export(str(tmp), file_type="ply")
            else:  # OBJ + MTL + texture images, zipped
                import zipfile

                from trimesh.exchange.obj import export_obj

                m = trimesh.load(str(needs), force="mesh", process=False)
                m.apply_transform(T)
                text, files = export_obj(m, include_texture=True, return_texture=True, mtl_name="model.mtl")
                with zipfile.ZipFile(tmp, "w") as z:
                    z.writestr("model.obj", text)
                    for name, data in files.items():
                        z.writestr(name, data)
            result["info"] = {"up": up, "height_mm": height}
        tmp.replace(dst)
        return {**result, "file": dst.name, "size": dst.stat().st_size, "format": fmt, "source": source}

    def export_file(self, ws: Workspace, name: str) -> Path:
        p = ws.root / ".ui" / "exports" / name
        if "/" in name or name.startswith(("tmp-", ".")) or not p.is_file():
            raise FileNotFoundError(name)
        return p

    # ---- scan previews: a small snapshot of the 3D view, taken when a run produces a result -----
    PREVIEW_SOURCES = ["mesh/model.glb", "mesh/mesh_clean.ply", "mesh/custom/model.glb",
                       "mesh/custom/mesh_clean.ply", "mvs/scene_mesh.ply", "vggt/tsdf_mesh.ply"]

    def preview_stale(self, ws: Workspace) -> bool:
        results = [ws.root / r for r in self.PREVIEW_SOURCES if (ws.root / r).exists()]
        if not results:
            return False
        p = ws.root / ".ui" / "preview.jpg"
        return not p.exists() or p.stat().st_mtime < max(r.stat().st_mtime for r in results)

    def preview(self, ws: Workspace) -> Path:
        p = ws.root / ".ui" / "preview.jpg"
        if p.exists():
            return p
        frames = ws.frame_paths()
        if not frames:
            raise FileNotFoundError("preview")
        return self.thumb(ws, "frames", frames[len(frames) // 2].name)

    def save_preview(self, ws: Workspace, data: bytes) -> dict:
        if not data.startswith(b"\xff\xd8") or len(data) > 2_000_000:
            raise ValueError("preview must be a JPEG under 2 MB")
        p = ws.root / ".ui" / "preview.jpg"
        p.parent.mkdir(exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(p)
        return {"ok": True}

    def thumb(self, ws: Workspace, kind: str, name: str) -> Path:
        import cv2

        src = (ws.frames / name) if kind == "frames" else ws.mask_for(name)
        if not src.exists() or src.resolve().parent not in (ws.frames.resolve(), ws.masks.resolve()):
            raise FileNotFoundError(name)

        def build(out: Path) -> None:
            img = cv2.imread(str(src), cv2.IMREAD_UNCHANGED)
            h, w = img.shape[:2]
            img = cv2.resize(img, (240, max(1, round(h * 240 / w))), interpolation=cv2.INTER_AREA)
            cv2.imwrite(str(out), img, [cv2.IMWRITE_JPEG_QUALITY, 80])

        return self._cached(ws, f"thumb_{kind}_{name}.jpg", [src], build)


def _copy_tree(src: Path, dst: Path) -> None:
    """cp -a with copy-on-write where the filesystem has it (btrfs, XFS): instant and free.
    Not hard links: stages rewrite files in place, which would change the original too."""
    if sys.platform.startswith("linux") and shutil.which("cp"):
        r = subprocess.run(["cp", "-a", "--reflink=auto", str(src), str(dst)], capture_output=True, text=True)
        if r.returncode == 0:
            return
        shutil.rmtree(dst, ignore_errors=True)
    shutil.copytree(src, dst, copy_function=shutil.copy2)


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "motiongrametry"

        def log_message(self, fmt, *args):  # quiet: the UI polls every second
            log.debug(fmt, *args)

        # ---- helpers ----
        def _json(self, obj, status=HTTPStatus.OK):
            body = json.dumps(obj, default=str).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _file(self, path: Path, ctype: str, cache: bool = False):
            data = path.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "max-age=3600" if cache else "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _file_bytes(self, data: bytes, ctype: str, name: str | None = None):
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            if name:
                self.send_header("Content-Disposition", f'attachment; filename="{name}"')
            self.end_headers()
            self.wfile.write(data)

        def _download(self, path: Path, name: str):
            # Streamed: the dense cloud can be several hundred MB.
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(path.stat().st_size))
            self.send_header("Content-Disposition", f'attachment; filename="{name}"')
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            with open(path, "rb") as f:
                shutil.copyfileobj(f, self.wfile, 1 << 20)

        def _binary(self, data: bytes, info: dict):
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Op-Info", json.dumps(info, default=float))
            self.end_headers()
            self.wfile.write(data)

        def _raw(self) -> bytes:
            n = int(self.headers.get("Content-Length") or 0)
            if n > 1 << 30:
                raise ValueError("upload too large")
            return self.rfile.read(n)

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")

        def _route(self):
            url = urlparse(self.path)
            parts = [unquote(p) for p in url.path.strip("/").split("/") if p]
            return parts, parse_qs(url.query)

        def _guard(self, fn):
            try:
                fn()
            except KeyError as e:
                self._json({"error": f"not found: {e}"}, HTTPStatus.NOT_FOUND)
            except FileNotFoundError as e:
                self._json({"error": f"not available yet: {e}"}, HTTPStatus.NOT_FOUND)
            except (ValueError, RuntimeError) as e:
                self._json({"error": str(e)}, HTTPStatus.BAD_REQUEST)
            except BrokenPipeError:
                pass
            except Exception as e:  # pragma: no cover - surfaced to the UI
                log.exception("request failed")
                self._json({"error": f"{type(e).__name__}: {e}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

        # ---- verbs ----
        def do_GET(self):
            self._guard(self._get)

        def do_POST(self):
            self._guard(self._post)

        def do_PUT(self):
            self._guard(self._put)

        def _get(self):
            parts, q = self._route()
            if not parts or parts == ["index.html"]:
                return self._file(STATIC / "index.html", "text/html; charset=utf-8")
            if parts in (["edit"], ["edit.html"]):
                return self._file(STATIC / "edit.html", "text/html; charset=utf-8")
            if len(parts) == 1 and re.fullmatch(r"[\w-]+\.js", parts[0]) and (STATIC / parts[0]).is_file():
                return self._file(STATIC / parts[0], "text/javascript; charset=utf-8")  # shared page modules
            if parts in (["about"], ["about.html"]):
                return self._file(STATIC / "about.html", "text/html; charset=utf-8")
            if parts == ["api", "about"]:
                from ..credits import about

                return self._json(about())
            if parts == ["api", "about", "license"]:
                from ..credits import license_text

                return self._file_bytes(license_text(q.get("kind", [""])[0], q.get("id", [""])[0]).encode(), "text/plain; charset=utf-8")
            if parts == ["api", "about", "notices.txt"]:
                from ..credits import notices

                return self._file_bytes(notices().encode(), "text/plain; charset=utf-8", "THIRD_PARTY_NOTICES.txt")
            if parts in (["help"], ["help.html"]):
                return self._file(STATIC / "help.html", "text/html; charset=utf-8")
            if len(parts) == 2 and parts[0] == "help" and re.fullmatch(r"[\w-]+\.(png|jpg|svg)", parts[1]):
                p = STATIC / "help" / parts[1]
                if not p.is_file():
                    raise FileNotFoundError(parts[1])
                return self._file(p, {"png": "image/png", "jpg": "image/jpeg", "svg": "image/svg+xml"}[p.suffix[1:]], cache=True)
            if parts == ["api", "assist"]:
                from ..advice import COVERAGE, SUBJECTS

                return self._json({"subjects": SUBJECTS, "coverage": COVERAGE})
            if parts[:2] == ["api", "workspaces"] and len(parts) == 2:
                return self._json(app.list())
            if parts == ["api", "options"]:
                from ..options import as_json
                from ..presets import PRESETS

                return self._json({"options": as_json(), "presets": PRESETS, "upstream": UPSTREAM})
            if parts[:2] != ["api", "workspaces"] or len(parts) < 4:
                raise KeyError(self.path)
            ws, what = app.ws(parts[2]), parts[3]
            if what == "state":
                return self._json(app.state(ws))
            if what == "job":
                job = app.job(ws)
                since = int(q.get("since", ["0"])[0])
                if job:
                    return self._json(job.snapshot(since))
                prev = ws.root / ".ui" / "last_run.log"  # survive server restarts
                lines = prev.read_text().splitlines() if prev.exists() else []
                return self._json({"status": None, "log": lines[since:], "next": len(lines)})
            if what == "scene":
                return self._json(app.scene(ws))
            if what == "history":
                return self._json(ws.history()[-200:])
            if what == "log" and "path" in q:
                p = app.log_file(ws, q["path"][0])
                return self._file(p, "text/plain; charset=utf-8")
            if what == "download" and "path" in q:
                rel = q["path"][0]
                p = app.output_file(ws, rel)
                # Self-contained files get the workspace name so downloads from several captures
                # don't collide; a PLY names its texture PNG, so those two keep their names.
                name = ("custom-" if rel.startswith("mesh/custom/") else "") + Path(rel).name
                return self._download(p, f"{ws.root.name}-{name}" if p.suffix in (".glb", ".stl", ".zip") else Path(rel).name)
            if what == "exports" and len(parts) == 5:
                return self._download(app.export_file(ws, parts[4]), parts[4])
            if what == "advice":
                from ..advice import advise

                return self._json(advise(ws, q.get("subject", [None])[0]))
            if what == "preview.jpg":
                return self._file(app.preview(ws), "image/jpeg")
            if what == "tool-logs":
                return self._json(sorted(str(p.relative_to(ws.root)) for p in ws.mvs.glob("*.log")))
            if what == "points" and len(parts) == 5:
                return self._file(app.points(ws, parts[4]), "application/octet-stream")
            if what == "mesh" and len(parts) == 5 and parts[4].endswith(".glb"):
                return self._file(app.mesh(ws, parts[4][:-4]), "model/gltf-binary")
            if what == "image" and len(parts) == 6 and parts[4] in ("frames", "masks"):
                p = app.image(ws, parts[4], parts[5])
                return self._file(p, "image/png" if p.suffix == ".png" else "image/jpeg")
            if what == "frame-stats":
                return self._file(app.frame_stats(ws), "application/json")
            if what == "thumb" and len(parts) == 6 and parts[4] in ("frames", "masks"):
                return self._file(app.thumb(ws, parts[4], parts[5]), "image/jpeg", cache=True)
            raise KeyError(self.path)

        def _post(self):
            parts, q = self._route()
            if parts == ["api", "workspaces"]:
                body = self._body()
                name = body.get("name", "")
                if not NAME_RE.match(name):
                    raise ValueError("name: letters, digits, '.', '_' or '-' only")
                if (app.root / name).exists():
                    raise ValueError(f"scan {name!r} already exists")
                video = body.get("video_path")
                if video and not Path(video).expanduser().is_file():
                    raise ValueError(f"no such video: {video}")
                ws = Workspace(app.root / name)
                if video:
                    app.set_meta(ws, video=str(Path(video).expanduser().resolve()))
                else:
                    app.set_meta(ws)
                return self._json(app.state(ws), HTTPStatus.CREATED)
            if len(parts) == 5 and parts[:2] == ["api", "workspaces"] and parts[3] == "edit":
                ws, what = app.ws(parts[2]), parts[4]
                if what == "mesh":
                    return self._json(app.save_mesh(ws, self._raw()))
                if what == "texture":
                    return self._json(app.save_texture(ws, int(q.get("i", ["0"])[0]), self._raw(),
                                                       q.get("from", ["pipeline"])[0]))
                if what == "delete":
                    return self._json(app.delete_custom(ws, self._body().get("what", "")))
                if what == "op":  # cap / plane / remesh: mesh in, mesh out, nothing saved
                    from ..meshops import run

                    data, info = run(q.get("kind", [""])[0], self._raw(), app.clean_input(ws))
                    return self._binary(data, info)
            if len(parts) == 4 and parts[:2] == ["api", "workspaces"]:
                ws = app.ws(parts[2])
                if parts[3] == "preview":
                    return self._json(app.save_preview(ws, self._raw()))
                if parts[3] == "export":
                    return self._json(app.export(ws, self._body()))
                if parts[3] == "orientation":
                    return self._json(app.set_orientation(ws, self._body().get("transform")))
                if parts[3] == "run":
                    body = self._body()
                    job = app.run(ws, body.get("stages", []), body.get("options", {}))
                    return self._json(job.snapshot(0))
                if parts[3] == "check":
                    body = self._body()
                    return self._json(app.check(ws, body.get("stages") or STAGES, body.get("options", {})))
                if parts[3] == "duplicate":
                    body = self._body()
                    dst = app.duplicate(ws, body.get("name", ""), bool(body.get("reconstruction")))
                    return self._json(app.state(dst), HTTPStatus.CREATED)
                if parts[3] == "cancel":
                    job = app.jobs.get(ws.root.name)
                    if job:
                        job.cancel()
                    return self._json({"ok": True})
            raise KeyError(self.path)

        def _put(self):
            parts, q = self._route()
            if len(parts) == 4 and parts[:2] == ["api", "workspaces"] and parts[3] == "video":
                ws = app.ws(parts[2])
                fname = Path(q.get("name", ["video.mp4"])[0]).name or "video.mp4"
                dst = ws.root / ".ui" / fname
                dst.parent.mkdir(exist_ok=True)
                remaining = int(self.headers.get("Content-Length") or 0)
                with open(dst, "wb") as f:
                    while remaining:
                        chunk = self.rfile.read(min(remaining, 1 << 20))
                        if not chunk:
                            break
                        f.write(chunk)
                        remaining -= len(chunk)
                app.set_meta(ws, video=str(dst.relative_to(ws.root)))
                return self._json(app.state(ws))
            raise KeyError(self.path)

    return Handler


def serve(root: Path, host: str = "127.0.0.1", port: int = 8765) -> None:
    app = App(root)
    httpd = ThreadingHTTPServer((host, port), make_handler(app))
    httpd.daemon_threads = True
    print(f"motiongrametry UI on http://{host}:{port}  (workspaces in {app.root})", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        for job in app.jobs.values():
            job.cancel()
        httpd.server_close()
