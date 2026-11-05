"""On-disk layout shared by every pipeline stage.

work/
  frames/          selected keyframes (JPEG)
  masks/           subject masks, COLMAP convention: <frame>.jpg.png, 0 = background
  colmap/          database.db + sparse/0 (raw SfM, distorted camera model)
  dense/           undistorted images + sparse model (input for OpenMVS and 2DGS)
  mvs/             OpenMVS intermediates
  vggt/            feed-forward depth maps and fused mesh
  mesh/            results (mesh_clean.ply, textured.ply + PNG; custom/: the editor's versions)
  .ui/exports/     files exported on request (exports.py), each with what it was made from
  logs/            one full log per stage run + history.jsonl
  manifest.json    per-stage stats and diagnostics
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

STAGES = ["frames", "mask", "sfm", "mvs", "vggt", "clean", "texture"]
# Which earlier stage's output each stage consumes (used for staleness). SfM also consumes the masks
# when it ran with mask_sfm (see sfm_upstream).
UPSTREAM = {"mask": ["frames"], "sfm": ["frames"], "mvs": ["sfm"], "vggt": ["frames"],
            "clean": ["sfm", "mask", "mvs", "vggt"], "texture": ["clean", "mvs"]}
# mvs and vggt are alternative producers of the raw mesh; clean depends only on the one it used.
RAW_MESH = {"mvs": "mvs/scene_mesh.ply", "vggt": "vggt/tsdf_mesh.ply"}


@dataclass(frozen=True)
class Workspace:
    root: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", Path(self.root).resolve())
        self.root.mkdir(parents=True, exist_ok=True)

    @property
    def frames(self) -> Path:
        return self.root / "frames"

    @property
    def masks(self) -> Path:
        return self.root / "masks"

    @property
    def colmap(self) -> Path:
        return self.root / "colmap"

    @property
    def sparse(self) -> Path:
        return self.colmap / "sparse" / "0"

    @property
    def dense(self) -> Path:
        return self.root / "dense"

    @property
    def mvs(self) -> Path:
        return self.root / "mvs"

    @property
    def vggt(self) -> Path:
        return self.root / "vggt"

    @property
    def mesh(self) -> Path:
        return self.root / "mesh"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def manifest_path(self) -> Path:
        return self.root / "manifest.json"

    def frame_paths(self) -> list[Path]:
        return sorted(p for p in self.frames.glob("*.jpg"))

    def has_masks(self) -> bool:
        return self.masks.is_dir() and any(self.masks.glob("*.png"))

    def mask_for(self, image_name: str) -> Path:
        return self.masks / f"{image_name}.png"

    def record(self, stage: str, info: dict) -> None:
        manifest = self.load_manifest()
        manifest[stage] = info
        self._write_manifest(manifest)

    def _write_manifest(self, manifest: dict) -> None:
        tmp = self.manifest_path.with_suffix(".tmp")  # atomic: a crash never leaves half a file
        tmp.write_text(json.dumps(manifest, indent=2, default=str))
        tmp.replace(self.manifest_path)

    def load_manifest(self) -> dict:
        if self.manifest_path.exists():
            try:
                return json.loads(self.manifest_path.read_text())
            except json.JSONDecodeError:  # interrupted write: start over rather than crash
                return {}
        return {}

    def record_diagnostics(self, stage: str, diags: list[dict]) -> None:
        manifest = self.load_manifest()
        manifest.setdefault("diagnostics", {})[stage] = diags
        self._write_manifest(manifest)

    def append_history(self, entry: dict) -> None:
        self.logs.mkdir(exist_ok=True)
        with open(self.logs / "history.jsonl", "a") as f:
            f.write(json.dumps(entry, default=str) + "\n")

    def history(self) -> list[dict]:
        p = self.logs / "history.jsonl"
        if not p.exists():
            return []
        out = []
        for line in p.read_text().splitlines():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    def new_log_path(self, stage: str) -> Path:
        self.logs.mkdir(exist_ok=True)
        return self.logs / f"{time.strftime('%Y%m%d-%H%M%S')}-{stage}.log"

    # ---- freshness ------------------------------------------------------------------------
    def stamp(self, stage: str, by: str | None = None) -> None:
        """Mark a stage as completed now (file mtimes alone miss in-place overwrites).
        ``by`` records another stage that produced this one's output (vggt writes the cameras)."""
        d = self.root / ".stamps"
        d.mkdir(exist_ok=True)
        (d / stage).write_text(time.strftime("%Y-%m-%d %H:%M:%S") + (f" by {by}" if by else ""))

    def output_mtime(self, stage: str) -> float | None:
        stamp = self.root / ".stamps" / stage
        if stamp.exists():
            return stamp.stat().st_mtime
        p = {
            "frames": self.frames, "mask": self.masks, "sfm": self.sparse / "images.bin",
            "mvs": self.mvs / "scene_mesh.ply", "vggt": self.vggt / "tsdf_mesh.ply",
            "clean": self.mesh / "mesh_clean.ply", "texture": self.mesh / "textured.ply",
        }[stage]
        return p.stat().st_mtime if p.exists() else None

    def raw_mesh(self) -> Path | None:
        """The newest raw mesh: what clean uses unless given another one."""
        cands = [self.root / p for p in RAW_MESH.values() if (self.root / p).exists()]
        return max(cands, key=lambda p: p.stat().st_mtime) if cands else None

    def mesh_producer(self, mesh: Path | str | None) -> str | None:
        """'mvs' or 'vggt' for their raw meshes, None for anything else (e.g. a 2DGS mesh)."""
        return next((s for s, p in RAW_MESH.items() if mesh is not None and Path(mesh) == self.root / p), None)

    def cameras_from(self) -> str | None:
        """Which stage wrote the current camera model: 'sfm', 'vggt' (it writes one too) or None."""
        if not (self.sparse / "images.bin").exists():
            return None
        stamp = self.root / ".stamps" / "sfm"
        if stamp.exists():
            return "vggt" if stamp.read_text().endswith(" by vggt") else "sfm"
        # No stamp (sfm running, or a workspace from before stamps): vggt rewrites images.bin
        # right before it finishes its mesh.
        vggt = self.output_mtime("vggt")
        return "vggt" if vggt is not None and vggt >= (self.sparse / "images.bin").stat().st_mtime - 1 else "sfm"

    def sfm_upstream(self, manifest: dict | None = None) -> list[str]:
        """What the cameras on disk were computed from: the keyframes, plus the masks when SfM ran
        with mask_sfm (its manifest's ``masked_features``). Read from how SfM actually ran, not from
        the current options, as clean's raw mesh is: changing the option alone outdates nothing.
        Cameras written by VGGT used no masks."""
        m = (self.load_manifest() if manifest is None else manifest).get("sfm") or {}
        if m.get("masked_features") and self.cameras_from() != "vggt":
            return ["frames", "mask"]
        return ["frames"]

    def upstream(self, manifest: dict | None = None) -> dict[str, list[str]]:
        """UPSTREAM for this scan: with SfM's inputs as it ran here (sfm_upstream)."""
        return {**UPSTREAM, "sfm": self.sfm_upstream(manifest)}

    def stale_stages(self) -> list[str]:
        """Stages whose output is older than their input: re-running an upstream stage
        (new keyframes, new SfM gauge, new masks for an SfM that used them) leaves downstream
        files out of step."""
        out = {s: self.output_mtime(s) for s in STAGES}
        manifest = self.load_manifest()
        used = manifest.get("clean", {}).get("input")
        source = self.mesh_producer(used if used else self.raw_mesh())
        upstream = self.upstream(manifest)
        stale: set[str] = set()
        for stage in STAGES:  # topological order, so staleness propagates
            if out[stage] is None:
                continue
            ups = upstream.get(stage, [])
            if stage == "clean":
                ups = [u for u in ups if u not in RAW_MESH or u == source]
            for up in ups:
                if up in stale or (out[up] is not None and out[up] > out[stage] + 1.0):
                    stale.add(stage)
        return [s for s in STAGES if s in stale]
