"""Third-party software, models and their licenses: the About page and `mg notices`.

Python packages are read from the installed distributions (name, version, declared license and the
license files they ship), so the list follows whatever is installed. Everything else the app runs
or loads (COLMAP inside pycolmap, OpenMVS in its container, three.js in the browser, the AI models
downloaded at first use) is described in EXTERNAL, with the license texts kept in licenses/.
"""

from __future__ import annotations

import importlib.metadata as md
import re
from pathlib import Path

LICENSES = Path(__file__).parent / "licenses"

# Non-Python parts. "text": a file in licenses/, or "pkg:<dist>" for one shipped by a Python package.
EXTERNAL = [
    {"id": "colmap", "name": "COLMAP", "version": "bundled with pycolmap", "license": "BSD-3-Clause",
     "role": "camera positions (structure from motion) and undistortion, through pycolmap",
     "url": "https://colmap.github.io", "source": "https://github.com/colmap/colmap", "text": "colmap.txt",
     "copyright": "Copyright (c), ETH Zurich and UNC Chapel Hill"},
    {"id": "openmvs", "name": "OpenMVS", "version": "2.3.0", "license": "AGPL-3.0",
     "role": "depth maps, dense cloud, raw mesh and texturing; built from unmodified source in its own container "
             "(docker/openmvs.Dockerfile) and run as separate programs",
     "url": "https://cdcseacave.github.io/openMVS", "source": "https://github.com/cdcseacave/openMVS/tree/v2.3.0",
     "text": "openmvs-AGPL-3.0.txt", "copyright": "Copyright (c) Dan Cernea and the OpenMVS contributors",
     "note": "The complete corresponding source is the v2.3.0 tag linked here, built unchanged (with CUDA in the "
             "motiongrametry Docker image, docker/Dockerfile; CPU-only in docker/openmvs.Dockerfile). The Docker "
             "image also carries that source and VCGLib's in /usr/share/doc/openmvs. Both images contain Ubuntu "
             "24.04 libraries (Boost, CGAL, OpenCV, ...) under their own licenses, in /usr/share/doc/*/copyright."},
    {"id": "vcglib", "name": "VCGLib", "version": "master at build time", "license": "GPL-3.0",
     "role": "geometry library compiled into OpenMVS",
     "url": "https://vcg.isti.cnr.it/vcglib", "source": "https://github.com/cdcseacave/VCG", "text": "vcglib-GPL-3.0.txt",
     "copyright": "Copyright (C) Visual Computing Lab, ISTI - CNR"},
    {"id": "nvidia-cuda", "name": "NVIDIA CUDA runtime and cuDNN", "version": "13.2 / 9 (Docker image, nvidia variant)",
     "license": "NVIDIA proprietary", "role": "GPU acceleration in the Docker image: COLMAP, ONNX Runtime (masks), OpenMVS",
     "url": "https://hub.docker.com/r/nvidia/cuda", "source": None, "text": "nvidia-cuda-base.txt",
     "copyright": "Copyright (c) NVIDIA Corporation"},
    {"id": "three", "name": "three.js", "version": "0.170.0", "license": "MIT",
     "role": "the 3D views in the browser (loaded from the jsDelivr CDN)",
     "url": "https://threejs.org", "source": "https://github.com/mrdoob/three.js", "text": "three.js.txt",
     "copyright": "Copyright © 2010-2024 three.js authors"},
    {"id": "three-mesh-bvh", "name": "three-mesh-bvh", "version": "0.8.3", "license": "MIT",
     "role": "fast picking for the sculpt and paint brushes (loaded from the jsDelivr CDN)",
     "url": "https://github.com/gkjohnson/three-mesh-bvh", "source": "https://github.com/gkjohnson/three-mesh-bvh",
     "text": "three-mesh-bvh.txt", "copyright": "Copyright (c) 2018 Garrett Johnson"},
]
# AI models downloaded on first use (not shipped with the app).
MODELS = [
    {"id": "birefnet", "name": "BiRefNet (general, general-lite, portrait)", "license": "MIT",
     "role": "subject masks (mask_model object, object-hq, person, person-hq), via rembg",
     "url": "https://github.com/ZhengPeng7/BiRefNet", "text": "birefnet.txt", "copyright": "Copyright (c) 2024 ZhengPeng"},
    {"id": "isnet", "name": "IS-Net (DIS, general use)", "license": "Apache-2.0",
     "role": "subject masks: fallback of mask_model object, via rembg",
     "url": "https://github.com/xuebinqin/DIS", "text": "isnet-dis-Apache-2.0.txt", "copyright": "Xuebin Qin et al."},
    {"id": "u2net", "name": "U²-Net (human segmentation)", "license": "Apache-2.0",
     "role": "subject masks: fallback of mask_model person, via rembg",
     "url": "https://github.com/xuebinqin/U-2-Net", "text": "u2net-Apache-2.0.txt", "copyright": "Xuebin Qin et al."},
    {"id": "vggt-1b", "name": "VGGT-1B weights (facebook/VGGT-1B)", "license": "CC-BY-NC-4.0",
     "role": "the optional vggt stage (feed-forward reconstruction)",
     "url": "https://huggingface.co/facebook/VGGT-1B", "text": "CC-BY-NC-4.0.txt", "copyright": "Meta Platforms, Inc.",
     "note": "Non-commercial use only. For commercial use Meta publishes facebook/VGGT-1B-Commercial (gated, under the "
             "VGGT License and its acceptable use policy). Publications using VGGT should cite it."},
]
# What the project itself declares, and the extras that add optional features.
EXTRAS = {"mask": "subject masks", "vggt": "VGGT stage", "dev": "tests"}
# Packages whose metadata says too little, or that are installed by hand (see pyproject.toml).
DECLARED = {"vggt": ("VGGT License (Meta), see its license file", "optional: VGGT stage"),
            "cuda-toolkit": ("LicenseRef-NVIDIA-Proprietary", None)}
_NAME = re.compile(r"[-_.]+")


def _key(name: str) -> str:
    return _NAME.sub("-", name).lower()


def _license(dist: md.Distribution) -> str:
    m = dist.metadata
    if m.get("License-Expression"):
        return m["License-Expression"]
    lic = (m.get("License") or "").strip()
    # some put the whole text in License: keep only a short name then
    if lic and "\n" not in lic and len(lic) < 60 and lic.upper() != "UNKNOWN":
        return lic
    cls = [c.split("::")[-1].strip() for c in (m.get_all("Classifier") or []) if c.startswith("License ::")]
    return "; ".join(cls) or ("see license files" if _license_files(dist) else "unknown")


def _license_files(dist: md.Distribution) -> list:
    return [f for f in (dist.files or []) if re.search(r"(LICEN[CS]E|COPYING|NOTICE|AUTHORS)", f.name, re.I)
            and not str(f).endswith((".py", ".pyc"))]


def _closure(roots: list[str], dists: dict[str, md.Distribution], extras: dict[str, set] | None = None) -> set[str]:
    """Installed distributions needed by ``roots`` (following their requirements and extras)."""
    from packaging.requirements import Requirement

    seen, todo = set(), [(_key(r), set((extras or {}).get(_key(r), ()))) for r in roots]
    while todo:
        name, ex = todo.pop()
        d = dists.get(name)
        if d is None or (name, frozenset(ex)) in seen:
            continue
        seen.add((name, frozenset(ex)))
        for req in d.requires or []:
            try:
                r = Requirement(req)
            except ValueError:
                continue
            ok = r.marker is None or any(r.marker.evaluate({"extra": e}) for e in (ex or {""}))
            if ok and _key(r.name) in dists:
                todo.append((_key(r.name), set(r.extras)))
    return {n for n, _ in seen}


def python_packages() -> list[dict]:
    """Every installed distribution, marked by why it is there: the app itself, an optional feature
    (mask, vggt) or other tools installed alongside (e.g. tests)."""
    dists = {}
    for d in md.distributions():
        dists.setdefault(_key(d.metadata["Name"]), d)
    core = _closure(["motiongrametry"], dists) - {"motiongrametry"}
    groups = {extra: _closure(["motiongrametry"], dists, {"motiongrametry": {extra}}) - core - {"motiongrametry"}
              for extra in EXTRAS}
    out = []
    for key, d in sorted(dists.items()):
        if key == "motiongrametry":
            continue
        group = "required" if key in core else next((f"optional: {EXTRAS[e]}" for e, g in groups.items() if key in g), "installed alongside")
        m = d.metadata
        lic, grp = DECLARED.get(key, (None, None))
        group = grp or group
        url = m.get("Home-page") or next((u.split(",", 1)[1].strip() for u in (m.get_all("Project-URL") or [])
                                          if u.lower().startswith(("homepage", "source", "repository"))), None)
        out.append({"id": key, "name": m["Name"], "version": d.version, "license": lic or _license(d), "group": group,
                    "url": url, "files": [str(f) for f in _license_files(d)]})
    return out


def license_text(kind: str, ident: str) -> str:
    """The full license text(s) of one component: kind "python", "external" or "model"."""
    if kind == "python":
        d = next((d for d in md.distributions() if _key(d.metadata["Name"]) == _key(ident)), None)
        if d is None:
            raise KeyError(ident)
        parts = [f"===== {f} =====\n{Path(d.locate_file(f)).read_text(errors='replace')}" for f in _license_files(d)]
        if not parts:
            parts = [f"{d.metadata['Name']} {d.version}: no license file shipped; declared license: {_license(d)}"]
        return "\n\n".join(parts)
    item = next((x for x in (EXTERNAL if kind == "external" else MODELS) if x["id"] == ident), None) if kind in ("external", "model") else None
    if item is None:
        raise KeyError(ident)
    return f"{item['name']} ({item['license']})\n{item.get('copyright', '')}\n\n" + (LICENSES / item["text"]).read_text()


def about() -> dict:
    try:
        version = md.version("motiongrametry")
    except md.PackageNotFoundError:
        version = "dev"
    return {"name": "motiongrametry", "version": version, "external": EXTERNAL, "models": MODELS, "python": python_packages()}


def notices() -> str:
    """THIRD_PARTY_NOTICES: every component with its license text, for redistribution."""
    a = about()
    out = [f"motiongrametry {a['version']}: third-party software notices", "",
           "This software uses the components below. Each is the work of its authors and is used under its own "
           "license, reproduced in full in this file.", ""]
    for kind, items in (("external", a["external"]), ("model", a["models"]), ("python", a["python"])):
        for it in items:
            out += ["=" * 78, f"{it['name']} {it.get('version', '')}".strip(), f"License: {it['license']}",
                    *([f"Used for: {it['role']}"] if it.get("role") else []),
                    *([f"Source: {it.get('source') or it.get('url')}"] if it.get("source") or it.get("url") else []),
                    *([f"Note: {it['note']}"] if it.get("note") else []), "", license_text(kind, it["id"]), ""]
    return "\n".join(out)
