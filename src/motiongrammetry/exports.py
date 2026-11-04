"""Exports: the files people take out of a scan, built when asked and kept until something changes.

The pipeline and the editor only keep their working files (meshes in the reconstruction's frame,
PLY + PNG). Every file with an orientation, a size or a format baked in (GLB, OBJ, STL, 3MF, ...) is
made here, on request, into .ui/exports/. Next to each file, <file>.json records what it was made
from (the source files, the orientation, the cameras: their times and sizes) and with which
settings: the same request again is answered with that file at once; once an input changes, the
file is outdated and the next request builds it anew.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import numpy as np

from .workspace import RAW_MESH, Workspace

# content: (label, formats; the first is the default)
CONTENTS = {
    "textured": ("Textured model", ("glb", "obj")),
    "mesh": ("Clean mesh, vertex colours", ("glb", "ply", "obj")),
    "print": ("3D print", ("stl", "3mf")),
    "raw": ("Raw mesh before cleanup", ("ply", "obj", "glb")),
    "cloud": ("Dense point cloud", ("ply",)),
    "cameras": ("Cameras and sparse points", ("colmap",)),
}
LEGACY_FORMATS = {"stl": ("print", "stl"), "glb_textured": ("textured", "glb"), "glb_mesh": ("mesh", "glb"),
                  "obj": ("textured", "obj"), "ply": ("mesh", "ply")}
PRINT_SETTINGS = {"height_mm": float, "base": float, "mode": ("auto", "solid", "shell"), "stand": ("auto", "none"),
                  "min_wall": float, "shell": float, "shell_side": ("inward", "outward", "both")}
BUILD = 1  # bump when a change here makes earlier files wrong: they are built again


def sources_of(ws: Workspace, custom: bool) -> dict[str, Path | None]:
    """What each content is made from, None when this scan (or version) does not have it."""
    c = ws.mesh / "custom"
    mesh = c / "mesh_clean.ply" if custom else ws.mesh / "mesh_clean.ply"
    textured = c / "textured.ply" if custom else ws.mesh / "textured.ply"
    used = ws.load_manifest().get("clean", {}).get("input")
    raws = [ws.root / RAW_MESH[p] for p in [ws.mesh_producer(used), "mvs", "vggt"] if p]
    have = lambda p: p if p is not None and p.exists() else None  # noqa: E731
    return {"textured": have(textured), "mesh": have(mesh), "print": have(mesh),
            "raw": None if custom else next((p for p in raws if p.exists()), None),
            "cloud": None if custom else have(ws.root / "mvs/scene_dense.ply"),
            "cameras": None if custom else have(ws.sparse / "images.bin")}


def _texture_from(ws: Workspace) -> str | None:
    p = ws.mesh / "custom" / "info.json"
    return json.loads(p.read_text()).get("textured_from") if p.exists() else None


def _reference(ws: Workspace, content: str, custom: bool, sources: dict) -> Path:
    """The mesh the placement (orientation, height) is fitted to, so contents exported together line up."""
    src = sources[content]
    ref = sources["mesh"] or src
    if content == "textured" and custom and _texture_from(ws) != "custom":
        ref = ws.mesh / "mesh_clean.ply"  # a custom texture painted on the pipeline model
    if content in ("raw", "cloud"):
        ref = ws.mesh / "mesh_clean.ply" if (ws.mesh / "mesh_clean.ply").exists() else src
    return ref


def _inputs(ws: Workspace, content: str, custom: bool, sources: dict) -> list:
    """What a file depends on, as (path, time, size): its source, the mesh it is placed by, and what
    the placement reads (the orientation set by hand, the cameras, the dense cloud's floor)."""
    from .clean import ORIENTATION

    src = sources[content]
    paths = [src, ws.sparse / "images.bin"]
    if content == "cameras":
        paths += sorted(ws.sparse.glob("*.bin"))
    else:
        paths += [_reference(ws, content, custom, sources), ws.root / ORIENTATION, ws.mvs / "scene_dense.ply"]
    if content == "textured":
        paths += sorted(src.parent.glob(src.stem + "[0-9]*.png"))  # the texture images it names
    out = []
    for p in dict.fromkeys(paths):
        if p is not None and p.exists():
            st = p.stat()
            out.append([str(p.relative_to(ws.root)), st.st_mtime_ns, st.st_size])
    return out


def _parse(body: dict):
    """(source, items [(content, format)], up, height, print settings) from an export request."""
    source, opts = body.get("source", "pipeline"), body.get("options") or {}
    items = body.get("items")
    if items is None and body.get("format") in LEGACY_FORMATS:
        content, fmt = LEGACY_FORMATS[body["format"]]
        items = [{"content": content, "format": fmt}]
    if not items or source not in ("pipeline", "custom"):
        raise ValueError("unknown export")
    items = [(i.get("content"), i.get("format")) for i in items]
    for content, fmt in items:
        if content not in CONTENTS or fmt not in CONTENTS[content][1]:
            raise ValueError(f"unknown export: {content} as {fmt}")
    if len({c for c, _ in items}) != len(items):
        raise ValueError("each content once")
    up = opts.get("up", "auto")
    if up not in ("auto", "plane", "camera", "none"):
        raise ValueError("up: auto, plane, camera or none")
    print_kw = {}
    for k, kind in PRINT_SETTINGS.items():
        if opts.get(k) in (None, ""):
            continue
        if isinstance(kind, tuple) and opts[k] not in kind:
            raise ValueError(f"{k}: one of {', '.join(kind)}")
        print_kw[k] = opts[k] if isinstance(kind, tuple) else float(opts[k])
    height = opts.get("height_mm")
    height = None if height in (None, "") else float(height)
    return source, items, up, height, print_kw


def _plan(ws: Workspace, body: dict) -> dict:
    source, items, up, height, print_kw = _parse(body)
    d = defaults(ws)  # the scan's own print settings and up, under what the request sets
    print_kw = {**{k: d[k] for k in PRINT_SETTINGS if k in d and k != "height_mm"}, **print_kw}
    if height is None and "height_mm" not in print_kw:
        print_kw["height_mm"] = d.get("height_mm", 100.0)  # a print always has a size
    if not (body.get("options") or {}).get("up"):
        up = d["up"]
    custom = source == "custom"
    sources = sources_of(ws, custom)
    for content, _ in items:
        if sources[content] is None:
            raise FileNotFoundError(f"{'custom ' if custom else ''}{CONTENTS[content][0].lower()}")
    stem = f"{ws.root.name}-{'custom-' if custom else ''}"
    plan = []
    for content, fmt in items:
        name = {"obj": f"{content}-obj.zip", "colmap": f"{content}-colmap.zip"}.get(fmt, f"{content}.{fmt}")
        settings = {"build": BUILD, "format": fmt}
        if content != "cameras":
            settings.update(up=up, height_mm=height)
        if content == "print":
            settings["print"] = print_kw
        plan.append({"content": content, "format": fmt, "file": stem + name,
                     "key": {"inputs": _inputs(ws, content, custom, sources), "settings": settings}})
    return {"source": source, "custom": custom, "sources": sources, "stem": stem, "items": plan,
            "up": up, "height": height, "print_kw": print_kw}


def _out_dir(ws: Workspace) -> Path:
    return ws.root / ".ui" / "exports"


def _cached(ws: Workspace, item: dict) -> tuple[str, dict | None]:
    """"ready" (built with these inputs and settings), "outdated" (built, from something else) or
    "none"; with the file's record when there is one."""
    d = _out_dir(ws)
    side, f = d / (item["file"] + ".json"), d / item["file"]
    if not side.exists() or not f.exists():
        return "none", None
    rec = json.loads(side.read_text())
    preview = rec["entry"].get("preview")
    if rec.get("key") == item["key"] and (not preview or (d / preview).exists()):
        return "ready", rec
    return "outdated", rec


def status(ws: Workspace, body: dict) -> dict:
    """Which of the requested files are ready, outdated or not built yet (nothing is built). A ready
    one also names its file (and the STL shown for a 3MF), so the Export window can show it as it is."""
    plan = _plan(ws, body)
    out = []
    for item in plan["items"]:
        state, rec = _cached(ws, item)
        row = {"content": item["content"], "format": item["format"], "state": state,
               "size": (_out_dir(ws) / item["file"]).stat().st_size if state != "none" else None}
        if state == "ready":
            row.update(file=rec["entry"]["file"], preview=rec["entry"].get("preview"))
        out.append(row)
    return {"source": plan["source"], "items": out}


def export(ws: Workspace, body: dict) -> dict:
    """Build the chosen contents (each in its format), from the pipeline or custom version, into
    .ui/exports/ (files already built from the same inputs and settings are reused): one file, or a
    zip of them all. A lone {"format"} is one content (older API)."""
    plan = _plan(ws, body)
    out_dir = _out_dir(ws)
    out_dir.mkdir(parents=True, exist_ok=True)
    files = []
    for item in plan["items"]:
        state, rec = _cached(ws, item)
        if state == "ready":
            entry = {**rec["entry"], "cached": True}
        else:
            entry = _build(ws, item, plan, out_dir)
            (out_dir / (item["file"] + ".json")).write_text(json.dumps({"key": item["key"], "entry": entry}))
            entry = {**entry, "cached": False}
        files.append({**entry, "size": (out_dir / entry["file"]).stat().st_size})
    source = plan["source"]
    if len(files) == 1:
        return {**files[0], "files": files, "source": source, "bundle": False}
    # several: one zip, each content in its own folder (an OBJ's or COLMAP model's zip unpacked into it)
    dst = out_dir / f"{plan['stem']}export.zip"
    tmp = dst.with_name("tmp-" + dst.name)
    with zipfile.ZipFile(tmp, "w") as z:
        for f in files:
            p = out_dir / f["file"]
            if p.suffix == ".zip":
                with zipfile.ZipFile(p) as inner:
                    for n in inner.namelist():
                        z.writestr(f"{f['content']}/{n}", inner.read(n), zipfile.ZIP_DEFLATED)
            else:
                z.write(p, f"{f['content']}/{p.name}", zipfile.ZIP_STORED if p.suffix in (".glb", ".3mf") else zipfile.ZIP_DEFLATED)
    tmp.replace(dst)
    return {"file": dst.name, "size": dst.stat().st_size, "files": files, "source": source, "bundle": True,
            "cached": all(f["cached"] for f in files), "issues": [i for f in files for i in f["issues"]]}


def _build(ws: Workspace, item: dict, plan: dict, out_dir: Path) -> dict:
    import trimesh

    from .clean import export_glb, export_print_stl, workspace_alignment, write_3mf

    content, fmt, custom, sources = item["content"], item["format"], plan["custom"], plan["sources"]
    up, height = plan["up"], plan["height"]
    src = sources[content]
    dst = out_dir / item["file"]
    tmp = dst.with_name("tmp-" + dst.name)
    entry = {"content": content, "label": CONTENTS[content][0], "format": fmt, "file": dst.name, "issues": []}
    if content == "print":
        stl = dst.with_suffix(".stl") if fmt == "stl" else out_dir / f"{plan['stem']}{content}-preview.stl"
        info = export_print_stl(src, tmp.with_suffix(".stl"), workspace_alignment(ws, src, up=up), ws=ws, **plan["print_kw"])
        tmp.with_suffix(".stl").replace(stl)
        if fmt == "3mf":
            write_3mf(trimesh.load(str(stl), force="mesh", process=False), tmp)
            tmp.replace(dst)
            entry["preview"] = stl.name  # the browser has no 3MF loader: the same mesh as STL
        entry.update(issues=info.pop("print_issues"), info=info)
    elif content == "cameras":  # the COLMAP model as sfm wrote it, in the reconstruction's frame
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
            for p in sorted(ws.sparse.glob("*.bin")):
                z.write(p, f"sparse/0/{p.name}")
        tmp.replace(dst)
        entry["info"] = {"frame": "reconstruction"}
    else:
        # placed like the clean mesh, so contents exported together line up; a height sets the
        # real size of the clean model (glTF in metres, the others in millimetres)
        ref = _reference(ws, content, custom, sources)
        T = workspace_alignment(ws, ref, up=up)
        T = np.eye(4) if T is None else T
        if height is not None:
            v = np.asarray(trimesh.load(str(ref), force="mesh", process=False).vertices)
            ext = float(np.ptp(v @ T[:3, :3].T, axis=0)[1])
            unit = 0.001 if fmt == "glb" else 1.0
            if ext > 0:
                T = np.diag([*(3 * [height * unit / ext]), 1.0]) @ T
        if fmt == "glb":
            export_glb(src, tmp.with_suffix(".glb"), T)
            tmp.with_suffix(".glb").replace(tmp)
        elif content == "cloud":  # Open3D: OpenMVS adds per-point view lists trimesh cannot read
            import open3d as o3d

            pc = o3d.io.read_point_cloud(str(src))
            pc.transform(T)
            o3d.io.write_point_cloud(str(tmp.with_suffix(".ply")), pc)
            tmp.with_suffix(".ply").replace(tmp)
        elif fmt == "ply":
            m = trimesh.load(str(src), force="mesh", process=False)
            m.apply_transform(T)
            m.export(str(tmp), file_type="ply")
        else:  # OBJ (+ MTL + texture images when textured), zipped
            from trimesh.exchange.obj import export_obj

            m = trimesh.load(str(src), force="mesh", process=False)
            m.apply_transform(T)
            textured = content == "textured"
            text, extra = export_obj(m, include_texture=textured, include_color=not textured,
                                     return_texture=True, mtl_name="model.mtl")
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
                z.writestr("model.obj", text)
                for n, data in extra.items():
                    z.writestr(n, data)
        tmp.replace(dst)
        entry["info"] = {"up": up, "height_mm": height}
    return entry


def defaults(ws: Workspace) -> dict:
    """The Export window's starting settings for this scan: its print_* and up options, as its last
    clean run had them (the assisted run sets them from what was filmed), else the defaults."""
    from .clean import print_kwargs
    from .options import resolve

    last = next((h for h in reversed(ws.history()) if h["stage"] == "clean" and h["status"] != "skipped"), {})
    o = resolve({k: v for k, v in (last.get("options") or {}).items() if k.startswith("print_") or k == "up"})
    kw = print_kwargs({k: v for k, v in o.items() if k.startswith("print_")})
    return {**kw, "base": kw.get("base", 0.02), "up": o.get("up", "auto")}


def export_file(ws: Workspace, name: str) -> Path:
    p = _out_dir(ws) / name
    if "/" in name or name.startswith(("tmp-", ".")) or name.endswith(".json") or not p.is_file():
        raise FileNotFoundError(name)
    return p


def print_check(ws: Workspace) -> dict:
    """Per version (pipeline, custom): the report of its last 3D-print export, and whether it still
    applies (made from the files as they are now)."""
    d = _out_dir(ws)
    out = {}
    for source in ("pipeline", "custom"):
        custom = source == "custom"
        stem = f"{ws.root.name}-{'custom-' if custom else ''}print."
        recs = sorted((p for p in (d / (stem + "stl.json"), d / (stem + "3mf.json")) if p.exists()),
                      key=lambda p: p.stat().st_mtime)
        if not recs or sources_of(ws, custom)["print"] is None:
            continue
        rec = json.loads(recs[-1].read_text())
        now = _inputs(ws, "print", custom, sources_of(ws, custom))
        info = rec["entry"].get("info") or {}
        out[source] = {"issues": rec["entry"].get("issues", []), "size_mm": info.get("print_size_mm"),
                       "watertight": info.get("print_watertight"), "current": rec["key"]["inputs"] == now}
    return out
