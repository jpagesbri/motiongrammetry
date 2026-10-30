"""Silhouette carving, loop pairs and mask undistortion on synthetic cameras."""

from pathlib import Path

import cv2
import numpy as np
import open3d as o3d
import pycolmap

from motiongrametry.clean import clean_mesh, restore_holes
from motiongrametry.sfm import _loop_pairs
from motiongrametry.splat import _undistorted_masks
from motiongrametry.workspace import Workspace

W, H, F = 320, 240, 300.0


def _look_at(eye, target=(0, 0, 0.5)):
    z = np.asarray(target, float) - eye
    z /= np.linalg.norm(z)
    x = np.cross(z, [0, 0, 1.0])
    x /= np.linalg.norm(x)
    y = np.cross(z, x)
    R = np.stack([x, y, z])  # rows: camera axes in world -> cam_from_world rotation
    return R, -R @ eye


def _orbit_reconstruction(n=12, model="PINHOLE", params=None):
    rec = pycolmap.Reconstruction()
    cam = pycolmap.Camera.create_from_model_name(1, model, F, W, H)
    if params is not None:
        cam.params = params
    rec.add_camera_with_trivial_rig(cam)
    for i in range(n):
        a = 2 * np.pi * i / n
        R, t = _look_at(np.array([4 * np.cos(a), 4 * np.sin(a), 1.5]))
        img = pycolmap.Image(name=f"f{i:02d}.jpg", camera_id=1, image_id=i + 1)
        rec.add_image_with_trivial_frame(img, pycolmap.Rigid3d(pycolmap.Rotation3d(R), t))
    return rec


def _sphere_masks(rec, masks_dir: Path, center=(0, 0, 0.5), radius=0.5):
    masks_dir.mkdir(parents=True, exist_ok=True)
    pts = np.asarray(o3d.geometry.TriangleMesh.create_sphere(radius, 40).translate(center).vertices)
    for image in rec.images.values():
        T = image.cam_from_world().matrix()
        uv = image.camera.img_from_cam(pts @ T[:, :3].T + T[:, 3])
        hull = cv2.convexHull(uv.astype(np.float32)).astype(np.int32)
        mask = np.zeros((H, W), np.uint8)
        cv2.fillConvexPoly(mask, hull, 255)
        cv2.imwrite(str(masks_dir / f"{image.name}.png"), cv2.dilate(mask, np.ones((5, 5), np.uint8)))


def test_silhouette_carving_removes_floor_and_floaters(tmp_path):
    rec = _orbit_reconstruction()
    _sphere_masks(rec, tmp_path / "masks")
    sphere = o3d.geometry.TriangleMesh.create_sphere(0.5, 30).translate((0, 0, 0.5))
    floor = o3d.geometry.TriangleMesh.create_box(6, 6, 0.01).translate((-3, -3, -0.01))
    floor = floor.subdivide_midpoint(6)
    floater = o3d.geometry.TriangleMesh.create_sphere(0.05, 8).translate((1.5, 1.5, 1.0))
    o3d.io.write_triangle_mesh(str(tmp_path / "raw.ply"), sphere + floor + floater)

    info = clean_mesh(tmp_path / "raw.ply", tmp_path / "clean.ply", rec, tmp_path / "masks")
    out = o3d.io.read_triangle_mesh(str(tmp_path / "clean.ply"))
    v = np.asarray(out.vertices)
    dist = np.linalg.norm(v - [0, 0, 0.5], axis=1)
    assert info["carved_vertices"] > 0
    assert np.all(dist < 0.6), "background geometry survived carving"
    assert len(out.triangles) >= 0.9 * len(sphere.triangles)


def test_restore_holes_only_puts_back_small_enclosed_disputed_patches():
    sphere = o3d.geometry.TriangleMesh.create_sphere(0.5, 30)
    floor = o3d.geometry.TriangleMesh.create_box(2, 2, 0.01).translate((-1, -1, -1)).subdivide_midpoint(3)
    mesh = sphere + floor
    v, t = np.asarray(mesh.vertices), np.asarray(mesh.triangles)
    on_sphere = np.arange(len(v)) < len(sphere.vertices)
    cap = on_sphere & (v[:, 2] > 0.48)          # a small patch on top of the sphere
    half = on_sphere & (v[:, 0] > 0.0)          # half of it: far too big to be a hole
    carve = cap | ~on_sphere                    # the floor is carved too (not enclosed)
    bg = np.full(len(v), 0.3)

    out, n = restore_holes(t, v, carve, bg, max_area=0.03)
    assert n == 1 and not out[cap].any() and out[~on_sphere].all()
    out, n = restore_holes(t, v, carve, np.full(len(v), 0.9), max_area=0.03)
    assert n == 0, "patches the masks agree are background must stay carved"
    out, n = restore_holes(t, v, half, bg, max_area=0.03)
    assert n == 0
    out, n = restore_holes(t, v, carve, bg, max_area=0.0)
    assert n == 0 and (out == carve).all()


def test_carving_holes_from_bad_masks_are_filled_from_the_raw_mesh(tmp_path):
    rec = _orbit_reconstruction()
    _sphere_masks(rec, tmp_path / "masks")
    p = np.array([0.5, 0.0, 0.5])               # sphere surface facing camera 0
    for image in list(rec.images.values())[:3]:  # three bad masks miss a blob there
        T = image.cam_from_world().matrix()
        u, w = image.camera.img_from_cam((T[:, :3] @ p + T[:, 3])[None])[0]
        path = tmp_path / "masks" / f"{image.name}.png"
        m = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
        cv2.circle(m, (int(u), int(w)), 6, 0, -1)
        cv2.imwrite(str(path), m)
    sphere = o3d.geometry.TriangleMesh.create_sphere(0.5, 30).translate((0, 0, 0.5))
    o3d.io.write_triangle_mesh(str(tmp_path / "raw.ply"), sphere)

    holed = clean_mesh(tmp_path / "raw.ply", tmp_path / "holed.ply", rec, tmp_path / "masks", fill_holes=0)
    filled = clean_mesh(tmp_path / "raw.ply", tmp_path / "filled.ply", rec, tmp_path / "masks")
    assert holed["carved_vertices"] > 0, "the bad masks should punch a hole"
    assert filled["holes_filled"] >= 1 and filled["carved_vertices"] == 0
    assert filled["faces_out"] == len(sphere.triangles)


def test_close_holes_caps_every_opening():
    import trimesh

    from motiongrametry.clean import boundary_loops, close_holes

    sphere = o3d.geometry.TriangleMesh.create_sphere(0.5, 30)
    v, t = np.asarray(sphere.vertices), np.asarray(sphere.triangles)
    top, side = v[t].mean(1)[:, 2] > 0.3, v[t].mean(1)[:, 0] > 0.45  # a big and a small hole
    t = t[~(top | side)]
    assert len(boundary_loops(t)) == 2
    V, F = close_holes(v, t)
    mesh = trimesh.Trimesh(V, F, process=False)
    assert mesh.is_watertight and mesh.is_winding_consistent
    assert boundary_loops(F) == []
    assert np.linalg.norm(V, axis=1).max() < 0.5 + 1e-9, "caps should stay inside the opening, not balloon"


def test_print_mesh_is_z_up_sized_and_flat_based(tmp_path):
    from motiongrametry.clean import print_mesh

    # A tall cone along SfM +z with an open base, and a transform taking +z to glTF +Y.
    cone = o3d.geometry.TriangleMesh.create_cone(0.5, 2.0, 40)
    v, t = np.asarray(cone.vertices), np.asarray(cone.triangles)
    t = t[~(v[t][:, :, 2] < 1e-9).all(1)]  # drop the base disc: an open bottom
    cone = o3d.geometry.TriangleMesh(o3d.utility.Vector3dVector(v), o3d.utility.Vector3iVector(t))
    o3d.io.write_triangle_mesh(str(tmp_path / "c.ply"), cone)
    T = np.eye(4)
    T[:3, :3] = [[1, 0, 0], [0, 0, 1], [0, -1, 0]]  # z -> y
    out, report = print_mesh(tmp_path / "c.ply", T, height_mm=80, base=0.05)
    assert out.is_watertight and out.volume > 0
    assert abs(out.extents[2] - 80) < 1e-6 and abs(out.bounds[0, 2]) < 1e-9
    assert out.extents[2] > 1.8 * out.extents[0], "the cone should stand up along Z"
    assert np.abs(out.bounds[:, :2].sum(0)).max() < 1e-6  # centred on the plate
    bottom = out.vertices[:, 2] < 1e-9
    assert np.ptp(out.vertices[bottom, 0]) > 0.5 * out.extents[0], "a flat base to stand on"


def test_gravity_alignment_maps_world_up_to_gltf_y():
    from motiongrametry.clean import gravity_alignment

    rec = _orbit_reconstruction()
    verts = np.asarray(o3d.geometry.TriangleMesh.create_sphere(0.5, 20).translate((0, 0, 0.5)).vertices)
    T = gravity_alignment(rec, verts)
    np.testing.assert_allclose(T[:3, :3] @ [0, 0, 1], [0, 1, 0], atol=0.02)
    p = verts @ T[:3, :3].T + T[:3, 3]
    assert abs(p[:, 1].min()) < 1e-9 and np.abs(np.median(p[:, [0, 2]], axis=0)).max() < 0.05


def test_ground_plane_fit_and_removal(tmp_path):
    from motiongrametry.clean import fit_ground_plane

    rec = _orbit_reconstruction()
    rng = np.random.default_rng(0)
    floor_pts = np.c_[rng.uniform(-3, 3, (400, 2)), np.zeros(400)]
    sphere_pts = rng.normal(size=(150, 3))
    sphere_pts = 0.5 * sphere_pts / np.linalg.norm(sphere_pts, axis=1, keepdims=True) + [0, 0, 0.5]
    for p in np.vstack([floor_pts, sphere_pts]):
        rec.add_point3D(p, pycolmap.Track(), np.zeros(3, np.uint8))
    n, d = fit_ground_plane(rec)
    np.testing.assert_allclose(n, [0, 0, 1], atol=0.01)  # oriented towards the cameras
    assert abs(d) < 0.01

    sphere = o3d.geometry.TriangleMesh.create_sphere(0.5, 30).translate((0, 0, 0.5))
    floor = o3d.geometry.TriangleMesh.create_box(1.6, 1.6, 0.005).translate((-0.8, -0.8, -0.005))
    o3d.io.write_triangle_mesh(str(tmp_path / "raw.ply"), sphere + floor.subdivide_midpoint(4))
    info = clean_mesh(tmp_path / "raw.ply", tmp_path / "clean.ply", ground=(n, d))
    v = np.asarray(o3d.io.read_triangle_mesh(str(tmp_path / "clean.ply")).vertices)
    assert info["ground_removed_vertices"] > 0
    assert np.all(np.linalg.norm(v - [0, 0, 0.5], axis=1) < 0.51)


def test_largest_component_without_masks(tmp_path):
    big = o3d.geometry.TriangleMesh.create_sphere(1.0, 30)
    small = o3d.geometry.TriangleMesh.create_sphere(0.2, 10).translate((3, 0, 0))
    o3d.io.write_triangle_mesh(str(tmp_path / "raw.ply"), big + small)
    info = clean_mesh(tmp_path / "raw.ply", tmp_path / "clean.ply")
    assert info["components_kept"] == 1
    assert info["faces_out"] == len(big.triangles)


def test_loop_pairs_skip_sequential_neighbours():
    names = [f"{i}" for i in range(30)]
    pairs = _loop_pairs(names, stride=5, overlap=6)
    assert ("0", "10") in pairs and ("0", "25") in pairs
    assert ("0", "5") not in pairs
    assert all(int(b) - int(a) > 6 for a, b in pairs)


def test_mask_undistortion_identity_for_pinhole(tmp_path):
    ws = Workspace(tmp_path / "work")
    rec = _orbit_reconstruction(n=3, model="PINHOLE")
    ws.sparse.mkdir(parents=True)
    rec.write(ws.sparse)
    _sphere_masks(rec, ws.masks)
    out = _undistorted_masks(ws, rec)
    for name, m in out.items():
        ref = cv2.imread(str(ws.mask_for(name)), cv2.IMREAD_GRAYSCALE)
        assert (m != ref).mean() < 0.001


def test_mask_undistortion_moves_pixels_for_distorted_camera(tmp_path):
    ws = Workspace(tmp_path / "work")
    distorted = _orbit_reconstruction(n=2, model="SIMPLE_RADIAL", params=[F, W / 2, H / 2, -0.3])
    ws.sparse.mkdir(parents=True)
    distorted.write(ws.sparse)
    _sphere_masks(distorted, ws.masks)
    undistorted = _orbit_reconstruction(n=2, model="PINHOLE")
    out = _undistorted_masks(ws, undistorted)
    _sphere_masks(undistorted, tmp_path / "ref")
    for name, m in out.items():
        ref = cv2.imread(str(tmp_path / "ref" / f"{name}.png"), cv2.IMREAD_GRAYSCALE)
        assert (m != ref).mean() < 0.01  # matches the ideal pinhole silhouette


def _mm(m):
    return np.asarray(m.vertices, float), np.asarray(m.triangles)


def test_solidify_turns_crossing_surfaces_into_one_clean_solid():
    import trimesh

    from motiongrametry.clean import solidify

    a = o3d.geometry.TriangleMesh.create_sphere(10, 30).translate((0, 0, 10))
    b = o3d.geometry.TriangleMesh.create_sphere(10, 30).translate((8, 0, 10))
    v, f = _mm(a + b)  # one mesh whose two shells cut through each other
    V, F, info = solidify(v, f, pitch=0.5, min_wall=0, stand="none")
    out = trimesh.Trimesh(V, F, process=False)
    assert out.is_watertight and len(out.split(only_watertight=False)) == 1
    m = o3d.geometry.TriangleMesh(o3d.utility.Vector3dVector(V), o3d.utility.Vector3iVector(F))
    assert not m.is_self_intersecting()
    # the union, not the parity "XOR" (which would hollow out the overlap)
    d, r = 8.0, 10.0
    union = 2 * 4 / 3 * np.pi * r**3 - np.pi * (4 * r + d) * (2 * r - d) ** 2 / 12
    assert abs(out.volume - union) / union < 0.05


def test_solidify_adds_a_stand_when_the_model_would_topple():
    from scipy.spatial import Delaunay

    from motiongrametry.clean import solidify

    post = o3d.geometry.TriangleMesh.create_cylinder(1.5, 30, 20).translate((0, 0, 15))
    ball = o3d.geometry.TriangleMesh.create_sphere(8, 20).translate((9, 0, 30))  # heavy, off to one side
    v, f = _mm(post + ball)
    v[:, 2] -= v[:, 2].min()
    V, F, info = solidify(v, f, pitch=0.5, min_wall=0, stand="auto")
    assert not info["stable"] and info["stand_radius_mm"] > 5
    foot = V[V[:, 2] < 0.3][:, :2]
    com = V.mean(0)  # rough: the stand makes the footprint wide anyway
    assert Delaunay(foot).find_simplex(com[:2]) >= 0
    V2, _, info2 = solidify(v, f, pitch=0.5, min_wall=0, stand="none")
    assert "stand_radius_mm" not in info2


def test_shell_grows_behind_the_scanned_surface_and_keeps_it_in_place():
    import trimesh

    from motiongrametry.clean import solidify

    s = o3d.geometry.TriangleMesh.create_sphere(15, 60)
    v, f = _mm(s)
    f = f[v[f].mean(1)[:, 2] > 0]  # the top half: an open bowl, like a partial scan (normals out)
    half_shell = lambda r_out, r_in: 2 / 3 * np.pi * (r_out**3 - r_in**3)  # noqa: E731
    for side, outer, (ro, ri) in (("inward", 30.0, (15, 12)), ("both", 33.0, (16.5, 13.5)), ("outward", 36.0, (18, 15))):
        V, F, _ = solidify(v, f, pitch=0.4, mode="shell", shell=3.0, stand="none", outward=True, shell_side=side)
        out = trimesh.Trimesh(V, F, process=False)
        assert out.is_watertight and len(out.split(only_watertight=False)) == 1
        assert abs(np.ptp(V[:, 0]) - outer) < 0.6, (side, np.ptp(V[:, 0]))  # the scanned surface is the outside
        assert abs(out.volume / half_shell(ro, ri) - 1) < 0.12, (side, out.volume)
    # normals wound the other way round: told so, the result is the same
    V, F, _ = solidify(v, f[:, ::-1], pitch=0.4, mode="shell", shell=3.0, stand="none", outward=False)
    assert abs(np.ptp(V[:, 0]) - 30.0) < 0.6


def test_thin_walls_thicken_behind_the_scanned_side_only():
    from motiongrametry.clean import solidify

    # an open sheet (a flap whose back was never seen), normals up, 0 mm thick
    sheet = o3d.geometry.TriangleMesh.create_box(30, 30, 1).subdivide_midpoint(2)
    v, f = _mm(sheet)
    top = f[(v[f][:, :, 2] > 0.99).all(1)]
    V, F, _ = solidify(v, top, pitch=0.1, min_wall=1.0, stand="none", outward=True)
    z = V[:, 2] + (v[:, 2].max() - V[:, 2].max())  # back in the input's frame via the top face
    assert abs(np.ptp(V[:, 2]) - 1.0) < 0.15  # thickened to the minimum wall...
    assert abs(z.min() - 0.0) < 0.15  # ...downwards, behind the scanned (top) side
    # a closed thin wall keeps both scanned faces where they are (it is reported, not moved)
    plate = o3d.geometry.TriangleMesh.create_box(30, 30, 0.3).subdivide_midpoint(2)
    V, F, _ = solidify(*_mm(plate), pitch=0.1, min_wall=1.0, stand="none", outward=True)
    assert abs(np.ptp(V[:, 2]) - 0.3) < 0.1


def test_normals_outward_asks_the_cameras(tmp_path):
    from motiongrametry.clean import normals_outward

    ws = Workspace(tmp_path / "w")
    rec = _orbit_reconstruction()
    ws.sparse.mkdir(parents=True)
    rec.write(str(ws.sparse))
    sphere = o3d.geometry.TriangleMesh.create_sphere(0.5, 30).translate((0, 0, 0.5))
    o3d.io.write_triangle_mesh(str(tmp_path / "out.ply"), sphere)
    assert normals_outward(ws, tmp_path / "out.ply") is True
    flipped = o3d.geometry.TriangleMesh(sphere.vertices, o3d.utility.Vector3iVector(np.asarray(sphere.triangles)[:, ::-1]))
    o3d.io.write_triangle_mesh(str(tmp_path / "in.ply"), flipped)
    assert normals_outward(ws, tmp_path / "in.ply") is False
    # a bowl seen from outside (a partial scan): its centroid sits in front of it, cameras know better
    v, f = np.asarray(sphere.vertices), np.asarray(sphere.triangles)
    bowl = o3d.geometry.TriangleMesh(sphere.vertices, o3d.utility.Vector3iVector(f[v[f].mean(1)[:, 0] > 0.2]))
    o3d.io.write_triangle_mesh(str(tmp_path / "bowl.ply"), bowl)
    assert normals_outward(ws, tmp_path / "bowl.ply") is True


def test_print_report_flags_a_mostly_invented_print(tmp_path):
    from motiongrametry.clean import print_mesh

    s = o3d.geometry.TriangleMesh.create_sphere(1.0, 30)
    v, f = _mm(s)
    f = f[v[f].mean(1)[:, 1] < -0.2]  # a small cap of a sphere: most of a closed print is lid
    m = o3d.geometry.TriangleMesh(o3d.utility.Vector3dVector(v), o3d.utility.Vector3iVector(f))
    o3d.io.write_triangle_mesh(str(tmp_path / "m.ply"), m)
    out, rep = print_mesh(tmp_path / "m.ply", None, height_mm=40, pitch=0.5, mode="solid")
    assert out.is_watertight and rep["lid_share"] > 0.35
    assert "print-invented" in {i["code"] for i in rep["issues"]}
    out, rep = print_mesh(tmp_path / "m.ply", None, height_mm=40, pitch=0.5)  # auto
    codes = {i["code"] for i in rep["issues"]}
    assert out.is_watertight and rep["mode"] == "shell" and "print-shell" in codes and "print-invented" not in codes
