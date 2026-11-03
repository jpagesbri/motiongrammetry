"""Mesh checks (what breaks or spoils texturing) and the editor's Sanitize mesh fixes."""

import json
import struct

import numpy as np
import open3d as o3d
import pytest
import trimesh

from motiongrammetry import meshfix
from motiongrammetry.clean import boundary_loops
from motiongrammetry.meshops import decode_mesh, encode_mesh, run


def _sphere(res=20):
    m = o3d.geometry.TriangleMesh.create_sphere(1.0, res)
    v, f = np.asarray(m.vertices), np.asarray(m.triangles).astype(np.int64)
    return v, f, np.full((len(v), 3), 150, np.uint8)


@pytest.fixture(autouse=True)
def quick_estimates(monkeypatch):
    """Keep the one-off benchmark small and per test."""
    monkeypatch.setattr(meshfix, "BENCH_FACES", 4000)
    monkeypatch.setattr(meshfix, "_rates", {})


def test_the_check_finds_each_problem_of_a_broken_mesh():
    v, f, c = meshfix.test_mesh(4000)
    r = meshfix.inspect(v, f, intersections=True)
    assert r["pillow_faces"] == 1 and r["duplicate_faces"] == 1 and r["degenerate_faces"] == 1
    assert r["bowtie_vertices"] == 1
    assert r["unreferenced_vertices"] >= 1
    assert r["holes"] >= 1 and r["pieces"] >= 3  # sphere (with the cone), tetrahedron, the stray face
    assert r["intersecting_pairs"] >= 1
    assert r["hole_sizes"] == sorted(r["hole_sizes"], reverse=True) and r["piece_sizes"][0] == 1.0
    codes = {p["code"] for p in meshfix.problems(r)}
    assert {"pillows", "bowtie", "degenerate", "duplicates"} <= codes
    for p in meshfix.problems(r):
        assert p["count"] > 0 and 0 < p["share"] < 1 and p["fix"] and p["effect"] and p["fixes"]
        assert set(p["fixes"]) <= set(meshfix.FIXES)


def test_a_clean_closed_mesh_has_no_problems():
    v, f, _ = _sphere()
    r = meshfix.inspect(v, f, intersections=True)
    assert meshfix.problems(r) == []
    assert r["holes"] == 0 and r["pieces"] == 1 and r["intersecting_pairs"] == 0 and r["flipped_faces"] == 0


def test_zero_area_faces_count_as_degenerate():
    v, f, _ = _sphere()
    v = np.concatenate([v, [[3, 0, 0], [4, 0, 0], [5, 0, 0]]])  # three points on a line
    n = len(v)
    f = np.concatenate([f, [[n - 3, n - 2, n - 1]]])
    assert meshfix.degenerate_faces(v, f).sum() == 1


def test_a_piece_that_is_only_a_pillow_flattens_to_no_area():
    v, f, _ = _sphere()
    n = len(v)
    v = np.concatenate([v, [[3, 0, 0], [3, 1, 0], [3, 0, 1]]])
    f = np.concatenate([f, [[n, n + 1, n + 2], [n + 2, n + 1, n]]])
    r = meshfix.inspect(v, f)
    assert r["flat_pieces"] == 1 and r["flat_piece_faces"] == 2 and r["pillow_faces"] == 1
    assert "flat-pieces" in {p["code"] for p in meshfix.problems(r)}


def test_sanitize_makes_the_broken_mesh_clean():
    """#29's acceptance: a mesh with a pillow, a hole and a non-manifold vertex comes out with no
    pillows, manifold and its holes closed."""
    v, f, c = meshfix.test_mesh(4000)
    V, F, C, info = meshfix.sanitize(v, f, c, meshfix.FIXES, hole_size=0.5)
    assert [s["fix"] for s in info["steps"]] == list(meshfix.FIXES)
    assert all(s["seconds"] >= 0 for s in info["steps"]) and not info["unchanged"]
    a = info["after"]
    for k in ("pillow_faces", "duplicate_faces", "degenerate_faces", "nonmanifold_edges", "bowtie_vertices",
              "holes", "flipped_faces", "unreferenced_vertices", "flat_pieces"):
        assert a[k] == 0, k
    assert a["pieces"] == 1
    t = trimesh.Trimesh(V, F, process=False)
    assert t.is_watertight and t.is_winding_consistent and t.volume > 0
    assert C.shape == (len(V), 3) and C.dtype == np.uint8
    steps = {s["fix"]: s for s in info["steps"]}
    assert steps["duplicates"]["pillows"] == 1 and steps["manifold"]["bowtie_vertices"] >= 1
    assert steps["holes"]["closed_holes"] >= 1 and steps["pieces"]["removed_pieces"] >= 2


def test_each_fix_on_its_own():
    v, f, c = meshfix.test_mesh(4000)
    # duplicates: only the broken faces go
    _, F, _, i = meshfix.STEPS["duplicates"](v, f, c)
    assert (i["degenerate"], i["duplicates"], i["pillows"]) == (1, 1, 1) and len(F) == len(f) - 3
    # manifold: no bow-ties or non-manifold edges left
    V, F, C, i = meshfix.STEPS["manifold"](v, F, c)
    assert i["bowtie_vertices"] >= 1
    r = meshfix.inspect(V, F)
    assert r["bowtie_vertices"] == 0 and r["nonmanifold_edges"] == 0
    # pieces: the threshold decides
    _, F2, _, i = meshfix.STEPS["pieces"](V, F, C, piece_size=1e-9)
    assert i["removed_pieces"] == 0 and len(F2) == len(F)
    _, F2, _, i = meshfix.STEPS["pieces"](V, F, C, piece_size=0.05)
    assert i["removed_pieces"] >= 2 and meshfix.inspect(V, F2)["pieces"] == 1
    # unreferenced vertices
    V3, F3, C3, i = meshfix.STEPS["unreferenced"](V, F2, C)
    assert i["removed_vertices"] > 0 and meshfix.inspect(V3, F3)["unreferenced_vertices"] == 0
    assert len(C3) == len(V3)


def test_holes_are_closed_up_to_the_size_given():
    v, f, c = _sphere(30)
    cen = v[f].mean(1)
    small = np.linalg.norm(cen - [1, 0, 0], axis=1) < 0.15
    big = cen[:, 2] < -0.6
    f = f[~small & ~big]
    r = meshfix.inspect(v, f)
    assert r["holes"] == 2
    _, F, _, i = meshfix.STEPS["holes"](v, f, c, hole_size=r["hole_sizes"][1] * 1.01)
    assert (i["closed_holes"], i["left_open"]) == (1, 1) and len(boundary_loops(F)) == 1
    _, F, _, i = meshfix.STEPS["holes"](v, f, c, hole_size=r["hole_sizes"][0] * 1.01)
    assert i["closed_holes"] == 2 and len(boundary_loops(F)) == 0


def _flap_over_hole():
    """A sphere with a face missing and, on one of its edges, a flap folded across the gap: a face
    whose two other edges are open, so the hole's rim runs around it (as in a scanned face)."""
    v, f, c = _sphere(30)
    p, q, r = f[100]
    w = len(v)
    mid = (v[p] + v[q]) / 2
    tip = mid - (v[r] - mid)  # folded back flat over the face on the other side of pq
    v = np.concatenate([v, [tip]])
    f = np.concatenate([np.delete(f, 100, 0), [[p, q, w]]])
    return v, f, np.full((len(v), 3), 150, np.uint8)


def test_a_flap_across_a_hole_goes_and_the_hole_closes_in_one_run():
    v, f, c = _flap_over_hole()
    r = meshfix.inspect(v, f)
    assert r["holes"] == 1 and r["pillow_faces"] == 0
    V, F, C, info = meshfix.sanitize(v, f, c, list(meshfix.FIXES))
    a = info["after"]
    assert (a["holes"], a["open_edges"], a["pillow_faces"], a["nonmanifold_edges"], a["flipped_faces"]) == (0, 0, 0, 0, 0)
    holes = next(s for s in info["steps"] if s["fix"] == "holes")
    assert holes["removed_flaps"] == 1 and holes["added_faces"] >= 1
    # and running it again finds nothing more to do
    assert meshfix.sanitize(V, F, C, list(meshfix.FIXES))[3]["unchanged"]


def test_orientation_is_made_consistent_and_outward():
    v, f, c = _sphere()
    g = f.copy()
    g[::5] = g[::5, ::-1]  # some faces wound against their neighbours
    r = meshfix.inspect(v, g)
    assert r["flipped_faces"] > 0 and "flipped" in {p["code"] for p in meshfix.problems(r)}
    _, F, _, i = meshfix.STEPS["orient"](v, g, c)
    assert i["flipped_faces"] > 0
    t = trimesh.Trimesh(v, F, process=False)
    assert t.is_winding_consistent and t.volume > 0
    # all inside out: turned back as a whole
    _, F, _, i = meshfix.STEPS["orient"](v, f[:, ::-1].copy(), c)
    assert i["flipped_faces"] == len(f) and trimesh.Trimesh(v, F, process=False).volume > 0


def test_self_intersections_are_reported_not_changed():
    v, f, c = _sphere()
    n = len(v)
    v = np.concatenate([v, [[0, 0, 0], [0, 0.1, 2], [0.1, 0, 2]]])  # a sliver from the centre out through the top
    f = np.concatenate([f, [[n, n + 1, n + 2]]])
    V, F, _, i = meshfix.STEPS["intersections"](v, f, c)
    assert i["intersecting_pairs"] >= 1 and i["intersecting_faces"] >= 2
    assert np.array_equal(F, f) and V is v
    assert len(meshfix.self_intersections(*_sphere()[:2])) == 0


def test_sanitize_of_a_clean_mesh_changes_nothing():
    v, f, c = _sphere()
    V, F, C, info = meshfix.sanitize(v, f, c, ["duplicates", "manifold", "pieces", "unreferenced", "intersections"])
    assert info["unchanged"] and np.array_equal(F, f)
    with pytest.raises(ValueError, match="unknown fix"):
        meshfix.sanitize(v, f, c, ["polish"])


def test_estimates_scale_with_the_faces_and_learn_from_runs():
    e = meshfix.estimates(1_000_000)
    assert set(e["seconds"]) == {*meshfix.FIXES, "check"}
    assert all(s >= 0 for s in e["seconds"].values()) and e["seconds"]["holes"] > 0
    assert e["machine"]["cpu"] and e["machine"]["threads"] >= 1 and "benchmark" in e["basis"]
    half = meshfix.estimates(500_000)["seconds"]
    assert half["manifold"] == pytest.approx(e["seconds"]["manifold"] / 2, abs=0.02)
    meshfix._learn("pieces", 10_000, 2.0)  # a run on this machine replaces the benchmark's rate
    assert meshfix.estimates(1_000_000)["seconds"]["pieces"] == pytest.approx(200, rel=1e-6)


def test_editor_requests_check_and_sanitize():
    v, f, c = meshfix.test_mesh(4000)
    params = json.dumps({"fixes": list(meshfix.FIXES), "hole_size": 0.5, "piece_size": 0.05}).encode()
    body = struct.pack("<I", len(params)) + params + encode_mesh(v, f, c)
    r = meshfix.inspect_request(body)
    assert r["report"]["pillow_faces"] == 1 and r["problems"] and r["seconds"]["holes"] >= 0
    data, info = run("sanitize", body)
    V, F, C = decode_mesh(data)
    assert len(F) == info["faces_out"] and info["after"]["holes"] == 0 and info["after"]["pillow_faces"] == 0
    assert trimesh.Trimesh(V, F, process=False).is_watertight


def test_of_a_pillow_the_face_kept_agrees_with_its_neighbours():
    """The editor's face order is arbitrary: whichever face of a pillow comes first, the one kept
    is wound like the surface around it."""
    v, f, c = _sphere()
    g = np.concatenate([f[:5, ::-1], f])  # the wrongly wound copies first
    _, F, _, i = meshfix.STEPS["duplicates"](v, g, c)
    assert i["pillows"] == 5 and len(F) == len(f)
    t = trimesh.Trimesh(v, F, process=False)
    assert t.is_winding_consistent and t.volume > 0
    r = meshfix.inspect(v, g)
    assert r["flipped_faces"] == 0 and r["holes"] == 0 and r["pillow_faces"] == 5  # checked as the fix leaves it
    g[7] = g[7, ::-1]
    r = meshfix.inspect(v, g)
    assert r["flipped_faces"] == 1 and r["holes"] == 0  # a flipped face is not a hole


def test_self_intersections_come_as_spots_the_editor_can_show():
    v, f, c = meshfix.test_mesh(4000)
    r = meshfix.inspect(v, f, intersections=True)
    spots, tris = r["intersection_spots"], r["intersecting_tris"]
    assert r["intersecting_faces"] > 0 and len(tris) == 3 * r["intersecting_faces"]
    assert sum(s["faces"] for s in spots) == r["intersecting_faces"]
    # the face put through the surface near (0, -1, 0), and the sphere faces it crosses: one spot
    assert len(spots) == 1 and np.linalg.norm(np.array(spots[0]["centre"]) - [0, -1, 0]) < 0.2
    assert 0 < spots[0]["radius"] < 0.5
    assert set(tris) <= set(range(len(v)))  # the editor's own vertex ids
    # two separate crossings are two spots, the larger first
    k = len(v)
    v2 = np.concatenate([v, [[0.0, 1.2, -0.02], [0.02, 0.8, 0.02], [-0.02, 0.8, 0.02]]])
    f2 = np.concatenate([f, [[k, k + 1, k + 2]]])
    s2 = meshfix.inspect(v2, f2, intersections=True)["intersection_spots"]
    assert len(s2) == 2 and s2[0]["faces"] >= s2[1]["faces"]
    assert "intersection_spots" not in meshfix.summary(r)


# ---- the sorting stand-ins for np.unique / np.isin (NumPy 2.3+ hashes, ~20x slower) ----------
def _random_int_arrays(seed=0, rounds=300):
    """Pairs (x, keys) of integer arrays of every kind the helpers must take."""
    rng = np.random.default_rng(seed)
    dtypes = (np.int8, np.int16, np.int32, np.int64, np.uint8, np.uint16, np.uint32, np.uint64)
    for r in range(rounds):
        dt = dtypes[r % len(dtypes)]
        kd = dtypes[rng.integers(len(dtypes))] if r % 3 == 0 else dt  # a third: keys of another int type
        info, kinfo = np.iinfo(dt), np.iinfo(kd)
        # small ranges give many duplicates; the full range reaches the type's limits
        span = int(rng.integers(1, 20)) if r % 2 else None
        def draw(d, inf, size):
            if span is None:
                return rng.integers(inf.min, inf.max, size, dtype=d, endpoint=True)
            lo = max(inf.min, -span)
            return rng.integers(lo, lo + 2 * span, size, dtype=d)
        shape = [(0,), (1,), (int(rng.integers(2, 300)),), (int(rng.integers(1, 20)), 3), (4, 0)][r % 5]
        nk = [0, 1, int(rng.integers(2, 50)), int(rng.integers(50, 2000))][(r // 5) % 4]
        x = draw(dt, info, shape)
        keys = draw(kd, kinfo, (nk,))
        if r % 7 == 0 and len(x.ravel()) and nk:  # make sure some hit
            keys[: min(nk, x.size)] = x.ravel()[: min(nk, x.size)].astype(kd, casting="unsafe")
        if r % 11 == 0:
            keys = keys.reshape(-1, 1)  # keys as a column: np.isin flattens them
        yield x, keys


def test_unique_sorted_matches_np_unique_on_random_integer_arrays():
    for x, keys in _random_int_arrays(1):
        for a in (x, keys):
            want = np.unique(a)
            got = meshfix.unique_sorted(a)
            assert got.dtype == want.dtype and got.shape == want.shape
            np.testing.assert_array_equal(got, want)
    # near the int64 limits, and negative
    big = np.array([2**63 - 1, -2**63, 0, -1, 2**63 - 1, -2**63, 5], np.int64)
    np.testing.assert_array_equal(meshfix.unique_sorted(big), np.unique(big))
    np.testing.assert_array_equal(meshfix.unique_sorted(big.reshape(7, 1)), np.unique(big))


def test_isin_sorted_matches_np_isin_on_random_integer_arrays():
    n = 0
    for x, keys in _random_int_arrays(2):
        want = np.isin(x, keys)
        got = meshfix.isin_sorted(x, keys)
        assert got.dtype == bool and got.shape == x.shape, (x.dtype, keys.dtype, x.shape)
        np.testing.assert_array_equal(got, want, err_msg=f"{x.dtype} in {keys.dtype}")
        n += int(want.sum())
    assert n > 100, "the random cases do hit"
    # empty keys: all False, x's shape
    assert meshfix.isin_sorted(np.arange(6).reshape(2, 3), np.array([], np.int64)).shape == (2, 3)
    assert not meshfix.isin_sorted(np.arange(6), np.array([], np.int64)).any()
    # unsorted keys with duplicates, negative values, values past both ends of the keys
    x = np.array([[-9, -3, 0], [4, 7, 100]], np.int64)
    keys = np.array([7, -3, 7, 4, -3], np.int32)
    np.testing.assert_array_equal(meshfix.isin_sorted(x, keys), np.isin(x, keys))
    # near the int64 limits (edge keys a * n + b of a large mesh still fit)
    big = np.array([2**63 - 1, -2**63, 2**62 + 3, -1], np.int64)
    probe = np.array([2**63 - 1, 2**63 - 2, -2**63, -2**63 + 1, 2**62 + 3, 2**62 + 2, -1, 0], np.int64)
    np.testing.assert_array_equal(meshfix.isin_sorted(probe, big), np.isin(probe, big))
    # uint64 with int64 has no common integer type: still np.isin's answer
    u = np.array([2**64 - 1, 3, 0], np.uint64)
    np.testing.assert_array_equal(meshfix.isin_sorted(u, np.array([-1, 3], np.int64)), np.isin(u, np.array([-1, 3])))
    # a scalar and a list, like np.isin
    assert meshfix.isin_sorted(np.int64(4), keys).shape == () and bool(meshfix.isin_sorted(np.int64(4), keys))
    np.testing.assert_array_equal(meshfix.isin_sorted([1, 4, 7], [7, 1]), [True, False, True])
