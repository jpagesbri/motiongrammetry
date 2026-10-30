"""What to change next, read from a scan's results: the rules behind the UI's assisted mode.

Each stage records its numbers in the manifest (registered frames, mask coverage, how much clean
carved and how sure the masks were, ...). The rules here turn them into plain-language advice,
each with the options that would address it and the stages to re-run, so the assistant can offer
"apply and re-run" instead of leaving the user to guess. Thresholds come from real captures.
"""

from __future__ import annotations

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


def _item(stage, level, title, detail, apply=None, rerun=None):
    return {"stage": stage, "level": level, "title": title, "detail": detail,
            "apply": apply or {}, "rerun": rerun or []}


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
        if f.get("total_motion_widths", 99) < 1.0:
            add(_item("frames", "fix", "the camera barely moved",
                      "3D needs different viewpoints: walk around the subject instead of turning on the spot"))
        if not any(x["stage"] == "frames" for x in out):
            add(_item("frames", "ok", f"{k} sharp keyframes", "enough views, spread over the video"))

    mk = m.get("mask")
    if mk:
        n, cov, cv, empty = mk.get("frames", 1) or 1, mk.get("mean_coverage", 0), mk.get("coverage_cv", 0), mk.get("empty_masks", 0)
        other = {"object": "person", "object-hq": "person-hq", "person": "object", "person-hq": "object-hq"}
        current = last_options(ws, "mask").get("mask_model", "object")
        if empty > 0.05 * n:
            alt = other.get(current, "object-hq")
            add(_item("mask", "fix", f"{empty} keyframes found no subject",
                      f"the segmentation missed the subject in some views: try the {alt} model",
                      {"mask_model": alt}, ["mask", "clean", "texture"]))
        if cov < 0.05:
            add(_item("mask", "fix", f"the subject covers only {cov:.0%} of the frame",
                      "film closer: small subjects get few pixels and little detail"))
        elif cv > 0.6:
            hq = current if current.endswith("-hq") else f"{current}-hq" if current in other else "object-hq"
            add(_item("mask", "tip", "the subject's size jumps between keyframes",
                      "some masks probably caught background or lost part of the subject; check the mask overlay "
                      f"in the keyframe strip, or try the {hq} model", {"mask_model": hq}, ["mask", "clean", "texture"]))
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
                  "download it or print it from Export…, or touch it up in Edit & paint"))
    return out
