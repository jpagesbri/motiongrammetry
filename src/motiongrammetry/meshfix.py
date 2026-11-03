"""Mesh checks and repairs: what breaks or spoils texturing and painting, and the editor's
Sanitize mesh.

- inspect: count what is wrong with a mesh - degenerate (zero-area) faces, duplicate faces and
  opposite-winding "pillows", non-manifold edges and bow-tie vertices, faces wound against their
  neighbours, holes, loose pieces, pieces that flatten to no area, unreferenced vertices - and,
  when asked, faces that cut through each other.
- problems: those counts as warnings for texturing and painting, each with what it affects, how
  much of the mesh, and the fix.
- sanitize: the chosen fixes, in an order where each leaves the mesh ready for the next, with what
  each changed and how long it took.
- estimates: how long each fix takes on this machine for a mesh of n faces, from a short benchmark
  run once (on the first request) and then from the fixes actually run.

Meshes are (vertices float64 Nx3, faces int Mx3, colours uint8 Nx3), as in meshops.
"""

from __future__ import annotations

import functools
import logging
import os
import threading
import time

import numpy as np

log = logging.getLogger(__name__)

# The fixes, in the order they run: duplicates first (the rest assume a face per vertex set),
# manifold before orientation (a consistent winding needs at most two faces per edge), orientation
# before holes (lids are wound against the rim, so the rim must already face the right way).
FIXES = ("duplicates", "manifold", "pieces", "orient", "holes", "unreferenced", "intersections")
ZERO_AREA = 1e-6  # a face is zero-area when its height is below this share of its longest edge


# ---- measures --------------------------------------------------------------------------------
# NumPy 2.3+ answers a plain np.unique (and np.isin, which calls it) with a hash table, ~20x slower
# than sorting on the integer edge keys used here; these two sort instead.
def _unique(x: np.ndarray) -> np.ndarray:
    """Sorted distinct values of x."""
    s = np.sort(np.asarray(x).ravel())
    return s[np.concatenate([[True], s[1:] != s[:-1]])] if len(s) else s


def _isin(x: np.ndarray, keys: np.ndarray) -> np.ndarray:
    """np.isin(x, keys) for integer arrays, by binary search in the sorted distinct keys."""
    k = _unique(keys)
    if not len(k):
        return np.zeros(np.shape(x), bool)
    i = np.minimum(np.searchsorted(k, x), len(k) - 1)
    return k[i] == x


def _cross(v: np.ndarray, f: np.ndarray) -> np.ndarray:
    a, b, c = v[f[:, 0]], v[f[:, 1]], v[f[:, 2]]
    return np.cross(b - a, c - a)


def face_areas(v: np.ndarray, f: np.ndarray) -> np.ndarray:
    return 0.5 * np.linalg.norm(_cross(v, f), axis=1) if len(f) else np.zeros(0)


def model_size(v: np.ndarray, f: np.ndarray) -> float:
    """Diagonal of the bounding box of the vertices the faces use."""
    if not len(f):
        return 0.0
    p = v[_unique(f)]
    return float(np.linalg.norm(p.max(0) - p.min(0)))


def degenerate_faces(v: np.ndarray, f: np.ndarray) -> np.ndarray:
    """Faces with a vertex twice, or (nearly) no area: height below ZERO_AREA of the longest edge."""
    if not len(f):
        return np.zeros(0, bool)
    idx = (f[:, 0] == f[:, 1]) | (f[:, 1] == f[:, 2]) | (f[:, 2] == f[:, 0])
    a, b, c = v[f[:, 0]], v[f[:, 1]], v[f[:, 2]]
    longest2 = np.max([((b - a) ** 2).sum(1), ((c - b) ** 2).sum(1), ((a - c) ** 2).sum(1)], axis=0)
    cross = np.linalg.norm(np.cross(b - a, c - a), axis=1)
    return idx | ~(cross > ZERO_AREA * longest2)


def _face_sets(f: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(first, inverse, counts) of the faces' vertex sets, as np.unique(np.sort(f, 1), axis=0)
    gives them (first: the earliest face of each set), but on one integer key per face, which
    sorts several times faster than rows."""
    s = np.sort(f, axis=1)
    n = int(s.max()) + 1
    key = (s[:, 0] * n + s[:, 1]) * n + s[:, 2] if n ** 3 < 2 ** 63 else s
    _, first, inv, cnt = np.unique(key, axis=0 if key.ndim == 2 else None, return_index=True,
                                   return_inverse=True, return_counts=True)
    return first, inv.ravel(), cnt


def repeats(f: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """(duplicates, pillows): masks of the faces on a vertex set an earlier face already has, with
    the same winding (a plain duplicate) or the opposite one (a "pillow": the same triangle both
    ways, which OpenMVS texturing crashes on). The first face of each set is in neither."""
    if not len(f):
        return np.zeros(0, bool), np.zeros(0, bool)
    first, inv, _ = _face_sets(f)
    rep = first[inv] != np.arange(len(f))
    wind = _parity(f)
    opposite = wind != wind[first[inv]]
    return rep & ~opposite, rep & opposite


def _parity(f: np.ndarray) -> np.ndarray:
    """Winding of each face: with its smallest vertex first, (a, b, c) and (a, c, b) are the two."""
    r = np.argmin(f, axis=1)
    rows = np.arange(len(f))
    return f[rows, (r + 1) % 3] < f[rows, (r + 2) % 3]


def edge_clashes(faces: np.ndarray, others: np.ndarray, n: int) -> np.ndarray:
    """For each of `faces`, how many of its half-edges a face of `others` also runs along the same
    way (n: the vertex count). Faces wound alike run along a shared edge opposite ways, so a face
    wound like its neighbours scores 0 and the same face turned scores its shared edges: of a
    "pillow" (one triangle stored both ways) the face to keep is the one that scores less."""
    if not len(faces) or not len(others):
        return np.zeros(len(faces), int)
    faces, others = np.asarray(faces, np.int64), np.asarray(others, np.int64)  # the keys overflow int32
    theirs = _unique(_edges(others, n)[3])
    return sum(_isin(faces[:, i] * n + faces[:, (i + 1) % 3], theirs).astype(int) for i in range(3))


def _edges(f: np.ndarray, n: int):
    """Half-edges (a, b) of every face, their face, the undirected key and the directed key."""
    he = np.concatenate([f[:, [0, 1]], f[:, [1, 2]], f[:, [2, 0]]])
    fid = np.tile(np.arange(len(f)), 3)
    und = np.minimum(he[:, 0], he[:, 1]) * n + np.maximum(he[:, 0], he[:, 1])
    return he, fid, und, he[:, 0] * n + he[:, 1]


def pieces(f: np.ndarray, n: int) -> np.ndarray:
    """Connected piece of each face (faces sharing an edge)."""
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components

    if not len(f):
        return np.zeros(0, np.int64)
    _, fid, und, _ = _edges(f, n)
    o = np.argsort(und, kind="stable")
    same = und[o][1:] == und[o][:-1]
    a, b = fid[o][1:][same], fid[o][:-1][same]
    g = coo_matrix((np.ones(len(a), np.int8), (a, b)), shape=(len(f), len(f)))
    return connected_components(g, directed=False)[1]


def bowtie_vertices(v: np.ndarray, f: np.ndarray) -> np.ndarray:
    """Vertices whose faces don't form one fan (two cones touching at a point)."""
    import open3d as o3d

    if not len(f):
        return np.zeros(0, np.int64)
    m = o3d.geometry.TriangleMesh(o3d.utility.Vector3dVector(v), o3d.utility.Vector3iVector(f.astype(np.int32)))
    return np.asarray(m.get_non_manifold_vertices(), np.int64)


def self_intersections(v: np.ndarray, f: np.ndarray) -> np.ndarray:
    """Pairs of faces that cut through each other (K x 2): every face's edges cast as segments
    against the mesh (Open3D's ray casting, a BVH), a hit strictly inside the segment on a face
    that shares no vertex with it is a crossing. Neighbours always touch, so faces sharing a vertex
    are not compared, and coplanar overlaps are not found (the duplicate checks catch the usual ones)."""
    import open3d as o3d

    if len(f) < 2:
        return np.zeros((0, 2), np.int64)
    scene = o3d.t.geometry.RaycastingScene()
    scene.add_triangles(o3d.core.Tensor(v.astype(np.float32)), o3d.core.Tensor(f.astype(np.uint32)))
    a = np.concatenate([f[:, 0], f[:, 1], f[:, 2]])
    b = np.concatenate([f[:, 1], f[:, 2], f[:, 0]])
    fid = np.tile(np.arange(len(f)), 3)
    rays = np.concatenate([v[a], v[b] - v[a]], 1).astype(np.float32)  # t in units of the edge: 0..1
    hits = scene.list_intersections(o3d.core.Tensor(rays))
    rid = hits["ray_ids"].numpy().astype(np.int64)
    pid = hits["primitive_ids"].numpy().astype(np.int64)
    t = hits["t_hit"].numpy()
    inside = (t > 1e-4) & (t < 1 - 1e-4)
    fa, fb = fid[rid[inside]], pid[inside]
    sa, sb = f[fa], f[fb]
    shared = np.zeros(len(fa), bool)
    for i in range(3):
        for j in range(3):
            shared |= sa[:, i] == sb[:, j]
    fa, fb = fa[~shared], fb[~shared]
    if not len(fa):
        return np.zeros((0, 2), np.int64)
    return np.unique(np.stack([np.minimum(fa, fb), np.maximum(fa, fb)], 1), axis=0)


SPOT_FACES = 200_000  # at most this many intersecting faces are sent to the editor to be shown


def intersection_spots(v: np.ndarray, f: np.ndarray, pairs: np.ndarray) -> list[dict]:
    """The faces that cut through each other, grouped into spots: faces that share a vertex or cross
    each other are in the same spot. Each spot: its centre, radius (from the centre to its farthest
    vertex) and number of faces, the most faces first."""
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components

    if not len(pairs):
        return []
    bad = _unique(pairs)
    k, n = len(bad), len(v)
    # a graph of the bad faces (0..k-1) and their vertices (k..k+n-1), plus the crossings
    pos = np.searchsorted(bad, pairs)
    rows = np.concatenate([np.repeat(np.arange(k), 3), pos[:, 0]])
    cols = np.concatenate([k + f[bad].ravel(), pos[:, 1]])
    g = coo_matrix((np.ones(len(rows), np.int8), (rows, cols)), shape=(k + n, k + n))
    lab = connected_components(g, directed=False)[1][:k]
    _, lab = np.unique(lab, return_inverse=True)
    out = []
    for i in range(int(lab.max()) + 1):
        ff = bad[lab == i]
        p = v[_unique(f[ff])]
        c = p.mean(0)
        out.append({"centre": [round(float(x), 6) for x in c],
                    "radius": round(float(np.linalg.norm(p - c, axis=1).max()), 6), "faces": int(len(ff))})
    return sorted(out, key=lambda s: -s["faces"])


# ---- the check -------------------------------------------------------------------------------
def inspect(v: np.ndarray, f: np.ndarray, intersections: bool = False) -> dict:
    """What is wrong with the mesh. Counts are of faces unless named otherwise; holes and pieces
    come with their sizes (hole perimeter and piece area as a share of the model's size and of the
    largest piece) so the editor can say how many a threshold takes."""
    v, f = np.asarray(v, np.float64), np.asarray(f, np.int64)
    n, F = len(v), len(f)
    size = model_size(v, f)
    rep: dict = {"faces": F, "vertices": n, "size": size}
    used = np.zeros(n, bool)
    used[f.ravel()] = True
    rep["unreferenced_vertices"] = int((~used).sum())
    degen = degenerate_faces(v, f)
    dup, pil = repeats(f)
    rep["degenerate_faces"] = int(degen.sum())
    rep["duplicate_faces"] = int((dup & ~degen).sum())
    rep["pillow_faces"] = int((pil & ~degen).sum())
    good = ~(degen | dup | pil)
    g = _fix_duplicates(v, f, None)[1]  # the faces that are fine on their own, a pillow's kept as the fix keeps it
    # pieces that flatten to no area: without their degenerate faces and every face whose vertex set
    # appears more than once (a pillow's two faces cancel when laid flat), nothing with area remains
    lab_all = pieces(f, n)
    area_all = face_areas(v, f)
    k_all = int(lab_all.max()) + 1 if F else 0
    shared_set = np.zeros(F, bool)
    if F:
        _, inv, cnt = _face_sets(f)
        shared_set = cnt[inv] > 1
    good_area = np.bincount(lab_all, area_all * ~(degen | shared_set), k_all) if F else np.zeros(0)
    total = float(area_all[good].sum())
    flat = good_area <= 1e-9 * max(total, 1e-300)
    rep["flat_pieces"] = int(flat.sum())
    rep["flat_piece_faces"] = int(flat[lab_all].sum()) if F else 0
    # topology of the faces that are fine on their own
    he, fid, und, dirk = _edges(g, n)
    u, uc = np.unique(und, return_counts=True)
    rep["nonmanifold_edges"] = int((uc > 2).sum())
    rep["open_edges"] = int((uc == 1).sum())
    two = _isin(und, u[uc == 2])
    d, dc = np.unique(dirk[two], return_counts=True)
    rep["flipped_edges"] = int((dc > 1).sum())  # two faces running along an edge the same way
    # how many faces the orientation fix would turn (only run when some edge says it has work)
    rep["flipped_faces"] = _fix_orient(v, g, None)[3]["flipped_faces"] if rep["flipped_edges"] else 0
    rep["bowtie_vertices"] = int(len(bowtie_vertices(v, g)))
    # holes: the open edges, joined where they share a vertex, whichever way their faces are wound
    # (boundary_loops follows the winding, so a flipped face would read as a hole of its own)
    per = _open_rims(v, u[uc == 1], n)
    rep["holes"] = len(per)
    rep["hole_sizes"] = sorted((round(p / size, 6) if size else 0.0 for p in per), reverse=True)
    lab = pieces(g, n)
    areas = np.bincount(lab, face_areas(v, g)) if len(g) else np.zeros(0)
    nfaces = np.bincount(lab) if len(g) else np.zeros(0, np.int64)
    o = np.argsort(-areas)
    big = float(areas.max()) if len(areas) else 0.0
    rep["pieces"] = int(len(areas))
    rep["piece_sizes"] = [round(float(a) / big, 6) if big else 0.0 for a in areas[o]]
    rep["piece_faces"] = [int(x) for x in nfaces[o]]
    if intersections:
        pairs = self_intersections(v, g)
        rep["intersecting_pairs"] = int(len(pairs))
        rep["intersecting_faces"] = int(len(_unique(pairs))) if len(pairs) else 0
        # where they are, for the editor to show: the spots, and the faces as vertex ids (the
        # editor's own: g only drops and turns faces, it keeps the vertices)
        rep["intersection_spots"] = intersection_spots(v, g, pairs)
        rep["intersecting_tris"] = g[_unique(pairs)[:SPOT_FACES]].ravel().tolist() if len(pairs) else []
    return rep


def _open_rims(v: np.ndarray, open_keys: np.ndarray, n: int) -> list[float]:
    """Perimeter of each set of open edges (undirected keys a * n + b) connected through vertices."""
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components

    if not len(open_keys):
        return []
    a, b = open_keys // n, open_keys % n
    g = coo_matrix((np.ones(len(a), np.int8), (a, b)), shape=(n, n))
    lab = connected_components(g, directed=False)[1][a]
    _, lab = np.unique(lab, return_inverse=True)
    return [float(x) for x in np.bincount(lab, np.linalg.norm(v[a] - v[b], axis=1))]


def summary(rep: dict) -> dict:
    """The report without its per-hole and per-piece lists."""
    return {k: x for k, x in rep.items() if not isinstance(x, list)}


def problems(rep: dict) -> list[dict]:
    """The report as warnings for texturing and painting: what breaks or spoils it, how much of the
    mesh it affects, and the fix (the Sanitize mesh fixes that take it away)."""
    F = max(rep.get("faces", 0), 1)
    share = lambda k: k / F  # noqa: E731
    out = []

    def add(code, severity, title, count, faces, effect, fix, fixes):
        out.append({"code": code, "severity": severity, "title": title, "count": int(count),
                    "share": round(share(faces), 6), "effect": effect, "fix": fix, "fixes": fixes})

    save = "Sanitize mesh… in Sculpt (or just saving the mesh there) removes them"
    if rep.get("pillow_faces"):
        add("pillows", "breaks", "pillows: faces repeated with the opposite winding", rep["pillow_faces"],
            2 * rep["pillow_faces"], "OpenMVS texturing crashes on them (Texture leaves them out, so they stay "
            "untextured); paint lands on one of the two and flickers", save, ["duplicates"])
    if rep.get("bowtie_vertices"):
        add("bowtie", "breaks", "bow-tie vertices: two fans of faces touching at one point", rep["bowtie_vertices"],
            6 * rep["bowtie_vertices"], "OpenMVS texturing can crash on them (std::out_of_range in its atlas)",
            "Sanitize mesh… → Make it manifold (and Close holes for the small openings that leaves)",
            ["manifold", "holes"])
    if rep.get("nonmanifold_edges"):
        add("nonmanifold-edges", "breaks", "non-manifold edges: three or more faces on one edge",
            rep["nonmanifold_edges"], 3 * rep["nonmanifold_edges"],
            "texturing and the texture layout can't tell the sides apart there",
            "Sanitize mesh… → Make it manifold (and Close holes)", ["manifold", "holes"])
    if rep.get("flat_pieces"):
        add("flat-pieces", "breaks", "pieces that flatten to no area", rep["flat_pieces"], rep["flat_piece_faces"],
            "Carry the texture over (and Rebuild layout) can't lay them out: the new layout comes out degenerate",
            "Sanitize mesh… → Remove degenerate and duplicate faces (or Remove small loose pieces)",
            ["duplicates", "pieces"])
    if rep.get("degenerate_faces"):
        add("degenerate", "spoils", "degenerate faces: no area", rep["degenerate_faces"], rep["degenerate_faces"],
            "they get no texture and can upset OpenMVS texturing", save, ["duplicates"])
    if rep.get("duplicate_faces"):
        add("duplicates", "spoils", "duplicate faces", rep["duplicate_faces"], 2 * rep["duplicate_faces"],
            "two faces on the same spot fight for the texture: paint shows on one only and flickers", save,
            ["duplicates"])
    if rep.get("flipped_faces"):
        add("flipped", "spoils", "faces wound against their neighbours", rep["flipped_faces"], rep["flipped_faces"],
            "their normals point inwards: they shade dark and the photos are projected from the wrong side",
            "Sanitize mesh… → Recompute normals and orientation", ["orient"])
    return out


# ---- the fixes ---------------------------------------------------------------------------------
def _fix_duplicates(v, f, c, **_):
    degen = degenerate_faces(v, f)
    dup, pil = repeats(f)
    drop = degen | dup | pil
    if pil.any():
        # Of a pillow the first face stays, and the editor's face order is arbitrary (its BVH reorders
        # it): turn the one kept to run along its edges the opposite way to its neighbours, as they do.
        # (clean.repeated_faces, for the save and the clean stage, keeps that face instead.)
        _, inv, cnt = _face_sets(f)
        has_pillow = np.zeros(len(cnt), bool)
        has_pillow[inv[pil]] = True
        kept = np.flatnonzero(~drop & has_pillow[inv])
        alone = f[~drop & (cnt[inv] == 1)]
        fk = f[kept]
        turn = edge_clashes(fk[:, ::-1], alone, len(v)) < edge_clashes(fk, alone, len(v))
        f = f.copy()
        f[kept[turn]] = fk[turn][:, ::-1]
    return v, f[~drop], c, {"removed_faces": int(drop.sum()), "degenerate": int(degen.sum()),
                            "duplicates": int((dup & ~degen).sum()), "pillows": int((pil & ~degen).sum())}


def _fix_manifold(v, f, c, **_):
    """Faces on non-manifold edges (Open3D keeps the two largest), then bow-tie vertices with their
    faces (a small opening: Close holes closes it), until none are left."""
    import open3d as o3d

    m = o3d.geometry.TriangleMesh(o3d.utility.Vector3dVector(v), o3d.utility.Vector3iVector(f.astype(np.int32)))
    m.vertex_colors = o3d.utility.Vector3dVector(c / 255.0)
    f0, edges, verts = len(f), 0, 0
    for _ in range(10):
        nf = len(m.triangles)
        m.remove_non_manifold_edges()
        edges += nf - len(m.triangles)
        bad = np.asarray(m.get_non_manifold_vertices())
        if not len(bad):
            break
        mask = np.zeros(len(m.vertices), bool)
        mask[bad] = True
        m.remove_vertices_by_mask(mask)
        verts += len(bad)
    V, F = np.asarray(m.vertices), np.asarray(m.triangles).astype(np.int64)
    C = np.clip(np.round(np.asarray(m.vertex_colors) * 255), 0, 255).astype(np.uint8)
    return V, F, C, {"removed_faces": f0 - len(F), "nonmanifold_edge_faces": edges, "bowtie_vertices": verts}


def _fix_pieces(v, f, c, piece_size: float = 0.05, **_):
    lab = pieces(f, len(v))
    if not len(lab):
        return v, f, c, {"removed_pieces": 0, "removed_faces": 0}
    areas = np.bincount(lab, face_areas(v, f))
    small = areas < piece_size * areas.max()
    drop = small[lab]
    return v, f[~drop], c, {"removed_pieces": int(small.sum()), "removed_faces": int(drop.sum())}


def _fix_orient(v, f, c, **_):
    """One winding throughout each piece (Open3D's orient_triangles), then each piece turned so its
    normals point out: the volume it encloses, measured from its centre, is positive."""
    import open3d as o3d

    if not len(f):
        return v, f, c, {"flipped_faces": 0}
    m = o3d.geometry.TriangleMesh(o3d.utility.Vector3dVector(v), o3d.utility.Vector3iVector(f.astype(np.int32)))
    consistent = bool(m.orient_triangles())
    F = np.asarray(m.triangles).astype(np.int64) if consistent else f.copy()
    lab = pieces(F, len(v))
    a, b, cc = v[F[:, 0]], v[F[:, 1]], v[F[:, 2]]
    k = int(lab.max()) + 1
    w = face_areas(v, F)
    centre = np.stack([np.bincount(lab, (a + b + cc)[:, i] / 3 * w, k) for i in range(3)], 1)
    centre /= np.maximum(np.bincount(lab, w, k), 1e-300)[:, None]
    o = centre[lab]
    vol = np.bincount(lab, np.einsum("ij,ij->i", a - o, np.cross(b - o, cc - o)), k)
    inward = vol < 0
    F[inward[lab]] = F[inward[lab]][:, ::-1]
    flipped = int((_parity(f) != _parity(F)).sum())  # orient_triangles keeps the faces in order
    return v, F, c, {"flipped_faces": flipped, "pieces_turned_out": int(inward.sum()),
                     **({} if consistent else {"not_orientable": True})}


def _cancel_flaps(F: np.ndarray, n_old: int) -> tuple[np.ndarray, int]:
    """Lid faces (F[n_old:]) that are an existing face wound the other way: a flap folded across
    the hole, whose open edges the rim runs along. The lid covers the flap's spot, so the flap and
    its mirror cancel: both go, and the rest of the lid closes the hole without a pillow or a third
    face on the flap's edge. Returns the faces and how many flaps went."""
    old, lid = F[:n_old], F[n_old:]
    if not len(lid):
        return F, 0
    # only a lid face made of existing vertices can be one (most of a lid is new vertices)
    used = np.zeros(int(F.max()) + 1, bool)
    used[old.ravel()] = True
    cand = np.flatnonzero(used[lid].all(1))
    on = np.zeros(len(used), bool)
    on[lid[cand].ravel()] = True
    near = np.flatnonzero(on[old].all(1))  # old faces made of those vertices only
    seen = {tuple(sorted(x)): int(i) for i, x in zip(near, old[near].tolist())}
    drop_old, drop_lid = [], []
    for j, x in zip(cand.tolist(), lid[cand].tolist()):
        i = seen.get(tuple(sorted(x)))
        if i is not None and _parity(old[i:i + 1])[0] != _parity(lid[j:j + 1])[0]:
            drop_old.append(i)
            drop_lid.append(n_old + j)
            del seen[tuple(sorted(x))]
    if not drop_old:
        return F, 0
    keep = np.ones(len(F), bool)
    keep[drop_old + drop_lid] = False
    return F[keep], len(drop_old)


def _fix_holes(v, f, c, hole_size: float = 0.1, **_):
    """Close every hole whose rim is at most ``hole_size`` of the model's size (its bounding box
    diagonal) long, with the editor's membrane lids (meshops.close_loops). A flap folded across a
    hole goes with its mirror in the lid (_cancel_flaps), or the next run would find a pillow there,
    take it out and open the hole again."""
    from .clean import boundary_loops
    from .meshops import close_loops

    loops = boundary_loops(f)
    size = model_size(v, f)
    per = [float(np.linalg.norm(v[l] - v[np.roll(l, -1)], axis=1).sum()) for l in loops]
    sel = [l for l, p in zip(loops, per) if p <= hole_size * size]
    if not sel:
        return v, f, c, {"closed_holes": 0, "left_open": len(loops)}
    V, F, C = close_loops(v, f, c, sel)
    F, flaps = _cancel_flaps(F, len(f))
    return V, F, C, {"closed_holes": len(sel), "left_open": len(loops) - len(sel), "added_faces": len(F) - len(f) + flaps,
                     **({"removed_flaps": flaps} if flaps else {})}


def _fix_unreferenced(v, f, c, **_):
    used = np.zeros(len(v), bool)
    used[f.ravel()] = True
    remap = np.cumsum(used) - 1
    return v[used], remap[f], c[used], {"removed_vertices": int((~used).sum())}


def _fix_intersections(v, f, c, **_):
    pairs = self_intersections(v, f)
    return v, f, c, {"intersecting_pairs": int(len(pairs)),
                     "intersecting_faces": int(len(_unique(pairs))) if len(pairs) else 0}


STEPS = {"duplicates": _fix_duplicates, "manifold": _fix_manifold, "pieces": _fix_pieces, "orient": _fix_orient,
         "holes": _fix_holes, "unreferenced": _fix_unreferenced, "intersections": _fix_intersections}


def sanitize(v, f, c, fixes, hole_size: float = 0.1, piece_size: float = 0.05):
    """Run the chosen fixes (FIXES order, whatever order they are given in). Returns the new mesh and
    {"steps": [{"fix", "seconds", ...what it changed}], "unchanged", "faces_in", "faces_out",
    "after": summary of inspect on the result}."""
    v, f, c = np.asarray(v, np.float64), np.asarray(f, np.int64), np.asarray(c, np.uint8)
    unknown = set(fixes) - set(FIXES)
    if unknown:
        raise ValueError(f"unknown fix: {', '.join(sorted(unknown))}")
    V, F, C = v, f, c
    steps = []
    for fix in FIXES:
        if fix not in fixes:
            continue
        t = time.perf_counter()
        n = len(F)
        V, F, C, info = STEPS[fix](V, F, C, hole_size=hole_size, piece_size=piece_size)
        dt = time.perf_counter() - t
        _learn(fix, n, dt)
        steps.append({"fix": fix, "seconds": round(dt, 3), **info})
        if not len(F):
            raise ValueError(f"nothing would be left of the mesh after '{fix}': not changed")
    unchanged = len(V) == len(v) and len(F) == len(f) and np.array_equal(F, f) and np.array_equal(V, v)
    after = summary(inspect(V, F))
    return V, F, C, {"steps": steps, "unchanged": bool(unchanged), "faces_in": len(f), "faces_out": len(F),
                     "after": after}


# ---- time estimates --------------------------------------------------------------------------
# Seconds per million faces for each fix (and the check itself), measured on this machine: a short
# benchmark on a synthetic mesh the first time they are asked for, then the fixes actually run
# (the latest run of a fix on a mesh of at least BENCH_FACES / 4 faces replaces the benchmark's).
BENCH_FACES = 120_000
_rates: dict[str, float] = {}
_lock = threading.Lock()


@functools.lru_cache(maxsize=1)
def machine() -> dict:
    cpu = ""
    try:
        for line in open("/proc/cpuinfo"):
            if line.startswith("model name"):
                cpu = line.split(":", 1)[1].strip()
                break
    except OSError:
        pass
    if not cpu:
        import platform

        cpu = platform.processor() or platform.machine()
    return {"cpu": cpu, "threads": os.cpu_count() or 1, "device": "cpu"}


def _learn(fix: str, faces: int, seconds: float) -> None:
    if faces >= BENCH_FACES // 4:
        with _lock:
            _rates[fix] = seconds / faces * 1e6


def test_mesh(faces: int = BENCH_FACES):
    """A sphere of about ``faces`` faces with one of each problem: a pillow, a duplicate, a degenerate
    face, a bow-tie vertex (a small cone touching it at a pole), a hole, a loose piece and a face
    through the surface. Also the benchmark's mesh."""
    import open3d as o3d

    res = max(4, int(np.sqrt(faces / 2)))
    s = o3d.geometry.TriangleMesh.create_sphere(1.0, res)
    v, f = np.asarray(s.vertices), np.asarray(s.triangles).astype(np.int64)
    top = int(np.argmax(v[:, 2]))
    # a hole: drop the faces around a vertex on the equator
    eq = int(np.argmin(np.abs(v[:, 2]) + np.abs(v[:, 1]) + np.abs(v[:, 0] - 1)))
    f = f[~(f == eq).any(1)]
    n = len(v)
    # a cone touching the sphere at its top vertex only: a bow-tie
    ring = np.array([[np.cos(t) * 0.1, np.sin(t) * 0.1, 1.15] for t in np.linspace(0, 2 * np.pi, 6, endpoint=False)])
    cone = np.array([[top, n + (i + 1) % 6, n + i] for i in range(6)])
    capf = np.array([[n + 6, n + i, n + (i + 1) % 6] for i in range(6)])
    v = np.concatenate([v, ring, [[0, 0, 1.15]]])
    # a loose piece: a small tetrahedron away from the sphere
    m = len(v)
    tet = np.array([[2, 2, 2], [2.05, 2, 2], [2, 2.05, 2], [2, 2, 2.05]])
    v = np.concatenate([v, tet])
    tetf = m + np.array([[0, 2, 1], [0, 1, 3], [0, 3, 2], [1, 2, 3]])
    # a face cutting through the surface near (0, -1, 0)
    k = len(v)
    v = np.concatenate([v, [[0.0, -1.2, -0.02], [0.02, -0.8, 0.02], [-0.02, -0.8, 0.02]]])
    f = np.concatenate([f, cone, capf, tetf, [[k, k + 1, k + 2]],
                        f[:1, ::-1],            # a pillow
                        f[1:2],                 # a duplicate
                        [[f[2, 0], f[2, 0], f[2, 1]]]])  # a degenerate face
    v = np.concatenate([v, [[5.0, 5.0, 5.0]]])  # an unreferenced vertex
    return v, f, np.full((len(v), 3), 180, np.uint8)


def _benchmark() -> None:
    v, f, c = test_mesh(BENCH_FACES)
    t = time.perf_counter()
    inspect(v, f)
    rates = {"check": (time.perf_counter() - t) / len(f) * 1e6}
    V, F, C = v, f, c
    for fix in FIXES:
        t = time.perf_counter()
        n = len(F)
        V, F, C, _ = STEPS[fix](V, F, C)
        rates[fix] = (time.perf_counter() - t) / n * 1e6
    log.info("mesh fix benchmark (s per million faces): %s", {k: round(x, 2) for k, x in rates.items()})
    with _lock:
        for k, x in rates.items():
            _rates.setdefault(k, x)
        _rates["_benchmarked"] = 1.0


def estimates(faces: int) -> dict:
    """{"seconds": {fix: s, "check": s}, "machine": {...}, "basis": text} for a mesh of ``faces``."""
    if "_benchmarked" not in _rates:
        _benchmark()
    with _lock:
        rates = dict(_rates)
    return {"seconds": {k: round(rates[k] * faces / 1e6, 2) for k in (*FIXES, "check")},
            "machine": machine(),
            "basis": f"measured on this machine's CPU ({machine()['cpu']}, {machine()['threads']} threads): a "
                     f"{BENCH_FACES // 1000}k-face benchmark, then the fixes you run, scaled by the face count"}


# ---- the editor's requests -------------------------------------------------------------------
def check_file(path) -> dict:
    """inspect() of a mesh file (a textured model's vertices split at texture seams are joined
    first, so the check sees the surface), with its problems."""
    import trimesh

    m = trimesh.load(str(path), force="mesh", process=False)
    v, f = np.asarray(m.vertices, np.float64), np.asarray(m.faces, np.int64)
    pv, pid = np.unique(v, axis=0, return_inverse=True)
    rep = inspect(pv, pid.ravel()[f])
    return {"faces": rep["faces"], "problems": problems(rep), "report": summary(rep)}


def inspect_request(data: bytes) -> dict:
    """The editor's check of its current mesh (body as meshops.run): the report with its problems,
    self-intersections included, and the time each fix would take."""
    import struct

    from .meshops import decode_mesh

    (n,) = struct.unpack_from("<I", data)
    v, f, _ = decode_mesh(data, 4 + n)
    est = estimates(len(f))
    t = time.perf_counter()
    rep = inspect(v, f, intersections=True)
    _learn("check", len(f), time.perf_counter() - t)
    return {"report": rep, "problems": problems(rep), **est}
