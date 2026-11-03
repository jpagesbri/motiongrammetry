"""Topology edits for the editor: capping openings, plane cuts and voxel remeshing.

Sculpt brushes only move vertices. These change the surface itself, so they run here (numpy /
Open3D / scikit-image) and hand the editor a new mesh; nothing is written to disk until it saves.

- cap: close boundary loops with even, membrane-relaxed lids (all of them, or the ones a cut just
  opened): flat where the rim is flat, curving to meet it where it rises.
- plane_cut: split the triangles along a plane, drop one side, close the cut with a flat cap.
- restore: bring back raw-mesh triangles inside brushed spheres (what clean carved away).
- keep_pieces: drop loose pieces, e.g. a part a cut separated from the body.
- sanitize: the chosen repairs of meshfix.py (duplicates, manifold, holes, orientation, ...).
- voxel_remesh: rebuild the surface from a signed-distance volume, optionally unioned with
  spheres the editor's fill brush placed. Separate shells, inner walls and membranes merge
  into one closed surface with even triangles, ready to be smoothed as a single piece.
- local_merge: the same, but only around the fill spheres and at the mesh's own resolution; the
  rest of the mesh is kept exactly and the rebuilt patch is stitched in along a seam.

Meshes travel as (vertices float64 Nx3, faces int Mx3, colours uint8 Nx3).
"""

from __future__ import annotations

import logging
import struct

import numpy as np

from .clean import boundary_loops, close_holes
from .meshfix import _edges, isin_sorted, unique_sorted

log = logging.getLogger(__name__)

MAX_VOXELS = 40_000_000


# ---- wire format shared with the editor ---------------------------------------------------
def decode_mesh(data: bytes, offset: int = 0) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """uint32 vertex count, uint32 face count, float32 xyz, uint8 rgb, uint32 faces."""
    nv, nf = struct.unpack_from("<II", data, offset)
    o = offset + 8
    want = o + nv * 15 + nf * 12
    if not nv or not nf or len(data) != want:
        raise ValueError(f"mesh upload is {len(data) - offset} bytes, expected {want - offset}")
    v = np.frombuffer(data, "<f4", nv * 3, o).reshape(-1, 3).astype(np.float64)
    c = np.frombuffer(data, np.uint8, nv * 3, o + nv * 12).reshape(-1, 3).copy()
    f = np.frombuffer(data, "<u4", nf * 3, o + nv * 15).reshape(-1, 3).astype(np.int64)
    if f.max() >= nv or not np.isfinite(v).all():
        raise ValueError("mesh upload is inconsistent")
    return v, f, c


def encode_mesh(v: np.ndarray, f: np.ndarray, c: np.ndarray) -> bytes:
    return (struct.pack("<II", len(v), len(f)) + v.astype("<f4").tobytes()
            + c.astype(np.uint8).tobytes() + f.astype("<u4").tobytes())


def compact(v, f, c):
    """Drop degenerate faces and unused vertices."""
    f = f[(f[:, 0] != f[:, 1]) & (f[:, 1] != f[:, 2]) & (f[:, 2] != f[:, 0])]
    used = np.zeros(len(v), bool)
    used[f.ravel()] = True
    remap = np.cumsum(used) - 1
    return v[used], remap[f], c[used]


def _lid_colours(c, loops, owner):
    rim = np.array([c[l].mean(0) for l in loops]) if loops else np.zeros((0, 3))
    return np.round(rim[owner]).astype(np.uint8) if len(owner) else np.zeros((0, 3), np.uint8)


def _triangulate(U, h):
    """Even triangulation of the simple polygon U (n x 2): its own vertices and a hexagonal grid of
    spacing h inside, kept clear of the rim. Returns (grid points, triangles indexing U then grid),
    or None when some rim edge isn't in the Delaunay triangulation (a zigzag in the outline)."""
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components
    from scipy.spatial import Delaunay, cKDTree
    from skimage.measure import points_in_poly

    n = len(U)
    lo, hi = U.min(0), U.max(0)
    gx, gy = np.meshgrid(np.arange(lo[0], hi[0] + h, h), np.arange(lo[1], hi[1] + h, h * 0.866))
    gx = gx + (np.arange(len(gx)) % 2)[:, None] * h / 2
    G = np.stack([gx.ravel(), gy.ravel()], 1)
    G = G[points_in_poly(G, U)]
    if len(G):
        G = G[cKDTree(U).query(G)[0] > 0.7 * h]
    if len(G):  # nor inside a rim edge's diametral circle, which would keep that edge out of the Delaunay
        mid, rad = (U + np.roll(U, -1, 0)) / 2, np.linalg.norm(np.roll(U, -1, 0) - U, axis=1) / 2
        near = cKDTree(G).query_ball_point(mid, rad * 1.05)
        clear = np.ones(len(G), bool)
        clear[[i for ids in near for i in ids]] = False
        G = G[clear]
    dt = Delaunay(np.concatenate([U, G]))
    S, N, T = dt.simplices.astype(np.int64), dt.neighbors.astype(np.int64), len(dt.simplices)
    # rim[t, k]: whether the edge of triangle t facing its corner k is a rim edge
    a, b = S[:, [1, 2, 0]], S[:, [2, 0, 1]]
    rim = (a < n) & (b < n) & np.isin((a - b) % n, (1, n - 1))
    # inside or outside, from the hull inwards and flipping only across rim edges (a centroid test
    # misjudges the slivers in thin notches); any contradiction means a rim edge is missing. Solved
    # all at once as a graph: node 2t + s is "triangle t is on side s", 2T is "true", 2T + 1 "false";
    # a constraint joins the statements it makes equal, and a contradiction joins t's two statements.
    t, k = np.nonzero(N >= 0)
    u, r = N[t, k], rim[t, k].astype(np.int64)
    th, kh = np.nonzero(N < 0)
    rh = rim[th, kh].astype(np.int64)
    src = np.concatenate([2 * t, 2 * t + 1, 2 * th + rh, 2 * th + 1 - rh])
    dst = np.concatenate([2 * u + r, 2 * u + 1 - r, np.full(len(th), 2 * T), np.full(len(th), 2 * T + 1)])
    g = coo_matrix((np.ones(len(src), np.int8), (src, dst)), shape=(2 * T + 2, 2 * T + 2))
    lab = connected_components(g, directed=False)[1]
    if lab[2 * T] == lab[2 * T + 1] or (lab[0:2 * T:2] == lab[1:2 * T:2]).any():
        return None
    tri = S[lab[1:2 * T:2] == lab[2 * T]]
    # every rim edge, and nothing else, must be the lid's border
    m = n + len(G)
    e = np.sort(np.concatenate([tri[:, [0, 1]], tri[:, [1, 2]], tri[:, [2, 0]]]), 1)
    ue, cnt = np.unique(e[:, 0] * m + e[:, 1], return_counts=True)
    border = ue[cnt == 1]
    ring = np.sort(np.stack([np.arange(n), (np.arange(n) + 1) % n], 1), 1)
    if not len(tri) or len(border) != n or len(np.unique(np.concatenate([border, ring[:, 0] * m + ring[:, 1]]))) != n:
        return None
    pts = np.concatenate([U, G])
    a, b, c = pts[tri[:, 0]], pts[tri[:, 1]], pts[tri[:, 2]]
    ccw = ((b - a)[:, 0] * (c - a)[:, 1] - (b - a)[:, 1] * (c - a)[:, 0]) > 0
    tri[~ccw] = tri[~ccw][:, ::-1]
    return G, tri


def _lid(v, c, loop, base: int, max_points: int = 80_000):
    """Lid for one boundary loop: the rim seen along its best-fit plane, filled with an even
    triangulation, then relaxed as a membrane with the rim held. Where the rim is flat the lid is
    flat; where it rises (a shoe's toe and heel) the lid curves up to meet it, with no crease and
    no fan of slivers. Colours blend in from the rim the same way. The new vertices are numbered
    from ``base``. None when the rim doesn't project to a simple outline (it folds over itself);
    close_holes takes those."""
    from scipy.sparse import coo_matrix
    from scipy.sparse.linalg import splu

    loop = np.asarray(loop, np.int64)
    n = len(loop)
    P = v[loop]
    _, _, vt = np.linalg.svd(P - P.mean(0), full_matrices=False)
    U = (P - P.mean(0)) @ vt[:2].T
    area = 0.5 * abs(np.dot(U[:, 0], np.roll(U[:, 1], -1)) - np.dot(U[:, 1], np.roll(U[:, 0], -1)))
    h = float(np.median(np.linalg.norm(np.roll(U, -1, 0) - U, axis=1)))
    if area <= 0 or h <= 0:
        return None
    h = max(h, np.sqrt(area / max_points / 0.87))
    # The 2D layout only decides how the lid is connected (the membrane below places it in 3D), so
    # a rim that zigzags in projection is triangulated on a smoothed copy of its outline.
    Us, done = U, 0
    for k in (0, 8, 32, 128):
        for _ in range(k - done):
            Us = 0.5 * Us + 0.25 * (np.roll(Us, 1, 0) + np.roll(Us, -1, 0))
        done = k
        if (r := _triangulate(Us, h)) is not None:
            break
    else:
        return None
    G, tri = r
    # wind against the rim's half-edges (loop[i] -> loop[i+1] belongs to the surface)
    t0 = tri[np.isin(tri, [0, 1]).sum(1) == 2][0]
    k = int(np.flatnonzero(t0 == 0)[0])
    if t0[(k + 1) % 3] == 1:  # holds 0 -> 1, the surface's own direction: flip
        tri = tri[:, ::-1]
    # membrane: each inner vertex at the mean of its neighbours, the rim fixed
    m = len(G)
    X = np.zeros((m, 3))
    Cc = np.zeros((m, 3))
    if m:
        nv = n + m
        ed = np.sort(np.concatenate([tri[:, [0, 1]], tri[:, [1, 2]], tri[:, [2, 0]]]), 1).astype(np.int64)
        ed = np.sort(ed[:, 0] * nv + ed[:, 1])  # distinct by sorting (numpy's own unique hashes, slower here)
        ed = ed[np.concatenate([[True], ed[1:] != ed[:-1]])]
        ed = np.stack([ed // nv, ed % nv], 1)
        ed = np.concatenate([ed, ed[:, ::-1]])
        ed = ed[ed[:, 0] >= n]
        i, j = ed[:, 0] - n, ed[:, 1]
        inner = j >= n
        deg = np.bincount(i, minlength=m).astype(float)
        A = coo_matrix((np.concatenate([deg, -np.ones(inner.sum())]),
                        (np.concatenate([np.arange(m), i[inner]]), np.concatenate([np.arange(m), j[inner] - n]))),
                       shape=(m, m)).tocsc()
        rhs = np.zeros((m, 6))
        np.add.at(rhs, i[~inner], np.concatenate([P, c[loop].astype(float)], 1)[j[~inner]])
        sol = splu(A).solve(rhs)
        X, Cc = sol[:, :3], sol[:, 3:]
    ids = np.concatenate([loop, base + np.arange(m)])
    return X, ids[tri], np.clip(np.round(Cc), 0, 255).astype(np.uint8)


def _scraps(f, every, n):
    """Scraps: pieces held to the rest by their corners only (no shared edge), every vertex on their
    own rim and that rim one loop: a lone face left between holes, a strip of such faces, a fan with
    no inner vertex. boundary_loops reports that rim as a hole of its own, but it runs round the
    piece: a lid on it is the piece again, wound the other way (a pillow). ``every``: all boundary
    loops of f; n: the vertex count. Returns (the scraps' faces, the scrap of each, the directed
    keys a * n + b of their half-edges)."""
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components

    none = np.zeros(0, np.int64)
    if not every:
        return none, none, none
    rim = np.zeros(n, bool)
    rim[np.concatenate([np.asarray(l, np.int64) for l in every])] = True
    near = np.flatnonzero(rim[f].sum(1) >= 2)  # the only faces that can share an edge with a scrap's
    cand = rim[f[near]].all(1)                 # a scrap's faces have all three corners on rims
    if not cand.any():
        return none, none, none
    he, fid, und, key = _edges(f[near], n)
    o = np.argsort(und, kind="stable")
    su = und[o]
    first = np.concatenate([[True], su[1:] != su[:-1]])
    grp = np.cumsum(first) - 1
    cnt = np.bincount(grp)[grp]
    # a half-edge's partner, where its edge has exactly two faces (it follows or precedes it in order)
    nxt = np.concatenate([o[1:], o[:1]])
    prv = np.concatenate([o[-1:], o[:-1]])
    partner = np.full(len(und), -1, np.int64)
    two = cnt == 2
    partner[o[two & first]] = nxt[two & first]
    partner[o[two & ~first]] = prv[two & ~first]
    count = np.empty(len(und), np.int64)
    count[o] = cnt
    cf = cand[fid]
    p = np.maximum(partner, 0)
    # an edge a candidate shares well: two faces, both candidates, wound against each other
    joined = cf & (count == 2) & cand[fid[p]] & (key[p] != key)
    bad = cf & ~joined & (count != 1)  # shared with a non-candidate, with 3+ faces, or wound alike
    m = len(near)
    a, b = fid[joined], fid[p[joined]]
    lab = connected_components(coo_matrix((np.ones(len(a), np.int8), (a, b)), shape=(m, m)), directed=False)[1]
    lab = np.where(cand, lab, -1)
    k = int(lab.max()) + 1
    ok = np.ones(k, bool)
    ok[lab[fid[bad]]] = False
    nf = np.bincount(lab[cand], minlength=k)
    nb = np.bincount(lab[fid[cf & (count == 1)]], minlength=k)
    ni = np.bincount(lab[fid[joined]], minlength=k) // 2
    corner = unique_sorted(lab[cand][:, None] * n + f[near[cand]])
    nv = np.bincount(corner // n, minlength=k)
    # one rim through every vertex: a disc (V - E + F = 1) with as many rim edges as vertices
    ok &= (nv - nb - ni + nf == 1) & (nb == nv)
    hit = cand & ok[np.maximum(lab, 0)]
    if not hit.any():
        return none, none, none
    faces = near[hit]
    return faces, lab[hit], unique_sorted(key[hit[fid]])


def _close(v, f, c, loops, every=None):
    """close_loops, also returning how many loops got a lid and how many scrap faces went."""
    n = len(v)
    sf, lab, own = _scraps(f, boundary_loops(f) if every is None else every, n)
    dropped = 0
    if len(sf):
        # a scrap's own rim is no hole: no lid. A scrap touching a hole that gets one goes: the lid
        # covers its spot, and keeping it would put a third face on any lid edge between its corners.
        loops = [l for l in loops if not isin_sorted(np.int64(l[0]) * n + l[1], own)]
        mark = np.zeros(n, bool)
        for l in loops:
            mark[np.asarray(l, np.int64)] = True
        gone = unique_sorted(lab[mark[f[sf]].any(1)])
        drop = sf[isin_sorted(lab, gone)] if len(gone) else sf[:0]
        if len(drop):
            keep = np.ones(len(f), bool)
            keep[drop] = False
            f, dropped = f[keep], len(drop)
    out_v, out_f, out_c, rest = [v], [f], [c], []
    base = len(v)
    for loop in loops:
        lid = _lid(v, c, loop, base)
        if lid is None:
            rest.append(loop)
            continue
        out_v.append(lid[0])
        out_f.append(lid[1])
        out_c.append(lid[2])
        base += len(lid[0])
    V, F, C = np.concatenate(out_v), np.concatenate(out_f), np.concatenate(out_c)
    if rest:
        V2, F, owner = close_holes(V, F, loops=rest, owners=True)
        C = np.concatenate([C, _lid_colours(C, rest, owner)])
        V = V2
    return V, F, C, len(loops), dropped


def close_loops(v, f, c, loops, every=None):
    """Close the given boundary loops: even membrane lids (_lid), soap-film rings where that fails.
    The faces come back in order, then the lids. A scrap (_scraps: a lone face left between holes)
    gets no lid of its own; it goes when it touches a hole that gets one. ``every``: all boundary
    loops of f, when the caller has them (found again otherwise)."""
    return _close(v, f, c, loops, every)[:3]


# ---- operations ------------------------------------------------------------------------------
def cap(v, f, c, seeds: np.ndarray | None = None):
    """Close the boundary loops touching any ``seeds`` vertex (all loops when None)."""
    v, f, c = compact(v, f, c) if seeds is None else (v, f, c)  # seeds index the mesh as given
    every = loops = boundary_loops(f)
    if seeds is not None:
        mark = np.zeros(len(v), bool)
        mark[np.asarray(seeds, np.int64)[np.asarray(seeds) < len(v)]] = True
        loops = [l for l in loops if mark[l].any()]
    if not loops:
        return v, f, c, {"capped": 0}
    V, F, C, capped, dropped = _close(v, f, c, loops, every)
    if not capped:  # only a scrap's own rim (_scraps): no hole to close
        return v, f, c, {"capped": 0}
    V, F, C = compact(V, F, C)
    return V, F, C, {"capped": capped, **({"removed_scraps": dropped} if dropped else {})}


def plane_cut(v, f, c, point, normal, close: bool = True):
    """Remove everything on the side ``normal`` points to; triangles crossing the plane are split
    exactly on it, and the openings left on the plane are closed with flat caps."""
    n = np.asarray(normal, float)
    n /= np.linalg.norm(n)
    d = (v - np.asarray(point, float)) @ n
    keep = d <= 0
    k = keep[f]
    cnt = k.sum(1)
    out_f = [f[cnt == 3]]
    new_v, new_c, index = [], [], {}

    def on_plane(a, b):  # vertex where edge a-b crosses the plane (shared by both triangles)
        key = (a, b) if a < b else (b, a)
        if key not in index:
            t = d[a] / (d[a] - d[b])
            index[key] = len(v) + len(new_v)
            new_v.append(v[a] + t * (v[b] - v[a]))
            new_c.append(c[a] + t * (c[b].astype(float) - c[a]))
        return index[key]

    split = []
    for tri, kk in zip(f[(cnt == 1) | (cnt == 2)], k[(cnt == 1) | (cnt == 2)]):
        # rotate (keeping the winding) so the odd vertex out comes first
        odd = int(np.flatnonzero(kk != (kk.sum() == 2))[0])
        a, b, cc = np.roll(tri, -odd)
        if kk.sum() == 1:   # a kept
            split.append((a, on_plane(a, b), on_plane(a, cc)))
        else:               # a dropped, b and c kept
            ab, ca = on_plane(a, b), on_plane(cc, a)
            split += [(ab, b, cc), (ab, cc, ca)]
    if split:
        out_f.append(np.array(split, np.int64))
    V = np.concatenate([v, np.array(new_v).reshape(-1, 3)])
    C = np.concatenate([c, np.round(np.array(new_c)).reshape(-1, 3).astype(np.uint8)])
    V, F, C = compact(V, np.concatenate(out_f), C)
    info = {"removed_faces": int(len(f) - (cnt == 3).sum()), "capped": 0}
    if close and len(F):
        dist = np.abs((V - np.asarray(point, float)) @ n)
        eps = 1e-6 * float(np.linalg.norm(np.ptp(V, 0)))
        # An opening the cut made or crossed: most of its rim lies on the plane. Not all of it when the
        # cut goes through an opening the scan already had (a shoe's open bottom reaching up into the
        # toe): its rim there stays above the plane, and the lid follows it.
        every = boundary_loops(F)
        loops = [l for l in every if (dist[l] <= eps).mean() >= 0.5]
        if loops:
            V, F, C, info["capped"], dropped = _close(V, F, C, loops, every)
            if dropped:  # a scrap's corner only it had is left unused
                V, F, C = compact(V, F, C)
                info["removed_scraps"] = dropped
    return V, F, C, info


def voxel_remesh(v, f, c, spheres=None, voxel: float = 0.004, min_piece: float = 0.001):
    """One closed surface from the mesh unioned with spheres (x, y, z, r), at a voxel size of
    ``voxel`` times the bounding-box diagonal. Detail finer than a voxel is smoothed away."""
    import open3d as o3d
    from scipy.spatial import cKDTree
    from skimage.measure import marching_cubes

    v, f, c = compact(v, f, c)
    sph = np.asarray(spheres if spheres is not None else [], float).reshape(-1, 4)
    lo, hi = v.min(0), v.max(0)
    if len(sph):
        lo = np.minimum(lo, (sph[:, :3] - sph[:, 3:]).min(0))
        hi = np.maximum(hi, (sph[:, :3] + sph[:, 3:]).max(0))
    h = voxel * float(np.linalg.norm(hi - lo))
    lo, hi = lo - 3 * h, hi + 3 * h
    dims = np.ceil((hi - lo) / h).astype(int) + 1
    if int(np.prod(dims)) > MAX_VOXELS:
        raise ValueError(f"{int(np.prod(dims)) / 1e6:.0f}M voxels: choose a coarser detail")
    sdf = _union_sdf(v, f, sph, lo, dims, h)

    verts, faces, _, _ = marching_cubes(sdf, level=0.0, spacing=(h, h, h))
    verts += lo
    faces = faces.astype(np.int64)
    tri = verts[faces]
    if np.einsum("ij,ij->i", tri[:, 0], np.cross(tri[:, 1], tri[:, 2])).sum() < 0:
        faces = faces[:, ::-1]  # outward normals
    # drop specks the volume can produce from noise
    m = o3d.geometry.TriangleMesh(o3d.utility.Vector3dVector(verts), o3d.utility.Vector3iVector(faces))
    clusters, _, areas = m.cluster_connected_triangles()
    areas = np.asarray(areas)
    if len(areas) > 1:
        faces = faces[areas[np.asarray(clusters)] >= min_piece * areas.max()]
    colours = c[cKDTree(v).query(verts)[1]]  # nearest original vertex (fill takes its rim colour)
    V, F, C = compact(verts, faces, colours)
    return V, F, C, {"voxel": h, "faces": len(F), "spheres": len(sph)}


def _union_sdf(v, f, sph, lo, dims, h):
    """Signed distance (negative inside) of the mesh unioned with the spheres, sampled on the grid
    lo + h * index, dims cells per axis (the grid may cover only part of the mesh)."""
    import open3d as o3d

    # Inside/outside comes from ray parity, so it needs closed shells (cap the openings; several
    # rays vote) and one shell at a time: inside two overlapping shells a ray crosses four walls
    # and parity says "outside". Each shell's signed distance is computed over its own box and
    # the union is the minimum.
    vc, fc = close_holes(v, f) if boundary_loops(f) else (v, f)
    shells = o3d.geometry.TriangleMesh(o3d.utility.Vector3dVector(vc), o3d.utility.Vector3iVector(fc))
    shell_of = np.asarray(shells.cluster_connected_triangles()[0])
    axes = [lo[i] + h * np.arange(dims[i]) for i in range(3)]
    sdf = np.full(dims, np.float32(1e9))
    for s_id in unique_sorted(shell_of):
        sf = fc[shell_of == s_id]
        if len(sf) < 4:
            continue
        sv = vc[unique_sorted(sf)]
        a = np.maximum(np.floor((sv.min(0) - lo) / h).astype(int) - 2, 0)
        b = np.minimum(np.ceil((sv.max(0) - lo) / h).astype(int) + 3, dims)
        if (a >= b).any():
            continue  # this shell is outside the grid
        scene = o3d.t.geometry.RaycastingScene()
        scene.add_triangles(o3d.core.Tensor(vc.astype(np.float32)), o3d.core.Tensor(sf.astype(np.uint32)))
        yz = np.stack(np.meshgrid(axes[1][a[1]:b[1]], axes[2][a[2]:b[2]], indexing="ij"), -1).reshape(-1, 2)
        for i in range(a[0], b[0]):  # one slab at a time keeps the query arrays small
            q = np.column_stack([np.full(len(yz), axes[0][i]), yz]).astype(np.float32)
            d = scene.compute_signed_distance(o3d.core.Tensor(q), nsamples=3).numpy().reshape(b[1] - a[1], b[2] - a[2])
            np.minimum(sdf[i, a[1]:b[1], a[2]:b[2]], d, out=sdf[i, a[1]:b[1], a[2]:b[2]])
    for x, y, z, r in sph:  # union: the nearer surface wins
        a = np.maximum(np.floor(([x, y, z] - r - lo) / h).astype(int) - 1, 0)
        b = np.minimum(np.ceil(([x, y, z] + r - lo) / h).astype(int) + 2, dims)
        if (a >= b).any():
            continue
        gx, gy, gz = np.meshgrid(*[axes[k][a[k]:b[k]] for k in range(3)], indexing="ij")
        ds = np.sqrt((gx - x) ** 2 + (gy - y) ** 2 + (gz - z) ** 2) - r
        sub = sdf[a[0]:b[0], a[1]:b[1], a[2]:b[2]]
        np.minimum(sub, ds, out=sub)
    return sdf


def _clip(v, f, c, d):
    """Keep the side where ``d`` (per vertex) is <= 0; triangles crossing d = 0 are split on it.
    Returns the mesh and a mask of the vertices made on the cut (the seam)."""
    k = (d <= 0)[f]
    cnt = k.sum(1)
    out_f = [f[cnt == 3]]
    new_v, new_c, index = [], [], {}

    def cut(a, b):  # where edge a-b crosses d = 0, shared by both triangles of the edge
        key = (a, b) if a < b else (b, a)
        if key not in index:
            t = d[a] / (d[a] - d[b])
            index[key] = len(v) + len(new_v)
            new_v.append(v[a] + t * (v[b] - v[a]))
            new_c.append(c[a] + t * (c[b].astype(float) - c[a]))
        return index[key]

    split = []
    for tri, kk in zip(f[(cnt == 1) | (cnt == 2)], k[(cnt == 1) | (cnt == 2)]):
        odd = int(np.flatnonzero(kk != (kk.sum() == 2))[0])  # rotate (keeping the winding): odd one first
        a, b, cc = np.roll(tri, -odd)
        if kk.sum() == 1:
            split.append((a, cut(a, b), cut(a, cc)))
        else:
            ab, ca = cut(a, b), cut(cc, a)
            split += [(ab, b, cc), (ab, cc, ca)]
    if split:
        out_f.append(np.array(split, np.int64))
    V = np.concatenate([v, np.array(new_v).reshape(-1, 3)])
    C = np.concatenate([c, np.round(np.array(new_c)).reshape(-1, 3).astype(np.uint8)])
    seam = np.zeros(len(V), bool)
    seam[len(v):] = True
    F = np.concatenate(out_f)
    used = np.zeros(len(V), bool)
    used[F.ravel()] = True
    remap = np.cumsum(used) - 1
    return V[used], remap[F], C[used], seam[used]


def _stitch(VA, loop_a, VB, loop_b):
    """Triangles joining two nearby closed loops (indices into VA / VB, each in its own mesh's
    rim winding): walk both together, each step taking the shorter diagonal. Triangles use the
    rim edges reversed, so they continue both meshes' winding. B's indices are returned negated
    minus one (-1 - j) so the caller can tell the meshes apart."""
    A, B = VA[loop_a], VB[loop_b]
    na, nb = len(A), len(B)
    j0 = int(np.argmin(np.linalg.norm(B - A[0], axis=1)))
    # walk B in the direction that runs alongside A (opposite winding when both meshes agree)
    ta, tb = np.linspace(0, 1, 8, endpoint=False), np.arange(8) / 8
    ia = (ta * na).astype(int)
    fwd = np.linalg.norm(A[ia] - B[(j0 + (tb * nb).astype(int)) % nb], axis=1).mean()
    bwd = np.linalg.norm(A[ia] - B[(j0 - (tb * nb).astype(int)) % nb], axis=1).mean()
    step = 1 if fwd < bwd else -1
    tris, i, j = [], 0, 0
    while i < na or j < nb:
        a0, a1 = loop_a[i % na], loop_a[(i + 1) % na]
        jb0, jb1 = (j0 + step * j) % nb, (j0 + step * (j + 1)) % nb
        b0, b1 = loop_b[jb0], loop_b[jb1]
        adv_a = j >= nb or (i < na and np.linalg.norm(VA[a1] - VB[b0]) <= np.linalg.norm(VA[a0] - VB[b1]))
        if adv_a:  # rim edge a0 -> a1 of A, used as a1 -> a0
            tris.append((a1, a0, -1 - b0))
            i += 1
        else:      # rim edge of B, used against its winding
            p, q = (b0, b1) if step == 1 else (b1, b0)
            tris.append((-1 - q, -1 - p, a0))
            j += 1
    return np.array(tris, np.int64)


def local_merge(v, f, c, spheres, margin: float = 0.4, max_voxels: int = 12_000_000):
    """Merge the fill spheres into the mesh without touching the rest of it: the surface is rebuilt
    (mesh unioned with the spheres) only inside a region a little larger than the spheres, at the
    mesh's own edge length, cut along the region's border and stitched to the untouched mesh.

    Raises ValueError when the patch cannot be joined cleanly (an opening that continues past the
    spheres, a region too big for the voxel budget); Merge whole mesh still works then."""
    from scipy.spatial import cKDTree
    from skimage.measure import marching_cubes

    v, f, c = compact(v, f, c)
    sph = np.asarray(spheres, float).reshape(-1, 4)
    if not len(sph):
        raise ValueError("no fill spheres: place some with the Fill brush first")
    # resolution: the mesh's own edge length where the spheres are
    near = np.zeros(len(v), bool)
    for x, y, z, r in sph:
        near |= np.linalg.norm(v - [x, y, z], axis=1) < 2 * r
    ef = f[near[f].any(1)] if near.any() else f
    h = float(np.median(np.linalg.norm(v[ef[:, 0]] - v[ef[:, 1]], axis=1))) if len(ef) else 0.0
    R = sph[:, 3] * (1 + margin)
    region = np.column_stack([sph[:, :3], R])
    lo, hi = (sph[:, :3] - R[:, None]).min(0), (sph[:, :3] + R[:, None]).max(0)
    h = max(h, float(np.prod(hi - lo) / max_voxels) ** (1 / 3), 1e-9)
    R = np.maximum(R, sph[:, 3] + 4 * h)  # room for the rebuilt surface to meet the original
    region[:, 3] = R

    def inside(p):  # signed distance to the region (union of the enlarged spheres): < 0 inside
        d = np.full(len(p), np.inf)
        for x, y, z, r in region:
            np.minimum(d, np.linalg.norm(p - [x, y, z], axis=1) - r, out=d)
        return d

    # An opening the spheres only partly cover is taken in whole (its lid is part of the rebuilt
    # surface), as long as it is small next to the fill: the scan's own outer rim is not.
    closed = 0
    for _ in range(3):
        grow = []
        for loop in boundary_loops(f):
            d = inside(v[loop])
            if (d < 0).any() and (d >= 0).any():
                lv = v[loop]
                cen = lv.mean(0)
                rad = float(np.linalg.norm(lv - cen, axis=1).max())
                if rad > 2.5 * R.max():
                    raise ValueError("the fill touches a large opening (like the scan's outer edge): keep the spheres "
                                     "inside the hole, or tick 'whole mesh'")
                grow.append([*cen, rad * 1.1 + 4 * h])
        if not grow:
            break
        region = np.vstack([region, grow])
        closed += len(grow)
    lo, hi = (region[:, :3] - region[:, 3:]).min(0) - 3 * h, (region[:, :3] + region[:, 3:]).max(0) + 3 * h
    if np.prod((hi - lo) / h) > 2 * max_voxels:
        h = float(np.prod(hi - lo) / (2 * max_voxels)) ** (1 / 3)
    dims = np.ceil((hi - lo) / h).astype(int) + 1

    # the patch: the union surface inside the region
    sdf = _union_sdf(v, f, sph, lo, dims, h)
    pv, pf, _, _ = marching_cubes(sdf, level=0.0, spacing=(h, h, h))
    pv += lo
    pf = pf.astype(np.int64)
    # outward: the distance grows along the face normals (checked on the volume itself)
    cen = pv[pf].mean(1)
    nrm = np.cross(pv[pf[:, 1]] - pv[pf[:, 0]], pv[pf[:, 2]] - pv[pf[:, 0]])
    nrm /= np.linalg.norm(nrm, axis=1, keepdims=True) + 1e-30
    probe = lambda q: sdf[tuple(np.clip(np.round((q - lo) / h).astype(int), 0, dims - 1).T)]  # noqa: E731
    if (probe(cen + nrm * h) - probe(cen - nrm * h)).mean() < 0:
        pf = pf[:, ::-1]
    pc = c[cKDTree(v).query(pv)[1]]
    PV, PF, PC, pseam = _clip(pv, pf, pc, inside(pv))
    OV, OF, OC, oseam = _clip(v, f, c, -inside(v))
    if not len(PF):
        raise ValueError("the fill spheres do not touch the mesh")

    def seam_loops(F, seam):
        loops = boundary_loops(F)
        on = [l for l in loops if seam[l].all()]
        mixed = [l for l in loops if seam[l].any() and not seam[l].all()]
        return on, mixed

    oloops, omixed = seam_loops(OF, oseam)
    ploops, pmixed = seam_loops(PF, pseam)
    if omixed or pmixed:
        raise ValueError("an opening continues past the fill spheres: cover the whole hole with spheres, "
                         "or tick 'whole mesh'")
    if len(oloops) != len(ploops):
        raise ValueError(f"the rebuilt patch does not line up with the mesh ({len(oloops)} vs {len(ploops)} "
                         "seams): try bigger spheres, or tick 'whole mesh'")
    bridges, used = [], set()
    for la in oloops:
        ca = OV[la].mean(0)
        k = min((k for k in range(len(ploops)) if k not in used), key=lambda k: np.linalg.norm(PV[ploops[k]].mean(0) - ca))
        used.add(k)
        lb = ploops[k]
        if np.linalg.norm(PV[lb].mean(0) - ca) > 0.5 * max(np.ptp(OV[la], 0).max(), 4 * h):
            raise ValueError("the rebuilt patch does not line up with the mesh: try bigger spheres, or tick 'whole mesh'")
        bridges.append(_stitch(OV, np.asarray(la), PV, np.asarray(lb)))
    off = len(OV)
    V = np.concatenate([OV, PV])
    C = np.concatenate([OC, PC])
    parts = [OF, PF + off]
    for t in bridges:
        parts.append(np.where(t < 0, off + (-1 - t), t))
    V, F, C = compact(V, np.concatenate(parts), C)
    return V, F, C, {"voxel": h, "faces": len(F), "spheres": len(sph), "local": True,
                     "rebuilt_faces": int(len(PF)), "kept_faces": int(len(OF)), "seams": len(oloops),
                     "openings_closed": closed}


def restore(v, f, c, raw_v, raw_f, spheres):
    """Bring back the raw mesh's triangles inside the spheres (x, y, z, r) that the mesh lacks.

    Clean only deletes raw vertices, so the clean mesh's vertices sit exactly on raw ones: the
    restored triangles share them and weld in seamlessly. Where the mesh was sculpted they no
    longer coincide; Merge (voxel_remesh) then joins the pieces."""
    from scipy.spatial import cKDTree
    import open3d as o3d

    v, f, c = compact(v, f, c)
    sph = np.asarray(spheres, float).reshape(-1, 4)
    if not len(sph):
        return v, f, c, {"restored_faces": 0}
    cen = raw_v[raw_f].mean(1)
    tree = cKDTree(cen)
    hit = np.zeros(len(raw_f), bool)
    for x, y, z, r in sph:
        hit[tree.query_ball_point([x, y, z], r)] = True
    cand = raw_f[hit]
    if not len(cand):
        return v, f, c, {"restored_faces": 0}
    # already there: the centroid lies on the current surface
    scene = o3d.t.geometry.RaycastingScene()
    scene.add_triangles(o3d.core.Tensor(v.astype(np.float32)), o3d.core.Tensor(f.astype(np.uint32)))
    tol = 1e-4 * float(np.linalg.norm(np.ptp(v, 0)))
    present = scene.compute_distance(o3d.core.Tensor(cen[hit].astype(np.float32))).numpy() < tol
    new = cand[~present]
    if not len(new):
        return v, f, c, {"restored_faces": 0}
    used = unique_sorted(new)
    remap = np.full(len(raw_v), -1, np.int64)
    remap[used] = len(v) + np.arange(len(used))
    V = np.concatenate([v, raw_v[used]])
    C = np.concatenate([c, c[cKDTree(v).query(raw_v[used])[1]]])  # colour of the nearest kept vertex
    F = np.concatenate([f, remap[new]])
    # weld: raw vertices that are also in the mesh (bit-identical positions) become one vertex
    _, first, inv = np.unique(V, axis=0, return_index=True, return_inverse=True)
    V, C, F = V[first], C[first], inv.ravel()[F]
    V, F, C = compact(V, F, C)
    return V, F, C, {"restored_faces": int(len(new))}


def keep_pieces(v, f, c, keep: float = 0.1):
    """Drop pieces smaller than ``keep`` times the largest one's area (e.g. a limb cut off)."""
    import open3d as o3d

    v, f, c = compact(v, f, c)
    m = o3d.geometry.TriangleMesh(o3d.utility.Vector3dVector(v), o3d.utility.Vector3iVector(f))
    clusters, _, areas = m.cluster_connected_triangles()
    areas = np.asarray(areas)
    small = areas[np.asarray(clusters)] < keep * areas.max()
    V, F, C = compact(v, f[~small], c)
    return V, F, C, {"removed_pieces": int((areas < keep * areas.max()).sum()), "removed_faces": int(small.sum())}


def run(kind: str, data: bytes, raw_mesh=None) -> tuple[bytes, dict]:
    """Editor request: uint32 JSON length, JSON parameters, then the mesh (see decode_mesh)."""
    import json

    (n,) = struct.unpack_from("<I", data)
    params = json.loads(data[4:4 + n] or b"{}")
    v, f, c = decode_mesh(data, 4 + n)
    if kind == "cap":
        V, F, C, info = cap(v, f, c, params.get("seeds"))
    elif kind == "plane":
        V, F, C, info = plane_cut(v, f, c, params["point"], params["normal"], params.get("close", True))
    elif kind == "restore":
        import open3d as o3d

        if raw_mesh is None or not raw_mesh.exists():
            raise FileNotFoundError("raw mesh")
        raw = o3d.io.read_triangle_mesh(str(raw_mesh))
        V, F, C, info = restore(v, f, c, np.asarray(raw.vertices), np.asarray(raw.triangles).astype(np.int64),
                                params.get("spheres", []))
    elif kind == "pieces":
        V, F, C, info = keep_pieces(v, f, c, float(params.get("keep", 0.1)))
    elif kind == "remesh":
        V, F, C, info = voxel_remesh(v, f, c, params.get("spheres"), float(params.get("voxel", 0.004)))
    elif kind == "sanitize":
        from .meshfix import sanitize

        V, F, C, info = sanitize(v, f, c, params.get("fixes") or [], float(params.get("hole_size", 0.1)),
                                 float(params.get("piece_size", 0.05)))
    elif kind == "merge_local":
        V, F, C, info = local_merge(v, f, c, params.get("spheres") or [])
    else:
        raise KeyError(kind)
    log.info("mesh %s: %d -> %d faces %s", kind, len(f), len(F),
             {k: x for k, x in info.items() if k != "after"} if kind == "sanitize" else info)
    return encode_mesh(V, F, C), info
