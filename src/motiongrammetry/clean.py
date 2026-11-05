"""Isolate the subject and tidy the mesh.

Reconstruction meshes contain the floor, walls and floaters around the subject.
With masks available we apply silhouette carving: a vertex that projects onto
*background* in a meaningful fraction of the views it falls inside cannot be on
the subject (the visual-hull argument needs no occlusion reasoning). Without
masks we fall back to keeping the dominant connected component. A few bad masks
punch holes into the subject; small carved patches enclosed by kept surface that
the masks only disagree about are put back from the raw mesh. Then: drop small
islands, optional quadric decimation, and export PLY + GLB.
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import open3d as o3d
import pycolmap

from .mask import load_mask
from .meshfix import edge_clashes, isin_sorted, unique_sorted
from .workspace import Workspace

log = logging.getLogger(__name__)


def silhouette_votes(
    vertices: np.ndarray, recon: pycolmap.Reconstruction, masks_dir: Path
) -> tuple[np.ndarray, np.ndarray]:
    """For each vertex count (views it lands inside, views where it lands on background)."""
    n_in = np.zeros(len(vertices), dtype=np.int32)
    n_bg = np.zeros(len(vertices), dtype=np.int32)
    X = vertices.astype(np.float64)
    for image in recon.images.values():
        if not image.has_pose:
            continue
        mask = load_mask(masks_dir / f"{image.name}.png")
        if mask is None:
            continue
        cam = image.camera
        T = image.cam_from_world().matrix()
        pc = X @ T[:, :3].T + T[:, 3]
        front = np.flatnonzero(pc[:, 2] > 1e-6)
        if front.size == 0:
            continue
        uv = cam.img_from_cam(pc[front], check_cheirality=False)
        finite = (np.abs(uv) < 1e7).all(axis=1)  # distortion models blow up far off-axis (inf/NaN)
        front, uv = front[finite], uv[finite]
        u = np.floor(uv[:, 0]).astype(np.int64)
        v = np.floor(uv[:, 1]).astype(np.int64)
        h, w = mask.shape
        if (w, h) != (cam.width, cam.height):
            u = (u * w) // cam.width
            v = (v * h) // cam.height
        inside = (u >= 0) & (u < w) & (v >= 0) & (v < h)
        idx = front[inside]
        n_in[idx] += 1
        n_bg[idx] += ~mask[v[inside], u[inside]]
    return n_in, n_bg


def restore_holes(
    triangles: np.ndarray, vertices: np.ndarray, carve: np.ndarray, bg_frac: np.ndarray,
    max_area: float, max_bg: float = 0.5,
) -> tuple[np.ndarray, int]:
    """Un-carve the patches carving punched into the subject, keeping the raw geometry there.

    A connected patch of carved vertices goes back when it is enclosed by kept surface (it
    borders some and does not reach the raw mesh's open edge, where background runs off), is
    smaller than ``max_area`` times the kept area, and the masks only disagree about it: seen
    as background in at most ``max_bg`` of its views on average. A real gap (between arm and
    body) is background in most views; a hole from a few bad masks is not.
    Returns the new carve mask and the number of patches restored.
    """
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components

    n = len(vertices)
    if not carve.any() or max_area <= 0:
        return carve, 0
    edges = np.concatenate([triangles[:, [0, 1]], triangles[:, [1, 2]], triangles[:, [2, 0]]])
    both = edges[carve[edges[:, 0]] & carve[edges[:, 1]]]
    graph = coo_matrix((np.ones(len(both), np.int8), (both[:, 0], both[:, 1])), shape=(n, n))
    _, label = connected_components(graph, directed=False)
    label = np.where(carve, label, -1)
    n_comp = int(label.max()) + 1

    # Carved vertices of one triangle share an edge, hence a patch: label triangles by it.
    tri_label = label[triangles].max(axis=1)
    v = vertices[triangles]
    area = 0.5 * np.linalg.norm(np.cross(v[:, 1] - v[:, 0], v[:, 2] - v[:, 0]), axis=1)
    touched = tri_label >= 0
    comp_area = np.bincount(tri_label[touched], area[touched], minlength=n_comp)
    kept_area = area[~touched].sum()
    borders_kept = np.zeros(n_comp, bool)
    borders_kept[tri_label[touched & (~carve[triangles]).any(axis=1)]] = True

    key = np.sort(edges, axis=1).astype(np.int64)
    key, count = np.unique(key[:, 0] * n + key[:, 1], return_counts=True)
    open_edge = np.zeros(n, bool)
    open_edge[np.concatenate([key[count == 1] // n, key[count == 1] % n])] = True
    reaches_edge = np.bincount(label[open_edge & carve], minlength=n_comp) > 0

    sizes = np.bincount(label[carve], minlength=n_comp)
    mean_bg = np.bincount(label[carve], bg_frac[carve], minlength=n_comp) / np.maximum(sizes, 1)

    hole = borders_kept & ~reaches_edge & (comp_area <= max_area * kept_area) & (mean_bg <= max_bg)
    return carve & ~np.where(carve, hole[np.maximum(label, 0)], False), int(hole.sum())


def pieces_to_keep(faces: np.ndarray, areas: np.ndarray, min_component: float,
                   only_main: bool = False) -> np.ndarray:
    """Which connected pieces of the mesh to keep, from each piece's face count and area.

    The main piece is the one with the most faces, not the largest area: the reconstruction puts
    faces where the depth maps agree, so a well-seen surface is dense, while a misplaced camera
    leaves a few huge triangles far from the rest (on a walk through a flat, 7k faces spanning
    more area than the 1.9M-face flat itself). Other pieces are kept when they have at least
    ``min_component`` of the main piece's area and of its faces (a piece of a few huge triangles
    has the area but not the faces); with ``only_main``, none are.
    """
    main = int(np.argmax(faces))
    if only_main:
        keep = np.zeros(len(faces), bool)
    else:
        keep = (areas >= min_component * areas[main]) & (faces >= min_component * faces[main])
    keep[main] = True
    return keep


def clean_mesh(
    mesh_path: Path,
    out_path: Path,
    recon: pycolmap.Reconstruction | None = None,
    masks_dir: Path | None = None,
    bg_tolerance: float = 0.15,
    min_views: int = 3,
    min_component: float = 0.02,
    target_faces: int | None = None,
    ground: tuple[np.ndarray, float] | None = None,
    ground_margin: float = 0.03,
    fill_holes: float = 0.05,
) -> dict:
    mesh = o3d.io.read_triangle_mesh(str(mesh_path))
    if not mesh.has_triangles():
        raise RuntimeError(f"{mesh_path} has no triangles")
    info: dict = {"input": str(mesh_path), "faces_in": len(mesh.triangles), "vertices_in": len(mesh.vertices)}

    if recon is not None and masks_dir is not None and masks_dir.is_dir():
        verts = np.asarray(mesh.vertices)
        n_in, n_bg = silhouette_votes(verts, recon, masks_dir)
        # Unseen vertices (n_in < min_views) are kept: they are usually the underside.
        carve = (n_in >= min_views) & (n_bg > bg_tolerance * n_in)
        if fill_holes > 0:
            before = carve
            carve, holes = restore_holes(np.asarray(mesh.triangles), verts, carve,
                                         n_bg / np.maximum(n_in, 1), fill_holes)
            info["holes_filled"] = holes
            info["hole_vertices_restored"] = int((before & ~carve).sum())
            if holes:
                log.info("filled %d carved holes from the raw mesh (%d vertices)",
                         holes, info["hole_vertices_restored"])
        mesh.remove_vertices_by_mask(carve)
        info["carved_vertices"] = int(carve.sum())
        # How sure the masks were about what was carved: mostly unsure (background in at most half
        # its views) means carving probably took parts of the subject (advice.py suggests a gentler
        # bg_tolerance then), mostly sure means it was background.
        if carve.any():
            info["carved_uncertain"] = round(float((n_bg[carve] <= 0.5 * n_in[carve]).mean()), 3)
        log.info("silhouette carving removed %d/%d vertices", carve.sum(), len(verts))

    if ground is not None:
        # Drop the support surface: masks often leak into the contact shadow, leaving
        # a skirt of desk around the base. Margin is relative to the subject's height.
        n, d = ground
        h = np.asarray(mesh.vertices) @ n + d
        top = np.percentile(h, 99) if len(h) else 0.0
        if top > 0:
            below = h < ground_margin * top
            mesh.remove_vertices_by_mask(below)
            info["ground_removed_vertices"] = int(below.sum())
            log.info("ground plane removal dropped %d vertices", below.sum())

    mesh.remove_degenerate_triangles()
    mesh.remove_duplicated_triangles()
    mesh.remove_duplicated_vertices()
    mesh.remove_non_manifold_edges()

    clusters, counts, areas = mesh.cluster_connected_triangles()
    clusters = np.asarray(clusters)
    areas = np.asarray(areas)
    if len(areas):
        # No masks: only the main piece is kept (the subject, or the room of a scene).
        keep = pieces_to_keep(np.asarray(counts), areas, min_component,
                              only_main=recon is None or masks_dir is None)
        mesh.remove_triangles_by_mask(~keep[clusters])
        mesh.remove_unreferenced_vertices()
        info["components_kept"] = int(keep.sum())
        info["components_total"] = int(len(areas))

    if target_faces and len(mesh.triangles) > target_faces:
        mesh = mesh.simplify_quadric_decimation(target_faces)
    info["nonmanifold_vertices_removed"] = _make_manifold(mesh)
    mesh.compute_vertex_normals()
    if not mesh.has_triangles():
        raise RuntimeError("cleanup removed the whole mesh - masks may be wrong or misaligned "
                           "(try --bg-tolerance 0.4 or `mg clean --no-masks`)")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    o3d.io.write_triangle_mesh(str(out_path), mesh)
    info.update(output=str(out_path), faces_out=len(mesh.triangles), vertices_out=len(mesh.vertices))
    log.info("clean mesh: %d faces -> %s", len(mesh.triangles), out_path)
    return info


def repeated_faces(faces: np.ndarray) -> np.ndarray:
    """Mask of the faces on a vertex set another face already has, in either winding: every face of
    such a set but one. The one kept is wound like its neighbours (the faces on a vertex set of their
    own): of a "pillow", the same triangle stored both ways, the editor's face order is arbitrary (its
    BVH reorders faces), so the first is often the copy wound against them, and keeping it leaves a
    flipped face (meshfix.check_file reports it). When the windings tie, as for plain same-winding
    duplicates or a pillow with no neighbours, the earliest face stays."""
    if not len(faces):
        return np.zeros(0, bool)
    key = np.sort(faces, axis=1)  # the triangle's vertex set, winding-independent
    order = np.lexsort(key.T[::-1])  # group equal vertex sets together (stable: earliest first)
    skey = key[order]
    group = np.empty(len(faces), int)
    group[order] = np.cumsum(np.r_[True, np.any(skey[1:] != skey[:-1], axis=1)]) - 1
    shared = np.bincount(group)[group] > 1
    score = np.zeros(len(faces), int)  # half-edges run the same way as a neighbour's
    if shared.any():
        degenerate = (faces[:, 0] == faces[:, 1]) | (faces[:, 1] == faces[:, 2]) | (faces[:, 2] == faces[:, 0])
        score[shared] = edge_clashes(faces[shared], faces[~shared & ~degenerate], int(faces.max()) + 1)
    order = np.lexsort((np.arange(len(faces)), score, group))  # by set, then fewest clashes, then earliest
    sg = group[order]
    drop = np.ones(len(faces), bool)
    drop[order[np.r_[True, sg[1:] != sg[:-1]]]] = False  # keep the best face of each vertex set
    return drop


def broken_faces(faces: np.ndarray) -> np.ndarray:
    """Mask of the faces OpenMVS texturing can't take: a vertex used twice in one face, or a face
    on the same three vertices as another (a "pillow"; see _remove_opposite_faces), all but the one
    repeated_faces keeps. The clean stage and the editor's save drop them; a mesh saved before they
    did can still have them."""
    if not len(faces):
        return np.zeros(0, bool)
    degenerate = (faces[:, 0] == faces[:, 1]) | (faces[:, 1] == faces[:, 2]) | (faces[:, 2] == faces[:, 0])
    return degenerate | repeated_faces(faces)


def _remove_opposite_faces(mesh: o3d.geometry.TriangleMesh) -> int:
    """Drop faces that repeat another face's three vertices with the opposite winding.

    Open3D's ``remove_duplicated_triangles`` compares vertices as an ordered triple, so it
    keeps ``(a,b,c)`` and ``(c,b,a)`` - the same triangle traversed both ways. Editing leaves
    such pairs (a degenerate two-face "pillow"), and OpenMVS TextureMesh aborts on them:
    ``std::out_of_range`` from ``MeshTexture::GlobalSeamLeveling``, which builds its vertex/
    patch row map from the labelling and then looks a patch up that the face does not have.
    Two faces on the same three vertices contribute no area and no surface, so dropping one
    loses nothing; the one kept is the one wound like its neighbours (see repeated_faces)."""
    faces = np.asarray(mesh.triangles)
    if not len(faces):
        return 0
    drop = repeated_faces(faces)
    if drop.any():
        mesh.remove_triangles_by_mask(drop)
        mesh.remove_unreferenced_vertices()
    return int(drop.sum())


def _make_manifold(mesh: o3d.geometry.TriangleMesh, max_iter: int = 10) -> int:
    """Drop bow-tie vertices (carving leaves a few). OpenMVS TextureMesh crashes on them
    (std::out_of_range in the atlas builder) and they break most downstream tools."""
    _remove_opposite_faces(mesh)
    removed = 0
    for _ in range(max_iter):
        mesh.remove_non_manifold_edges()
        bad = np.asarray(mesh.get_non_manifold_vertices())
        if len(bad) == 0:
            break
        mask = np.zeros(len(mesh.vertices), bool)
        mask[bad] = True
        mesh.remove_vertices_by_mask(mask)
        removed += len(bad)
    mesh.remove_unreferenced_vertices()
    return removed


def fit_ground_plane(recon: pycolmap.Reconstruction, masks_dir: Path | None = None,
                     min_inlier_ratio: float = 0.25, points: np.ndarray | None = None):
    """Dominant plane of the background SfM points (desk / floor), oriented towards the cameras.

    Returns (unit normal n, offset d) with n.x + d = height above the plane, or None when
    there is no trustworthy plane. A plain white desk yields few SfM points, so RANSAC can
    lock onto the subject itself; with masks the fit uses background points only and the
    plane is rejected unless the subject lies above it. ``points`` (e.g. the dense MVS
    cloud, which does contain the desk) replaces the sparse SfM points when given.
    """
    pts = points if points is not None else np.array([p.xyz for p in recon.points3D.values()])
    if len(pts) < 50:
        return None
    subject = None
    if masks_dir is not None:
        n_in, n_bg = silhouette_votes(pts, recon, masks_dir)
        seen = n_in >= 3
        subject = pts[seen & (n_bg <= 0.15 * n_in)]
        pts = pts[seen & (n_bg >= 0.5 * n_in)]
        if len(pts) < 50 or len(subject) < 20:
            return None
    lo, hi = np.percentile(pts, [2, 98], axis=0)
    pcd = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(pts))
    (a, b, c, d), inliers = pcd.segment_plane(0.005 * float(np.linalg.norm(hi - lo)), 3, 2000)
    if len(inliers) < min_inlier_ratio * len(pts):
        return None
    n = np.array([a, b, c])
    scale = np.linalg.norm(n)
    n, d = n / scale, d / scale
    centres = np.array([img.projection_center() for img in recon.images.values() if img.has_pose])
    if np.median(centres @ n + d) < 0:
        n, d = -n, -d
    if subject is not None:
        # A few subject points below the plane are expected (reflections in a glossy
        # desk reconstruct as geometry under it); a plane through the subject is not.
        h = subject @ n + d
        top = np.percentile(h, 99)
        if top <= 0 or np.median(h) < 0.2 * top or (h > -0.05 * top).mean() < 0.85:
            log.info("plane fit rejected: it cuts through the subject")
            return None
    return n, float(d)


def gravity_alignment(recon: pycolmap.Reconstruction, mesh_vertices: np.ndarray,
                      ground: tuple[np.ndarray, float] | None = None) -> np.ndarray:
    """4x4 transform taking the SfM frame to glTF's Y-up frame, subject centred on the origin
    and resting on y=0. SfM has no notion of "up": use the support plane's normal when
    there is one, else the normal of the camera orbit when the cameras circle the subject on a
    roughly flat path (a phone pitched at the subject over a partial orbit tilts the mean camera
    up-vector by ~10 degrees; the orbit plane does not), else the mean camera up-vector (-y in
    OpenCV camera axes), since hand-held captures mostly keep the phone upright.
    """
    if ground is not None:
        up = ground[0]
    else:
        posed = [img for img in recon.images.values() if img.has_pose]
        ups = [img.cam_from_world().rotation.matrix().T @ np.array([0.0, -1.0, 0.0]) for img in posed]
        up = np.mean(ups, axis=0)
        centres = np.array([img.projection_center() for img in posed])
        if len(centres) >= 8:
            _, s, vt = np.linalg.svd(centres - centres.mean(0))
            if s[1] > 0.3 * s[0] and s[2] < 0.25 * s[1]:  # an arc, not a line, and flat
                up = vt[2] if vt[2] @ up > 0 else -vt[2]
    up = up / np.linalg.norm(up)
    # Rotation taking `up` to +Y (Rodrigues; handle the antiparallel case).
    y = np.array([0.0, 1.0, 0.0])
    v, c = np.cross(up, y), float(up @ y)
    if c < -0.999999:
        R = np.diag([1.0, -1.0, -1.0])
    else:
        vx = np.array([[0, -v[2], v[1]], [v[2], 0, -v[0]], [-v[1], v[0], 0]])
        R = np.eye(3) + vx + vx @ vx / (1 + c)
    p = mesh_vertices @ R.T
    centre = np.array([np.median(p[:, 0]), p[:, 1].min(), np.median(p[:, 2])])
    T = np.eye(4)
    T[:3, :3], T[:3, 3] = R, -centre
    return T


def export_glb(src: Path, dst: Path, transform: np.ndarray | None = None) -> Path:
    """Convert a PLY (vertex colours or OpenMVS-textured, over one image or several) into a single
    GLB file."""
    from .atlas import textured_scene

    scene = textured_scene(src)
    if transform is not None:
        scene.apply_transform(transform)
    scene.export(str(dst))
    return dst


def boundary_loops(faces: np.ndarray) -> list[list[int]]:
    """Ordered loops of open edges, each following its faces' winding (rim half-edge a -> b).
    From each open edge the next is found by turning around its end vertex through that vertex's
    own fan of faces (however many it has), so a vertex where two openings touch (a cut that reached
    another hole) is passed through correctly instead of losing both loops."""
    F = len(faces)
    if not F:
        return []
    faces = np.asarray(faces, np.int64)
    # half-edge k = slot * F + face: slot 0 is (v0, v1), 1 is (v1, v2), 2 is (v2, v0)
    he = np.concatenate([faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]]])
    n = int(faces.max()) + 1
    key = he[:, 0] * n + he[:, 1]
    order = np.argsort(key)
    pos = np.minimum(np.searchsorted(key[order], he[:, 1] * n + he[:, 0]), len(key) - 1)
    twin = np.where(key[order][pos] == he[:, 1] * n + he[:, 0], order[pos], -1)
    is_open = twin < 0
    opens = np.flatnonzero(is_open)

    # following[i]: the open half-edge (by its position in opens) leaving the end vertex b of open
    # half-edge opens[i], found by turning through b's fan: in the same face, the edge leaving b; if
    # that edge is shared, across it into the next face. All open half-edges turn together, one face
    # per step, for a few steps; the walks still going are round busy vertices (any valence) and
    # finish by pointer jumping over all the half-edges into those vertices, which doubles the steps
    # covered each pass. A walk that never meets an open edge goes round a fan with none (only on
    # broken meshes) and gets -1.
    following = np.full(len(opens), -1, np.int64)
    src, k = np.arange(len(opens)), opens
    for _ in range(16):
        nx = ((k // F + 1) % 3) * F + k % F      # same face, the edge leaving that vertex
        done = is_open[nx]
        following[src[done]] = np.searchsorted(opens, nx[done])
        src, k = src[~done], twin[nx[~done]]     # across it, into the next face of the fan
        if not len(k):
            break
    if len(k):
        busy = np.zeros(n, bool)
        busy[he[k, 1]] = True
        S = np.flatnonzero(busy[he[:, 1]])       # half-edges into a busy vertex (sorted)
        nx = ((S // F + 1) % 3) * F + S % F
        res = np.where(is_open[nx], nx, -1)      # the open edge each one reaches, once known
        jump = np.searchsorted(S, np.where(is_open[nx], S, twin[nx]))  # one face on (stays put once found)
        for _ in range(len(S).bit_length() + 1):  # a walk is never longer than len(S) faces
            res = np.where(res < 0, res[jump], res)
            jump = jump[jump]
        res = res[np.searchsorted(S, k)]
        following[src] = np.where(res < 0, -1, np.searchsorted(opens, res))

    tails, following = he[opens, 0].tolist(), following.tolist()
    loops, seen = [], [False] * len(opens)
    for start in range(len(opens)):
        if seen[start]:
            continue
        loop, k = [], start
        while k >= 0 and not seen[k]:
            seen[k] = True
            loop.append(tails[k])
            k = following[k]
        if k == start and len(loop) >= 3:
            loops.append(loop)
    # a loop through a shared vertex twice (a figure of eight: two openings touching) is two loops
    out = []
    while loops:
        loop = loops.pop()
        first = {}
        for i, a in enumerate(loop):
            if a in first:
                j = first[a]
                loops += [loop[j:i], loop[:j] + loop[i:]]
                break
            first[a] = i
        else:
            out.append(loop)
    return [l for l in out[::-1] if len(l) >= 3]


def close_holes(verts: np.ndarray, faces: np.ndarray, iters: int = 200, loops: list[list[int]] | None = None,
                owners: bool = False):
    """Cap holes with a soap film: concentric rings from the rim to its centroid, smoothed with
    the rim held fixed, so a large curved opening gets a smooth dome rather than a fan (and a
    planar opening a flat cap). ``loops`` limits it to those boundary loops (default: all).
    With ``owners`` also returns, for each new vertex, the index of the loop it caps."""
    out_v, out_f, base, own = [verts], [faces], len(verts), []
    for li, loop in enumerate(boundary_loops(faces) if loops is None else loops):
        n = len(loop)
        rings = int(np.clip(np.sqrt(n) / 2, 1, 12))
        rim = verts[loop]
        c = rim.mean(0)
        t = np.linspace(0, 1, rings + 2)[1:-1, None, None]
        grid = np.concatenate([rim[None], c + (rim[None] - c) * (1 - t), np.repeat(c[None, None], n, 1)])
        for _ in range(iters):  # Jacobi: each inner vertex moves to the mean of its 4 grid neighbours
            inner = (np.roll(grid[1:-1], 1, 1) + np.roll(grid[1:-1], -1, 1) + grid[:-2] + grid[2:]) / 4
            grid[1:-1] = inner
            grid[-1] = grid[-2].mean(0)
        ids = np.empty((rings + 2, n), np.int64)
        ids[0] = loop
        ids[1:-1] = base + np.arange(rings * n).reshape(rings, n)
        ids[-1] = base + rings * n
        out_v += [grid[1:-1].reshape(-1, 3), grid[-1, :1]]
        base += rings * n + 1
        # Wound against the rim's half-edges, so the cap faces the same way as the surface.
        a, b = ids[:-2], np.roll(ids[:-2], -1, 1)
        a2, b2 = ids[1:-1], np.roll(ids[1:-1], -1, 1)
        quads = np.stack([np.stack([b, a, a2], -1), np.stack([b, a2, b2], -1)], 2).reshape(-1, 3)
        fan = np.stack([np.roll(ids[-2], -1), ids[-2], ids[-1]], -1)
        out_f += [quads, fan]
        own.append(np.full(rings * n + 1, li))
    V, F = np.concatenate(out_v), np.concatenate(out_f)
    if owners:
        return V, F, np.concatenate(own) if own else np.zeros(0, np.int64)
    return V, F


def normals_outward(ws: Workspace, mesh_path: Path, n_cameras: int = 24) -> bool | None:
    """Whether the mesh's triangle normals point out of the subject, from what the cameras saw:
    rays from the real camera positions hit the visible surface from its front when they do
    (98-99.9% of hits on real scans). Geometry alone cannot tell for a partial scan: a face
    mask's centroid lies in front of it. None when there are no cameras to ask."""
    from .sfm import load_reconstruction

    try:
        rec = load_reconstruction(ws)
    except RuntimeError:
        return None
    m = o3d.io.read_triangle_mesh(str(mesh_path))
    if not m.has_triangles():
        return None
    scene = o3d.t.geometry.RaycastingScene()
    scene.add_triangles(o3d.core.Tensor(np.asarray(m.vertices, np.float32)),
                        o3d.core.Tensor(np.asarray(m.triangles, np.uint32)))
    imgs = [i for i in rec.images.values() if i.has_pose]
    front = back = 0
    for im in imgs[:: max(1, len(imgs) // n_cameras)]:
        cam, R, C = im.camera, im.cam_from_world().rotation.matrix(), im.projection_center()
        u, w = np.meshgrid(np.linspace(0, cam.width, 48), np.linspace(0, cam.height, 36))
        xy = cam.cam_from_img(np.column_stack([u.ravel(), w.ravel()]))
        dirs = np.column_stack([xy, np.ones(len(xy))]) @ R  # rows: R^T x, the rays in world axes
        dirs /= np.linalg.norm(dirs, axis=1, keepdims=True)
        hit = scene.cast_rays(o3d.core.Tensor(np.hstack([np.repeat(C[None], len(dirs), 0), dirs]).astype(np.float32)))
        ok = np.isfinite(hit["t_hit"].numpy())
        facing = np.einsum("ij,ij->i", dirs[ok], hit["primitive_normals"].numpy()[ok])
        front += int((facing < 0).sum())
        back += int((facing > 0).sum())
    return None if front + back < 50 else front >= back


def _on_open_edge(f: np.ndarray, tri: np.ndarray, uv: np.ndarray, eps: float = 1e-4) -> np.ndarray:
    """For closest points (triangle id, barycentric u, v): whether each lies on a boundary edge."""
    n = int(f.max()) + 1
    e = np.sort(np.concatenate([f[:, [0, 1]], f[:, [1, 2]], f[:, [2, 0]]]), 1).astype(np.int64)
    key, cnt = np.unique(e[:, 0] * n + e[:, 1], return_counts=True)
    open_keys = key[cnt == 1]
    if not len(open_keys):
        return np.zeros(len(tri), bool)
    w = np.column_stack([1 - uv[:, 0] - uv[:, 1], uv[:, 0], uv[:, 1]])  # weights of the 3 corners
    out = np.zeros(len(tri), bool)
    for k, (a, b) in enumerate(((1, 2), (0, 2), (0, 1))):  # weight k ~ 0: on the edge opposite corner k
        sel = np.flatnonzero(w[:, k] < eps)
        if len(sel):
            ea, eb = f[tri[sel], a].astype(np.int64), f[tri[sel], b].astype(np.int64)
            out[sel] |= isin_sorted(np.minimum(ea, eb) * n + np.maximum(ea, eb), open_keys)
    return out


AUTO_SHELL_LIDS = 0.35  # print_mode auto: shell when lids would invent more of the surface than this


def solidify(v: np.ndarray, f: np.ndarray, pitch: float = 0.3, min_wall: float = 0.8, mode: str = "solid",
             shell: float = 3.0, stand: str = "auto", stand_h: float = 2.0, min_piece: float = 0.01,
             outward: bool | None = None, shell_side: str = "inward"):
    """Rebuild a print mesh (mm, Z-up, resting on z=0) as a clean solid on a voxel grid.

    The scanned surface is the only thing that was measured, so it stays where it is and
    material is only ever added behind it (``outward``: whether the triangle normals point out
    of the subject, see ``normals_outward``; None guesses from the mesh). solid: inside is what
    a flood fill from the grid's border cannot reach through the surface, so surfaces crossing
    each other (a lid cutting through the scan) cannot confuse it, unlike ray parity; walls
    thinner than ``min_wall`` are thickened inwards. shell: the surface ``shell`` mm thick, on
    ``shell_side`` (inward / outward / both), for a partial scan: prints what was scanned and
    invents nothing. Either way the result is one closed surface without self-intersections.
    With ``stand`` "auto" a disc is added underneath when the centre of mass is not over the
    model's own base. Returns (vertices, faces, info).
    """
    from scipy import ndimage
    from scipy.spatial import Delaunay
    from skimage.measure import marching_cubes

    pad = max(min_wall, shell if mode == "shell" else 0.0) + 3 * pitch
    lo, hi = v.min(0) - pad, v.max(0) + pad
    lo[2] -= stand_h
    if stand == "auto":  # sideways room for a stand that reaches past the model's outline
        room = 0.15 * float(np.ptp(v[:, :2], 0).max())
        lo[:2] -= room
        hi[:2] += room
    dims = np.ceil((hi - lo) / pitch).astype(int) + 1
    scene = o3d.t.geometry.RaycastingScene()
    scene.add_triangles(o3d.core.Tensor(v.astype(np.float32)), o3d.core.Tensor(f.astype(np.uint32)))
    ax = [lo[i] + pitch * np.arange(dims[i]) for i in range(3)]
    yz = np.stack(np.meshgrid(ax[1], ax[2], indexing="ij"), -1).reshape(-1, 2)
    d = np.empty(dims, np.float32)
    for i in range(dims[0]):  # one slab at a time keeps the query arrays small
        q = np.column_stack([np.full(len(yz), ax[0][i]), yz]).astype(np.float32)
        d[i] = scene.compute_distance(o3d.core.Tensor(q)).numpy().reshape(dims[1], dims[2])

    # Near the surface: which side of it each voxel centre lies on (> 0: outside the subject).
    if outward is None:
        tri = v[f]
        outward = bool(np.einsum("ij,ij->i", tri[:, 0], np.cross(tri[:, 1], tri[:, 2])).sum() >= 0)
    thick = shell if mode == "shell" else max(min_wall, 0.87 * pitch)
    band = np.argwhere(d < thick + pitch)
    qb = (lo + band * pitch).astype(np.float32)
    near = scene.compute_closest_points(o3d.core.Tensor(qb))
    side = np.einsum("ij,ij->i", qb - near["points"].numpy(), near["primitive_normals"].numpy())
    if not outward:
        side = -side
    db = d[tuple(band.T)]

    # Past the rim of a partial scan the nearest point is on the rim (an open edge), and what lies
    # "behind" it there is not the subject. Interior creases of a noisy scan are fine.
    straight = ~_on_open_edge(f, near["primitive_ids"].numpy(), near["primitive_uvs"].numpy())

    def add_behind(depth, sel):  # material from the surface to ``depth`` mm on the selected side
        idx = tuple(band[sel].T)
        sdf[idx] = np.minimum(sdf[idx], np.maximum(-db[sel], db[sel] - depth))

    if mode == "shell":
        if shell_side == "both":
            sdf = d - shell / 2
        else:
            sdf = d.copy()
            add_behind(shell, straight & (side < 0 if shell_side == "inward" else side >= 0))
    else:
        # voxels the surface passes through (centre within half a diagonal) block the flood
        wall = d < pitch * 0.87
        lab, _ = ndimage.label(~wall)
        faces6 = [lab[0], lab[-1], lab[:, 0], lab[:, -1], lab[:, :, 0], lab[:, :, -1]]
        border = unique_sorted(np.concatenate([x.ravel() for x in faces6]))
        outside = np.isin(lab, border[border > 0])
        # Blocking voxels on the outer skin take the side of the nearest triangle (else the
        # surface would sit up to 0.87 voxel too far out).
        skin = wall[tuple(band.T)] & ndimage.binary_dilation(outside, iterations=2)[tuple(band.T)]
        outside[tuple(band[skin].T)] = side[skin] >= 0
        sdf = np.where(outside, d, -d)
        if min_wall > 0:  # thin flaps get thicker behind the scanned skin, never in front of it
            add_behind(min_wall, straight & (side < 0))
    # Fins thinner than ~2 voxels (a scan's ragged rim, hair) cannot be printed and weld into
    # non-manifold edges: drop them. Everything thicker keeps its exact, sub-voxel surface.
    solid = sdf < 0
    fins = solid & ~ndimage.binary_opening(solid)
    sdf[fins] = pitch / 2
    info: dict = {}
    occ = np.argwhere(sdf < 0)
    if not len(occ):
        raise RuntimeError("nothing solid left to print")
    com = lo + occ.mean(0) * pitch
    k0 = occ[:, 2].min()
    foot = lo[:2] + np.argwhere((sdf < 0)[:, :, min(k0 + 1, dims[2] - 1)]) * pitch
    stable = len(foot) >= 3 and np.ptp(foot, 0).min() > 0 and Delaunay(foot).find_simplex(com[:2]) >= 0
    info.update(footprint_mm2=round(float(len(foot) * pitch * pitch)), stable=bool(stable))
    if not stable and stand == "auto":
        # a disc around the footprint and the centre of mass, kept 3 voxels inside the grid
        # (a surface cut by the grid's edge would stay open)
        pts = np.vstack([foot, com[None, :2]]) if len(foot) else com[None, :2]
        centre = (pts.min(0) + pts.max(0)) / 2
        r = float(np.linalg.norm(pts - centre, axis=1).max()) + 3.0
        r = min(r, float(np.min(np.r_[centre - lo[:2], hi[:2] - centre])) - 3 * pitch)
        zb = float(v[:, 2].min())
        X, Y, Z = np.meshgrid(*ax, indexing="ij")
        disc = np.maximum(np.hypot(X - centre[0], Y - centre[1]) - r, np.maximum((zb - stand_h) - Z, Z - (zb + pitch)))
        sdf = np.minimum(sdf, disc)
        info["stand_radius_mm"] = round(float(r), 1)
    vv, ff, _, _ = marching_cubes(sdf, 0.0, spacing=(pitch,) * 3, allow_degenerate=False)
    # STL stores float32: weld at the precision it will be written with, so slivers that would
    # collapse when rounded (opening edges, folding into "self-intersections") go now.
    vv = np.round((vv + lo) / (pitch * 1e-3)) * (pitch * 1e-3)
    vv = vv.astype(np.float32).astype(np.float64)
    ff = ff.astype(np.int64)
    m = o3d.geometry.TriangleMesh(o3d.utility.Vector3dVector(vv), o3d.utility.Vector3iVector(ff))
    m.remove_duplicated_vertices()
    m.remove_degenerate_triangles()
    m.remove_duplicated_triangles()
    m.remove_unreferenced_vertices()
    clusters, _, areas = m.cluster_connected_triangles()
    areas = np.asarray(areas)
    if len(areas) > 1:  # specks from the grid, and voids sealed inside the solid
        m.remove_triangles_by_mask(areas[np.asarray(clusters)] < min_piece * areas.max())
        m.remove_unreferenced_vertices()
    # No _make_manifold here: a pinch vertex is fine for slicers, deleting it would open a hole.
    vv, ff = np.asarray(m.vertices), np.asarray(m.triangles)
    # Welding at STL precision can leave a pinhole (a duplicate triangle dropped): close any.
    pinholes = [lp for lp in boundary_loops(ff) if len(lp) <= 12]
    if pinholes:
        vv, ff = close_holes(vv, ff, loops=pinholes)
    tri = vv[ff]
    if np.einsum("ij,ij->i", tri[:, 0], np.cross(tri[:, 1], tri[:, 2])).sum() < 0:
        ff = ff[:, ::-1]  # outward normals
    vv = vv - [0.0, 0.0, vv[:, 2].min()]
    return vv, ff, info


def print_report(mesh, lid_share: float, info: dict, min_wall: float, mode: str, stand: str) -> list[dict]:
    """What may still go wrong when printing, and the option that addresses it."""
    out = []
    add = lambda level, code, msg, hint="": out.append({"level": level, "code": code, "message": msg, "hint": hint})  # noqa: E731
    if mode == "solid" and lid_share > 0.35:
        add("warning", "print-invented", f"{lid_share:.0%} of the print closes parts the camera never saw "
            "(smooth lids, not the subject)", "film all around the subject, or print_mode shell to print only "
            "the scanned surface as a shell")
    if info.get("stand_radius_mm"):
        add("info", "print-stand", f"added a {2 * info['stand_radius_mm']:.0f} mm stand: the model would not "
            "balance on its own base", "print_stand none to leave it off (then print with supports)")
    elif not info.get("stable", True):
        add("warning", "print-unstable", "the model does not balance on its base", "print_stand auto adds a stand")
    pieces = len(mesh.split(only_watertight=False))
    if pieces > 1:
        add("warning", "print-pieces", f"the print is {pieces} separate pieces", "remove loose pieces in the editor")
    # wall thickness, sampled: a ray from just inside the surface, against the normal
    scene = o3d.t.geometry.RaycastingScene()
    scene.add_triangles(o3d.core.Tensor(mesh.vertices.astype(np.float32)), o3d.core.Tensor(mesh.faces.astype(np.uint32)))
    idx = np.random.default_rng(0).choice(len(mesh.faces), min(20000, len(mesh.faces)), replace=False)
    p, n = mesh.triangles_center[idx], mesh.face_normals[idx]
    t = scene.cast_rays(o3d.core.Tensor(np.hstack([p - n * 1e-3, -n]).astype(np.float32)))["t_hit"].numpy()
    t = t[np.isfinite(t)]
    thin = float((t < 0.9 * min_wall).mean()) if len(t) and min_wall > 0 else 0.0
    if thin > 0.01:
        add("warning", "print-thin", f"{thin:.0%} of the surface is thinner than {min_wall} mm",
            "mostly the scan's ragged edges, where the shell tapers: trim them in the editor, or raise print_shell"
            if mode == "shell" else "raise print_min_wall")
    over = (mesh.face_normals[:, 2] < -np.cos(np.radians(45))) & (mesh.triangles_center[:, 2] > 1.0)
    share = float(mesh.area_faces[over].sum() / mesh.area)
    if share > 0.25:
        add("info", "print-supports", f"{share:.0%} of the surface overhangs more than 45 degrees",
            "enable supports in the slicer")
    return out


def print_mesh(mesh_path: Path, transform: np.ndarray | None, height_mm: float = 100.0,
               base: float = 0.02, min_island: float = 0.25, mode: str = "auto", min_wall: float = 0.8,
               stand: str = "auto", shell: float = 3.0, pitch: float | None = None,
               outward: bool | None = None, shell_side: str = "inward"):
    """A printable mesh for slicers, from the clean mesh (SfM frame): Z-up, in millimetres,
    one closed solid without self-intersections. Returns (trimesh, report).

    Pieces under ``min_island`` of the main piece's area are dropped (a print is one object);
    in solid mode holes are capped (lids); the mesh is turned upright with ``transform`` (SfM ->
    Y-up, as for the GLB) and then Z-up, the bottom ``base`` fraction flattened so it stands,
    scaled to ``height_mm`` and rebuilt by ``solidify`` (which also adds a stand if needed).
    """
    import trimesh

    mesh = o3d.io.read_triangle_mesh(str(mesh_path))
    mesh.remove_duplicated_vertices()
    mesh.remove_degenerate_triangles()
    clusters, _, areas = mesh.cluster_connected_triangles()
    areas = np.asarray(areas)
    if len(areas):
        mesh.remove_triangles_by_mask(areas[np.asarray(clusters)] < min_island * areas.max())
        mesh.remove_unreferenced_vertices()
    _make_manifold(mesh)
    if not mesh.has_triangles():
        raise RuntimeError(f"{mesh_path} has no printable surface")
    v0, f0 = np.asarray(mesh.vertices), np.asarray(mesh.triangles)
    v, f = close_holes(v0, f0)
    tri = v[f]
    area = 0.5 * np.linalg.norm(np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0]), axis=1)
    lid_share = float(area[len(f0):].sum() / area.sum()) if len(f) > len(f0) else 0.0
    chosen = mode
    if mode == "auto":  # a lid inventing a third of the surface is a guess, not the subject
        chosen = "shell" if lid_share > AUTO_SHELL_LIDS else "solid"
    if chosen == "shell":
        v, f = v0, f0
    if transform is not None:
        v = v @ transform[:3, :3].T + transform[:3, 3]
    v = v[:, [0, 2, 1]] * [1, -1, 1]  # Y-up (glTF) -> Z-up (slicers), front stays at -Y
    z0, z1 = v[:, 2].min(), v[:, 2].max()
    if z1 <= z0:
        raise RuntimeError("the print mesh is flat")
    if chosen == "solid":
        v[:, 2] = np.maximum(v[:, 2], z0 + base * (z1 - z0))
    v[:, 2] -= v[:, 2].min()
    v *= height_mm / v[:, 2].max()
    v[:, :2] -= (v[:, :2].min(0) + v[:, :2].max(0)) / 2  # centred on the build plate
    if pitch is None:  # ~330 voxels over the height, at most 25M voxels for wide subjects
        span = np.ptp(v, 0) + 2 * (max(min_wall, shell) + 1)
        pitch = max(0.15, height_mm / 330, float(np.prod(span) / 25e6) ** (1 / 3))
    vs, fs, info = solidify(v, f, pitch, min_wall, chosen, shell, stand, outward=outward, shell_side=shell_side)
    vs *= height_mm / vs[:, 2].max()  # thickening adds a fraction of a mm: keep the asked height
    vs[:, :2] -= (vs[:, :2].min(0) + vs[:, :2].max(0)) / 2
    out = trimesh.Trimesh(vs, fs, process=False)
    report = print_report(out, lid_share, info, min_wall, chosen, stand)
    if mode == "auto" and chosen == "shell":
        report.insert(0, {"level": "info", "code": "print-shell", "message": f"printed as a {shell:g} mm shell of "
                          f"what was scanned: closing it would have invented {lid_share:.0%} of the surface",
                          "hint": "print_mode solid to close it anyway (smooth lids over the unseen parts)"})
    return out, {"mode": chosen, "lid_share": round(lid_share, 3), **info, "issues": report}


def export_print_stl(mesh_path: Path, dst: Path, transform: np.ndarray | None, ws: Workspace | None = None,
                     **kw) -> dict:
    if ws is not None and kw.get("outward") is None:  # which side is the outside, from the cameras
        kw["outward"] = normals_outward(ws, mesh_path)
    out, rep = print_mesh(mesh_path, transform, **kw)
    tmp = dst.with_suffix(".stl.tmp")  # never leave half a file where a download could find it
    out.export(str(tmp), file_type="stl")
    tmp.replace(dst)
    info = {"print_faces": len(out.faces), "print_watertight": bool(out.is_watertight),
            "print_size_mm": out.extents.round(1).tolist(), "print_volume_cm3": round(float(out.volume) / 1000, 1),
            "print_issues": rep.pop("issues"), "print": rep}
    log.info("print STL: %s mm, %d faces, %s -> %s", "x".join(f"{e:.0f}" for e in out.extents),
             len(out.faces), "watertight" if out.is_watertight else "NOT watertight", dst)
    for i in info["print_issues"]:
        log.info("print %s: %s (%s)", i["code"], i["message"], i["hint"])
    return info


def write_3mf(mesh, dst: Path) -> Path:
    """A 3MF file (the slicers' zip of XML, with its unit: millimetres) from a trimesh, without lxml."""
    import zipfile

    v = "\n".join(f'<vertex x="{x:.4f}" y="{y:.4f}" z="{z:.4f}"/>' for x, y, z in np.asarray(mesh.vertices))
    t = "\n".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in np.asarray(mesh.faces))
    model = ('<?xml version="1.0" encoding="UTF-8"?>\n'
             '<model unit="millimeter" xml:lang="en-US" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">\n'
             f'<resources><object id="1" type="model"><mesh><vertices>\n{v}\n</vertices><triangles>\n{t}\n'
             '</triangles></mesh></object></resources>\n<build><item objectid="1"/></build>\n</model>\n')
    types = ('<?xml version="1.0" encoding="UTF-8"?>\n<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
             '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
             '<Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/></Types>\n')
    rels = ('<?xml version="1.0" encoding="UTF-8"?>\n<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Target="/3D/3dmodel.model" Id="rel0" '
            'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/></Relationships>\n')
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", types)
        z.writestr("_rels/.rels", rels)
        z.writestr("3D/3dmodel.model", model)
    return dst


def print_kwargs(opts: dict) -> dict:
    """print_* options (as the pipeline and UI name them) -> print_mesh arguments."""
    names = {"print_height": "height_mm", "print_base": "base", "print_mode": "mode", "print_stand": "stand",
             "print_min_wall": "min_wall", "print_shell": "shell", "print_shell_side": "shell_side"}
    return {names[k]: v for k, v in opts.items() if k in names and v is not None}


def texture_source(ws: Workspace) -> tuple[Path, str]:
    """The mesh texture projects the photos onto: the custom (edited) mesh from the editor when it
    is newer than the clean mesh, else the clean mesh. An older custom mesh was made from a clean
    mesh that has since been replaced, so it is left alone (and the UI says so)."""
    clean, custom = ws.mesh / "mesh_clean.ply", ws.mesh / "custom" / "mesh_clean.ply"
    if custom.exists() and (not clean.exists() or custom.stat().st_mtime >= clean.stat().st_mtime):
        return custom, "custom"
    return clean, "clean"


def _ground_for_workspace(ws: Workspace, recon: pycolmap.Reconstruction) -> tuple[np.ndarray, float] | None:
    masks = ws.masks if ws.has_masks() else None
    dense = ws.mvs / "scene_dense.ply"
    if dense.exists():
        pts = np.asarray(o3d.io.read_point_cloud(str(dense)).points)
        if len(pts) > 100_000:
            pts = pts[np.random.default_rng(0).choice(len(pts), 100_000, replace=False)]
        ground = fit_ground_plane(recon, masks, points=pts)
        if ground is not None:
            return ground
    return fit_ground_plane(recon, masks)


ORIENTATION = "orientation.json"


def user_orientation(ws: Workspace) -> np.ndarray | None:
    """The orientation set by hand in the UI (same meaning as gravity_alignment's result), while
    the cameras it was set against are current: re-running SfM changes the frame, so it lapses."""
    import json

    p, cams = ws.root / ORIENTATION, ws.sparse / "images.bin"
    if not p.exists() or not cams.exists():
        return None
    try:
        d = json.loads(p.read_text())
        T = np.array(d["transform"], float)
    except (ValueError, KeyError, TypeError):
        return None
    return T if T.shape == (4, 4) and d.get("cameras") == cams.stat().st_mtime_ns else None


def workspace_alignment(ws: Workspace, mesh_path: Path, ground=None, up: str = "auto") -> np.ndarray | None:
    """Transform for the exported GLB. up: auto (the orientation set in the UI, else plane, else
    cameras), plane, camera or none."""
    from .sfm import load_reconstruction

    if up == "none":
        return None
    if up == "auto" and (T := user_orientation(ws)) is not None:
        return T
    try:
        recon = load_reconstruction(ws)
    except RuntimeError:
        return None
    verts = np.asarray(o3d.io.read_triangle_mesh(str(mesh_path)).vertices)
    if not len(verts):
        return None
    if up == "camera":
        ground = None
    elif ground is None:
        ground = _ground_for_workspace(ws, recon)
        if ground is None and up == "plane":
            log.warning("up=plane but no support plane was found: using the camera up-vector")
    return gravity_alignment(recon, verts, ground)


def clean_workspace_mesh(ws: Workspace, mesh_path: Path, use_masks: bool = True,
                         remove_ground: bool = True, **kw) -> dict:
    """mesh_clean.ply, in the SfM frame (OpenMVS texturing needs it). Exports (GLB, the 3D print,
    ...) are made from it when asked (exports.py)."""
    from .sfm import load_reconstruction

    try:
        recon = load_reconstruction(ws)
    except RuntimeError:
        recon = None
    masks = None
    if use_masks and ws.has_masks() and recon is not None:
        masks = ws.masks
    elif use_masks:
        log.warning("no masks in %s: keeping only the main connected piece (the most faces)", ws.masks)
    ground = _ground_for_workspace(ws, recon) if recon is not None else None
    if ground is not None:
        log.info("support plane found (normal %s)", np.round(ground[0], 3).tolist())
    out = ws.mesh / "mesh_clean.ply"
    info = clean_mesh(mesh_path, out, recon if masks else None, masks,
                      ground=ground if remove_ground else None, **kw)
    info["support_plane"] = ground is not None
    info["masks_used"] = masks is not None
    for old in ("mesh_clean.glb", "print.stl"):  # written by earlier versions: exports are made on request now
        (ws.mesh / old).unlink(missing_ok=True)
    return info
