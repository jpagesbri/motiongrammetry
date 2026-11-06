"""What to change next, read from a scan's results: the rules behind the UI's assisted mode.

Each stage records its numbers in the manifest (registered frames, mask coverage, how much clean
carved and how sure the masks were, ...). The rules here turn them into plain-language advice,
each with the options that would address it and the stages to re-run, so the assistant can offer
"apply and re-run" instead of leaving the user to guess. Thresholds come from real captures.

Every item has a short form the UI shows first, the same everywhere: a title (at most 5 words),
one line (at most 12) and, when it can be applied, a button label (at most 3 words, verb first)
and the settings it changes in words. The longer title and detail are its Details. ``codes`` are
the diagnostics an item explains: the UI folds them into the item instead of showing them twice.
"""

from __future__ import annotations

from .checks import keyframe_ranges
from .options import OPTIONS
from .workspace import Workspace

# What the user said they are scanning (assisted mode, first step) -> starting options.
SUBJECTS = {
    "object": {"label": "An object on a table or the floor",
               "options": {"mask_model": "object"},
               "why": "general-purpose segmentation; the table or floor is found and cut away"},
    "figure": {"label": "A figure, statue or detailed object",
               "options": {"mask_model": "object-hq", "num": 180},
               "why": "the high-quality object model keeps thin parts, more keyframes keep detail"},
    "person": {"label": "A person (head, bust or whole body)",
               "preset": "person",
               "why": "portrait segmentation, more frames, gentle carving so hair and fingers stay"},
    "drone": {"label": "Something big, filmed with a drone (a castle, a bridge, a building)",
              "preset": "drone",
              "why": "AI masks find only parts of a big structure in each view: carve very gently, use more of the 4K video"},
    "other": {"label": "Something else",
              "options": {},
              "why": "defaults; the advice after each step adjusts them"},
}
COVERAGE = {
    "around": {"label": "I walked all the way around it", "options": {}},
    "partial": {"label": "Only one side (for example a face filmed from the front)",
                "options": {"print_mode": "shell", "print_shell_side": "inward"},
                "why": "the unseen side cannot be reconstructed: the print keeps only the scanned surface"},
    "turntable": {"label": "The camera stayed still and the object turned", "preset": "turntable",
                  "why": "the background must not be used for camera poses"},
}


TITLE_WORDS, LINE_WORDS, ACTION_WORDS = 5, 12, 3


def _item(stage, level, title, detail, apply=None, rerun=None, *, short, line, action="", codes=(), ran=None,
          plain=None):
    """An advice item: ``title``/``detail`` the long form (Details); ``short``/``line``/``action`` the short
    form; ``changes`` (from ``apply`` and the options the stage last ran with, ``ran``) in words."""
    apply = apply or {}
    item = {"stage": stage, "level": level, "title": title, "detail": detail, "apply": apply, "rerun": rerun or [],
            "short": short, "line": line, "action": action if apply else "", "codes": list(codes),
            "changes": changes(apply, ran or {})}
    if plain:
        item["plain"] = plain
    return item


# Options an item can change, in words: (label, {value: words}); numbers are shown as they are.
SETTING_WORDS = {
    "mapper": ("Solver", {"global": "fast", "incremental": "thorough"}),
    "matcher": ("Matching", {"sequential": "neighbours", "exhaustive": "all pairs", "vocab_tree": "look-alikes"}),
    "mask_sfm": ("Masks for cameras", {True: "on", False: "off"}),
    "mask_model": ("Mask model", {"object": "object", "object-hq": "object HQ", "person": "person",
                                  "person-hq": "person HQ"}),
    "sample_fps": ("Frames analysed", {}),
    "blur_reject": ("Blur rejection", {}),
    "resolution_level": ("Depth detail", {0: "full", 1: "half", 2: "quarter", 3: "eighth"}),
    "bg_tolerance": ("Carving tolerance", {}),
}
DEFAULTS = {o.name: o.default for o in OPTIONS}


def changes(apply: dict, ran: dict) -> list[dict]:
    """The settings an item changes, from what the stage last ran with to the new value, in words;
    only those that really change."""
    out = []
    for k, v in apply.items():
        label, words = SETTING_WORDS.get(k, (k.replace("_", " ").capitalize(), {}))
        old = ran.get(k, DEFAULTS.get(k))
        if old != v:
            out.append({"label": label, "from": str(words.get(old, old)), "to": str(words.get(v, v))})
    return out


# The quality warnings of sfm's camera-path check. An assisted run stops after SfM on them (the
# server's `pause_on`) and asks what to do: a card with a figure of the path (camera_path_plain).
CAMERA_PATH_CODES = ("camera-path-jump", "cameras-far-away", "cameras-collapsed")
MOST_FLAGS = 4  # breaks listed one per line in the Details; the rest are counted


def _count(n: int) -> str:
    return ["no", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten"][n] if n <= 10 else str(n)


def _kf(r: str) -> str:
    return ("keyframes " if "–" in r else "keyframe ") + r


def camera_path_breaks(p: dict) -> list[dict]:
    """The jumps of a camera path grouped where they follow each other (0→1, 1→2, 2→3 is one break
    at keyframes 0–3): one marker in the page's figure and one line in the Details each."""
    out = []
    for j in sorted(p.get("jumps") or [], key=lambda j: j["from"]):
        if out and j["from"] == out[-1]["to"]:
            out[-1]["to"] = j["to"]
        else:
            out.append({"from": j["from"], "to": j["to"]})
    for b in out:
        b["label"] = f"{b['from']}–{b['to'] - 1}" if b["to"] - 1 > b["from"] else f"{b['from']}"
    return out


def camera_path_plain(p: dict, masked: bool = False) -> dict:
    """What the assisted run's camera-path card needs besides the item's short form: the kind, the
    breaks (for the figure), one line per problem with an icon and a tip (its Details), and the likely
    defect (Continue's warning). No jargon: the technical messages stay in the diagnostics."""
    if p.get("collapsed"):
        n, total = p.get("collapsed_cameras", 0), p.get("cameras", 0)
        flags = [{"icon": "stack", "text": f"{n} of {total} photos placed on top of each other"}]
        if masked:
            flags.append({"icon": "mask", "text": "Cameras placed from the subject's outline (a turntable setting)"})
        return {"kind": "collapsed", "breaks": [], "flags": flags, "impact": "Likely a mostly empty or garbled model.",
                "tip": ("Using the outline is meant for a turntable, where the camera stays still." if masked
                        else "This is a calculation problem, not a problem with the video.")}
    breaks = camera_path_breaks(p)
    far = p.get("far") or []
    flags = [{"icon": "break", "text": f"Views don't connect at {_kf(b['label'])}"} for b in breaks[:MOST_FLAGS]]
    if len(breaks) > MOST_FLAGS:
        flags.append({"icon": "break", "text": f"…and {len(breaks) - MOST_FLAGS} more"})
    if far:
        n = p.get("far_count", len(far))
        where = ", ".join(keyframe_ranges(sorted(f["keyframe"] for f in far)))
        flags.append({"icon": "far", "text": f"{_count(n).capitalize()} photo{'s' if n > 1 else ''} placed far away "
                                             f"({'keyframes' if n > 1 else 'keyframe'} {where}{'…' if n > len(far) else ''})"})
    return {"kind": "jump" if breaks else "far", "breaks": breaks, "flags": flags,
            "impact": "Likely misplaced or doubled parts." if breaks else "Likely stray, misplaced pieces.",
            "tip": "Quick turns and doorways often cause this."}


def last_options(ws: Workspace, stage: str) -> dict:
    """The options the stage last ran with (as the UI and CLI recorded them)."""
    last = next((h for h in reversed(ws.history()) if h["stage"] == stage and h["status"] != "skipped"), {})
    return last.get("options") or {}


MASK_ACTION = {"object": "Try object masks", "object-hq": "Try HQ masks", "person": "Try person masks",
               "person-hq": "Try HQ masks"}
SFM_RERUN = ["sfm", "mvs", "clean", "texture"]


def advise(ws: Workspace, subject: str | None = None) -> list[dict]:
    m = ws.load_manifest()
    out: list[dict] = []
    add = out.append

    f = m.get("frames")
    if f:
        k = f.get("keyframes", 0)
        ran = last_options(ws, "frames")
        if k < 60:
            add(_item("frames", "fix", f"only {k} keyframes",
                      "too few views for a detailed model: film for longer, or move more slowly around the subject",
                      short="Few keyframes", line=f"Only {k}: film longer, or move more slowly.",
                      codes=["few-frames", "too-few-frames"]))
        segs = f.get("segments") or 0
        if segs and f.get("blur_rejected", 0) / segs > 0.25:
            add(_item("frames", "fix", "much of the video is blurred",
                      "film more slowly or with more light; meanwhile analyse every frame and reject blur harder",
                      {"sample_fps": 30.0, "blur_reject": 0.55}, ["frames", "mask", "sfm", "mvs", "clean", "texture"],
                      short="Blurry video", line="Much of the video is blurred.", action="Reject more blur",
                      codes=["blurry-video"], ran=ran))
        if f.get("total_motion_widths", 99) < 1.0 and subject != "drone":  # a distant subject moves little in the frame
            add(_item("frames", "fix", "the camera barely moved",
                      "3D needs different viewpoints: walk around the subject instead of turning on the spot",
                      short="Camera barely moved", line="Walk around the subject, not on the spot.",
                      codes=["little-motion"]))
        if not any(x["stage"] == "frames" for x in out):
            add(_item("frames", "ok", f"{k} sharp keyframes", "enough views, spread over the video",
                      short="Keyframes ready", line=f"{k} sharp keyframes, spread over the video."))

    mk = m.get("mask")
    if mk:
        n, cov, cv, empty = mk.get("frames", 1) or 1, mk.get("mean_coverage", 0), mk.get("coverage_cv", 0), mk.get("empty_masks", 0)
        other = {"object": "person", "object-hq": "person-hq", "person": "object", "person-hq": "object-hq"}
        ran = last_options(ws, "mask")
        current = ran.get("mask_model", "object")
        # New masks outdate clean and texture, and SfM too (so MVS) when it ran with them (mask_sfm).
        rerun = ["mask"] + (["sfm", "mvs"] if "mask" in ws.sfm_upstream(m) else []) + ["clean", "texture"]
        if empty > 0.05 * n:
            alt = other.get(current, "object-hq")
            add(_item("mask", "fix", f"{empty} keyframes found no subject",
                      f"the segmentation missed the subject in some views: try the {alt} model",
                      {"mask_model": alt}, rerun, short="Subject missed in some frames",
                      line=f"{empty} of {n} keyframes found no subject.", action=MASK_ACTION.get(alt, "Try other masks"),
                      codes=["masks-empty", "subject-not-found"], ran=ran))
        if cov < 0.05:
            add(_item("mask", "fix", f"the subject covers only {cov:.0%} of the frame",
                      "film closer: small subjects get few pixels and little detail",
                      short="Subject is small", line=f"It fills only {cov:.0%} of the frame: film closer."))
        elif cv > 0.6:
            hq = current if current.endswith("-hq") else f"{current}-hq" if current in other else "object-hq"
            add(_item("mask", "fix", "the subject's size jumps between keyframes",
                      "some masks probably caught background or lost part of the subject; check the mask overlay "
                      f"in the keyframe strip, or try the {hq} model", {"mask_model": hq} if hq != current else {},
                      rerun if hq != current else None, short="Masks vary a lot",
                      line="The subject's outline changes between keyframes.", action="Try HQ masks",
                      codes=["masks-unstable"], ran=ran))
        if not any(x["stage"] == "mask" for x in out):
            add(_item("mask", "ok", f"subject found in every keyframe ({cov:.0%} of the frame)",
                      "check the mask overlay in the keyframe strip if anything looks off",
                      short="Subject found", line=f"In every keyframe, {cov:.0%} of the frame."))

    s = m.get("sfm")
    if s:
        reg, n = s.get("registered", 0), s.get("frames", 1) or 1
        ran = {"mapper": s.get("mapper") or DEFAULTS["mapper"], "matcher": s.get("matcher") or DEFAULTS["matcher"],
               "mask_sfm": bool(s.get("masked_features"))}
        tried = ran["mapper"] == "incremental" and ran["matcher"] == "exhaustive"
        path = s.get("camera_path") or {}
        if reg < 0.9 * n:
            apply = {"mapper": "incremental", "matcher": "exhaustive"} if n <= 200 else {"mapper": "incremental"}
            add(_item("sfm", "fix", f"only {reg} of {n} keyframes got a camera position",
                      "the views did not connect well (plain or shiny surfaces, gaps in the video): try the slower "
                      "incremental mapper" + (" with exhaustive matching" if n <= 200 else "") +
                      ", or the low-texture preset", apply, SFM_RERUN, short="Some cameras not placed",
                      line=f"Only {reg} of {n} keyframes got a position.", action="Thorough solver",
                      codes=["poor-registration", "partial-registration"], ran=ran))
        elif path.get("collapsed"):
            masked = ran["mask_sfm"]
            apply = {"mask_sfm": False} if masked else {} if tried else {"mapper": "incremental", "matcher": "exhaustive"}
            add(_item("sfm", "fix", f"{path['collapsed_cameras']} of {path['cameras']} cameras sit on top of each other",
                      f"only {path['distinct_positions']} distinct camera positions: SfM did not recover how the camera "
                      "moved, however low the error. Not a filming problem: "
                      + ("the masks were used for camera poses (mask_sfm), which is for turntables (camera fixed, "
                         "object turning); run SfM without them. If the camera really stayed still, keep mask_sfm "
                         "and use the incremental mapper with exhaustive matching instead" if masked else
                         "the slower incremental mapper with exhaustive matching did not help either; film again"
                         if tried else "try the slower incremental mapper with exhaustive matching"),
                      apply, SFM_RERUN if apply else None, short="Camera movement not found",
                      line="The camera's movement couldn't be worked out.",
                      action="Retry without masks" if masked else "Thorough solver",
                      codes=list(CAMERA_PATH_CODES), ran=ran, plain=camera_path_plain(path, masked)))
        elif not path.get("ok", True):
            breaks = camera_path_breaks(path)
            where = sorted({j["from"] for j in path.get("jumps", [])} | {f["keyframe"] for f in path.get("far", [])})
            film = ("Film again: walk slowly through doorways and keep part of the room you are leaving in view "
                    "while you turn; no quick pans or close-ups in passing; for an object, a steady orbit at an "
                    "even pace")
            apply = {} if tried else {"mapper": "incremental", "matcher": "exhaustive"}
            nfar = path.get("far_count", len(path.get("far") or []))
            short, line = (("Camera path broken", f"Views don't connect at {len(breaks)} place{'s' if len(breaks) > 1 else ''}.")
                           if breaks else ("Photos placed far away", f"{nfar} photo{'s' if nfar > 1 else ''} far from the others."))
            add(_item("sfm", "fix", f"the camera path breaks near keyframe{'s' if len(where) > 1 else ''} "
                      + ", ".join(keyframe_ranges(where)),
                      "the keyframes got a position, but some views did not connect, so part of the scene is misplaced "
                      "or built twice. " + (film if tried else
                      "First try the slower incremental mapper with exhaustive matching, which can connect views the "
                      "defaults missed. If it still breaks: " + film[0].lower() + film[1:]),
                      apply, SFM_RERUN if apply else None, short=short, line=line, action="Thorough solver",
                      codes=list(CAMERA_PATH_CODES), ran=ran, plain=camera_path_plain(path, ran["mask_sfm"])))
        elif s.get("mean_reproj_error_px", 0) > 2.0:
            add(_item("sfm", "tip", f"camera positions are approximate ({s['mean_reproj_error_px']:.1f} px error)",
                      "the subject may have moved, or the video is soft; the result can look smeared",
                      short="Camera positions approximate",
                      line=f"Error {s['mean_reproj_error_px']:.1f} px: the model may look smeared.",
                      codes=["high-reprojection"]))
        else:
            add(_item("sfm", "ok", f"all {reg} keyframes placed", f"{s.get('points3D', 0):,} 3D points, "
                      f"{s.get('mean_reproj_error_px', 0):.2f} px error", short="Cameras placed",
                      line=f"All {reg} keyframes, {s.get('mean_reproj_error_px', 0):.2f} px error."))

    mv = m.get("mvs")
    if mv:
        lvl = mv.get("resolution_level", 1)
        if lvl >= 2:
            add(_item("mvs", "tip", "quarter-resolution depth maps", "fast but soft: if the disk allows, "
                      "resolution_level 1 (or 0) gives much more detail", {"resolution_level": 1},
                      ["mvs", "clean", "texture"], short="Low-detail depth maps", line="Quarter resolution: fast but soft.",
                      action="Use half resolution", ran={"resolution_level": lvl}))
        else:
            add(_item("mvs", "ok", f"{mv.get('mesh_faces', 0):,} faces in the raw mesh",
                      f"from {mv.get('dense_points', 0):,} dense points", short="Raw mesh built",
                      line=f"{mv.get('mesh_faces', 0):,} faces from {mv.get('dense_points', 0):,} points."))

    c = m.get("clean")
    if c:
        carved = c.get("carved_vertices", 0) / max(c.get("vertices_in", 1), 1)
        unsure = c.get("carved_uncertain")
        ran = last_options(ws, "clean")
        tol = float(ran.get("bg_tolerance", 0.15))
        if carved > 0.08 and unsure is not None and unsure > 0.8 and tol < 0.35:
            add(_item("clean", "fix", f"clean carved away {carved:.0%} of the mesh, mostly where the masks were unsure",
                      "that is usually the subject itself (sides of a head, hair, shoulders, thin parts), not "
                      "background: carve more gently", {"bg_tolerance": 0.35}, ["clean", "texture"],
                      short="Subject partly carved away", line=f"{carved:.0%} removed where the masks were unsure.",
                      action="Carve gently", codes=["heavy-carving"], ran={"bg_tolerance": tol}))
        elif carved > 0.08 and unsure is not None and unsure < 0.3:
            add(_item("clean", "ok", f"background removed ({carved:.0%} of the raw mesh)",
                      "the masks agreed it was background", short="Background removed",
                      line=f"{carved:.0%} of the raw mesh; the masks agreed."))
        if not c.get("support_plane") and subject in ("object", "figure"):
            add(_item("clean", "tip", "no table or floor plane found",
                      "orientation falls back to the camera orbit; if a table is left under the model, cut it in "
                      "the editor with Plane cut", short="No table or floor found",
                      line="Cut any table left in the editor (Plane cut).", codes=["no-support-plane"]))
        pr = c.get("print") or {}
        if pr.get("mode") == "shell":
            add(_item("clean", "tip", f"the print is a shell: {pr.get('lid_share', 0):.0%} of a closed print would be invented",
                      "only part of the subject was filmed; film all the way around for a full 3D print",
                      short="Print is a shell", line=f"{pr.get('lid_share', 0):.0%} of a closed print would be invented."))
        if not any(x["stage"] == "clean" for x in out):
            add(_item("clean", "ok", f"{c.get('faces_out', 0):,} faces kept", "the clean mesh is ready",
                      short="Clean mesh ready", line=f"{c.get('faces_out', 0):,} faces kept."))

    t = m.get("texture")
    if t and "texture" not in (ws.stale_stages() or []):
        edited = t.get("source") == "custom"
        add(_item("texture", "ok", "textured model ready" + (" (of your edited mesh)" if edited else ""),
                  "download it or print it from Export…, or touch it up in Edit", short="Textured model ready",
                  line=("Of your edited mesh. " if edited else "") + "Export it, or touch it up in Edit."))
    return out
