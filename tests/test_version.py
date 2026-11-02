import subprocess

from motiongrammetry import _branch_suffix


def _git(root, *args):
    subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                   check=True, capture_output=True)


def _repo(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'motiongrammetry'\n")
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "first")
    return tmp_path


def test_a_branch_checkout_names_its_branch(tmp_path):
    root = _repo(tmp_path)
    _git(root, "switch", "-q", "-c", "fix/some-bug")
    assert _branch_suffix(root, "0.2.0") == "-fix-some-bug"


def test_the_release_commit_is_the_plain_version(tmp_path):
    root = _repo(tmp_path)
    _git(root, "tag", "v0.2.0")
    assert _branch_suffix(root, "0.2.0") == ""


def test_main_past_the_release_says_so(tmp_path):
    root = _repo(tmp_path)
    _git(root, "tag", "v0.2.0")
    _git(root, "commit", "-q", "--allow-empty", "-m", "merged work")
    assert _branch_suffix(root, "0.2.0") == "-main"


def test_an_installed_copy_has_no_suffix(tmp_path):
    assert _branch_suffix(tmp_path, "0.2.0") == ""
