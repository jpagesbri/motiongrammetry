"""Dense reconstruction, meshing and texturing with OpenMVS (CPU-capable).

Runs native OpenMVS binaries when they are on PATH, otherwise the Docker image
built from ``docker/openmvs.Dockerfile``. All paths are relative to the workspace
root, which is mounted at /work inside the container.
"""

from __future__ import annotations

import logging
import os
import re
import select
import shutil
import subprocess
import time
from pathlib import Path

from .diagnostics import EXIT_PREFLIGHT, Diag, StageError, error
from .workspace import Workspace

log = logging.getLogger(__name__)

DOCKER_IMAGE = os.environ.get("MG_OPENMVS_IMAGE", "motiongrametry/openmvs:2.3.0")


class OpenMVS:
    def __init__(self, ws: Workspace, threads: int = 0, idle_timeout_min: float = 30.0):
        self.ws = ws
        self.threads = threads
        self.idle_timeout = idle_timeout_min * 60
        self.native = shutil.which("DensifyPointCloud") is not None
        if not self.native and not shutil.which("docker"):
            raise StageError(error("openmvs-missing", "OpenMVS not found and Docker not available",
                                   "install OpenMVS, or Docker and build docker/openmvs.Dockerfile"), EXIT_PREFLIGHT)
        # Absolute paths as seen by the OpenMVS process (host or container).
        self.root = ws.root if self.native else Path("/work")

    def p(self, rel: str) -> str:
        return str(self.root / rel)

    def run(self, tool: str, *args: str) -> None:
        # Image paths inside .mvs files are stored relative to the .mvs, but resolved
        # against the working folder: keep that the folder holding the scene files.
        args = (*args, "--max-threads", str(self.threads), "-w", self.p("mvs"))
        container = None
        if self.native:
            cmd = [tool, *args]
        else:
            # A named container can be killed for sure: killing only the docker client on
            # cancel/timeout could leave OpenMVS running (and writing GBs) in the background.
            container = f"mg-{tool.lower()}-{os.getpid()}-{int(time.time() * 1000) % 10**8}"
            cmd = [
                "docker", "run", "--rm", "--name", container, "--user", f"{os.getuid()}:{os.getgid()}",
                "-v", f"{self.ws.root}:/work", "-w", "/work", DOCKER_IMAGE, tool, *args,
            ]
        log.info("$ %s %s", tool, " ".join(args))
        log_file = self.ws.mvs / f"{tool}.log"
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        lines: list[str] = []
        hung = False
        try:
            hung = not _stream_progress(tool, proc, lines, self.idle_timeout)
            proc.wait(timeout=60)
        finally:
            # Any exit path that leaves the tool running (cancel, watchdog, our own crash).
            if proc.poll() is None:
                proc.kill()
            if container:
                subprocess.run(["docker", "kill", container], capture_output=True)
            log_file.write_text("\n".join(lines))
        if hung:
            raise StageError(error("openmvs-hung", f"{tool} printed nothing for {self.idle_timeout / 60:.0f} min "
                                   "and was stopped", f"see {log_file}; raise idle_timeout if your scene is huge"))
        if proc.returncode != 0:
            raise StageError(_explain_exit(tool, proc.returncode, lines, log_file))


def _explain_exit(tool: str, rc: int, lines: list[str], log_file: Path) -> Diag:
    tail = "\n".join(lines[-12:])
    if rc in (137, -9):
        return error("out-of-memory", f"{tool} was killed (exit {rc}), most likely out of memory",
                     "raise resolution_level, use fewer keyframes, or the low-disk preset")
    if rc in (139, -11):
        return error("openmvs-crashed", f"{tool} crashed (segfault)",
                     f"often a degenerate mesh or scene; re-run clean; log: {log_file}\n{tail}")
    if any("No space left" in line for line in lines):
        return error("disk-full", f"{tool} ran out of disk space", "free space or raise resolution_level")
    return error("openmvs-failed", f"{tool} failed (exit {rc}); log: {log_file}", tail)


def _stream_progress(tool: str, proc: subprocess.Popen, lines: list[str], idle_timeout: float,
                     every: float = 1.0) -> bool:
    """Collect OpenMVS output, forwarding progress ("... (42.1%, ...)") and stage
    completions to the logger so long steps are visible while they run.

    Returns False if the tool went silent for ``idle_timeout`` seconds (hung)."""
    fd = proc.stdout.fileno()
    buf = b""
    last_log = 0.0
    last_output = time.monotonic()
    while True:
        ready, _, _ = select.select([fd], [], [], 5.0)
        if not ready:
            if time.monotonic() - last_output > idle_timeout:
                return False
            continue
        chunk = os.read(fd, 65536)
        if not chunk:
            break
        last_output = time.monotonic()
        buf += chunk
        *done, buf = re.split(rb"[\r\n]", buf)
        for raw in done:
            line = raw.decode(errors="replace").strip()
            if not line:
                continue
            lines.append(line)
            now = time.monotonic()
            if "completed" in line or "error" in line.lower():
                log.info("%s: %s", tool, _strip_stamp(line))
            elif "%" in line and now - last_log >= every:
                log.info("%s: %s", tool, _strip_stamp(line))
                last_log = now
    if buf.strip():
        lines.append(buf.decode(errors="replace").strip())
    return True


def _strip_stamp(line: str) -> str:
    # "10:31:20 [App     ] Densifying ..." -> "Densifying ..."
    return re.sub(r"^\d\d:\d\d:\d\d \[\w+\s*\]\s*", "", line)


def available() -> str | None:
    if shutil.which("DensifyPointCloud"):
        return "native"
    if shutil.which("docker"):
        probe = subprocess.run(["docker", "image", "inspect", DOCKER_IMAGE], capture_output=True)
        if probe.returncode == 0:
            return f"docker ({DOCKER_IMAGE})"
    return None


# Measured on OpenMVS 2.3: a depth map (depth + normals + confidence + view ids) takes
# ~80 bytes per pixel of the image size it is estimated at.
DMAP_BYTES_PER_PIXEL = 80


def depth_map_bytes(ws: Workspace, resolution_level: int) -> int:
    """Disk space DensifyPointCloud needs for its intermediate depth maps."""
    import pycolmap

    rec = pycolmap.Reconstruction(ws.dense / "sparse")
    total = 0
    for img in rec.images.values():
        c = img.camera
        total += (c.width >> resolution_level) * (c.height >> resolution_level)
    return total * DMAP_BYTES_PER_PIXEL


def densify_and_mesh(ws: Workspace, resolution_level: int = 1, threads: int = 0, keep_depth: bool = False,
                     idle_timeout_min: float = 30.0) -> Path:
    """Undistorted COLMAP model -> dense point cloud -> raw mesh. Returns the mesh path."""
    if not (ws.dense / "sparse").exists():
        raise RuntimeError(f"no undistorted model in {ws.dense}; run `mg sfm` first")
    ws.mvs.mkdir(parents=True, exist_ok=True)
    # DensifyPointCloud silently reuses depth maps it finds; ones left by an earlier run
    # (other poses, other resolution) would be fused into the new cloud.
    for old in ws.mvs.glob("depth*.dmap*"):
        old.unlink()
    need = depth_map_bytes(ws, resolution_level)
    free = shutil.disk_usage(ws.mvs).free
    if need * 1.15 > free:
        raise StageError(error(
            "disk-low", f"not enough disk space in {ws.mvs}: dense reconstruction needs ~{need / 1e9:.1f} GB of "
            f"depth maps, {free / 1e9:.1f} GB free",
            "raise resolution_level (each step divides it by 4), fewer keyframes, or a workspace on a bigger "
            "disk (/tmp is often RAM-backed)"), EXIT_PREFLIGHT)
    log.info("depth maps will take ~%.1f GB (%.1f GB free)", need / 1e9, free / 1e9)
    mvs = OpenMVS(ws, threads, idle_timeout_min)
    mvs.run("InterfaceCOLMAP", "-i", mvs.p("dense"), "-o", mvs.p("mvs/scene.mvs"),
            "--image-folder", mvs.p("dense/images"))
    try:
        mvs.run("DensifyPointCloud", "-i", mvs.p("mvs/scene.mvs"), "-o", mvs.p("mvs/scene_dense.mvs"),
                "--resolution-level", str(resolution_level))
    except StageError as e:
        explained = _explain_densify_failure(ws, e)
        _remove_depth_maps(ws)
        raise explained from e
    except BaseException:  # cancelled: don't leave GBs of partial depth maps behind
        _remove_depth_maps(ws)
        raise
    mvs.run("ReconstructMesh", "-i", mvs.p("mvs/scene_dense.mvs"), "-o", mvs.p("mvs/scene_mesh.ply"))
    mesh = ws.mvs / "scene_mesh.ply"
    if not mesh.exists():
        raise RuntimeError(f"ReconstructMesh produced no {mesh}")
    if not keep_depth:  # tens of MB per frame and not needed for texturing
        _remove_depth_maps(ws)
    return mesh


def _remove_depth_maps(ws: Workspace) -> None:
    for dmap in ws.mvs.glob("depth*.dmap*"):
        dmap.unlink(missing_ok=True)


def _explain_densify_failure(ws: Workspace, err: StageError) -> StageError:
    """OpenMVS reports a full disk only as unreadable depth maps in its own log file."""
    internal = sorted(ws.mvs.glob("DensifyPointCloud-*.log"), key=lambda p: p.stat().st_mtime)
    text = internal[-1].read_text(errors="replace") if internal else ""
    empty = [p for p in ws.mvs.glob("depth*.dmap*") if p.stat().st_size == 0]
    for p in empty:
        p.unlink()
    if "for reading depth-data" in text or empty:
        free = shutil.disk_usage(ws.mvs).free
        return StageError(error(
            "disk-full", f"DensifyPointCloud could not write its depth maps ({len(empty)} empty files, "
            f"{free / 1e9:.1f} GB free now): the disk most likely filled up",
            "raise resolution_level, use fewer keyframes, or a workspace on a bigger disk"))
    return err


def texture(ws: Workspace, mesh: Path, out_name: str = "textured", resolution_level: int = 0, threads: int = 0,
            idle_timeout_min: float = 30.0) -> Path:
    """Texture an (already cleaned) mesh with the dense scene's images. Returns the PLY path."""
    if not (ws.mvs / "scene_dense.mvs").exists():
        raise RuntimeError("texturing needs the OpenMVS scene (run `mg mvs`); the VGGT path has none")
    mvs = OpenMVS(ws, threads, idle_timeout_min)
    ws.mesh.mkdir(parents=True, exist_ok=True)
    rel_mesh = os.path.relpath(mesh, ws.root)
    if rel_mesh.startswith(".."):
        raise RuntimeError(f"mesh to texture must live inside the workspace: {mesh}")
    # PLY (per-face UVs + TextureFile comment): OpenMVS 2.3's OBJ exporter segfaults.
    mvs.run("TextureMesh", "-i", mvs.p("mvs/scene_dense.mvs"), "-m", mvs.p(rel_mesh),
            "-o", mvs.p(f"mesh/{out_name}.ply"), "--resolution-level", str(resolution_level),
            "--empty-color", str(0x808080))  # faces no camera saw: neutral grey, not orange
    out = ws.mesh / f"{out_name}.ply"
    if not out.exists():
        raise RuntimeError(f"TextureMesh produced no {out}")
    return out
