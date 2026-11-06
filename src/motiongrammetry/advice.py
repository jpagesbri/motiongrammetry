"""What to change next, read from a scan's results: the rules behind the UI's assisted mode.

Each stage records its numbers in the manifest (registered frames, mask coverage, how much clean
carved and how sure the masks were, ...). The rules here turn them into plain-language advice,
each with the options that would address it and the stages to re-run, so the assistant can offer
"apply and re-run" instead of leaving the user to guess. Thresholds come from real captures.
"""

from __future__ import annotations

from .checks import keyframe_ranges
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


def _item(stage, level, title, detail, apply=None, rerun=None, plain=None):
    item = {"stage": stage, "level": level, "title": title, "detail": detail,
            "apply": apply or {}, "rerun": rerun or []}
    if plain:
        item["plain"] = plain
    return item


# The quality warnings of sfm's camera-path check. An assisted run stops after SfM on them (the
# server's `pause_on`) and asks what to do, with the short texts of camera_path_plain.
CAMERA_PATH_CODES = ("camera-path-jump", "cameras-far-away", "cameras-collapsed")
MOST_FLAGS = 4  # break flags listed one per line; the rest are counted

# The sfm options a camera-path fix changes, in words: (label, {value: words}).
SETTING_WORDS = {
    "mapper": ("Camera solver", {"global": "fast", "incremental": "thorough"}),
    "matcher": ("Photo matching", {"sequential": "neighbouring photos", "exhaustive": "every pair of photos",
                                   "vocab_tree": "similar-looking photos"}),
    "mask_sfm": ("Place cameras from the subject's outline", {True: "yes", False: "no"}),
}
SETTING_DEFAULTS = {"mapper": "global", "matcher": "sequential", "mask_sfm": False}


def _count(n: int) -> str:
    return ["no", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten"][n] if n <= 10 else str(n)


def _kf(r: str) -> str:
    return ("keyframes " if "–" in r else "keyframe ") + r


def camera_path_breaks(p: dict) -> list[dict]:
    """The jumps of a camera path grouped where they follow each other (0→1, 1→2, 2→3 is one break
    at keyframes 0–3): one marker in the page's figure and one flag each."""
    out = []
    for j in sorted(p.get("jumps") or [], key=lambda j: j["from"]):
        if out and j["from"] == out[-1]["to"]:
            out[-1]["to"] = j["to"]
        else:
            out.append({"from": j["from"], "to": j["to"]})
    for b in out:
        b["label"] = f"{b['from']}–{b['to'] - 1}" if b["to"] - 1 > b["from"] else f"{b['from']}"
    return out


def camera_path_plain(p: dict, apply: dict | None = None, ran: dict | None = None) -> dict:
    """The camera-path warning of an SfM run (its manifest's ``camera_path``) in a few plain words, for
    the assisted run's card: a headline, at most two short sentences, one line per problem found
    (with an icon name), a one-line tip, what continuing does to the model, and the fix as settings
    changed from -> to. No jargon: the technical messages stay in the diagnostics (Manual, CLI, logs).

    ``apply``: the options the advice item would set (empty: nothing left to try). ``ran``: the
    options SfM last ran with (mapper, matcher, mask_sfm), for the "from" side of the fix.
    """
    apply, ran = apply or {}, {**SETTING_DEFAULTS, **{k: v for k, v in (ran or {}).items() if v is not None}}
    changes = [{"label": SETTING_WORDS[k][0], "from": SETTING_WORDS[k][1].get(ran[k], str(ran[k])),
                "to": SETTING_WORDS[k][1].get(v, str(v))} for k, v in apply.items() if k in SETTING_WORDS and ran[k] != v]
    fix = None
    if apply:
        masks = "mask_sfm" in apply
        fix = {"label": "Place the cameras without the masks" if masks else "Use the thorough camera solver",
               "slower": not masks, "changes": changes}
    flags, summaries = [], {}
    if p.get("collapsed"):
        n, total = p.get("collapsed_cameras", 0), p.get("cameras", 0)
        flags.append({"icon": "stack", "text": f"{n} of {total} photos placed on top of each other"})
        if ran["mask_sfm"]:
            flags.append({"icon": "mask", "text": "Cameras placed from the subject's outline (a turntable setting)"})
        summaries["cameras-collapsed"] = f"{n} of {total} cameras on top of each other"
        return {"kind": "collapsed", "headline": "Camera movement not found",
                "summary": "The program couldn't work out how the camera moved. "
                           "The model will likely come out mostly empty or garbled.",
                "impact": "the model will likely come out mostly empty or garbled",
                "flags": flags, "breaks": [],
                "tip": ("Using the outline is meant for a turntable, where the camera stays still."
                        if ran["mask_sfm"] else "This is a calculation problem, not a problem with the video."),
                "fix": fix, "summaries": summaries}

    breaks = camera_path_breaks(p)
    far = p.get("far") or []
    for b in breaks[:MOST_FLAGS]:
        flags.append({"icon": "break", "text": f"Views don't connect at {_kf(b['label'])}"})
    if len(breaks) > MOST_FLAGS:
        flags.append({"icon": "break", "text": f"…and {len(breaks) - MOST_FLAGS} more"})
    if breaks:
        summaries["camera-path-jump"] = (f"camera path breaks at {len(breaks)} place{'s' if len(breaks) > 1 else ''}")
    if far:
        n = p.get("far_count", len(far))
        where = ", ".join(keyframe_ranges(sorted(f["keyframe"] for f in far)))
        flags.append({"icon": "far", "text": (f"{_count(n).capitalize()} photo{'s' if n > 1 else ''} placed far away "
                                              f"({'keyframes' if n > 1 else 'keyframe'} {where}{'…' if n > len(far) else ''})")})
        summaries["cameras-far-away"] = f"{n} photo{'s' if n > 1 else ''} placed far away"
    if breaks:
        headline, summary = "Camera path broken", ("Some views didn't connect, so the camera positions don't line "
                                                   "up. The model will likely have misplaced or doubled parts.")
        impact = "the model will likely have misplaced or doubled parts"
    else:
        headline, summary = "Photos placed far away", ("Some photos didn't connect to the rest and were placed far "
                                                       "away. The model will likely have stray, misplaced pieces.")
        impact = "the model will likely have stray, misplaced pieces"
    return {"kind": "jump" if breaks else "far", "headline": headline, "summary": summary, "impact": impact,
            "flags": flags, "breaks": breaks, "tip": "Quick turns and doorways often cause this.",
            "fix": fix, "summaries": summaries}


def last_options(ws: Workspace, stage: str) -> dict:
    """The options the stage last ran with (as the UI and CLI recorded them)."""
    last = next((h for h in reversed(ws.history()) if h["stage"] == stage and h["status"] != "skipped"), {})
    return last.get("options") or {}


def advise(ws: Workspace, subject: str | None = None) -> list[dict]:
    m = ws.load_manifest()
    out: list[dict] = []
    add = out.append

    f = m.get("frames")
    if f:
        k = f.get("keyframes", 0)
        if k < 60:
            add(_item("frames", "fix", f"only {k} keyframes",
                      "too few views for a detailed model: film for longer, or move more slowly around the subject"))
        segs = f.get("segments") or 0
        if segs and f.get("blur_rejected", 0) / segs > 0.25:
            add(_item("frames", "fix", "much of the video is blurred",
                      "film more slowly or with more light; meanwhile analyse every frame and reject blur harder",
                      {"sample_fps": 30.0, "blur_reject": 0.55}, ["frames", "mask", "sfm", "mvs", "clean", "texture"]))
        if f.get("total_motion_widths", 99) < 1.0 and subject != "drone":  # a distant subject moves little in the frame
            add(_item("frames", "fix", "the camera barely moved",
                      "3D needs different viewpoints: walk around the subject instead of turning on the spot"))
        if not any(x["stage"] == "frames" for x in out):
            add(_item("frames", "ok", f"{k} sharp keyframes", "enough views, spread over the video"))

    mk = m.get("mask")
    if mk:
        n, cov, cv, empty = mk.get("frames", 1) or 1, mk.get("mean_coverage", 0), mk.get("coverage_cv", 0), mk.get("empty_masks", 0)
        other = {"object": "person", "object-hq": "person-hq", "person": "object", "person-hq": "object-hq"}
        current = last_options(ws, "mask").get("mask_model", "object")
        # New masks outdate clean and texture, and SfM too (so MVS) when it ran with them (mask_sfm).
        rerun = ["mask"] + (["sfm", "mvs"] if "mask" in ws.sfm_upstream(m) else []) + ["clean", "texture"]
        if empty > 0.05 * n:
            alt = other.get(current, "object-hq")
            add(_item("mask", "fix", f"{empty} keyframes found no subject",
                      f"the segmentation missed the subject in some views: try the {alt} model",
                      {"mask_model": alt}, rerun))
        if cov < 0.05:
            add(_item("mask", "fix", f"the subject covers only {cov:.0%} of the frame",
                      "film closer: small subjects get few pixels and little detail"))
        elif cv > 0.6:
            hq = current if current.endswith("-hq") else f"{current}-hq" if current in other else "object-hq"
            add(_item("mask", "tip", "the subject's size jumps between keyframes",
                      "some masks probably caught background or lost part of the subject; check the mask overlay "
                      f"in the keyframe strip, or try the {hq} model", {"mask_model": hq}, rerun))
        if not any(x["stage"] == "mask" for x in out):
            add(_item("mask", "ok", f"subject found in every keyframe ({cov:.0%} of the frame)",
                      "check the mask overlay in the keyframe strip if anything looks off"))

    s = m.get("sfm")
    if s:
        reg, n = s.get("registered", 0), s.get("frames", 1) or 1
        if reg < 0.9 * n:
            apply = {"mapper": "incremental", "matcher": "exhaustive"} if n <= 200 else {"mapper": "incremental"}
            add(_item("sfm", "fix", f"only {reg} of {n} keyframes got a camera position",
                      "the views did not connect well (plain or shiny surfaces, gaps in the video): try the slower "
                      "incremental mapper" + (" with exhaustive matching" if n <= 200 else "") +
                      ", or the low-texture preset", apply, ["sfm", "mvs", "clean", "texture"]))
        elif (s.get("camera_path") or {}).get("collapsed"):
            p = s["camera_path"]
            masked = s.get("masked_features")
            tried = s.get("mapper") == "incremental" and s.get("matcher") == "exhaustive"
            apply = {"mask_sfm": False} if masked else {} if tried else {"mapper": "incremental", "matcher": "exhaustive"}
            add(_item("sfm", "fix", f"{p['collapsed_cameras']} of {p['cameras']} cameras sit on top of each other",
                      f"only {p['distinct_positions']} distinct camera positions: SfM did not recover how the camera "
                      "moved, however low the error. Not a filming problem: "
                      + ("the masks were used for camera poses (mask_sfm), which is for turntables (camera fixed, "
                         "object turning); run SfM without them. If the camera really stayed still, keep mask_sfm "
                         "and use the incremental mapper with exhaustive matching instead" if masked else
                         "the slower incremental mapper with exhaustive matching did not help either; film again"
                         if tried else "try the slower incremental mapper with exhaustive matching"),
                      apply, ["sfm", "mvs", "clean", "texture"] if apply else None,
                      plain=camera_path_plain(p, apply, {"mapper": s.get("mapper"), "matcher": s.get("matcher"),
                                                         "mask_sfm": bool(masked)})))
        elif not (s.get("camera_path") or {}).get("ok", True):
            p = s["camera_path"]
            where = sorted({j["from"] for j in p.get("jumps", [])} | {f["keyframe"] for f in p.get("far", [])})
            film = ("Film again: walk slowly through doorways and keep part of the room you are leaving in view "
                    "while you turn; no quick pans or close-ups in passing; for an object, a steady orbit at an "
                    "even pace")
            tried = s.get("mapper") == "incremental" and s.get("matcher") == "exhaustive"
            apply = {} if tried else {"mapper": "incremental", "matcher": "exhaustive"}
            add(_item("sfm", "fix", f"the camera path breaks near keyframe{'s' if len(where) > 1 else ''} "
                      + ", ".join(keyframe_ranges(where)),
                      "the keyframes got a position, but some views did not connect, so part of the scene is misplaced "
                      "or built twice. " + (film if tried else
                      "First try the slower incremental mapper with exhaustive matching, which can connect views the "
                      "defaults missed. If it still breaks: " + film[0].lower() + film[1:]),
                      apply, ["sfm", "mvs", "clean", "texture"] if apply else None,
                      plain=camera_path_plain(p, apply, {"mapper": s.get("mapper"), "matcher": s.get("matcher"),
                                                         "mask_sfm": bool(s.get("masked_features"))})))
        elif s.get("mean_reproj_error_px", 0) > 2.0:
            add(_item("sfm", "tip", f"camera positions are approximate ({s['mean_reproj_error_px']:.1f} px error)",
                      "the subject may have moved, or the video is soft; the result can look smeared"))
        else:
            add(_item("sfm", "ok", f"all {reg} keyframes placed", f"{s.get('points3D', 0):,} 3D points, "
                      f"{s.get('mean_reproj_error_px', 0):.2f} px error"))

    mv = m.get("mvs")
    if mv:
        lvl = mv.get("resolution_level", 1)
        if lvl >= 2:
            add(_item("mvs", "tip", "quarter-resolution depth maps", "fast but soft: if the disk allows, "
                      "resolution_level 1 (or 0) gives much more detail", {"resolution_level": 1},
                      ["mvs", "clean", "texture"]))
        else:
            add(_item("mvs", "ok", f"{mv.get('mesh_faces', 0):,} faces in the raw mesh",
                      f"from {mv.get('dense_points', 0):,} dense points"))

    c = m.get("clean")
    if c:
        carved = c.get("carved_vertices", 0) / max(c.get("vertices_in", 1), 1)
        unsure = c.get("carved_uncertain")
        tol = float(last_options(ws, "clean").get("bg_tolerance", 0.15))
        if carved > 0.08 and unsure is not None and unsure > 0.8 and tol < 0.35:
            add(_item("clean", "fix", f"clean carved away {carved:.0%} of the mesh, mostly where the masks were unsure",
                      "that is usually the subject itself (sides of a head, hair, shoulders, thin parts), not "
                      "background: carve more gently", {"bg_tolerance": 0.35}, ["clean", "texture"]))
        elif carved > 0.08 and unsure is not None and unsure < 0.3:
            add(_item("clean", "ok", f"background removed ({carved:.0%} of the raw mesh)",
                      "the masks agreed it was background"))
        if not c.get("support_plane") and subject in ("object", "figure"):
            add(_item("clean", "tip", "no table or floor plane found",
                      "orientation falls back to the camera orbit; if a table is left under the model, cut it in "
                      "the editor with Plane cut"))
        p = c.get("print") or {}
        if p.get("mode") == "shell":
            add(_item("clean", "tip", f"the print is a shell: {p.get('lid_share', 0):.0%} of a closed print would be invented",
                      "only part of the subject was filmed; film all the way around for a full 3D print"))
        if not any(x["stage"] == "clean" for x in out):
            add(_item("clean", "ok", f"{c.get('faces_out', 0):,} faces kept", "the clean mesh is ready"))

    t = m.get("texture")
    if t and "texture" not in (ws.stale_stages() or []):
        add(_item("texture", "ok", "textured model ready" + (" (of your edited mesh)" if t.get("source") == "custom" else ""),
                  "download it or print it from Export…, or touch it up in Edit"))
    return out
