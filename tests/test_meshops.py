"""Editor topology edits: capping, plane cuts and voxel remeshing with fill spheres."""

import json
import struct

import numpy as np
import pytest
import open3d as o3d
import trimesh

from motiongrametry.clean import boundary_loops
from motiongrametry.meshops import (cap, decode_mesh, encode_mesh, keep_pieces, local_merge, plane_cut, restore, run,
                                    voxel_remesh)


def _mesh(m):
    v, f = np.asarray(m.vertices), np.asarray(m.triangles).astype(np.int64)
    return v, f, np.full((len(v), 3), 200, np.uint8)


def _closed(v, f):
    t = trimesh.Trimesh(v, f, process=True)
    return t.is_watertight and t.is_winding_consistent and t.volume > 0, t


def test_fill_spheres_close_a_doughnut_into_one_solid():
    torus = o3d.geometry.TriangleMesh.create_torus(torus_radius=1.0, tube_radius=0.4, radial_resolution=40,
                                                   tubular_resolution=20)
    v, f, c = _mesh(torus)
    # spheres across the hole, in the torus' plane (z = 0), as the fill brush would place them
    spheres = [[x, y, 0.0, 0.45] for x in np.linspace(-0.6, 0.6, 5) for y in np.linspace(-0.6, 0.6, 5)]
    V, F, C, info = voxel_remesh(v, f, c, spheres, voxel=0.01)
    ok, t = _closed(V, F)
    assert ok and len(t.split(only_watertight=False)) == 1
    assert t.euler_number == 2, "the hole should be gone: one solid of genus 0"
    scene = o3d.t.geometry.RaycastingScene()
    scene.add_triangles(o3d.core.Tensor(V.astype(np.float32)), o3d.core.Tensor(F.astype(np.uint32)))
    inside = scene.compute_occupancy(o3d.core.Tensor(np.zeros((1, 3), np.float32))).numpy()[0]
    assert inside == 1, "the centre of the old hole is now inside"
    assert C.shape == (len(V), 3)

    # without spheres it stays a doughnut (genus 1), just remeshed
    V, F, _, _ = voxel_remesh(v, f, c, None, voxel=0.01)
    ok, t = _closed(V, F)
    assert ok and t.euler_number == 0


def test_remesh_merges_overlapping_shells_and_drops_inner_walls():
    a = o3d.geometry.TriangleMesh.create_sphere(0.5, 20)
    b = o3d.geometry.TriangleMesh.create_sphere(0.5, 20).translate((0.6, 0, 0))
    v, f, c = _mesh(a + b)
    V, F, _, _ = voxel_remesh(v, f, c, voxel=0.01)
    ok, t = _closed(V, F)
    assert ok and len(t.split(only_watertight=False)) == 1
    # no surface left inside the overlap (inner walls of either sphere)
    inner = np.linalg.norm(V - [0.3, 0, 0], axis=1) < 0.25
    assert not inner.any()


def test_plane_cut_splits_on_the_plane_and_caps_flat():
    v, f, c = _mesh(o3d.geometry.TriangleMesh.create_sphere(1.0, 30))
    V, F, C, info = plane_cut(v, f, c, point=(0, 0, 0.3), normal=(0, 0, 1))
    ok, t = _closed(V, F)
    assert ok and info["capped"] == 1
    assert V[:, 2].max() <= 0.3 + 1e-9 and np.isclose(V[:, 2].max(), 0.3)
    cap_faces = np.isclose(V[F][:, :, 2], 0.3).all(1)
    assert cap_faces.any()
    n = t.face_normals[np.isclose(t.triangles[:, :, 2], 0.3).all(1)]
    assert (n[:, 2] > 0.99).all(), "the cap is flat and faces out of the cut"
    # open cut when asked
    V, F, _, info = plane_cut(v, f, c, (0, 0, 0.3), (0, 0, 1), close=False)
    assert info["capped"] == 0 and len(boundary_loops(F)) == 1


def test_cap_closes_only_the_openings_touching_the_seeds():
    v, f, c = _mesh(o3d.geometry.TriangleMesh.create_sphere(1.0, 30))
    centres = v[f].mean(1)
    top, side = centres[:, 2] > 0.9, centres[:, 0] > 0.9  # two holes
    cut = f[top]
    f = f[~(top | side)]
    V, F, C, info = cap(v, f, c, seeds=np.unique(cut))
    assert info["capped"] == 1 and len(boundary_loops(F)) == 1  # the side hole stays open
    V, F, C, info = cap(v, f, c)
    ok, _ = _closed(V, F)
    assert info["capped"] == 2 and ok


def test_run_round_trips_the_wire_format():
    v, f, c = _mesh(o3d.geometry.TriangleMesh.create_sphere(1.0, 10))
    params = json.dumps({"point": [0, 0, 0], "normal": [0, 0, 1]}).encode()
    data, info = run("plane", struct.pack("<I", len(params)) + params + encode_mesh(v, f, c))
    V, F, C = decode_mesh(data)
    assert info["capped"] == 1 and (V[:, 2] <= 1e-6).all() and C.dtype == np.uint8


def test_keep_pieces_drops_what_a_cut_separated():
    body = o3d.geometry.TriangleMesh.create_sphere(1.0, 30)
    limb = o3d.geometry.TriangleMesh.create_sphere(0.2, 10).translate((2, 0, 0))
    v, f, c = _mesh(body + limb)
    V, F, _, info = keep_pieces(v, f, c)
    assert info["removed_pieces"] == 1 and len(V) == len(body.vertices)


def test_restore_brings_back_carved_raw_triangles_seamlessly():
    raw = o3d.geometry.TriangleMesh.create_sphere(1.0, 30)
    rv, rf = np.asarray(raw.vertices), np.asarray(raw.triangles).astype(np.int64)
    # "clean": the raw mesh minus a carved patch on top (same vertex positions, as clean leaves them)
    keep = ~(rv[rf].mean(1)[:, 2] > 0.8)
    v, f, c = rv.copy(), rf[keep], np.full((len(rv), 3), 200, np.uint8)
    V, F, C, info = restore(v, f, c, rv, rf, [[0, 0, 1.0, 0.7]])
    assert info["restored_faces"] == (~keep).sum()
    assert len(boundary_loops(F)) == 0, "restored seamlessly: no open edges left"
    ok, _ = _closed(V, F)
    assert ok and C.shape == (len(V), 3)
    # nothing to restore where the mesh is already complete
    _, _, _, info = restore(V, F, C, rv, rf, [[0, 0, 1.0, 0.5]])
    assert info["restored_faces"] == 0


def _holed_sphere(open_bottom=False):
    import open3d as o3d

    m = o3d.geometry.TriangleMesh.create_sphere(1.0, 60)
    v, f = np.asarray(m.vertices), np.asarray(m.triangles).astype(np.int64)
    cen = v[f].mean(1)
    keep = np.linalg.norm(cen - [0, 0, 1], axis=1) > 0.3  # a hole at the top
    if open_bottom:
        keep &= cen[:, 2] > -0.2  # and the scan's own open edge below, like a face scan
    return v, f[keep], np.full((len(v), 3), 150, np.uint8)


def test_local_merge_fills_a_hole_and_keeps_the_rest_exactly():
    import trimesh

    v, f, c = _holed_sphere()
    V, F, C, info = local_merge(v, f, c, [[0, 0, 0.95, 0.35]])
    tm = trimesh.Trimesh(V, F, process=False)
    assert tm.is_watertight and tm.is_winding_consistent and info["local"] and info["seams"] == 1
    far = v[np.linalg.norm(v - [0, 0, 0.95], axis=1) > 0.8]  # well outside the rebuilt region
    kept = {tuple(p) for p in V}
    assert all(tuple(p) in kept for p in far)  # bit-identical: nothing outside was touched
    assert info["rebuilt_faces"] < 0.5 * len(F)


def test_local_merge_closes_an_opening_it_only_partly_covers_but_not_the_scan_edge():
    from motiongrametry.clean import boundary_loops

    v, f, c = _holed_sphere(open_bottom=True)
    assert len(boundary_loops(f)) == 2
    V, F, C, info = local_merge(v, f, c, [[0, 0.25, 0.95, 0.15]])  # over one side of the hole
    assert info["openings_closed"] == 1 and len(boundary_loops(F)) == 1  # only the open edge is left
    with pytest.raises(ValueError, match="large opening"):
        local_merge(v, f, c, [[1, 0, -0.18, 0.1]])  # on the scan's open edge


def test_editor_request_for_a_local_merge():
    v, f, c = _holed_sphere()
    params = json.dumps({"spheres": [[0, 0, 0.95, 0.35]]}).encode()
    data, info = run("merge_local", struct.pack("<I", len(params)) + params + encode_mesh(v, f, c))
    V, F, C = decode_mesh(data)
    assert info["local"] and len(F) == info["faces"] and trimesh.Trimesh(V, F, process=False).is_watertight
