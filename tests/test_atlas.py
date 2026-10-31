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
