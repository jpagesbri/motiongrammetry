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
  GET  /api/workspaces/<ws>/state           stages, manifest, frames, layers (and which are outdated)
  POST /api/workspaces/<ws>/run             {"stages": [...], "options": {...}, "pause_on": [codes]}
                                            (pause_on: stop after a stage that ends with one of these
                                            warnings, the next stages skipped: status "paused")
  POST /api/workspaces/<ws>/cancel
  GET  /api/workspaces/<ws>/job?since=N     status + new log lines
  GET  /api/workspaces/<ws>/scene           transform, extent, cameras
  GET  /api/workspaces/<ws>/points/<sparse|dense>   uint32 n | f32 xyz*n | u8 rgb*n
  GET  /api/workspaces/<ws>/mesh/<raw|clean|textured>.glb
  GET  /api/workspaces/<ws>/thumb/<frames|masks>/<file>
  GET  /api/workspaces/<ws>/image/<frames|masks>/<file>   full size, for the frame viewer
  GET  /api/workspaces/<ws>/frame-stats     per keyframe: sharpness, mask coverage, has a camera
  POST /api/workspaces/<ws>/orientation     {"transform": 4x4 | null} -> set / reset the orientation
  POST /api/workspaces/<ws>/duplicate       {"name", "reconstruction": bool} -> copy to try other settings
  GET  /api/workspaces/<ws>/size            bytes on disk, and the video if it lives outside the scan
  POST /api/workspaces/<ws>/delete          {"confirm": <name>} -> the scan's folder and everything in it
  GET  /edit                                sculpt / paint editor (static/edit.html)
  POST /api/workspaces/<ws>/edit/mesh       sculpted mesh -> mesh/custom/ (see App.save_mesh)
  POST /api/workspaces/<ws>/edit/texture?i=N&from=pipeline|custom  painted texture -> mesh/custom/
  POST /api/workspaces/<ws>/edit/delete     {"what": "mesh"|"texture"} -> drop the custom version
  POST /api/workspaces/<ws>/edit/layout     {"angle","min_size","preview"} -> rebuild the texture layout
                                            (preview: true counts, "image" a low-resolution look; nothing saved)
  POST /api/workspaces/<ws>/edit/layout?seams=1&from=pipeline|custom[&preview=1]  one byte per face (seam
                                            bits) -> the same, pieces enclosed by seams drawn in the editor
  POST /api/workspaces/<ws>/edit/layout     {"undo": true} -> back to the layout before the last rebuild
  POST /api/workspaces/<ws>/edit/transfer   the texture carried over to the custom (sculpted) mesh, no photos
  POST /api/workspaces/<ws>/export          {"items":[{"content","format"}],"source","options"} -> .ui/exports/
                                            (built on request, reused until an input changes: exports.py)
  POST /api/workspaces/<ws>/export/status   the same body -> which files are ready, outdated or not built
                                            (a ready one with its file name, for the window's preview)
  GET  /api/workspaces/<ws>/exports/<file>  download it
  GET  /api/workspaces/<ws>/preview.jpg     scan thumbnail (3D snapshot, else a keyframe)
  GET  /api/workspaces/<ws>/advice?subject= what to change next (advice.py), for assisted mode
  GET  /api/assist                          assisted mode's subject / coverage choices, and the
                                            warnings its runs pause on
  GET  /<name>.js, /<name>.css             shared page modules and styles (static/viewcube.js, topbar.js, scanchooser.js,
                                            scrollbar.js, modes.js, export.js, projection.js, pagehelp.js,
                                            icons.js, topbar.css, scrollbar.css, modes.css, export.css, pagehelp.css,
                                            notes.js, notes.css, camerapath.js)
  GET  /vendor/<path>                       bundled three.js and three-mesh-bvh (static/vendor)
  GET  /about                               third-party software and licenses (credits.py)
  GET  /api/about[/license?kind=&id=|/notices.txt]   their list, one license text, all notices
  GET  /help                                the how-to page (static/help.html, images in static/help/)
  POST /api/workspaces/<ws>/preview         JPEG snapshot taken by the viewer
  POST /api/workspaces/<ws>/edit/op?kind=cap|plane|remesh|sanitize  mesh edit (see meshops.run), returns the mesh
  POST /api/workspaces/<ws>/edit/op?kind=inspect  the editor's mesh -> what is wrong with it, and how long
                                            each Sanitize mesh fix would take (meshfix.inspect / estimates)
  GET  /api/workspaces/<ws>/edit/check?what=paint|edits  problems of the saved mesh that break or spoil
                                            texturing: the textured model painted, or the edited mesh
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

from .. import exports
from ..diagnostics import EXIT_CANCELLED, EXIT_GATE, EXIT_PREFLIGHT
from ..workspace import RAW_MESH, STAGES, UPSTREAM, Workspace

log = logging.getLogger(__name__)

STATIC = Path(__file__).parent / "static"
MAX_POINTS = {"sparse": 200_000, "dense": 600_000}
NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
# What a duplicate copies per stage. Frames and masks always; the reconstruction on request.
STAGE_DIRS = {"frames": ["frames"], "mask": ["masks"], "sfm": ["colmap", "dense"], "mvs": ["mvs"], "vggt": ["vggt"]}
LAYOUT_PREVIOUS = ".layout-previous"  # in mesh/custom/: the texture layout the last rebuild replaced

# Progress patterns found in stage output (mg logging, COLMAP glog, OpenMVS).
PROGRESS = [
    re.compile(r"masked (\d+)/(\d+) frames"),
    re.compile(r"\((\d+(?:\.\d+)?)%"),
    re.compile(r"Processed file \[(\d+)/(\d+)\]"),
    re.compile(r"Matching block \[(\d+)/\d+, (\d+)/\d+\]"),
]


class Job:
    """One pipeline run on one workspace: stages executed in order as subprocesses."""

    def __init__(self, ws: Workspace, stages: list[str], options: dict, video: str | None,
                 pause_on: list[str] | None = None):
        self.ws = ws
        # Warning codes that stop the run after their stage, so the user can choose what to do
        # (the assisted run passes sfm's camera-path warnings): status "paused", the rest skipped.
        self.pause_on = set(pause_on or [])
        self.paused: dict | None = None
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
        # the vggt step may have its own Python environment (Docker image with the VGGT layer)
        python = os.environ.get("MG_VGGT_PYTHON") if stage == "vggt" else None
        cmd = [python if python and Path(python).exists() else sys.executable, "-m", "motiongrammetry.cli", stage]
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
            codes = [d["code"] for d in st["diagnostics"] if d["level"] == "warning" and d.get("code") in self.pause_on]
            if status == "warning" and codes and i + 1 < len(self.stages) and not self.cancelled:
                rest = self.stages[i + 1:]
                for r in rest:
                    self.info[r]["status"] = "skipped"
                self.paused = {"stage": stage, "codes": codes, "skipped": rest}
                self._emit(f"=== paused after {stage} ({', '.join(codes)}): {', '.join(rest)} not run, waiting for your choice")
                self.status = "paused"
                self.current = None
                return
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
                    "failure": self.failure, "paused": self.paused, "log": self.lines[since:], "next": len(self.lines)}


def _clock() -> float:
    """Seconds of the day on this machine's clock, the one the logs are stamped with: the page
    times running steps from the log lines against it, not against the browser's clock, which
    can be in another time zone (a container runs in UTC, the browser in local time)."""
    t = time.localtime()
    return t.tm_hour * 3600 + t.tm_min * 60 + t.tm_sec + time.time() % 1


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
            "textured": (ws.mesh / "textured.ply").exists(),
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
            "layers": self.layers(ws),  # which exist, outdated or not
            "layers_outdated": self.layers_outdated(ws),  # layer -> why it is outdated (no key: up to date)
            "stale": self.stale(ws),
            "upstream": ws.upstream(),  # UPSTREAM, with SfM's inputs as it ran here (masks with mask_sfm)
            "disk": self.disk(ws),
            "print_check": exports.print_check(ws),  # the last 3D-print export's report, per version
            "export_defaults": exports.defaults(ws),  # the scan's print_* / up options
            "can_texture": (ws.mvs / "scene_dense.mvs").exists(),  # the texture stage needs OpenMVS's scene
            "exportable": {v: [k for k, p in exports.sources_of(ws, v == "custom").items() if p]
                           for v in ("pipeline", "custom")},
            "cameras": ws.cameras_from(),
            "raw_meshes": [s for s, rel in RAW_MESH.items() if (ws.root / rel).exists()],
            "raw_source": ws.mesh_producer(ws.raw_mesh()),
            "clean_source": self._clean_source(ws),
            "custom": self.custom_info(ws),
            "scan_edge": self.scan_edge(ws),
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

    def size(self, ws: Workspace) -> dict:
        """What a scan holds and what deleting it frees. Copies made by Duplicate scan or a
        copy-on-write backup share data with it (btrfs, XFS): that data stays on disk. btrfs says
        exactly how much is the scan's alone; elsewhere the blocks used (hard links excluded) are the
        estimate."""
        total = files = blocks = 0
        for d, _, names in os.walk(ws.root):
            for n in names:
                try:
                    st = os.lstat(os.path.join(d, n))
                except OSError:
                    continue
                total += st.st_size
                files += 1
                if st.st_nlink == 1:
                    blocks += st.st_blocks * 512
        frees, exact = blocks, False
        if shutil.which("btrfs"):
            try:
                r = subprocess.run(["btrfs", "filesystem", "du", "-s", "--raw", str(ws.root)],
                                   capture_output=True, text=True, timeout=60)
                cols = r.stdout.strip().splitlines()[-1].split() if r.returncode == 0 and r.stdout.strip() else []
                if len(cols) >= 4 and cols[1].isdigit():
                    frees, exact = int(cols[1]), True  # Total, Exclusive, Set shared, path
            except (OSError, subprocess.TimeoutExpired):
                pass
        video = self.meta(ws).get("video")
        outside = video if video and not (ws.root / video).resolve().is_relative_to(ws.root.resolve()) else None
        return {"path": str(ws.root), "bytes": total, "files": files, "frees": frees, "frees_exact": exact,
                "shared": max(0, blocks - frees) if exact else 0, "video_outside": outside}

    def delete(self, ws: Workspace, confirm: str) -> dict:
        """Remove a scan's folder and everything in it (keyframes, masks, reconstruction, meshes,
        edits, exports, an uploaded video). A video it was made from elsewhere on disk is left alone."""
        name = ws.root.name
        if confirm != name:
            raise ValueError("to delete a scan, confirm with its exact name")
        job = self.jobs.get(name)
        if job and job.status == "running":
            raise RuntimeError("a run is using this scan: stop it first")
        info = self.size(ws)
        shutil.rmtree(ws.root)
        self.jobs.pop(name, None)
        log.info("deleted scan %s (%d files, %.1f MB)", name, info["files"], info["bytes"] / 1e6)
        return {"deleted": name, **info}

    # Which stage made each layer, so an outdated stage marks its layers (custom: the editor's own).
    LAYER_STAGE = {"cameras": "sfm", "sparse": "sfm", "dense": "mvs", "clean": "clean", "textured": "texture"}
    CHANGED = {"frames": "the keyframes changed", "mask": "the masks changed", "sfm": "the cameras changed",
               "mvs": "the MVS mesh changed", "vggt": "the VGGT mesh changed", "clean": "the clean mesh changed"}
    STAGE_NAME = {"frames": "Keyframes", "mask": "Masks", "sfm": "SfM", "mvs": "MVS", "vggt": "VGGT",
                  "clean": "Clean", "texture": "Texture"}

    def layers(self, ws: Workspace) -> dict[str, bool]:
        """Which 3D layers exist. Outdated ones too: they stay viewable, marked (layers_outdated)."""
        return {
            "cameras": (ws.sparse / "images.bin").exists(),
            "sparse": (ws.sparse / "points3D.bin").exists(),
            "dense": (ws.mvs / "scene_dense.ply").exists(),
            "raw": self._raw_mesh(ws) is not None,
            "clean": (ws.mesh / "mesh_clean.ply").exists(),
            "textured": (ws.mesh / "textured.ply").exists(),
            "custom": (ws.mesh / "custom" / "textured.ply").exists() or (ws.mesh / "custom" / "mesh_clean.ply").exists(),
        }

    def layers_outdated(self, ws: Workspace) -> dict[str, str]:
        """For each existing layer made by a stale stage, why, in plain words: which earlier step
        changed since, and where to re-run from (e.g. "made before the masks changed: re-run from SfM")."""
        causes = ws.stale_causes()
        made_by = {**self.LAYER_STAGE, "raw": ws.mesh_producer(ws.raw_mesh()) or "mvs"}
        out = {}
        for layer, there in self.layers(ws).items():
            cause = causes.get(made_by.get(layer)) if there else None
            if cause:
                out[layer] = (f"made before {self.CHANGED.get(cause['changed'], cause['changed'] + ' changed')}: "
                              f"re-run from {self.STAGE_NAME[cause['rerun']]}")
        return out

    def _version(self, ws: Workspace) -> float:
        # Changes whenever an output changes, so the viewer knows when to reload layers.
        paths = [ws.sparse / "images.bin", ws.mvs / "scene_dense.ply", ws.mesh / "mesh_clean.ply",
                 ws.mesh / "textured.ply", ws.vggt / "tsdf_mesh.ply", ws.mvs / "scene_mesh.ply",
                 ws.mesh / "custom" / "textured.ply", ws.mesh / "custom" / "mesh_clean.ply"]
        return max((p.stat().st_mtime for p in paths if p.exists()), default=0.0)

    @staticmethod
    def _raw_mesh(ws: Workspace) -> Path | None:
        return ws.raw_mesh()  # the one clean uses, so the raw and clean layers match

    def run(self, ws: Workspace, stages: list[str], options: dict, pause_on: list[str] | None = None) -> Job:
        name = ws.root.name
        if name in self.jobs and self.jobs[name].status == "running":
            raise RuntimeError("a run is already in progress for this scan")
        bad = [s for s in stages if s not in STAGES]
        if bad or not stages:
            raise ValueError(f"unknown stages {bad}")
        stages = [s for s in STAGES if s in stages]  # canonical order
        video = self.meta(ws).get("video")
        if "frames" in stages and not (video and Path(video).exists()):
            raise ValueError("this workspace has no video")
        from ..options import resolve

        resolve({k: v for k, v in options.items() if k != "preset"}, options.get("preset"))  # raises on bad input
        if not isinstance(pause_on or [], list) or not all(isinstance(c, str) for c in pause_on or []):
            raise ValueError("pause_on must be a list of warning codes")
        job = Job(ws, stages, options, video, pause_on)
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

        return {"user": user_orientation(ws) is not None,
                # set against cameras that were re-run since: no longer applies
                "lapsed": (ws.root / ORIENTATION).exists() and user_orientation(ws) is None}

    def set_orientation(self, ws: Workspace, T) -> dict:
        """Save the orientation (SfM frame -> Y-up, like gravity_alignment) or drop it (None). Nothing is
        rebuilt: exports made with the old one are outdated (exports.py) and built again when asked."""
        from ..clean import ORIENTATION

        self._editable(ws)
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
        return self.orientation(ws)

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
            from .. import atlas

            # Raw SfM frame, no transform: the viewer applies the shared scene transform. A texture
            # split over several images: a mesh per image, in their order (the editor paints them).
            atlas.textured_scene(src).export(str(out), file_type="glb")

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
            # the layout before the last rebuild is kept until the texture changes again (paint, texture run)
            "layout_undo": bool(tex.exists() and (c / LAYOUT_PREVIOUS / "after").exists()
                                and (c / LAYOUT_PREVIOUS / "after").read_text() == str(tex.stat().st_mtime_ns)),
        }

    def scan_edge(self, ws: Workspace) -> float | None:
        """The scan's typical edge: the median over the pipeline's clean mesh of each vertex's mean edge,
        averaged with its neighbours' (as static/sculpt-mesh.js measures the mesh under a brush). The
        editor's absolute Density (k times the scan's typical density) is relative to it, also when
        a sculpted custom mesh is loaded."""
        src = ws.mesh / "mesh_clean.ply"
        if not src.exists():
            return None

        def build(out: Path) -> None:
            import trimesh

            try:
                m = trimesh.load(str(src), process=False, force="mesh")
                e, length, nv = m.edges_unique, m.edges_unique_length, len(m.vertices)
                cnt = np.bincount(e.ravel(), minlength=nv)
                mean = (np.bincount(e[:, 0], length, nv) + np.bincount(e[:, 1], length, nv)) / np.maximum(cnt, 1)
                around = (np.bincount(e[:, 0], mean[e[:, 1]], nv) + np.bincount(e[:, 1], mean[e[:, 0]], nv)) / np.maximum(cnt, 1)
                ref = ((mean + around) / 2)[cnt > 0]
                out.write_text(json.dumps(float(np.median(ref)) if len(ref) else None))
            except Exception:  # not a mesh trimesh can read: the editor measures the loaded one
                out.write_text("null")

        return json.loads(self._cached(ws, "scan_edge.json", [src], build).read_text())

    @staticmethod
    def clean_input(ws: Workspace) -> Path | None:
        """The raw mesh the clean mesh was carved from (what the editor's Restore brings back)."""
        used = ws.load_manifest().get("clean", {}).get("input")
        return Path(used) if used and Path(used).exists() else ws.raw_mesh()

    def mesh_check(self, ws: Workspace, what: str) -> dict:
        """Problems of a saved mesh that break or spoil texturing (meshfix.problems): ``paint`` checks
        the textured model the editor paints, ``edits`` the edited mesh Texture my edits and Carry
        the texture over start from. Cached until the file changes."""
        from ..meshfix import check_file

        c = ws.mesh / "custom"
        if what == "paint":
            src, label = (c / "textured.ply", "your custom textured model") if (c / "textured.ply").exists() \
                else (ws.mesh / "textured.ply", "the textured model")
        elif what == "edits":
            src, label = c / "mesh_clean.ply", "your edited mesh"
        else:
            raise ValueError("what: paint or edits")
        if not src.exists():
            raise FileNotFoundError(src.name)

        def build(out: Path) -> None:
            out.write_text(json.dumps(check_file(src)))

        return {**json.loads(self._cached(ws, f"check_{what}.json", [src], build).read_text()), "mesh": label}

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
        """Body: see meshops.decode_mesh. Written as the custom mesh; its exports (GLB, print, ...) are
        made when asked (exports.py)."""
        import open3d as o3d

        from ..clean import _make_manifold
        from ..meshops import decode_mesh

        self._editable(ws)
        pos, tri, col = decode_mesh(data)
        c = ws.mesh / "custom"
        c.mkdir(parents=True, exist_ok=True)
        mesh = o3d.geometry.TriangleMesh(o3d.utility.Vector3dVector(pos),
                                         o3d.utility.Vector3iVector(tri.astype(np.int32)))
        mesh.vertex_colors = o3d.utility.Vector3dVector(col / 255.0)
        # As clean does: cuts leave bow-tie vertices and opposite-winding duplicate faces,
        # and OpenMVS TextureMesh aborts on them (std::out_of_range in the seam leveling).
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
        for old in ("mesh_clean.glb", "print.stl"):  # written by earlier versions: exports are made on request now
            (c / old).unlink(missing_ok=True)
        return {"faces": len(mesh.triangles), "nonmanifold_vertices_removed": fixed}

    def save_texture(self, ws: Workspace, index: int, data: bytes, source: str) -> dict:
        """A painted texture image. ``source`` is the model the editor painted: the pipeline's
        textured model (copied into custom/ on the first save) or the custom one."""
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
        os.utime(ply)  # the viewer's cached GLB and the exports of textured.ply follow its mtime
        return {"texture": f"custom/{png.name}", "bytes": len(data)}

    def relayout_texture(self, ws: Workspace, body: dict) -> dict:
        """Rebuild the texture layout of the model the editor paints (see atlas.py): pieces cut by the
        surface's sides. Written as the custom texture; the pipeline's files stay as they are, and the
        layout it replaces is kept aside (undo_layout)."""
        import base64

        from .. import atlas

        if body.get("undo"):
            return self.undo_layout(ws)
        self._editable(ws)
        angle, min_size = float(body.get("angle", 45)), float(body.get("min_size", 0.5)) / 100
        if not 10 <= angle <= 80 or not 0 <= min_size <= 0.2:
            raise ValueError("angle: 10 to 80 degrees; min_size: 0 to 20 (% of the surface)")
        c, info = ws.mesh / "custom", self.custom_info(ws)
        src = c / "textured.ply" if info["texture"] else ws.mesh / "textured.ply"
        if not src.exists():
            raise FileNotFoundError("textured.ply: run texture first")
        source = "custom" if info["texture"] else "pipeline"
        seams = None
        if "seams" in body:  # drawn on the model the editor showed: it must still be the one laid out here
            if body.get("from") != source:
                raise ValueError("the textured model changed since the editor loaded it: reload the editor")
            seams = np.frombuffer(body["seams"], np.uint8)
        elif body.get("preview") and body["preview"] != "image":
            return {**atlas.preview(src, angle, min_size), "source": source}
        if body.get("preview") == "image":  # a quick look at the new texture, nothing saved
            look = ws.root / ".ui" / "layout-preview"
            shutil.rmtree(look, ignore_errors=True)
            look.mkdir(parents=True)
            try:
                out = atlas.relayout(src, look / "look.ply", angle, min_size, seams=seams, preview=1024)
                png = base64.b64encode((look / "look0.png").read_bytes()).decode()
            finally:
                shutil.rmtree(look, ignore_errors=True)
            return {**out, "source": source, "image": "data:image/png;base64," + png}
        out = self._new_texture(ws, lambda dst: atlas.relayout(src, dst, angle, min_size, seams=seams),
                                # the pipeline's textured model laid out anew is still of its mesh
                                None if info["texture"] else "clean")
        return {**out, "source": source}

    def transfer_texture(self, ws: Workspace) -> dict:
        """The texture carried over to the custom (sculpted) mesh without the photos (atlas.transfer):
        removed parts go, closed holes take the colour of the surface around them."""
        from .. import atlas

        self._editable(ws)
        c, info = ws.mesh / "custom", self.custom_info(ws)
        mesh = c / "mesh_clean.ply"
        if not mesh.exists():
            raise FileNotFoundError("no edited mesh: sculpt and save first")
        src = c / "textured.ply" if info["texture"] else ws.mesh / "textured.ply"
        if not src.exists():
            raise FileNotFoundError("textured.ply: run texture first")
        return {**self._new_texture(ws, lambda dst: atlas.transfer(src, mesh, dst), "custom"),
                "source": "custom" if info["texture"] else "pipeline"}

    def _new_texture(self, ws: Workspace, make, textured_from: str | None) -> dict:
        """Install a texture make(dst) writes as the custom textured model, keeping the one it replaces
        aside (undo_layout). textured_from: what its geometry is, when it changes ("clean", "custom")."""
        c, info = ws.mesh / "custom", self.custom_info(ws)
        work, prev = c / ".layout", c / LAYOUT_PREVIOUS
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True)
        try:
            out = make(work / "textured.ply")
            shutil.rmtree(prev, ignore_errors=True)  # keep what it replaces: one step back
            prev.mkdir(parents=True)
            if info["texture"]:
                for p in [*c.glob("textured*"), c / "model.glb"]:
                    if p.exists():
                        p.replace(prev / p.name)
                (prev / "from").write_text(info["texture_from"] or "")
            else:
                (prev / "pipeline").write_text("")
            for p in work.iterdir():
                p.replace(c / p.name)
        finally:
            shutil.rmtree(work, ignore_errors=True)
        if textured_from:
            self._custom_meta(ws, textured_from=textured_from)
        (prev / "after").write_text(str((c / "textured.ply").stat().st_mtime_ns))
        return out

    def undo_layout(self, ws: Workspace) -> dict:
        """Back to the texture layout the last rebuild replaced (the pipeline's, or the custom one)."""
        self._editable(ws)
        c = ws.mesh / "custom"
        prev = c / LAYOUT_PREVIOUS
        if not self.custom_info(ws)["layout_undo"]:
            raise FileNotFoundError("no layout to go back to: saving paint or re-running texture ends the undo")
        for p in [*c.glob("textured*"), c / "model.glb"]:
            p.unlink(missing_ok=True)
        if (prev / "pipeline").exists():
            back = "pipeline"
            self._custom_meta(ws, textured_from=None)
        else:
            back = "custom"
            for p in [*prev.glob("textured*"), prev / "model.glb"]:
                if p.exists():
                    p.replace(c / p.name)
            self._custom_meta(ws, textured_from=(prev / "from").read_text() or None)
        shutil.rmtree(prev, ignore_errors=True)
        return {"restored": back}

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
            shutil.rmtree(c / LAYOUT_PREVIOUS, ignore_errors=True)
        if not gone:
            raise FileNotFoundError(f"no custom {what}")
        return {"deleted": gone}

    # ---- scan previews: a small snapshot of the 3D view, taken when a run produces a result -----
    PREVIEW_SOURCES = ["mesh/textured.ply", "mesh/mesh_clean.ply", "mesh/custom/textured.ply",
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
        server_version = "motiongrammetry"

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
            # cached files are revalidated on every use (a 304 when unchanged), so an update that
            # replaces a file under the same name (help screenshots, scripts) shows up at once
            st = path.stat()
            etag = f'"{st.st_mtime_ns:x}-{st.st_size:x}"'
            if cache and self.headers.get("If-None-Match") == etag:
                self.send_response(HTTPStatus.NOT_MODIFIED)
                self.send_header("ETag", etag)
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                return None
            data = path.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-cache" if cache else "no-store")
            if cache:
                self.send_header("ETag", etag)
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
            except FileExistsError as e:
                self.close_connection = True  # the refused upload's body is still on its way
                self._json({"error": str(e)}, HTTPStatus.CONFLICT)
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
            if parts and parts[0] == "vendor":  # three.js and three-mesh-bvh, bundled (no CDN at run time)
                p = (STATIC / Path(*parts)).resolve()
                if not p.is_relative_to((STATIC / "vendor").resolve()) or not p.is_file():
                    raise FileNotFoundError("/".join(parts))
                return self._file(p, "text/javascript; charset=utf-8" if p.suffix == ".js" else "text/plain; charset=utf-8", cache=True)
            if len(parts) == 1 and re.fullmatch(r"[\w-]+\.(js|css)", parts[0]) and (STATIC / parts[0]).is_file():
                ctype = "text/css" if parts[0].endswith(".css") else "text/javascript"
                return self._file(STATIC / parts[0], ctype + "; charset=utf-8")  # shared page modules and styles
            if parts in (["about"], ["about.html"]):
                return self._file(STATIC / "about.html", "text/html; charset=utf-8")
            if parts == ["api", "about"]:
                from ..checks import accel
                from ..credits import about

                return self._json({**about(), "system": {**accel(), "container": bool(os.environ.get("MG_IN_CONTAINER"))}})
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
                from ..advice import CAMERA_PATH_CODES, COVERAGE, SUBJECTS

                # pause_on: the warnings an assisted run stops on, to ask what to do (Job.pause_on)
                return self._json({"subjects": SUBJECTS, "coverage": COVERAGE, "pause_on": list(CAMERA_PATH_CODES)})
            if parts[:2] == ["api", "workspaces"] and len(parts) == 2:
                return self._json(app.list())
            if parts == ["api", "options"]:
                from ..options import as_json
                from ..presets import PRESETS

                from ..diagnostics import SHORT

                # short: every diagnostic code's title and line, for those stored before they carried them
                return self._json({"options": as_json(), "presets": PRESETS, "upstream": UPSTREAM,
                                   "short": {k: list(v) for k, v in SHORT.items()}})
            if parts[:2] != ["api", "workspaces"] or len(parts) < 4:
                raise KeyError(self.path)
            ws, what = app.ws(parts[2]), parts[3]
            if what == "state":
                return self._json(app.state(ws))
            if what == "job":
                job = app.job(ws)
                since = int(q.get("since", ["0"])[0])
                if job:
                    return self._json({**job.snapshot(since), "clock": _clock()})
                prev = ws.root / ".ui" / "last_run.log"  # survive server restarts
                lines = prev.read_text().splitlines() if prev.exists() else []
                return self._json({"status": None, "log": lines[since:], "next": len(lines), "clock": _clock()})
            if what == "scene":
                return self._json(app.scene(ws))
            if what == "edit" and parts[4:] == ["check"]:
                return self._json(app.mesh_check(ws, q.get("what", [""])[0]))
            if what == "history":
                return self._json(ws.history()[-200:])
            if what == "log" and "path" in q:
                p = app.log_file(ws, q["path"][0])
                return self._file(p, "text/plain; charset=utf-8")
            if what == "exports" and len(parts) == 5:
                return self._download(exports.export_file(ws, parts[4]), parts[4])
            if what == "advice":
                from ..advice import advise

                return self._json(advise(ws, q.get("subject", [None])[0]))
            if what == "size":
                return self._json(app.size(ws))
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
            if len(parts) == 5 and parts[:2] == ["api", "workspaces"] and parts[3:] == ["export", "status"]:
                return self._json(exports.status(app.ws(parts[2]), self._body()))
            if len(parts) == 5 and parts[:2] == ["api", "workspaces"] and parts[3] == "edit":
                ws, what = app.ws(parts[2]), parts[4]
                if what == "mesh":
                    return self._json(app.save_mesh(ws, self._raw()))
                if what == "texture":
                    return self._json(app.save_texture(ws, int(q.get("i", ["0"])[0]), self._raw(),
                                                       q.get("from", ["pipeline"])[0]))
                if what == "delete":
                    return self._json(app.delete_custom(ws, self._body().get("what", "")))
                if what == "transfer":
                    return self._json(app.transfer_texture(ws))
                if what == "layout":
                    if q.get("seams"):
                        return self._json(app.relayout_texture(ws, {"seams": self._raw(), "from": q.get("from", [""])[0],
                                                                    "preview": "image" if q.get("preview") else None}))
                    return self._json(app.relayout_texture(ws, self._body()))
                if what == "op":  # cap / plane / remesh / sanitize: mesh in, mesh out, nothing saved
                    from ..meshops import run

                    if q.get("kind", [""])[0] == "inspect":
                        from ..meshfix import inspect_request

                        return self._json(inspect_request(self._raw()))
                    data, info = run(q.get("kind", [""])[0], self._raw(), app.clean_input(ws))
                    return self._binary(data, info)
            if len(parts) == 4 and parts[:2] == ["api", "workspaces"]:
                ws = app.ws(parts[2])
                if parts[3] == "preview":
                    return self._json(app.save_preview(ws, self._raw()))
                if parts[3] == "export":
                    return self._json(exports.export(ws, self._body()))
                if parts[3] == "orientation":
                    return self._json(app.set_orientation(ws, self._body().get("transform")))
                if parts[3] == "run":
                    body = self._body()
                    job = app.run(ws, body.get("stages", []), body.get("options", {}), body.get("pause_on"))
                    return self._json(job.snapshot(0))
                if parts[3] == "check":
                    body = self._body()
                    return self._json(app.check(ws, body.get("stages") or STAGES, body.get("options", {})))
                if parts[3] == "duplicate":
                    body = self._body()
                    dst = app.duplicate(ws, body.get("name", ""), bool(body.get("reconstruction")))
                    return self._json(app.state(dst), HTTPStatus.CREATED)
                if parts[3] == "delete":
                    return self._json(app.delete(ws, self._body().get("confirm", "")))
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
                old = app.meta(ws).get("video")
                # A scan's video is replaced only when asked (replace=1, after the page asked you),
                # never by a run that is reading it.
                if old and q.get("replace", [""])[0] != "1":
                    raise FileExistsError(f"scan {ws.root.name!r} already has a video ({Path(old).name}): "
                                          "replacing it needs confirming")
                app._editable(ws)
                fname = Path(q.get("name", ["video.mp4"])[0]).name or "video.mp4"
                dst = ws.root / ".ui" / fname
                dst.parent.mkdir(exist_ok=True)
                tmp = dst.with_name(dst.name + ".part")  # a failed upload leaves the old video whole
                remaining = int(self.headers.get("Content-Length") or 0)
                with open(tmp, "wb") as f:
                    while remaining:
                        chunk = self.rfile.read(min(remaining, 1 << 20))
                        if not chunk:
                            break
                        f.write(chunk)
                        remaining -= len(chunk)
                if remaining:
                    tmp.unlink(missing_ok=True)
                    raise ValueError("the upload was cut short: the video is unchanged")
                tmp.replace(dst)
                app.set_meta(ws, video=str(dst.relative_to(ws.root)))
                prev = (ws.root / old).resolve() if old else None
                if prev and prev != dst.resolve() and prev.parent == (ws.root / ".ui").resolve():
                    prev.unlink(missing_ok=True)  # an earlier upload it replaces (not a video elsewhere)
                return self._json(app.state(ws))
            raise KeyError(self.path)

    return Handler


def serve(root: Path, host: str = "127.0.0.1", port: int = 8765) -> None:
    app = App(root)
    httpd = ThreadingHTTPServer((host, port), make_handler(app))
    httpd.daemon_threads = True
    print(f"motiongrammetry UI on http://{host}:{port}  (workspaces in {app.root})", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        for job in app.jobs.values():
            job.cancel()
        httpd.server_close()
