"""The scans the browser tests open, built in a temp folder at test time (nothing is committed).

- ``box``: what a finished run leaves, small enough to load in a moment: 12 keyframes and their
  masks, the cameras (SfM), a clean mesh (a box of about 7k faces, enough to sculpt) and a textured
  model (the same box, its texture cut into 54 scattered pieces as OpenMVS lays one out, so the
  texture-layout tools have something to rebuild). No pipeline is run and no GPU is needed.
- ``blank``: a scan made with New scan… and nothing else: no video, nothing to export.

Each stage gets its completion stamp, in pipeline order, so nothing shows as stale.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pycolmap
import trimesh
from PIL import Image, ImageDraw

from motiongrammetry.workspace import Workspace
from test_atlas import scattered_box
from test_geometry import _orbit_reconstruction

SCAN = "box"      # the finished scan
EMPTY = "blank"   # the empty one
KEYFRAMES = 12


def build_scans(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    _blank(root / EMPTY)
    _box(root / SCAN)  # made last: the most recently changed scan, the one Edit opens with no #scan


def _blank(folder: Path) -> None:
    (folder / ".ui").mkdir(parents=True)  # what New scan… makes (App.list lists folders with .ui/)
    (folder / ".ui" / "meta.json").write_text("{}")


def _box(folder: Path) -> None:
    ws = Workspace(folder)
    rec = _orbit_reconstruction(KEYFRAMES)  # cameras f00.jpg … f11.jpg on a circle around the origin
    rng = np.random.default_rng(0)
    for p in rng.uniform(-0.5, 0.5, size=(200, 3)):
        rec.add_point3D(p, pycolmap.Track(), np.array([180, 160, 120], np.uint8))

    ws.frames.mkdir(parents=True)
    ws.masks.mkdir()
    for i, image in enumerate(sorted(rec.images.values(), key=lambda im: im.name)):
        img = Image.new("RGB", (320, 240), (40 + 15 * i, 60, 90))
        ImageDraw.Draw(img).rectangle((100, 60, 220, 180), fill=(200, 170, 120))
        img.save(ws.frames / image.name, quality=85)
        mask = Image.new("L", (320, 240), 0)
        ImageDraw.Draw(mask).rectangle((96 + i, 56, 224, 184), fill=255)  # coverage changes a little
        mask.save(ws.mask_for(image.name))
    ws.stamp("frames")
    ws.record("frames", {"keyframes": KEYFRAMES})
    ws.stamp("mask")
    ws.record("mask", {"masked": KEYFRAMES})

    ws.sparse.mkdir(parents=True)
    rec.write(ws.sparse)
    ws.stamp("sfm")
    ws.record("sfm", {"registered": KEYFRAMES})

    ws.mesh.mkdir()
    scattered_box(ws.mesh)  # textured.ply + textured0.png: 54 texture pieces
    clean = trimesh.load(str(ws.mesh / "textured.ply"), process=True)  # welded: one closed surface
    clean = trimesh.Trimesh(clean.vertices, clean.faces, process=True)
    for _ in range(3):  # 108 -> 6,912 faces: room for a brush, quick to load
        clean = clean.subdivide()
    clean.visual = trimesh.visual.ColorVisuals(clean, vertex_colors=np.tile([170, 150, 120, 255], (len(clean.vertices), 1)))
    clean.export(str(ws.mesh / "mesh_clean.ply"))
    ws.stamp("clean")
    ws.record("clean", {"faces_out": int(len(clean.faces))})
    ws.stamp("texture")
    ws.record("texture", {"faces": 108})

    (folder / ".ui").mkdir()
    (folder / ".ui" / "meta.json").write_text(json.dumps({}))
