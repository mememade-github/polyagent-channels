"""Check the upstream base against its Git blobs without the repository index."""
import hashlib
import json
import os
from pathlib import Path
from subprocess import check_output, run
import sys

import pytest


IMPORTER = Path(__file__).resolve().parents[1] / "scripts/update_devcontainer_base.py"
REVIEWED_PATHS = (
    ".devcontainer/.env.example",
    ".devcontainer/devcontainer.json",
    ".devcontainer/docker-compose.yml",
    ".gitignore",
    "LICENSE",
    "PROJECT.md",
    "README.md",
    "REFERENCE.md",
    "UPGRADING.md",
)


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


@pytest.fixture
def upstream_history(tmp_path):
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    git = ["git", "-C", str(upstream), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
           "-c", "commit.gpgSign=false"]
    check_output([*git, "init", "--quiet", "--template="])
    (upstream / ".devcontainer").mkdir()
    paths = (*REVIEWED_PATHS, "base-owned.txt")
    previous = upstream_commit(git, upstream, paths, "previous")
    target = upstream_commit(git, upstream, paths, "target")
    return upstream, previous, target


def upstream_commit(git, upstream, paths, version):
    for name in paths:
        (upstream / name).write_text(f"{version} {name}\n", encoding="utf-8")
    check_output([*git, "add", "-f", "--", *paths])
    check_output([*git, "commit", "--quiet", "-m", version])
    return check_output([*git, "rev-parse", "HEAD"], text=True).strip()


def review_base(product, upstream, target):
    return run([sys.executable, str(IMPORTER), "--review", str(upstream), target],
               cwd=product, capture_output=True, text=True, check=False)


def write_review_lock(product, pin):
    lock = product / "devcontainer-base.lock.json"
    lock.write_text(json.dumps({"pin": pin, "blobs": {}}), encoding="utf-8")
    return lock


def test_review_shows_shared_and_compose_changes_without_importing(tmp_path, upstream_history):
    upstream, previous, target = upstream_history
    product = tmp_path / "product"
    product.mkdir()
    lock = write_review_lock(product, previous)
    original = lock.read_bytes()
    review = review_base(product, upstream, target)
    patches = [line for line in review.stdout.splitlines() if line.startswith("+target ")]
    assert (review.returncode, sorted(patches), list(product.iterdir()), lock.read_bytes()) == (
        0, sorted(f"+target {name}" for name in REVIEWED_PATHS), [lock], original,
    )


def test_review_same_pin_has_no_patch(tmp_path, upstream_history):
    upstream, _, target = upstream_history
    write_review_lock(tmp_path, target)
    review = review_base(tmp_path, upstream, target)
    assert (review.returncode, review.stdout, review.stderr) == (0, "", "")


@pytest.mark.parametrize("pin, message", [
    (None, "Restore devcontainer-base.lock.json"),
    ("0" * 40, "Fetch that commit before reviewing."),
])
def test_review_requires_the_previous_pin(tmp_path, upstream_history, pin, message):
    upstream, _, target = upstream_history
    product = tmp_path / "product"
    product.mkdir()
    if pin is not None:
        write_review_lock(product, pin)
    review = review_base(product, upstream, target)
    assert (review.returncode, review.stdout, message in review.stderr) == (1, "", True)
