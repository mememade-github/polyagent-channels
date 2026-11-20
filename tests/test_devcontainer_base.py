"""Check the upstream base against its Git blobs without the repository index."""
import json
import os
from pathlib import Path
from subprocess import check_output


def test_devcontainer_base_matches_lock(tmp_path):
    source = Path(__file__).resolve()
    tests = source.parent
    root = tests.parent
    lock_path = root / "devcontainer-base.lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    git_directory = tmp_path / "git"
    git = [
        "git", f"--git-dir={git_directory}", f"--work-tree={root}",
        "-c", "core.autocrlf=true", "-c", f"core.attributesFile={os.devnull}",
    ]
    check_output([*git, "init", "--quiet", "--template=", "--object-format=sha1"], cwd=root)
    actual = {name: working_blob(git, root, name) for name in lock["blobs"]}
    assert actual == lock["blobs"]


def working_blob(git, root, name):
    path = root / name
    if path.is_symlink() or not path.is_file():
        return None
    blob = check_output([*git, "hash-object", f"--path={name}", "--", name], cwd=root, text=True)
    return blob.strip()
