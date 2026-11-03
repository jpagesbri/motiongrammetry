"""Rebuild a textured mesh's texture layout: pieces cut along the surface's sides instead of by the
photo each patch came from, then the colours copied over triangle by triangle.

OpenMVS lays out the texture by view: each patch is the stretch of surface one photo saw best, so
a scan's atlas is thousands of tiny pieces (20 000 for a 580 000-face head, median 3 triangles),
which makes painting on the flat texture hopeless. Here:

1. segment: faces join while their normals agree, measured on a smoothed copy of the mesh so the
   scan's noise does not shatter the pieces; a piece whose normals spread more than ``angle`` is
   split in two along its normals (the idea of Variational Shape Approximation, Cohen-Steiner,
   Alliez and Desbrun 2004), and pieces smaller than ``min_size`` of the surface join the
   neighbour that faces most the same way;
2. flatten: each piece by least squares conformal maps (LSCM, Levy et al. 2002), at the texel
   density of the surface; a piece that folds over is split again;
3. pack: each piece by its outline (drawn on a grid with its margin, turned by quarter turns,
   dropped where it overlaps nothing, so pieces nest in each other's hollows), at the detail the
   photo texture had (lowered only if the image would pass 8192 texels);
4. bake: the geometry does not change, only the texture coordinates, so each new texel takes the
   colour of the same point of the same triangle in the old texture (bilinear), and a margin
   around every piece is filled from its edge so filtering shows no seams.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)


# ---- segmentation -------------------------------------------------------------------------
def _components(n: int, a: np.ndarray, b: np.ndarray) -> np.ndarray:
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components

    g = coo_matrix((np.ones(len(a), np.int8), (a, b)), shape=(n, n))
    return connected_components(g, directed=False)[1]


def _means(lab: np.ndarray, normals: np.ndarray, area: np.ndarray, k: int) -> np.ndarray:
    m = np.stack([np.bincount(lab, normals[:, i] * area, k) for i in range(3)], 1)
    return m / np.maximum(np.linalg.norm(m, axis=1, keepdims=True), 1e-12)


def segment(normals: np.ndarray, area: np.ndarray, adj: np.ndarray, angle: float = 45.0,
            min_size: float = 0.005, rounds: int = 12) -> np.ndarray:
    """A piece label per face. normals: (F, 3) unit (smoothed) face normals; adj: (E, 2) faces
    sharing an edge; angle: the most a piece's normals may differ from its mean (degrees);
    min_size: the smallest piece, as a share of the total area."""
    n = len(normals)
    cos_max = np.cos(np.radians(angle))
    a, b = adj[:, 0], adj[:, 1]
    # 1. join neighbours that face nearly the same way (a third of the angle: gentle curves pass)
    close = np.einsum("ij,ij->i", normals[a], normals[b]) > np.cos(np.radians(angle / 3))
    lab = _components(n, a[close], b[close])
    # 2. split pieces whose normals spread too far: two groups by normal, then their components
    for _ in range(rounds):
        k = lab.max() + 1
        mean = _means(lab, normals, area, k)
        dev = np.einsum("ij,ij->i", normals, mean[lab])
        worst = np.full(k, 2.0)
        np.minimum.at(worst, lab, dev)
        bad = np.flatnonzero(worst < cos_max)
        if not len(bad):
            break
        side = np.zeros(n, bool)
        order = np.argsort(lab, kind="stable")
        starts = np.searchsorted(lab[order], bad)
        ends = np.searchsorted(lab[order], bad, side="right")
        for s, e in zip(starts, ends):
            f = order[s:e]
            nf, wf = normals[f], area[f]
            c1 = nf[np.argmin(nf @ mean[lab[f[0]]])].copy()   # the face that strays most, and the
            c2 = nf[np.argmin(nf @ c1)].copy()                 # one farthest from it, seed two groups
            for _ in range(4):
                g = nf @ c1 > nf @ c2
                for c, sel in ((c1, g), (c2, ~g)):
                    if sel.any():
                        c[:] = (nf[sel] * wf[sel, None]).sum(0)
                        c /= max(np.linalg.norm(c), 1e-12)
            side[f] = nf @ c1 > nf @ c2
        keep = (lab[a] == lab[b]) & (side[a] == side[b])
        lab = _components(n, a[keep], b[keep])
    # 3. small pieces join the neighbour that faces most the same way, smallest first
    k = lab.max() + 1
    parent = np.arange(k)

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    tot = np.bincount(lab, area, k)
    nsum = np.stack([np.bincount(lab, normals[:, i] * area, k) for i in range(3)], 1)
    cross = lab[a] != lab[b]
    pairs = np.unique(np.sort(np.stack([lab[a][cross], lab[b][cross]], 1), 1), axis=0)
    nbrs: dict[int, set] = {}
    for p, q in pairs:
        nbrs.setdefault(p, set()).add(q)
        nbrs.setdefault(q, set()).add(p)
    limit = min_size * area.sum()
    for p in np.argsort(tot):
        p = find(p)
        if tot[p] >= limit:
            continue
        cand = {find(q) for q in nbrs.get(p, ())} - {p}
        if not cand:
            continue
        mine = nsum[p] / max(np.linalg.norm(nsum[p]), 1e-12)
        q = max(cand, key=lambda q: float(nsum[q] @ mine) / max(np.linalg.norm(nsum[q]), 1e-12))
        parent[p] = q
        tot[q] += tot[p]
        nsum[q] += nsum[p]
        nbrs[q] = (nbrs.get(q, set()) | nbrs.get(p, set())) - {p, q}
    roots = np.array([find(x) for x in range(k)])
    return np.unique(roots[lab], return_inverse=True)[1]


# ---- flattening ---------------------------------------------------------------------------
def lscm(v: np.ndarray, f: np.ndarray) -> np.ndarray:
    """Least squares conformal map of one piece (a connected patch with a boundary): (V, 2)."""
    from scipy.sparse import coo_matrix
    from scipy.sparse.linalg import spsolve

    p0, p1, p2 = v[f[:, 0]], v[f[:, 1]], v[f[:, 2]]
    e1, e2 = p1 - p0, p2 - p0
    l1 = np.linalg.norm(e1, axis=1)
    x = e1 / np.maximum(l1, 1e-20)[:, None]
    nrm = np.cross(e1, e2)
    a2 = np.linalg.norm(nrm, axis=1)                      # twice the area
    y = np.cross(nrm / np.maximum(a2, 1e-20)[:, None], x)
    z = np.stack([np.zeros(len(f)), l1 + 0j, (e2 * x).sum(1) + 1j * (e2 * y).sum(1)], 1)  # local complex coords
    w = np.stack([z[:, 2] - z[:, 1], z[:, 0] - z[:, 2], z[:, 1] - z[:, 0]], 1)
    w = w / np.sqrt(np.maximum(a2, 1e-20))[:, None] * (a2 > 1e-20)[:, None]  # degenerate faces: no say
    nv, nf = len(v), len(f)
    # unknowns [u..., v...]; per face: Re(sum W U) = 0 and Im(sum W U) = 0 with U = u + iv
    r = np.repeat(np.arange(nf), 3)
    cols, rows, vals = [], [], []
    for row_off, cu, cv in ((0, w.real, -w.imag), (nf, w.imag, w.real)):
        rows += [r + row_off, r + row_off]
        cols += [f.ravel(), f.ravel() + nv]
        vals += [cu.ravel(), cv.ravel()]
    A = coo_matrix((np.concatenate(vals), (np.concatenate(rows), np.concatenate(cols))), shape=(2 * nf, 2 * nv)).tocsc()
    # pin the two vertices farthest apart along the piece's main direction
    c = v - v.mean(0)
    axis = np.linalg.svd(c[:: max(1, nv // 2000)], full_matrices=False)[2][0]
    t = c @ axis
    i0, i1 = int(np.argmin(t)), int(np.argmax(t))
    pinned = np.array([i0, i1, i0 + nv, i1 + nv])
    vals_p = np.array([0.0, float(t[i1] - t[i0]), 0.0, 0.0])
    free = np.setdiff1d(np.arange(2 * nv), pinned)
    Af, Ap = A[:, free], A[:, pinned]
    rhs = -(Ap @ vals_p)
    sol = spsolve((Af.T @ Af).tocsc(), Af.T @ rhs)
    out = np.zeros(2 * nv)
    out[free], out[pinned] = sol, vals_p
    return np.stack([out[:nv], out[nv:]], 1)


def _signed_area(uv: np.ndarray, f: np.ndarray) -> np.ndarray:
    a, b, c = uv[f[:, 0]], uv[f[:, 1]], uv[f[:, 2]]
    return ((b[:, 0] - a[:, 0]) * (c[:, 1] - a[:, 1]) - (c[:, 0] - a[:, 0]) * (b[:, 1] - a[:, 1])) / 2


def flatten(v: np.ndarray, f: np.ndarray, normals: np.ndarray, area: np.ndarray, adj_local=None,
            depth: int = 0) -> list[tuple[np.ndarray, np.ndarray]]:
    """[(face ids, (len, 3, 2) uv per corner)] for a piece, split while LSCM folds much of it over."""
    ids = np.arange(len(f))
    used, local = np.unique(f, return_inverse=True)
    lf = local.reshape(-1, 3)
    uv = lscm(v[used], lf) if len(f) > 1 else np.array([[0, 0], [1, 0], [0, 1.0]])[: len(used)]
    sa = _signed_area(uv, lf)
    if sa.sum() < 0:  # mirrored: flip
        uv[:, 0] *= -1
        sa = -sa
    # A scan always has a few noisy slivers that fold: they overlap their neighbours by a texel.
    # Only a piece that folds over more than that (half a percent of it) is split.
    folded = sa <= 0
    if area[folded].sum() > 0.005 * area.sum() and depth < 6 and len(f) > 8:
        # two groups by normal, then their connected parts, each flattened on its own
        c1 = normals[np.argmin(normals @ normals.mean(0))]
        g = normals @ c1 > normals @ normals[np.argmin(normals @ c1)]
        if g.all() or not g.any():
            g = (v[f].mean(1) - v[f].mean((0, 1))) @ np.linalg.svd(v[f].mean(1) - v[f].mean((0, 1)), full_matrices=False)[2][0] > 0
        out = []
        for sel in (g, ~g):
            sub = ids[sel]
            sf = f[sub]
            # connected parts of the group
            e = np.concatenate([sf[:, [0, 1]], sf[:, [1, 2]], sf[:, [2, 0]]])
            key = np.sort(e, 1)
            fid = np.tile(np.arange(len(sf)), 3)
            o = np.lexsort((key[:, 1], key[:, 0]))
            same = np.all(key[o][1:] == key[o][:-1], 1)
            parts = _components(len(sf), fid[o][1:][same], fid[o][:-1][same])
            for p in range(parts.max() + 1):
                q = sub[parts == p]
                for fids, uvs in flatten(v, f[q], normals[q], area[q], depth=depth + 1):
                    out.append((q[fids], uvs))
        return out
    # at the texel density of the surface: uv area = 3D area
    total = float(np.abs(sa).sum())
    # A piece that flattens to (nearly) no area - two faces on the same three vertices, traversed
    # both ways, do exactly that - would need a scale of ~1/0 to reach the surface's texel density.
    # That blows its extent up by millions, which wrecks the packer for every other piece: the
    # atlas comes out a few pixels tall and nothing is painted. Only a true collapse does this
    # (LSCM keeps a piece's scale, so a real one needs a factor near 1); leave it unscaled, where
    # it covers no texel, instead of poisoning the whole layout.
    if not total > 1e-9 * max(float(area.sum()), 1e-30):
        log.warning("a piece of %d face(s) flattened to no area: packed unscaled", len(f))
        s = 1.0
    else:
        s = np.sqrt(area.sum() / total)
    return [(ids, (uv * s)[lf])]


# ---- the whole thing ----------------------------------------------------------------------
def texture_files(src: Path) -> list[str]:
    """The images a textured PLY names (its "comment TextureFile" lines, in order: OpenMVS splits a
    texture larger than --max-texture-size, 8192, over several, and says per face which one)."""
    with open(src, "rb") as fh:
        head = fh.read(1 << 16).split(b"end_header", 1)[0]
    return [ln.split(None, 2)[2].decode().strip() for ln in head.splitlines()
            if ln.startswith(b"comment TextureFile ") and len(ln.split(None, 2)) == 3]


def _texnumber(m, n_images: int) -> np.ndarray:
    """Per face, which of the model's images it is textured from (OpenMVS's "texnumber")."""
    try:
        tn = np.asarray(m.metadata["_ply_raw"]["face"]["data"]["texnumber"]).ravel().astype(np.int64)
    except (KeyError, ValueError, TypeError):
        raise ValueError(f"the model names {n_images} texture images but not which face uses which")
    if len(tn) != len(m.faces) or tn.min() < 0 or tn.max() >= n_images:
        raise ValueError("the model's faces name texture images it does not have")
    return tn


SHEET_GAP = 16  # texels between the images of a split texture laid side by side, so lookups never mix them


def _one_sheet(m, images: list, tn: np.ndarray):
    """A model textured from several images as one textured from them side by side (left to right,
    SHEET_GAP apart): the same surface and colours, its coordinates moved to each face's image.
    A vertex shared by faces of different images is split. Returns (mesh, image)."""
    import trimesh
    from PIL import Image

    f, uv, v = np.asarray(m.faces), np.asarray(m.visual.uv, np.float64), np.asarray(m.vertices)
    ws = [im.size[0] for im in images]
    W, H = sum(ws) + SHEET_GAP * (len(images) - 1), max(im.size[1] for im in images)
    x0 = np.concatenate([[0], np.cumsum(np.array(ws) + SHEET_GAP)[:-1]])
    sheet = Image.new("RGB", (W, H))
    for im, x in zip(images, x0):
        sheet.paste(im, (int(x), 0))
    key = np.stack([f.ravel(), np.repeat(tn, 3)], 1)
    uniq, vid = np.unique(key, axis=0, return_inverse=True)
    t = uniq[:, 1]
    iw, ih = np.array([im.size[0] for im in images])[t], np.array([im.size[1] for im in images])[t]
    px = np.stack([x0[t] + uv[uniq[:, 0], 0] * iw, (1 - uv[uniq[:, 0], 1]) * ih], 1)  # texels, y down
    out = trimesh.Trimesh(v[uniq[:, 0]], vid.reshape(-1, 3), process=False)
    out.visual = trimesh.visual.TextureVisuals(uv=np.stack([px[:, 0] / W, 1 - px[:, 1] / H], 1), image=sheet)
    return out, sheet


def _load(src: Path, preview: int | None = None):
    """The textured mesh and its texture (float RGB); for a preview, the texture at most twice the
    preview's size (read at a lower resolution, it is quick). A texture split over several images
    comes as one, the images side by side (_one_sheet)."""
    import trimesh
    from PIL import Image

    m = trimesh.load(str(src), process=False)
    if getattr(m.visual, "uv", None) is None:
        raise ValueError("the model has no texture to lay out")
    names = texture_files(src)
    if len(names) > 1:
        missing = [n for n in names if not (src.parent / n).exists()]
        if missing:
            raise ValueError(f"the model's texture images are missing: {', '.join(missing)}")
        m, img = _one_sheet(m, [Image.open(src.parent / n).convert("RGB") for n in names], _texnumber(m, len(names)))
    else:
        img = getattr(m.visual.material, "image", None) if hasattr(m.visual, "material") else None
        if img is None:
            raise ValueError("the model has no texture to lay out")
    img = img.convert("RGB")
    if preview:
        img.thumbnail((2 * preview, 2 * preview), Image.BILINEAR)
    return m, np.asarray(img, np.float32)


def textured_scene(src: Path):
    """A PLY as a trimesh scene to export (GLB): a texture split over several images becomes one
    mesh per image, in their order, each with its own material, since trimesh reads only the first
    image (every face would show it). Anything else: as trimesh loads it."""
    import trimesh
    from PIL import Image

    names = texture_files(src) if src.suffix.lower() == ".ply" else []
    if len(names) < 2:
        return trimesh.load(str(src), force="scene", process=False)
    m = trimesh.load(str(src), process=False)
    tn = _texnumber(m, len(names))
    f, uv, v = np.asarray(m.faces), np.asarray(m.visual.uv), np.asarray(m.vertices)
    scene = trimesh.Scene()
    for i, name in enumerate(names):
        sel = f[tn == i]
        if not len(sel):  # keep the images' order (the editor saves paint by it): one empty face
            sel = f[:1, [0, 0, 0]]
        used, local = np.unique(sel, return_inverse=True)
        part = trimesh.Trimesh(v[used], local.reshape(-1, 3), process=False)
        part.visual = trimesh.visual.TextureVisuals(uv=uv[used], image=Image.open(src.parent / name).convert("RGB"))
        scene.add_geometry(part, node_name=f"texture{i}", geom_name=f"texture{i}")
    return scene


def count_pieces(f: np.ndarray, uv: np.ndarray, v: np.ndarray) -> int:
    """Texture pieces: faces joined where they share an edge with the same coordinates on it."""
    return int(_piece_labels(f, uv, v).max() + 1)


def _piece_labels(f: np.ndarray, uv: np.ndarray, v: np.ndarray) -> np.ndarray:
    _, pid = np.unique(np.round(v, 6), axis=0, return_inverse=True)
    e = np.concatenate([f[:, [0, 1]], f[:, [1, 2]], f[:, [2, 0]]])
    fid = np.tile(np.arange(len(f)), 3)
    pe = pid[e]
    flip = pe[:, 0] > pe[:, 1]
    pe = np.where(flip[:, None], pe[:, ::-1], pe)
    ue = np.where(flip[:, None, None], uv[e][:, ::-1], uv[e])
    k = np.concatenate([pe, np.round(ue.reshape(len(e), -1) * 1e6).astype(np.int64)], 1)
    _, inv = np.unique(k, axis=0, return_inverse=True)
    o = np.argsort(inv, kind="stable")
    same = inv[o][1:] == inv[o][:-1]
    return _components(len(f), fid[o][1:][same], fid[o][:-1][same])


def _geometry(m, smooth: int | None):
    """Welded geometry and the face normals pieces are cut by. smooth: Taubin iterations, or None
    for about 1% of the model's size (a dense scan's noise goes, a coarse model is left as it is)."""
    import trimesh

    v, f = np.asarray(m.vertices, np.float64), np.asarray(m.faces)
    pv, pid = np.unique(np.round(v, 6), axis=0, return_inverse=True)  # a textured PLY splits vertices at seams
    gf = pid[f]
    g = trimesh.Trimesh(pv, gf, process=False)
    area = g.area_faces
    adj, adj_edges = g.face_adjacency, g.face_adjacency_edges
    if smooth is None:  # diffusion spreads ~sqrt(iterations) edges
        reach = 0.01 * float(np.linalg.norm(np.ptp(pv, axis=0))) / max(float(g.edges_unique_length.mean()), 1e-12)
        smooth = int(np.clip(round(reach ** 2), 0, 60))
    if smooth:
        s = trimesh.Trimesh(pv.copy(), gf, process=False)
        trimesh.smoothing.filter_taubin(s, iterations=smooth)
        normals = s.face_normals
    else:
        normals = g.face_normals
    return pv, gf, area, adj, adj_edges, normals


def preview(src: Path, angle: float = 45.0, min_size: float = 0.005, smooth: int | None = None) -> dict:
    """How many pieces the current texture has and how many a rebuild would make."""
    m, _ = _load(src)
    pv, gf, area, adj, _, normals = _geometry(m, smooth)
    pieces = sum(len(p) for p in _pieces(pv, gf, area, normals, segment(normals, area, adj, angle, min_size)))
    return {"pieces_now": count_pieces(np.asarray(m.faces), np.asarray(m.visual.uv), np.asarray(m.vertices)),
            "pieces": pieces, "faces": len(gf)}


def _pieces(pv, gf, area, normals, lab):
    """Per segment, its flattened pieces: [[(face ids, uv per corner), ...], ...]."""
    order = np.argsort(lab, kind="stable")
    bounds = np.searchsorted(lab[order], np.arange(lab.max() + 2))
    out = []
    for i in range(lab.max() + 1):
        fids = order[bounds[i]:bounds[i + 1]]
        out.append([(fids[sub], uvs) for sub, uvs in flatten(pv, gf[fids], normals[fids], area[fids])])
    return out


def from_seams(gf: np.ndarray, adj: np.ndarray, adj_edges: np.ndarray, seams: np.ndarray) -> np.ndarray:
    """A piece label per face from seams drawn by hand: seams[f] has bit k set when face f's edge
    from corner k to corner k + 1 is a seam. Faces join across every other shared edge."""
    if len(seams) != len(gf):
        raise ValueError("the seams are for another model (the faces differ): reload the editor")
    e = np.stack([gf, np.roll(gf, -1, axis=1)], 2)                 # (F, 3, 2): edge k = corner k -> k + 1
    cut = ((seams[:, None].astype(np.uint8) >> np.arange(3, dtype=np.uint8)) & 1).astype(bool)
    n = int(gf.max()) + 1
    key = lambda pairs: (np.minimum(pairs[:, 0], pairs[:, 1]).astype(np.int64) * n  # noqa: E731
                         + np.maximum(pairs[:, 0], pairs[:, 1]))
    keep = ~np.isin(key(adj_edges), key(e[cut]))
    return _components(len(gf), adj[keep, 0], adj[keep, 1])


def _detail(f: np.ndarray, uv: np.ndarray, v: np.ndarray, area: np.ndarray, ow: int, oh: int) -> tuple[float, int]:
    """Texels per unit of length to keep, and the number of pieces, of a textured mesh.

    OpenMVS gives each patch the detail of its photo, so keep the detail that 90% of the surface has
    (by area), not the average, or the sharp parts soften. Measured per piece (its texels over its
    area): within a flattened piece the stretch varies a little, and a per-triangle measure would
    grow the image at every rebuild."""
    lab = _piece_labels(f, uv, v)
    k = lab.max() + 1
    piece_area = np.bincount(lab, area, k)
    per_piece = np.sqrt(np.bincount(lab, np.abs(_signed_area(uv * [ow, oh], f)), k) / np.maximum(piece_area, 1e-20))
    o = np.argsort(per_piece)
    cum = np.cumsum(piece_area[o]) / max(piece_area.sum(), 1e-20)
    return float(per_piece[o][min(np.searchsorted(cum, 0.9), len(o) - 1)]), int(k)


def _layout(pv, gf, area, normals, lab, density, padding, limit, fit=False) -> dict:
    """Flatten and pack the pieces lab labels: one vertex per (piece, mesh vertex), so the pieces'
    seams split the mesh. Returns their vertices, faces, coordinates (v up) and the image size."""
    corner_uv = np.zeros((len(gf), 3, 2))
    chart = np.zeros(len(gf), np.int64)
    n = recut = 0
    for seg in _pieces(pv, gf, area, normals, lab):
        recut += len(seg) > 1  # folded over: cut further so it lies flat
        for fids, uvs in seg:
            corner_uv[fids] = uvs
            chart[fids] = n
            n += 1
    key = np.stack([np.repeat(chart, 3), gf.ravel()], 1)
    uniq, vid = np.unique(key, axis=0, return_inverse=True)
    vid = vid.reshape(-1, 3)
    corner_px, W, H, used = pack(corner_uv, chart, n, density, padding, limit, fit=fit)
    tri = np.arange(len(gf) * 3).reshape(-1, 3)
    kept = np.sqrt(np.abs(_signed_area(corner_px.reshape(-1, 2), tri)).sum()
                   / max(np.abs(_signed_area(corner_uv.reshape(-1, 2), tri)).sum(), 1e-30)) / density
    uvs = np.zeros((len(uniq), 2))
    uvs[vid.ravel()] = (corner_px / [W, H]).reshape(-1, 2)
    uvs[:, 1] = 1 - uvs[:, 1]  # pixels count down, texture coordinates up
    return {"vertices": pv[uniq[:, 1]], "faces": vid, "uv": uvs, "W": W, "H": H, "used": used,
            "pieces": n, "recut": recut, "detail": min(1.0, float(kept))}


def _write(dst: Path, vertices: np.ndarray, faces: np.ndarray, uv: np.ndarray, image: np.ndarray) -> None:
    """A textured PLY naming its PNG next to it (dst.stem + "0.png"), as OpenMVS writes them."""
    import trimesh
    from PIL import Image

    out = trimesh.Trimesh(vertices, faces, process=False)
    out.visual = trimesh.visual.TextureVisuals(uv=uv, image=Image.fromarray(image))
    png = dst.with_name(dst.stem + "0.png")
    Image.fromarray(image).save(png, optimize=False, compress_level=6)
    head, rest = out.export(file_type="ply").split(b"end_header\n", 1)
    head = head.replace(b"\nelement vertex", f"\ncomment TextureFile {png.name}\nelement vertex".encode(), 1)
    tmp = dst.with_suffix(".tmp.ply")
    tmp.write_bytes(head + b"end_header\n" + rest)
    tmp.replace(dst)


def relayout(src: Path, dst: Path, angle: float = 45.0, min_size: float = 0.005, smooth: int | None = None,
             size: int | None = None, padding: int = 4, max_size: int = 8192, seams: np.ndarray | None = None,
             preview: int | None = None) -> dict:
    """Write dst (a textured PLY naming its PNG, like OpenMVS's) with the new layout of src's texture.
    size: fit the atlas in a size x size image; by default it keeps the old texture's detail (texels
    per unit of surface), up to max_size. seams: pieces from seams drawn by hand (see from_seams)
    instead of cut by the surface's sides. preview: a quick look at most this many texels wide (the
    old texture read at a lower resolution too)."""
    t0 = time.time()
    m, old = _load(src, preview)
    if preview:
        max_size, padding = preview, 2
    f_old, uv_old = np.asarray(m.faces), np.asarray(m.visual.uv, np.float64)
    if seams is None:
        pv, gf, area, adj, _, normals = _geometry(m, smooth)
        lab = segment(normals, area, adj, angle, min_size)
    else:
        pv, gf, area, adj, adj_edges, normals = _geometry(m, 0)
        lab = from_seams(gf, adj, adj_edges, seams)
    t_seg = time.time()
    density, k = _detail(f_old, uv_old, np.asarray(m.vertices), area, old.shape[1], old.shape[0])
    lay = _layout(pv, gf, area, normals, lab, density, padding, size or max_size, fit=bool(size))
    t_pack = time.time()
    # the same triangles: each new texel takes the colour of the same point in the old texture
    sample, corners = _sampler(old, uv_old[f_old]), uv_old[f_old]
    new = _fill(lay["W"], lay["H"], lay["uv"][lay["faces"]],
                lambda tri, bc: sample(np.einsum("nk,nkj->nj", bc, corners[tri])), padding)
    t_bake = time.time()
    _write(dst, lay["vertices"], lay["faces"], lay["uv"], new)
    info = {"pieces_now": k, "pieces": lay["pieces"], "recut": lay["recut"], "size": [lay["W"], lay["H"]],
            "used": round(lay["used"], 2), "faces": len(gf),
            # the share of the old texture's detail kept: below 1 when it had to shrink to max_size
            "detail": round(lay["detail"], 2) if not (preview or size) else 1.0,
            "seconds": {"segment": round(t_seg - t0, 1), "layout": round(t_pack - t_seg, 1), "bake": round(t_bake - t_pack, 1)}}
    log.info("texture layout: %d -> %d pieces, %dx%d, %.0f%% used, %.0f s", k, lay["pieces"], lay["W"], lay["H"],
             100 * info["used"], time.time() - t0)
    return info


MIN_USED = 0.05  # a layout painting less of its image than this is broken (good ones: 30-70%)
NEW_SURFACE = 0.005  # carrying a texture over: farther than this share of the model's size from the old surface is new


def _blend_in(colour: np.ndarray, known: np.ndarray, f: np.ndarray) -> np.ndarray:
    """Vertex colours where known, and elsewhere the smoothest blend of them over the mesh (each
    vertex the mean of its neighbours: a harmonic fill, as the editor's lids blend in their rim's
    colours). New surface touching no known vertex (a separate new part) gets neutral grey."""
    from scipy.sparse import coo_matrix
    from scipy.sparse.linalg import spsolve

    out = colour.astype(np.float64).copy()
    unk = np.flatnonzero(~known)
    if not len(unk):
        return out
    e = np.unique(np.sort(np.concatenate([f[:, [0, 1]], f[:, [1, 2]], f[:, [2, 0]]]), 1), axis=0)
    e = e[e[:, 0] != e[:, 1]]
    e = np.concatenate([e, e[:, ::-1]])
    n = len(colour)
    idx = np.full(n, -1)
    idx[unk] = np.arange(len(unk))
    # unknown vertices with no path to a known one: grey
    lab = _components(n, e[:, 0], e[:, 1])
    anchored = np.zeros(lab.max() + 1, bool)
    anchored[lab[known]] = True
    lone = unk[~anchored[lab[unk]]]
    out[lone] = 128
    solve = unk[anchored[lab[unk]]]
    if not len(solve):
        return out
    idx = np.full(n, -1)
    idx[solve] = np.arange(len(solve))
    row = e[idx[e[:, 0]] >= 0]                                   # edges from a vertex to solve
    deg = np.bincount(idx[row[:, 0]], minlength=len(solve)).astype(np.float64)
    inner = idx[row[:, 1]] >= 0
    A = coo_matrix((np.concatenate([deg, -np.ones(inner.sum())]),
                    (np.concatenate([np.arange(len(solve)), idx[row[inner, 0]]]),
                     np.concatenate([np.arange(len(solve)), idx[row[inner, 1]]]))), shape=(len(solve),) * 2).tocsc()
    rhs = np.zeros((len(solve), 3))
    np.add.at(rhs, idx[row[~inner, 0]], out[row[~inner, 1]])
    out[solve] = spsolve(A, rhs).reshape(len(solve), 3)
    return np.clip(out, 0, 255)


def transfer(src: Path, mesh: Path, dst: Path, angle: float = 45.0, min_size: float = 0.005, padding: int = 4,
             max_size: int = 8192) -> dict:
    """Carry src's texture over to another mesh of the same subject (the editor's sculpted one),
    without the photos: mesh gets its own layout (cut by its sides, as relayout) and each texel the
    colour of the nearest point of src's textured surface. Removed parts are simply gone; new
    surface (closed holes, merged volume) takes a smooth blend of the colours around it."""
    import open3d as o3d
    import trimesh

    t0 = time.time()
    m, old = _load(src)
    f_src, uv_src, v_src = np.asarray(m.faces), np.asarray(m.visual.uv, np.float64), np.asarray(m.vertices)
    target = trimesh.load(str(mesh), force="mesh", process=False)
    pv, gf, area, adj, _, normals = _geometry(target, None)
    lab = segment(normals, area, adj, angle, min_size)
    src_area = trimesh.Trimesh(v_src, f_src, process=False).area_faces
    density, k = _detail(f_src, uv_src, v_src, src_area, old.shape[1], old.shape[0])
    lay = _layout(pv, gf, area, normals, lab, density, padding, max_size)
    t_lay = time.time()
    if lay["used"] < MIN_USED:
        # A piece that flattened to no area scaled itself by millions and squeezed every other
        # piece into a few pixels: almost none of the image is painted and the model comes out
        # blank. Writing that would replace a good texture with a blank one and report success,
        # so refuse, before baking it. (Judged by the share painted, not the image's shape: a
        # long, thin object is laid out long and thin and still fills it well.)
        raise ValueError(f"the new layout is degenerate ({lay['pieces']} pieces in {lay['W']}x{lay['H']}, "
                         f"{100 * lay['used']:.0f}% used): a piece of the mesh flattens to no area, so it "
                         f"needs cleaning before it can be textured")
    scene = o3d.t.geometry.RaycastingScene()
    scene.add_triangles(o3d.core.Tensor(v_src.astype(np.float32)), o3d.core.Tensor(f_src.astype(np.uint32)))
    sample, corners = _sampler(old, uv_src[f_src]), uv_src[f_src]

    def nearest(p):
        r = scene.compute_closest_points(o3d.core.Tensor(p.astype(np.float32)))
        pid = r["primitive_ids"].numpy().astype(np.int64)
        pu = r["primitive_uvs"].numpy().astype(np.float64)  # point = (1-u-v) a + u b + v c
        w = np.stack([1 - pu.sum(1), pu[:, 0], pu[:, 1]], 1)
        return sample(np.einsum("nk,nkj->nj", w, corners[pid])), np.linalg.norm(r["points"].numpy() - p, axis=1)

    # New surface (farther than tol from the old one: a lid, a merged volume) does not take the
    # colour of the nearest old point: across a big lid that is the rim's colours stretched into
    # streaks, and whatever lies behind it (a shoe's insole through its closed sole). It takes the
    # colours of the old surface around it, blended smoothly over it (harmonic, see _blend_in),
    # fading in from tol / 2 to tol so there is no edge where it starts.
    size = float(np.linalg.norm(np.ptp(pv, axis=0)))
    tol = NEW_SURFACE * size
    vcol, vd = nearest(pv)
    vcol = _blend_in(vcol, vd <= tol / 2, gf)
    far = []

    def colour(tri, bc):
        c, d = nearest(np.einsum("nk,nkj->nj", bc, pv[gf[tri]]))
        far.append(d)
        t = np.clip((d - tol / 2) / (tol / 2), 0, 1)[:, None]
        return c * (1 - t) + np.einsum("nk,nkj->nj", bc, vcol[gf[tri]]) * t

    new = _fill(lay["W"], lay["H"], lay["uv"][lay["faces"]], colour, padding)
    t_bake = time.time()
    _write(dst, lay["vertices"], lay["faces"], lay["uv"], new)
    d = np.concatenate(far) if far else np.zeros(0)
    info = {"pieces_now": k, "pieces": lay["pieces"], "recut": lay["recut"], "size": [lay["W"], lay["H"]],
            "used": round(lay["used"], 2), "faces": len(gf), "detail": round(lay["detail"], 2),
            # how much of the surface is new (more than 0.5% of the model's size from the old one)
            "new_surface": round(float((d > tol).mean()), 3) if len(d) else 0.0,
            "seconds": {"layout": round(t_lay - t0, 1), "bake": round(t_bake - t_lay, 1)}}
    log.info("texture carried over: %d pieces, %dx%d, %.0f%% new surface, %.0f s", lay["pieces"], lay["W"], lay["H"],
             100 * info["new_surface"], time.time() - t0)
    return info


def _min_rect(p: np.ndarray) -> np.ndarray:
    """Rotation (2 x 2) that turns points to their smallest enclosing rectangle, wider than tall."""
    from scipy.spatial import ConvexHull, QhullError

    try:
        h = p[ConvexHull(p).vertices]
    except (QhullError, ValueError):  # a line or a point
        h = p
    e = np.roll(h, -1, 0) - h
    ang = np.unique(np.mod(np.arctan2(e[:, 1], e[:, 0]), np.pi / 2))
    best, rot = np.inf, np.eye(2)
    for a in ang:
        c, s_ = np.cos(a), np.sin(a)
        r = np.array([[c, s_], [-s_, c]])
        q = h @ r.T
        ext = np.ptp(q, 0)
        if ext[0] * ext[1] < best:
            best, rot = ext[0] * ext[1], (r if ext[0] >= ext[1] else np.array([[0, 1], [-1, 0]]) @ r)
    return rot


_TURNS = [np.eye(2), np.array([[0.0, -1.0], [1.0, 0.0]]), -np.eye(2), np.array([[0.0, 1.0], [-1.0, 0.0]])]


def _outline(q: np.ndarray, padding: int, cell: int) -> np.ndarray:
    """The grid cells (cell x cell texels) a piece covers: its triangles q ((k, 3, 2) texels, x right,
    y down, from padding up) drawn with their edges, grown by padding texels. Conservative: a texel
    the piece or its margin touches is always in a covered cell."""
    import cv2

    ext = np.ceil(q.reshape(-1, 2).max(0)).astype(np.int64) + padding + 2
    hc, wc = -(-int(ext[1]) // cell), -(-int(ext[0]) // cell)
    m = np.zeros((hc * cell, wc * cell), np.uint8)
    pts = list(np.round(q * 16).astype(np.int32))           # 4 bits of sub-texel precision
    cv2.fillPoly(m, pts, 1, lineType=cv2.LINE_8, shift=4)
    cv2.polylines(m, pts, True, 1, lineType=cv2.LINE_8, shift=4)  # slivers fillPoly can miss
    if padding:
        m = cv2.dilate(m, np.ones((2 * padding + 1, 2 * padding + 1), np.uint8))
    return m.reshape(hc, cell, wc, cell).max((1, 3))


def _drop(pieces: list, s: float, padding: int, side: float):
    """One packing pass at s texels per unit (see pack): positions in texels per piece, W, H; None if
    the canvas (about side texels) was too small."""
    import cv2

    biggest = max(float(np.ptp(q.reshape(-1, 2), 0).max()) for _, q, _ in pieces) * s + 2 * padding + 2
    span = max(side, biggest)
    cell = max(1, int(np.ceil(span / 1200)))                # the grid: at most ~1200 cells across
    C = int(np.ceil(span / cell)) + 2
    occ = np.zeros((C, C), np.uint8)
    cur_w = cur_h = 0
    out = {}
    for c in sorted(range(len(pieces)), key=lambda c: -pieces[c][2]):
        _, q, _ = pieces[c]
        best = None
        for turn in _TURNS:
            qq = (q.reshape(-1, 2) @ turn.T) * s
            qq = qq - qq.min(0) + padding
            m = _outline(qq.reshape(-1, 3, 2), padding, cell)
            h, w = m.shape
            if h > C or w > C:
                continue
            # Beyond the square that holds the pieces so far everything is free, so a place past
            # its side never beats one just at it: search only that far.
            S = max(cur_w, cur_h)
            sub = occ[:min(C, S + h), :min(C, S + w)]
            if sub.shape[0] < h or sub.shape[1] < w:
                continue
            hits = (cv2.matchTemplate(sub, m, cv2.TM_CCORR) if sub.shape != m.shape
                    else np.array([[float((sub.astype(np.int64) * m).sum())]], np.float32))
            ys, xs = np.arange(hits.shape[0]), np.arange(hits.shape[1])
            # the smallest square holding everything, then the place nearest the corner
            key = (np.maximum(np.maximum(cur_h, ys + h)[:, None], np.maximum(cur_w, xs + w)[None, :]).astype(np.int64)
                   * (4 * C) + ys[:, None] + xs[None, :])
            key[hits > 0.5] = np.iinfo(np.int64).max
            k = int(np.argmin(key))
            if key.flat[k] == np.iinfo(np.int64).max:
                continue
            if best is None or key.flat[k] < best[0]:
                y, x = divmod(k, hits.shape[1])
                best = (key.flat[k], x, y, m, qq)
        if best is None:
            return None
        _, x, y, m, qq = best
        occ[y:y + m.shape[0], x:x + m.shape[1]] |= m
        cur_w, cur_h = max(cur_w, x + m.shape[1]), max(cur_h, y + m.shape[0])
        out[c] = qq.reshape(-1, 3, 2) + [x * cell, y * cell]
    ext = np.concatenate([p.reshape(-1, 2) for p in out.values()]).max(0)
    return out, int(np.ceil(ext[0])) + padding + 1, int(np.ceil(ext[1])) + padding + 1


def pack(corner_uv: np.ndarray, chart: np.ndarray, n: int, density: float, padding: int, limit: int,
         fit: bool = False):
    """Lay the pieces out in one image by their outlines, not their enclosing rectangles: each
    piece, turned to its smallest rectangle and then by quarter turns, is drawn on a grid with a
    margin of padding texels, and dropped where it overlaps no other, keeping the image as small
    and square as it can (largest pieces first) - so pieces nest in each other's hollows.
    corner_uv: (F, 3, 2) per piece, in surface units; density: texels per unit, lowered until the
    image fits in limit (fit: scaled to fill limit instead).
    Returns ((F, 3, 2) pixel positions, x right and y down, W, H, share of the image used)."""
    order = np.argsort(chart, kind="stable")
    bounds = np.searchsorted(chart[order], np.arange(n + 1))
    pieces = []
    for c in range(n):
        f = order[bounds[c]:bounds[c + 1]]
        p = corner_uv[f].reshape(-1, 2)
        q = (p - p.mean(0)) @ _min_rect(p - p.mean(0)).T
        pieces.append((f, q.reshape(-1, 3, 2), float(np.abs(_signed_area(q, np.arange(len(q)).reshape(-1, 3))).sum())))
    area = sum(a for _, _, a in pieces)
    s = density if not fit else limit / max(np.sqrt(area / 0.5), 1e-20)
    grow = 1.6  # the canvas: this much wider than a square the pieces would fill half of
    for _ in range(12):
        got = _drop(pieces, s, padding, grow * np.sqrt(area * s * s / 0.5))
        if got is None:
            grow *= 1.5
            continue
        placed, W, H = got
        if max(W, H) <= limit and (not fit or max(W, H) >= 0.97 * limit):
            break
        s *= (limit / max(W, H)) * (0.98 if max(W, H) > limit else 1.0)
    px = np.zeros_like(corner_uv)
    for c, (f, _, _) in enumerate(pieces):
        px[f] = placed[c]
    used = float(np.abs(_signed_area(px.reshape(-1, 2), np.arange(px.size // 2).reshape(-1, 3))).sum()) / (W * H)
    return px, W, H, used


def _raster(uv: np.ndarray, W: int, H: int, batch: int = 4_000_000):
    """The texels (centres) inside each triangle of a W x H image: yields (triangle, x, y,
    barycentric) in batches. uv: (F, 3, 2), v up."""
    P = np.stack([uv[..., 0] * W, (1 - uv[..., 1]) * H], -1)  # image pixels, y down
    lo = np.clip(np.floor(P.min(1)).astype(np.int64), 0, [W - 1, H - 1])
    hi = np.clip(np.ceil(P.max(1)).astype(np.int64), 0, [W - 1, H - 1])
    bw, bh = hi[:, 0] - lo[:, 0] + 1, hi[:, 1] - lo[:, 1] + 1
    a, e1, e2 = P[:, 0], P[:, 1] - P[:, 0], P[:, 2] - P[:, 0]
    det = e1[:, 0] * e2[:, 1] - e1[:, 1] * e2[:, 0]
    ok = np.abs(det) > 1e-12
    inv = np.zeros((len(P), 2, 2))
    inv[ok] = np.stack([np.stack([e2[ok, 1], -e2[ok, 0]], 1), np.stack([-e1[ok, 1], e1[ok, 0]], 1)], 1) / det[ok, None, None]
    count = bw * bh * ok
    starts = np.concatenate([[0], np.cumsum(count)])
    t = 0
    while t < len(P):
        t1 = max(int(np.searchsorted(starts, starts[t] + batch, side="right")) - 1, t + 1)
        tri = np.repeat(np.arange(t, t1), count[t:t1])
        if len(tri):
            off = np.arange(len(tri)) - (starts[tri] - starts[t])
            px = lo[tri, 0] + off % bw[tri]
            py = lo[tri, 1] + off // bw[tri]
            d = np.stack([px + 0.5 - a[tri, 0], py + 0.5 - a[tri, 1]], 1)
            l12 = np.einsum("nij,nj->ni", inv[tri], d)
            bc = np.concatenate([1 - l12.sum(1, keepdims=True), l12], 1)
            inside = (bc > -1e-3).all(1)
            yield tri[inside], px[inside], py[inside], bc[inside]
        t = t1


def _grow(img: np.ndarray, hit: np.ndarray, rings: int) -> None:
    """Fill the texels around the covered ones (hit) from their covered neighbours, ring by ring."""
    from scipy import ndimage

    k = np.ones((3, 3), np.float32)
    for _ in range(rings):
        ring = ~hit & ndimage.binary_dilation(hit)
        if not ring.any():
            break
        s = np.stack([ndimage.convolve(img[..., i] * hit, k, mode="constant") for i in range(3)], -1)
        n = ndimage.convolve(hit.astype(np.float32), k, mode="constant")
        img[ring] = s[ring] / n[ring, None]
        hit |= ring


def _sampler(old: np.ndarray, corners: np.ndarray):
    """Bilinear lookups in a texture (coordinates v up), its pieces first grown into their
    background so a lookup at an edge does not pick the background up. corners: (F, 3, 2)."""
    oh, ow = old.shape[:2]
    old = old.copy()
    covered = np.zeros((oh, ow), bool)
    for _, px, py, _ in _raster(corners, ow, oh):
        covered[py, px] = True
    _grow(old, covered, 3)

    def sample(q: np.ndarray) -> np.ndarray:
        x = np.clip(q[:, 0] * ow - 0.5, 0, ow - 1.001)
        y = np.clip((1 - q[:, 1]) * oh - 0.5, 0, oh - 1.001)
        x0, y0 = x.astype(np.int64), y.astype(np.int64)
        fx, fy = (x - x0)[:, None], (y - y0)[:, None]
        return (old[y0, x0] * (1 - fx) * (1 - fy) + old[y0, x0 + 1] * fx * (1 - fy)
                + old[y0 + 1, x0] * (1 - fx) * fy + old[y0 + 1, x0 + 1] * fx * fy)

    return sample


def _fill(W: int, H: int, uv: np.ndarray, colour, padding: int = 4) -> np.ndarray:
    """A W x H texture: every texel inside a triangle (uv: (F, 3, 2), v up) gets colour(triangle,
    barycentric); then a margin around every piece, filled from its edge, so filtering shows no seams."""
    out = np.zeros((H, W, 3), np.float32)
    hit = np.zeros((H, W), bool)
    for tri, px, py, bc in _raster(uv, W, H):
        out[py, px] = colour(tri, bc)
        hit[py, px] = True
    _grow(out, hit, padding + 2)
    out[~hit] = 128
    return np.clip(out + 0.5, 0, 255).astype(np.uint8)


def bake(old: np.ndarray, uv_from: np.ndarray, uv_to: np.ndarray, W: int, H: int, padding: int = 4) -> np.ndarray:
    """Copy a texture across layouts of the same triangles: every texel of the new (W x H) image
    takes the old texture's colour at the same point of its triangle. uv_from / uv_to: (F, 3, 2)."""
    sample = _sampler(old, uv_from)
    return _fill(W, H, uv_to, lambda tri, bc: sample(np.einsum("nk,nkj->nj", bc, uv_from[tri])), padding)
