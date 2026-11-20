"""Import base-owned files and generate their blob lock."""
import argparse
import json
from pathlib import Path
from subprocess import check_output


LOCK_PATH = Path("devcontainer-base.lock.json")
SHARED_PATHS = {
    ".devcontainer/.env.example",
    ".devcontainer/devcontainer.json",
    ".gitignore",
    "LICENSE",
    "PROJECT.md",
    "README.md",
    "REFERENCE.md",
    "UPGRADING.md",
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("upstream", type=Path)
    parser.add_argument("revision")
    options = parser.parse_args()
    pin = git(options.upstream, "rev-parse", "--verify", options.revision + "^{commit}").decode().strip()
    entries = base_entries(options.upstream, pin)
    previous = previous_paths()
    check_new_paths(entries.keys() - previous)
    for name in sorted(previous - entries.keys()):
        Path(name).unlink(missing_ok=True)
    for name, entry in entries.items():
        write_blob(options.upstream, Path(name), entry)
    lock = {"pin": pin, "blobs": {name: blob for name, (_, blob) in entries.items()}}
    LOCK_PATH.write_text(json.dumps(lock, indent=2) + "\n", encoding="utf-8", newline="\n")


def base_entries(upstream, pin):
    tree = git(upstream, "ls-tree", "-rz", pin).decode("utf-8")
    entries = {}
    for entry in tree.rstrip("\0").split("\0"):
        metadata, name = entry.split("\t", 1)
        mode, kind, blob = metadata.split()
        if name in SHARED_PATHS:
            continue
        if kind != "blob" or mode not in {"100644", "100755"}:
            raise SystemExit(f"Unsupported base entry {mode} {kind}: {name}")
        entries[name] = (mode, blob)
    return entries


def previous_paths():
    if LOCK_PATH.is_file():
        lock = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
        return set(lock["blobs"])
    return set()


def check_new_paths(new_paths):
    if not LOCK_PATH.exists():
        return
    for name in sorted(new_paths):
        path = Path(name)
        if path.exists() or path.is_symlink():
            raise SystemExit(f"New upstream path overlaps an existing file or directory: {name}")


def write_blob(upstream, path, entry):
    mode, blob = entry
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(git(upstream, "cat-file", "blob", blob))
    path.chmod(int(mode, 8) & 0o777)


def git(upstream, *arguments):
    return check_output(["git", "-C", str(upstream), *arguments])


if __name__ == "__main__":
    main()
