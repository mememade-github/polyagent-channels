"""Check the upstream base against its Git blobs without the repository index."""
import hashlib
import json
import os
from pathlib import Path
from subprocess import check_output, run


def test_devcontainer_base_matches_lock(tmp_path):
    source = Path(__file__).resolve()
    tests = source.parent
    root = tests.parent
    lock_path = root / "devcontainer-base.lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    git = working_git(root, tmp_path)
    actual = {name: working_blob(git, root, name) for name in lock["blobs"]}
    assert actual == lock["blobs"]


def working_git(root, tmp_path):
    git = ["git", "-c", "core.autocrlf=true"]
    probe = run([*git, "rev-parse", "--show-toplevel"], cwd=root, capture_output=True, check=False)
    if probe.returncode == 0 and Path(os.fsdecode(probe.stdout.removesuffix(b"\n"))) == root:
        return git
    if (root / ".git").exists():
        raise RuntimeError("Cannot locate the Git working tree for the base lock")
    git = [*git, f"--git-dir={tmp_path / 'git'}", f"--work-tree={root}",
           "-c", f"core.attributesFile={os.devnull}"]
    check_output([*git, "init", "--quiet", "--template=", "--object-format=sha1"], cwd=root)
    return git


def working_blob(git, root, name):
    path = root / name
    if path.is_symlink() or not path.is_file():
        return None
    object_format = check_output([*git, "rev-parse", "--show-object-format"], cwd=root).strip()
    arguments = [f"--path={name}", "--", name]
    if object_format == b"sha1":
        return check_output([*git, "hash-object", *arguments], cwd=root, text=True).strip()
    # The lock pins SHA-1 blobs. Let Git clean the working bytes before hashing in that format.
    blob = check_output([*git, "hash-object", "-w", *arguments], cwd=root).strip()
    content = check_output([*git, "cat-file", "blob", blob.decode("ascii")], cwd=root)
    header = f"blob {len(content)}\0".encode("ascii")
    return hashlib.sha1(header + content).hexdigest()


def test_lock_hash_accepts_line_endings_in_source_export(tmp_path):
    root = tmp_path / "export"
    root.mkdir()
    path = root / "file.txt"
    path.write_bytes(b"base\r\n")
    git = working_git(root, tmp_path)
    converted = working_blob(git, root, "file.txt")
    path.write_bytes(b"base\n")
    assert converted == working_blob(git, root, "file.txt")
