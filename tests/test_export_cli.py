"""`mg export --out`: what is a folder and what is a file, and nothing replaced without saying so (#50)."""

import zipfile

import pytest

from motiongrammetry import cli
from motiongrammetry.workspace import Workspace


@pytest.fixture()
def ws(tmp_path):
    import open3d as o3d

    ws = Workspace(tmp_path / "scan")
    ws.mesh.mkdir(parents=True)
    box = o3d.geometry.TriangleMesh.create_box(1, 2, 3)
    o3d.io.write_triangle_mesh(str(ws.mesh / "mesh_clean.ply"), box)
    return ws


def mg(capsys, *args):
    assert cli.main(["export", *map(str, args)]) == 0
    return capsys.readouterr().out.splitlines()


def test_a_new_folder_with_a_slash_gets_each_export_under_its_own_name(ws, tmp_path, capsys):
    out = str(tmp_path / "new") + "/"
    last = mg(capsys, ws.root, "mesh:glb", "--out", out)[-1]
    assert last == f"-> {tmp_path / 'new' / 'scan-mesh.glb'}"
    last = mg(capsys, ws.root, "mesh:ply", "--out", out)[-1]
    assert last == f"-> {tmp_path / 'new' / 'scan-mesh.ply'}"
    assert sorted(p.name for p in (tmp_path / "new").iterdir()) == ["scan-mesh.glb", "scan-mesh.ply"]


def test_missing_parent_folders_are_made(ws, tmp_path, capsys):
    mg(capsys, ws.root, "mesh:glb", "--out", str(tmp_path / "a" / "b" / "c") + "/")
    assert (tmp_path / "a" / "b" / "c" / "scan-mesh.glb").is_file()
    last = mg(capsys, ws.root, "mesh:ply", "--out", tmp_path / "x" / "y" / "model.ply")[-1]
    assert last == f"-> {tmp_path / 'x' / 'y' / 'model.ply'}" and (tmp_path / "x" / "y" / "model.ply").is_file()


def test_an_existing_folder_without_a_slash_is_a_folder(ws, tmp_path, capsys):
    (tmp_path / "here").mkdir()
    mg(capsys, ws.root, "mesh:glb", "--out", tmp_path / "here")
    assert [p.name for p in (tmp_path / "here").iterdir()] == ["scan-mesh.glb"]


def test_a_new_name_without_an_extension_is_a_folder(ws, tmp_path, capsys):
    last = mg(capsys, ws.root, "mesh:glb", "--out", tmp_path / "exports")[-1]
    assert (tmp_path / "exports").is_dir() and last == f"-> {tmp_path / 'exports' / 'scan-mesh.glb'}"


def test_a_name_with_an_extension_is_a_file(ws, tmp_path, capsys):
    lines = mg(capsys, ws.root, "mesh:glb", "--out", tmp_path / "model.glb")
    assert lines[-1] == f"-> {tmp_path / 'model.glb'}" and (tmp_path / "model.glb").is_file()
    assert not any(ln.startswith("warning") for ln in lines)


def test_another_extension_than_the_exports_is_written_with_a_warning(ws, tmp_path, capsys):
    lines = mg(capsys, ws.root, "mesh:glb", "--out", tmp_path / "model.stl")
    assert "warning: model.stl ends in .stl, but the export is a .glb file" in lines
    assert (tmp_path / "model.stl").is_file()


def test_an_existing_file_is_replaced_and_the_output_says_so(ws, tmp_path, capsys):
    dst = tmp_path / "model.glb"
    dst.write_bytes(b"old")
    last = mg(capsys, ws.root, "mesh:glb", "--out", dst)[-1]
    assert last == f"-> {dst} (replaced the file that was there)" and dst.read_bytes() != b"old"
    # into a folder too: the second export of the same content replaces the first, visibly
    mg(capsys, ws.root, "mesh:glb", "--out", str(tmp_path / "d") + "/")
    last = mg(capsys, ws.root, "mesh:glb", "--out", str(tmp_path / "d") + "/")[-1]
    assert last.endswith("scan-mesh.glb (replaced the file that was there)")


def test_an_existing_file_without_an_extension_is_that_file_with_a_warning(ws, tmp_path, capsys):
    """Such as the file the old code wrote for `--out exports/`."""
    (tmp_path / "exports").write_bytes(b"old")
    lines = mg(capsys, ws.root, "mesh:glb", "--out", tmp_path / "exports")
    assert "warning: exports has no extension, but the export is a .glb file" in lines
    assert lines[-1].endswith("(replaced the file that was there)")


def test_a_slash_after_an_existing_file_is_refused_before_exporting(ws, tmp_path, capsys):
    (tmp_path / "exports").write_bytes(b"old")
    with pytest.raises(SystemExit, match="ends in / but .* is a file"):
        cli.main(["export", str(ws.root), "mesh:glb", "--out", str(tmp_path / "exports") + "/"])
    assert (tmp_path / "exports").read_bytes() == b"old"
    assert not (ws.root / ".ui" / "exports").exists()  # refused before anything was made


def test_a_folder_inside_a_file_is_refused_clearly(ws, tmp_path):
    (tmp_path / "f").write_bytes(b"")
    with pytest.raises(SystemExit, match="cannot make a folder inside .*f: it is a file"):
        cli.main(["export", str(ws.root), "mesh:glb", "--out", str(tmp_path / "f" / "sub") + "/"])
    with pytest.raises(SystemExit, match="it is a file"):
        cli.main(["export", str(ws.root), "mesh:glb", "--out", str(tmp_path / "f" / "model.glb")])


def test_several_contents_make_one_zip(ws, tmp_path, capsys):
    """Each content at most once (the export refuses a repeat), each in its own folder of the zip."""
    last = mg(capsys, ws.root, "mesh:glb", "print:stl", "--out", str(tmp_path / "z") + "/")[-1]
    assert last == f"-> {tmp_path / 'z' / 'scan-export.zip'}"
    with zipfile.ZipFile(tmp_path / "z" / "scan-export.zip") as z:
        assert sorted(z.namelist()) == ["mesh/scan-mesh.glb", "print/scan-print.stl"]
    mg(capsys, ws.root, "mesh:glb", "print:stl", "--out", tmp_path / "both.zip")
    assert zipfile.is_zipfile(tmp_path / "both.zip")


def test_no_out_is_the_current_folder(ws, tmp_path, capsys, monkeypatch):
    (tmp_path / "cwd").mkdir()
    monkeypatch.chdir(tmp_path / "cwd")
    assert mg(capsys, ws.root, "mesh:glb")[-1] == f"-> {tmp_path / 'cwd' / 'scan-mesh.glb'}"


def test_out_into_the_scans_own_export_folder_is_not_an_error(ws, tmp_path, capsys):
    mg(capsys, ws.root, "mesh:glb", "--out", tmp_path / "first.glb")  # makes .ui/exports
    last = mg(capsys, ws.root, "mesh:glb", "--out", ws.root / ".ui" / "exports")[-1]
    assert last.endswith("scan-mesh.glb (replaced the file that was there)")
    assert (ws.root / ".ui" / "exports" / "scan-mesh.glb").stat().st_size > 0


def test_export_2dgs_never_deletes_a_folder_that_is_not_its_dataset(tmp_path):
    from motiongrammetry.splat import export_2dgs

    ws = Workspace(tmp_path / "scan")
    (ws.dense / "sparse").mkdir(parents=True)
    keep = tmp_path / "Documents"
    keep.mkdir()
    (keep / "precious.txt").write_text("keep me")
    with pytest.raises(RuntimeError, match="holds no earlier 2DGS dataset"):
        export_2dgs(ws, keep)
    assert (keep / "precious.txt").read_text() == "keep me"
    (tmp_path / "file").write_text("x")
    with pytest.raises(RuntimeError, match="is a file"):
        export_2dgs(ws, tmp_path / "file")
