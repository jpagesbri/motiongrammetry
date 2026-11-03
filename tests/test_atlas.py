from pathlib import Path

import numpy as np
import pytest
import trimesh
from PIL import Image

from motiongrammetry import atlas


def scattered_box(folder: Path, n: int = 3, chart: int = 64) -> Path:
    """A unit box whose sides are each cut into n x n texture pieces scattered and turned over the
    atlas, as OpenMVS lays one out; the front side (+z) carries a gradient, the others grey.
    Written as textured.ply + textured0.png, like OpenMVS's."""
    rng = np.random.default_rng(1)
    T, gap = 1024, chart + 8
    slots = [(i, j) for i in range(T // gap) for j in range(T // gap)]
    rng.shuffle(slots)
    sides = [((0, 0, 1), (1, 0, 0), (0, 1, 0)), ((1, 0, 0), (0, 0, -1), (0, 1, 0)), ((0, 0, -1), (-1, 0, 0), (0, 1, 0)),
             ((-1, 0, 0), (0, 0, 1), (0, 1, 0)), ((0, 1, 0), (1, 0, 0), (0, 0, -1)), ((0, -1, 0), (1, 0, 0), (0, 0, 1))]
    px = np.full((T, T, 3), 40, np.uint8)
    V, F, UV, k = [], [], [], 0
    g = (np.arange(chart) + 0.5) / chart
    for si, (nrm, ax, ay) in enumerate(map(lambda s: [np.array(x, float) for x in s], sides)):
        for ci in range(n):
            for cj in range(n):
                (sx, sy), rot = slots[k], int(rng.integers(4))
                k += 1
                x0, y0 = 4 + sx * gap, 4 + sy * gap
                st = [(ci / n, cj / n), ((ci + 1) / n, cj / n), ((ci + 1) / n, (cj + 1) / n), (ci / n, (cj + 1) / n)]
                tex = [(x0, y0 + chart), (x0 + chart, y0 + chart), (x0 + chart, y0), (x0, y0)]  # v up: rows flip
                tex = tex[rot:] + tex[:rot]
                base = len(V)
                for (s, t), (u, v) in zip(st, tex):
                    V.append(nrm * .5 + ax * (s - .5) + ay * (t - .5))
                    UV.append((u / T, 1 - v / T))
                F += [(base, base + 1, base + 2), (base, base + 2, base + 3)]
                # the texels: side coordinates at each texel, through the turned corners
                c = np.array(st)[[(i - rot) % 4 for i in range(4)]]  # side coords at atlas corners BL, BR, TR, TL
                A, B = np.meshgrid(g, g[::-1])                         # A right, B up (row 0 = top)
                w = [(1 - A) * (1 - B), A * (1 - B), A * B, (1 - A) * B]
                sc = sum(c[i] * w[i][..., None] for i in range(4))
                px[y0:y0 + chart, x0:x0 + chart] = (np.stack([sc[..., 0] * 255, sc[..., 1] * 255, np.full_like(A, 90)], -1)
                                                    if si == 0 else 128)
    m = trimesh.Trimesh(np.array(V), np.array(F), process=False)
    m.visual = trimesh.visual.TextureVisuals(uv=np.array(UV), image=Image.fromarray(px))
    Image.fromarray(px).save(folder / "textured0.png")
    head, rest = m.export(file_type="ply").split(b"end_header\n", 1)
    head = head.replace(b"\nelement vertex", b"\ncomment TextureFile textured0.png\nelement vertex", 1)
    (folder / "textured.ply").write_bytes(head + b"end_header\n" + rest)
    return folder / "textured.ply"


def pieces_in(path: Path) -> int:
    """Pieces in a written layout, counted back from its file (no two may have been packed as one)."""
    m = trimesh.load(str(path), process=False)
    return atlas.count_pieces(np.asarray(m.faces), np.asarray(m.visual.uv), np.asarray(m.vertices))


def colours_at(m, faces, weights):
    img = np.asarray(m.visual.material.image.convert("RGB"), float)
    h, w = img.shape[:2]
    q = (weights[:, :, None] * np.asarray(m.visual.uv)[np.asarray(m.faces)[faces]]).sum(1)
    return img[np.clip(((1 - q[:, 1]) * h).astype(int), 0, h - 1), np.clip((q[:, 0] * w).astype(int), 0, w - 1)]


def test_a_scattered_atlas_is_rebuilt_one_piece_per_side_with_the_same_colours(tmp_path):
    src = scattered_box(tmp_path)
    assert atlas.preview(src) == {"pieces_now": 54, "pieces": 6, "faces": 108}
    info = atlas.relayout(src, tmp_path / "out.ply")
    assert info["pieces_now"] == 54 and info["pieces"] == 6 == pieces_in(tmp_path / "out.ply")
    out = trimesh.load(str(tmp_path / "out.ply"), process=False)  # finds out0.png through its TextureFile
    assert out.visual.material.image.size == tuple(info["size"])
    uv = np.asarray(out.visual.uv)
    assert uv.min() >= 0 and uv.max() <= 1
    # the same triangles, in the same order: the same points of the surface keep their colour
    old = trimesh.load(str(src), process=False)
    rng = np.random.default_rng(0)
    faces, w = rng.integers(len(old.faces), size=5000), rng.dirichlet([4, 4, 4], 5000)
    pts = lambda m: (w[:, :, None] * np.asarray(m.vertices)[np.asarray(m.faces)[faces]]).sum(1)  # noqa: E731
    assert np.abs(pts(old) - pts(out)).max() < 1e-6
    diff = np.abs(colours_at(old, faces, w) - colours_at(out, faces, w)).mean(1)
    assert np.median(diff) < 2 and np.mean(diff) < 4


def test_lscm_flattens_a_curved_sheet_without_folding_or_stretching():
    # a quarter cylinder, 20 x 20 quads: it unrolls into a flat rectangle
    a, z = np.meshgrid(np.linspace(0, np.pi / 2, 21), np.linspace(0, 1, 21))
    v = np.stack([np.cos(a).ravel(), np.sin(a).ravel(), z.ravel()], 1)
    i = np.arange(20 * 21).reshape(20, 21)[:, :20].ravel()
    f = np.concatenate([np.stack([i, i + 1, i + 22], 1), np.stack([i, i + 22, i + 21], 1)])
    uv = atlas.lscm(v, f)
    sa = atlas._signed_area(uv, f)
    assert (np.sign(sa) == np.sign(sa.sum())).all()
    # lengths keep their proportions (conformal, and a cylinder unrolls exactly)
    e = np.concatenate([f[:, [0, 1]], f[:, [1, 2]]])
    ratio = np.linalg.norm(uv[e[:, 0]] - uv[e[:, 1]], axis=1) / np.linalg.norm(v[e[:, 0]] - v[e[:, 1]], axis=1)
    assert ratio.std() / ratio.mean() < 0.01


def test_a_model_without_texture_is_refused(tmp_path):
    trimesh.creation.box().export(str(tmp_path / "plain.ply"))
    with pytest.raises(ValueError, match="no texture"):
        atlas.preview(tmp_path / "plain.ply")


def seam_mask(m, keep=lambda a, b: True):
    """One byte per face, as the editor sends it: bit k when the edge from corner k to k + 1 is a
    texture seam (the faces on either side use different coordinates) and keep(a, b) on its ends."""
    f = np.asarray(m.faces)
    _, w = np.unique(np.round(np.asarray(m.vertices), 6), axis=0, return_inverse=True)
    first, seam = {}, set()
    for fi, face in enumerate(f):
        for k in range(3):
            sa, sb = face[k], face[(k + 1) % 3]
            e = tuple(sorted((w[sa], w[sb])))
            pair = (sa, sb) if w[sa] < w[sb] else (sb, sa)
            if first.setdefault(e, pair) != pair:
                seam.add(e)
    v = np.asarray(m.vertices)
    mask = np.zeros(len(f), np.uint8)
    for fi, face in enumerate(f):
        for k in range(3):
            sa, sb = face[k], face[(k + 1) % 3]
            if tuple(sorted((w[sa], w[sb]))) in seam and keep(v[sa], v[sb]):
                mask[fi] |= 1 << k
    return mask


def test_seams_drawn_by_hand_decide_the_pieces(tmp_path):
    src = scattered_box(tmp_path)
    m = trimesh.load(str(src), process=False)
    # the texture's own seams: the same 54 pieces
    assert atlas.relayout(src, tmp_path / "same.ply", seams=seam_mask(m))["pieces"] == 54
    # only the box's edges (both ends on the box's edges: two coordinates at +-0.5): one piece per side
    on_edge = lambda p: (np.abs(np.abs(p) - 0.5) < 1e-6).sum() >= 2  # noqa: E731
    info = atlas.relayout(src, tmp_path / "sides.ply", seams=seam_mask(m, lambda a, b: on_edge(a) and on_edge(b)))
    assert info["pieces"] == 6 == pieces_in(tmp_path / "sides.ply") and info["recut"] == 0
    # the front's own seams joined, the rest kept: its 9 pieces become one. Pieces that meet along an
    # edge flattened to the same coordinates must stay apart (they once were packed as one, overlapping)
    front_inside = lambda a, b: a[2] > 0.5 - 1e-6 and b[2] > 0.5 - 1e-6 and not any(  # noqa: E731
        abs(abs(a[i]) - 0.5) < 1e-6 and a[i] == b[i] for i in (0, 1))
    info = atlas.relayout(src, tmp_path / "front.ply", seams=seam_mask(m, lambda a, b: not front_inside(a, b)))
    assert info["pieces"] == 46 == pieces_in(tmp_path / "front.ply")
    # no seams at all: a closed box cannot lie flat, so it is cut further, and says so
    info = atlas.relayout(src, tmp_path / "none.ply", seams=np.zeros(len(m.faces), np.uint8))
    assert info["pieces"] > 1 and info["recut"] == 1
    with pytest.raises(ValueError, match="another model"):
        atlas.relayout(src, tmp_path / "bad.ply", seams=np.zeros(3, np.uint8))


def test_rebuilding_a_rebuilt_layout_keeps_its_size_and_pieces(tmp_path):
    src = scattered_box(tmp_path)
    first = atlas.relayout(src, tmp_path / "a.ply")
    second = atlas.relayout(tmp_path / "a.ply", tmp_path / "b.ply")
    assert second["pieces_now"] == second["pieces"] == first["pieces"] == 6
    assert second["size"] == first["size"]


def test_texture_carried_over_to_an_edited_mesh_keeps_its_colours(tmp_path):
    src = scattered_box(tmp_path)
    old = trimesh.load(str(src), process=False)
    # the "sculpted" mesh: other triangles (welded and subdivided), and the top side removed
    welded = trimesh.Trimesh(old.vertices, old.faces, process=True)
    edited = trimesh.Trimesh(*trimesh.remesh.subdivide(welded.vertices, welded.faces), process=False)
    edited.update_faces(edited.triangles_center[:, 1] < 0.5 - 1e-6)
    edited.remove_unreferenced_vertices()
    edited.export(str(tmp_path / "edited.ply"))
    info = atlas.transfer(src, tmp_path / "edited.ply", tmp_path / "carried.ply")
    out = trimesh.load(str(tmp_path / "carried.ply"), process=False)
    assert info["pieces"] == 5 == pieces_in(tmp_path / "carried.ply") and info["new_surface"] == 0
    assert len(out.faces) == len(edited.faces) and np.asarray(out.vertices)[:, 1].max() < 0.5 + 1e-6
    # the same 3D points have the same colour as on the old model (the front: a gradient)
    rng = np.random.default_rng(0)
    front = np.flatnonzero(old.triangles_center[:, 2] > 0.5 - 1e-6)
    faces, w = rng.choice(front, 300), rng.dirichlet([4, 4, 4], 300)
    pts = (w[:, :, None] * np.asarray(old.vertices)[np.asarray(old.faces)[faces]]).sum(1)
    tri = np.array([np.argmin(np.linalg.norm(trimesh.triangles.closest_point(out.triangles, np.repeat(p[None], len(out.faces), 0)) - p, axis=1))
                    for p in pts])
    bc = trimesh.triangles.points_to_barycentric(out.triangles[tri], pts)
    diff = np.abs(colours_at(old, faces, w) - colours_at(out, tri, bc)).mean(1)
    assert np.median(diff) < 3 and np.mean(diff) < 6


def pillow(textured_folder: Path, tmp_path: Path) -> Path:
    """A textured mesh plus a degenerate two-face "pillow": the same three vertices, traversed both
    ways. It flattens to no area, which used to blow the atlas up (a 8119x17 image, nothing painted)."""
    src = textured_folder
    m = trimesh.load(str(src), process=False)
    v, f = np.asarray(m.vertices), np.asarray(m.faces)
    base = len(v)
    pv = v[:3] + 2.0  # its own vertices, so it stays a piece of its own
    mesh = trimesh.Trimesh(np.vstack([v, pv]),
                           np.vstack([f, [[base, base + 1, base + 2], [base + 2, base + 1, base]]]), process=False)
    out = tmp_path / "pillow.ply"
    mesh.export(str(out))
    return out


def test_a_piece_that_flattens_to_no_area_does_not_ruin_the_atlas(tmp_path):
    # a degenerate pair on its own vertices: it must not scale itself up and squeeze everything
    # else into a few pixels (it used to give a 8119x17 atlas with nothing painted)
    src = scattered_box(tmp_path)
    mesh = pillow(src, tmp_path)
    info = atlas.transfer(src, mesh, tmp_path / "carried.ply")
    W, H = info["size"]
    assert max(W, H) < 4 * min(W, H), f"one piece squeezed the atlas: {info['size']}"
    assert info["used"] > 0.2
    out = trimesh.load(str(tmp_path / "carried.ply"), process=False)
    assert len(out.visual.material.image.getcolors(maxcolors=1 << 20)) > 10, "the carried texture is blank"


def test_the_size_of_a_piece_is_not_blown_up_by_a_collapsed_one():
    # flatten's own guard, without the atlas around it: a real piece must come out at the
    # surface's texel density (uv area = 3D area), and a piece with no area must stay at LSCM's
    # scale instead of being scaled by ~1/0
    v = np.array([[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0]], float)
    square = np.array([[0, 1, 2], [0, 2, 3]])  # a flat 1 x 1 patch: area 1
    ids, uv = atlas.flatten(v, square, np.array([[0, 0, 1.0], [0, 0, 1.0]]), np.array([0.5, 0.5]))[0]
    # uv is one vertex per (face, corner): its total area equals the patch's 3D area
    e1, e2 = uv[:, 1] - uv[:, 0], uv[:, 2] - uv[:, 0]
    flat_area = 0.5 * np.abs(e1[:, 0] * e2[:, 1] - e1[:, 1] * e2[:, 0])
    assert np.isclose(flat_area.sum(), 1.0)

    # the pillow: the same three vertices both ways, so it has no area at all
    pv = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0]], float)
    pillow_f = np.array([[0, 1, 2], [2, 1, 0]])
    _, puv = atlas.flatten(pv, pillow_f, np.array([[0, 0, 1.0], [0, 0, -1.0]]), np.array([0.5, 0.5]))[0]
    assert np.isfinite(puv).all() and np.ptp(puv, axis=0).max() < 1e3, \
        "a collapsed piece must not be scaled by millions"


def test_a_sliver_atlas_is_refused_instead_of_writing_a_blank_texture(tmp_path):
    # the shape of the real failure: one piece scaled up by millions squeezed the atlas to 8119x17
    # and every piece came out a sliver. A blank texture must never replace a good one silently.
    src = scattered_box(tmp_path)
    seen = {}

    def fake_layout(pv, gf, area, normals, lab, density, padding, limit, fit=False):
        seen.setdefault("n", 0)
        seen["n"] += 1
        if seen["n"] > 1:
            raise AssertionError("should have stopped at the first layout")
        return {"vertices": pv[gf].reshape(-1, 3), "faces": np.arange(len(gf) * 3).reshape(-1, 3),
                "uv": np.zeros((len(gf) * 3, 2)), "W": 8119, "H": 17, "used": 0.0, "pieces": 41, "recut": 0}

    mesh = trimesh.load(str(src), process=False)
    mesh.export(str(tmp_path / "m.ply"))
    out = tmp_path / "carried.ply"
    real = atlas._layout
    atlas._layout = fake_layout
    try:
        with pytest.raises(ValueError, match="degenerate"):
            atlas.transfer(src, tmp_path / "m.ply", out)
    finally:
        atlas._layout = real
    assert not out.exists(), "a blank texture must not be written over a real one"


def test_a_long_thin_layout_is_not_mistaken_for_a_broken_one(tmp_path):
    # a long, thin object packs into a long, thin image that it still fills well: carrying its
    # texture over must work (the broken layout of the pillow bug was 0% used, this one ~26%)
    rng = np.random.default_rng(0)
    T = 256
    img = Image.fromarray(rng.integers(0, 255, (T, T, 3), dtype=np.uint8))
    strip = trimesh.Trimesh([[0, 0, 0], [80, 0, 0], [80, 1, 0], [0, 1, 0]], [[0, 1, 2], [0, 2, 3]], process=False)
    strip.visual = trimesh.visual.TextureVisuals(uv=np.array([[0, 0], [1, 0], [1, 0.0125], [0, 0.0125]]), image=img)
    img.save(tmp_path / "textured0.png")
    head, rest = strip.export(file_type="ply").split(b"end_header\n", 1)
    head = head.replace(b"\nelement vertex", b"\ncomment TextureFile textured0.png\nelement vertex", 1)
    (tmp_path / "textured.ply").write_bytes(head + b"end_header\n" + rest)
    trimesh.Trimesh(strip.vertices, strip.faces, process=False).export(str(tmp_path / "edited.ply"))
    info = atlas.transfer(tmp_path / "textured.ply", tmp_path / "edited.ply", tmp_path / "carried.ply")
    W, H = info["size"]
    assert max(W, H) > 20 * min(W, H) and info["used"] > 4 * atlas.MIN_USED  # long and thin, and well filled
    assert (tmp_path / "carried.ply").exists()


def claimed_twice(px: np.ndarray, chart: np.ndarray, n: int, W: int, H: int, margin: int) -> int:
    """Texels that two pieces (each grown by margin texels) both cover, in a packed layout."""
    import cv2

    count = np.zeros((H, W), np.int32)
    for c in range(n):
        m = np.zeros((H, W), np.uint8)
        cv2.fillPoly(m, list(np.round(px[chart == c] * 16).astype(np.int32)), 1, shift=4)
        count += cv2.dilate(m, np.ones((2 * margin + 1, 2 * margin + 1), np.uint8))
    return int((count > 1).sum())


def test_pieces_are_packed_by_their_outlines_without_overlapping():
    # right triangles and L shapes: their enclosing rectangles are half or more empty, so packing
    # rectangles (as atlas.pack used to) fills under half the image; by their outlines they nest
    rng = np.random.default_rng(0)
    tri = np.array([[[0, 0], [1, 0], [0, 1]]], float)
    ell = np.array([[[0, 0], [1, 0], [1, .3]], [[0, 0], [1, .3], [.3, .3]], [[0, 0], [.3, .3], [.3, 1]], [[0, 0], [.3, 1], [0, 1]]])
    corner_uv, chart = [], []
    for c in range(40):
        shape = (tri if c % 2 else ell) * rng.uniform(0.5, 1.5)
        corner_uv.append(shape @ np.array([[np.cos(c), np.sin(c)], [-np.sin(c), np.cos(c)]]))  # any way round
        chart += [c] * len(shape)
    corner_uv, chart = np.concatenate(corner_uv), np.array(chart)
    px, W, H, used = atlas.pack(corner_uv, chart, 40, 100.0, 4, 8192)
    assert used > 0.55, f"only {used:.0%} of the image used"
    assert max(W, H) < 1.3 * min(W, H)                         # about square
    assert px.min() >= 0 and px[..., 0].max() <= W and px[..., 1].max() <= H
    assert claimed_twice(px, chart, 40, W, H, 2) == 0           # 4 texels of margin: 2 each, no overlap
    # every piece kept its shape and size (turned only, never mirrored or scaled)
    for c in range(40):
        a = atlas._signed_area(corner_uv[chart == c].reshape(-1, 2), np.arange(3 * (chart == c).sum()).reshape(-1, 3))
        b = atlas._signed_area(px[chart == c].reshape(-1, 2), np.arange(3 * (chart == c).sum()).reshape(-1, 3))
        assert np.allclose(b, a * 100 ** 2, rtol=1e-6)
    # too big for the limit: the detail is lowered until it fits; fit: scaled up to fill it
    _, W, H, _ = atlas.pack(corner_uv, chart, 40, 1000.0, 4, 512)
    assert 0.9 * 512 <= max(W, H) <= 512
    _, W, H, _ = atlas.pack(corner_uv, chart, 40, 1.0, 4, 512, fit=True)
    assert 0.97 * 512 <= max(W, H) <= 512
