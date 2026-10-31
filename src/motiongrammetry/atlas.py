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
3. pack: the pieces into one image with xatlas (MIT, used through its pack-only mode);
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
    s = np.sqrt(area.sum() / max(np.abs(sa).sum(), 1e-20))
    return [(ids, (uv * s)[lf])]


# ---- the whole thing ----------------------------------------------------------------------
def _load(src: Path):
    import trimesh

    pngs = sorted(src.parent.glob(src.stem + "[0-9]*.png"))
    if len(pngs) > 1:
        raise ValueError("this model's texture is split over several images: not supported yet")
    m = trimesh.load(str(src), process=False)
    img = getattr(m.visual.material, "image", None) if hasattr(m.visual, "material") else None
    if img is None or getattr(m.visual, "uv", None) is None:
        raise ValueError("the model has no texture to lay out")
    return m, np.asarray(img.convert("RGB"), np.float32)


def count_pieces(f: np.ndarray, uv: np.ndarray, v: np.ndarray) -> int:
    """Texture pieces: faces joined where they share an edge with the same coordinates on it."""
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
    lab = _components(len(f), fid[o][1:][same], fid[o][:-1][same])
    return int(lab.max() + 1)


def _geometry(m, smooth: int | None):
    """Welded geometry and the face normals pieces are cut by. smooth: Taubin iterations, or None
    for about 1% of the model's size (a dense scan's noise goes, a coarse model is left as it is)."""
    import trimesh

    v, f = np.asarray(m.vertices, np.float64), np.asarray(m.faces)
    pv, pid = np.unique(np.round(v, 6), axis=0, return_inverse=True)  # a textured PLY splits vertices at seams
    gf = pid[f]
    g = trimesh.Trimesh(pv, gf, process=False)
    area = g.area_faces
    adj = g.face_adjacency
    if smooth is None:  # diffusion spreads ~sqrt(iterations) edges
        reach = 0.01 * float(np.linalg.norm(np.ptp(pv, axis=0))) / max(float(g.edges_unique_length.mean()), 1e-12)
        smooth = int(np.clip(round(reach ** 2), 0, 60))
    if smooth:
        s = trimesh.Trimesh(pv.copy(), gf, process=False)
        trimesh.smoothing.filter_taubin(s, iterations=smooth)
        normals = s.face_normals
    else:
        normals = g.face_normals
    return pv, gf, area, adj, normals


def preview(src: Path, angle: float = 45.0, min_size: float = 0.005, smooth: int | None = None) -> dict:
    """How many pieces the current texture has and how many a rebuild would make."""
    m, _ = _load(src)
    pv, gf, area, adj, normals = _geometry(m, smooth)
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


def relayout(src: Path, dst: Path, angle: float = 45.0, min_size: float = 0.005, smooth: int | None = None,
             size: int | None = None, padding: int = 4, max_size: int = 8192) -> dict:
    """Write dst (a textured PLY naming its PNG, like OpenMVS's) with the new layout of src's texture.
    size: fit the atlas in a size x size image; by default it keeps the old texture's detail (texels
    per unit of surface), up to max_size."""
    import trimesh
    import xatlas
    from PIL import Image

    t0 = time.time()
    m, old = _load(src)
    f_old, uv_old = np.asarray(m.faces), np.asarray(m.visual.uv, np.float64)
    pv, gf, area, adj, normals = _geometry(m, smooth)
    lab = segment(normals, area, adj, angle, min_size)
    t_seg = time.time()
    # flatten each piece; every piece gets its own copy of its vertices (seams split)
    corner_uv = np.zeros((len(gf), 3, 2))
    chart = np.zeros(len(gf), np.int64)
    n_charts = 0
    for seg in _pieces(pv, gf, area, normals, lab):
        for fids, uvs in seg:
            corner_uv[fids] = uvs
            chart[fids] = n_charts
            n_charts += 1
    t_flat = time.time()
    # pack: one vertex per (chart, mesh vertex), so xatlas sees the pieces as separate islands
    key = np.stack([np.repeat(chart, 3), gf.ravel()], 1)
    uniq, vid = np.unique(key, axis=0, return_inverse=True)
    vid = vid.reshape(-1, 3)
    vuv = np.zeros((len(uniq), 2))
    vuv[vid.ravel()] = corner_uv.reshape(-1, 2)
    oh, ow = old.shape[:2]
    # texels per unit of length: OpenMVS gives each patch the detail of its photo, so keep the
    # detail that 90% of the surface has (by area), not the average, or the sharp parts soften
    per_face = np.sqrt(np.abs(_signed_area(uv_old * [ow, oh], f_old)) / np.maximum(area, 1e-20))
    o = np.argsort(per_face)
    cum = np.cumsum(area[o]) / max(area.sum(), 1e-20)
    density = float(per_face[o][min(np.searchsorted(cum, 0.9), len(o) - 1)])

    def pack(resolution: int, texels_per_unit: float):
        a = xatlas.Atlas()
        a.add_mesh(pv[uniq[:, 1]].astype(np.float32), vid.astype(np.uint32), uvs=vuv.astype(np.float32))
        co = xatlas.ChartOptions()
        co.use_input_mesh_uvs = True
        po = xatlas.PackOptions()
        po.resolution, po.texels_per_unit = resolution, texels_per_unit
        po.padding, po.bilinear, po.rotate_charts = padding, True, True
        a.generate(co, po)
        return a

    if size:
        atlas = pack(int(size), 0.0)
    else:
        atlas = pack(0, density)
        if max(atlas.width, atlas.height) > max_size:  # too big: same layout, less detail
            atlas = pack(0, density * max_size / max(atlas.width, atlas.height) * 0.98)
    vmap, faces, uvs = atlas[0]
    if len(faces) != len(gf) or not np.array_equal(vmap[faces], vid):
        raise RuntimeError("the packer reordered the triangles: cannot copy the texture across")
    W, H = int(atlas.width), int(atlas.height)
    t_pack = time.time()
    new = bake(old, uv_old[f_old], uvs[faces], W, H, padding)
    t_bake = time.time()
    # write: OpenMVS-style textured PLY (per-vertex coordinates here) next to its PNG
    out = trimesh.Trimesh(pv[uniq[vmap, 1]], faces, process=False)
    out.visual = trimesh.visual.TextureVisuals(uv=uvs, image=Image.fromarray(new))
    png = dst.with_name(dst.stem + "0.png")
    Image.fromarray(new).save(png, optimize=False, compress_level=6)
    data = out.export(file_type="ply")
    head, rest = data.split(b"end_header\n", 1)
    head = head.replace(b"\nelement vertex", f"\ncomment TextureFile {png.name}\nelement vertex".encode(), 1)
    tmp = dst.with_suffix(".tmp.ply")
    tmp.write_bytes(head + b"end_header\n" + rest)
    tmp.replace(dst)
    info = {"pieces_now": count_pieces(f_old, uv_old, np.asarray(m.vertices)), "pieces": n_charts,
            "size": [W, H], "used": round(float(atlas.utilization), 2), "faces": len(gf),
            "seconds": {"segment": round(t_seg - t0, 1), "flatten": round(t_flat - t_seg, 1),
                        "pack": round(t_pack - t_flat, 1), "bake": round(t_bake - t_pack, 1)}}
    log.info("texture layout: %d -> %d pieces, %dx%d, %.0f%% used, %.0f s", info["pieces_now"], n_charts, W, H,
             100 * info["used"], time.time() - t0)
    return info


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


def bake(old: np.ndarray, uv_from: np.ndarray, uv_to: np.ndarray, W: int, H: int, padding: int = 4) -> np.ndarray:
    """Copy a texture across layouts: every texel of the new (W x H) image inside a triangle takes
    the old texture's colour at the same point of that triangle. uv_from / uv_to: (F, 3, 2)."""
    oh, ow = old.shape[:2]
    # first grow the old pieces into their background, so sampling at an edge does not pick it up
    old = old.copy()
    covered = np.zeros((oh, ow), bool)
    for _, px, py, _ in _raster(uv_from, ow, oh):
        covered[py, px] = True
    _grow(old, covered, 3)
    out = np.zeros((H, W, 3), np.float32)
    hit = np.zeros((H, W), bool)
    S = np.stack([uv_from[..., 0] * ow - 0.5, (1 - uv_from[..., 1]) * oh - 0.5], -1)  # old texel coords
    for tri, px, py, bc in _raster(uv_to, W, H):
        q = np.einsum("nk,nkj->nj", bc, S[tri])
        x = np.clip(q[:, 0], 0, ow - 1.001)
        y = np.clip(q[:, 1], 0, oh - 1.001)
        x0, y0 = x.astype(np.int64), y.astype(np.int64)
        fx, fy = (x - x0)[:, None], (y - y0)[:, None]
        out[py, px] = (old[y0, x0] * (1 - fx) * (1 - fy) + old[y0, x0 + 1] * fx * (1 - fy)
                       + old[y0 + 1, x0] * (1 - fx) * fy + old[y0 + 1, x0 + 1] * fx * fy)
        hit[py, px] = True
    # a margin around every piece, filled from its edge: no seams when the texture is filtered
    _grow(out, hit, padding + 2)
    out[~hit] = 128
    return np.clip(out + 0.5, 0, 255).astype(np.uint8)
