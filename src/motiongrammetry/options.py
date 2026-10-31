"""Every tunable of the pipeline, in one table shared by the CLI, the web UI and presets.

Resolution order for a value: explicit flag / UI field > preset > default here.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Opt:
    name: str
    type: type
    default: object
    help: str
    stages: tuple[str, ...]
    choices: tuple | None = None
    group: str = "basic"  # "basic" | "advanced"


OPTIONS: list[Opt] = [
    # general
    Opt("threads", int, 0, "CPU threads for COLMAP/OpenMVS (0 = all)", ("sfm", "mvs", "texture"), group="advanced"),
    Opt("idle_timeout", float, 30.0, "minutes without OpenMVS output before the step is killed as hung",
        ("mvs", "texture"), group="advanced"),
    Opt("strict", bool, False, "treat quality warnings as failures (stop the pipeline)", ("*",), group="advanced"),
    Opt("force", bool, False, "run even if pre-flight checks fail (stale inputs, low disk, ...)", ("*",),
        group="advanced"),
    # frames
    Opt("num", int, 150, "target number of keyframes", ("frames",)),
    Opt("sample_fps", float, 10.0, "analysis rate; raise for short or fast captures", ("frames",), group="advanced"),
    Opt("max_side", int, 2000, "longest side of saved keyframes (px)", ("frames",), group="advanced"),
    Opt("blur_reject", float, 0.35, "drop keyframes sharper than only this fraction of their neighbourhood",
        ("frames",), group="advanced"),
    # mask
    Opt("mask_model", str, "object", "segmentation preset or rembg model name", ("mask",),
        ("object", "object-hq", "person", "person-hq")),
    Opt("mask_threshold", float, 0.5, "alpha threshold for the subject mask", ("mask",), group="advanced"),
    Opt("mask_dilate", int, 4, "grow masks by this many px (protects thin parts from carving)", ("mask",),
        group="advanced"),
    # sfm
    Opt("mapper", str, "global", "GLOMAP (fast) or incremental (robust on short/odd sequences)", ("sfm",),
        ("global", "incremental"), group="advanced"),
    Opt("matcher", str, "sequential", "sequential + loop pairs (video) or exhaustive (slow, most robust)",
        ("sfm",), ("sequential", "exhaustive"), group="advanced"),
    Opt("camera_model", str, "RADIAL", "COLMAP camera model (single focal; OPENCV can stretch the model)",
        ("sfm",), ("SIMPLE_RADIAL", "RADIAL", "OPENCV", "PINHOLE"), group="advanced"),
    Opt("max_image_size", int, 1600, "feature extraction resolution (px)", ("sfm",), group="advanced"),
    Opt("sift_peak", float, 0.0015, "SIFT detection threshold; lower = more features on dark/low-texture scenes",
        ("sfm",), group="advanced"),
    Opt("max_features", int, 8192, "max SIFT features per frame", ("sfm",), group="advanced"),
    Opt("overlap", int, 12, "sequential matching window (frames)", ("sfm",), group="advanced"),
    Opt("loop_stride", int, 5, "match every Nth frame against each other to close the orbit", ("sfm",),
        group="advanced"),
    Opt("mask_sfm", bool, False, "ignore background features (turntable / moving background)", ("sfm",)),
    # mvs
    Opt("resolution_level", int, 1, "dense resolution: 0 full, 1 half, 2 quarter (disk/RAM ÷4 per step)",
        ("mvs",), (0, 1, 2, 3)),
    Opt("keep_depth", bool, False, "keep OpenMVS depth maps (large) after meshing", ("mvs",), group="advanced"),
    # vggt
    Opt("max_frames", int, None, "frames fed to VGGT (default: 24 on CPU; on a GPU as many as its free memory allows, up to 120: ~44 on 8 GB)", ("vggt",), group="advanced"),
    Opt("device", str, "auto", "torch device", ("vggt",), ("auto", "cpu", "cuda"), group="advanced"),
    # clean
    Opt("mesh", str, None, "raw mesh to clean instead of the mvs/vggt output (e.g. a 2DGS mesh)", ("clean",),
        group="advanced"),
    Opt("no_masks", bool, False, "don't carve with masks (keep the largest component only)", ("clean",)),
    Opt("bg_tolerance", float, 0.15, "carve vertices seen as background in more than this fraction of views",
        ("clean",), group="advanced"),
    Opt("fill_holes", float, 0.05, "put back carved holes enclosed by the subject, from the raw mesh, up to "
        "this fraction of its area (0 = off)", ("clean",)),
    Opt("keep_ground", bool, False, "don't cut the support plane (desk/floor)", ("clean",)),
    Opt("ground_margin", float, 0.03, "height above the plane cut away, as a fraction of the subject height",
        ("clean",), group="advanced"),
    Opt("target_faces", int, None, "decimate the clean mesh to this many faces", ("clean",), group="advanced"),
    Opt("print_height", float, 100.0, "height of the 3D-print STL (print.stl) in mm", ("clean",)),
    Opt("print_mode", str, "auto", "3D print: a closed solid (openings capped with lids), only the scanned "
        "surface as a shell (invents nothing), or auto: shell when lids would invent over a third of it",
        ("clean",), ("auto", "solid", "shell")),
    Opt("print_stand", str, "auto", "add a stand under the print when it would not balance on its own base",
        ("clean",), ("auto", "none")),
    Opt("print_min_wall", float, 0.8, "thicken print walls thinner than this (mm)", ("clean",), group="advanced"),
    Opt("print_shell", float, 3.0, "shell thickness for print_mode shell (mm)", ("clean",), group="advanced"),
    Opt("print_shell_side", str, "inward", "which way the shell grows from the scanned surface: inward keeps the "
        "scanned detail exactly where it was measured", ("clean",), ("inward", "outward", "both"), group="advanced"),
    Opt("print_base", float, 0.02, "flatten this fraction of the print's height at the bottom so it stands",
        ("clean",), group="advanced"),
    Opt("up", str, "auto", "how to orient the GLB: support plane, camera up-vector, or leave as SfM",
        ("clean", "texture"), ("auto", "plane", "camera", "none"), group="advanced"),
    # texture
    Opt("texture_resolution", int, 0, "texture image resolution level (0 full, 1 half)", ("texture",),
        (0, 1, 2), group="advanced"),
]

BY_NAME = {o.name: o for o in OPTIONS}


def for_stage(stage: str) -> list[Opt]:
    return [o for o in OPTIONS if stage in o.stages or "*" in o.stages]


def resolve(explicit: dict, preset: str | None = None) -> dict:
    """Full option dict: explicit values (None = unset) over preset over defaults."""
    from .presets import PRESETS

    values = {o.name: o.default for o in OPTIONS}
    if preset:
        if preset not in PRESETS:
            raise ValueError(f"unknown preset {preset!r}; choose from {', '.join(PRESETS)}")
        values.update(PRESETS[preset]["options"])
    for k, v in explicit.items():
        if v is not None and k in BY_NAME:
            values[k] = _coerce(BY_NAME[k], v)
    return values


def _coerce(opt: Opt, v):
    if opt.type is bool:
        return v if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes", "on")
    if v == "" and opt.default is None:
        return None
    return opt.type(v)


def as_json() -> list[dict]:
    out = []
    for o in OPTIONS:
        d = asdict(o)
        d["type"] = o.type.__name__
        out.append(d)
    return out
