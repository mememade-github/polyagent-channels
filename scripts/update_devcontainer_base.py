"""Review upstream changes or import base-owned files and generate their blob lock."""
import argparse
import json
from pathlib import Path
from subprocess import check_output, run
import sys


LOCK_PATH = Path("devcontainer-base.lock.json")
SHARED_PATHS = {
    ".devcontainer/devcontainer.json": (
        "Review upstream settings against [DevContainer settings](#devcontainer-settings)."
    ),
    ".devcontainer/.env.example": (
        "Keep the product override form. Review upstream variables against "
        "[Base environment inputs](#base-environment-inputs) and derive effective defaults in the "
        "reference from product Compose."
    ),
    ".gitignore": (
        "Replace its upstream prefix with the new upstream blob, then retain the product block. Use the "
        "base rules for Codex runtime markers."
    ),
    "README.md": (
        "Keep product prerequisites, discovery and attribution. Review changed upstream prerequisites and "
        "notices."
    ),
    "LICENSE": (
        "Keep the product license at the root. Review upstream license changes and retain applicable "
        "notices under the locations in [Attribution](README.md#license--attribution)."
    ),
    "PROJECT.md": (
        "Keep product context and the pointer to the product rule for clients that load project context. "
        "Review any changed base session-start requirements."
    ),
    "REFERENCE.md": (
        "Keep product commands and a link to the upstream lifecycle section at the new pin. Edit the "
        "[rendering template](scripts/REFERENCE.md.in) and regenerate the reference."
    ),
    "UPGRADING.md": (
        "Keep product release-specific steps. Add actions operators need when an upstream update affects "
        "them."
    ),
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--review", action="store_true", help="show changes since the locked pin without importing")
    parser.add_argument("upstream", type=Path)
    parser.add_argument("revision")
    options = parser.parse_args()
    pin = git(options.upstream, "rev-parse", "--verify", options.revision + "^{commit}").decode().strip()
    if options.review:
        return review(options.upstream, pin)
    import_base(options.upstream, pin)


def review(upstream, pin):
    try:
        lock = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"Restore {LOCK_PATH} from the product checkout before reviewing the update.") from None
    previous = lock["pin"]
    if previous == pin:
        return
    probe = run(["git", "-C", str(upstream), "cat-file", "-e", previous + "^{commit}"],
                capture_output=True, check=False)
    if probe.returncode:
        raise SystemExit(f"Locked pin {previous} is unavailable in {upstream}. Fetch that commit before reviewing.")
    patch = git(upstream, "diff", "--patch", "--no-ext-diff", "--no-textconv", "--no-renames",
                previous, pin, "--", *SHARED_PATHS, ".devcontainer/docker-compose.yml")
    sys.stdout.buffer.write(patch)


def import_base(upstream, pin):
    entries = base_entries(upstream, pin)
    checkout = checkout_root()
    previous = previous_paths()
    check_new_paths(entries.keys() - previous)
    for name in sorted(previous - entries.keys()):
        Path(name).unlink(missing_ok=True)
    for name, entry in entries.items():
        write_blob(upstream, Path(name), entry, checkout)
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


def checkout_root():
    root = Path.cwd()
    probe = run(["git", "rev-parse", "--show-toplevel"], capture_output=True, check=False)
    if probe.returncode == 0 and Path(probe.stdout.decode().removesuffix("\n")) == root:
        return root
    if (root / ".git").exists():
        raise SystemExit("Cannot locate the destination Git working tree")
    return None


def write_blob(upstream, path, entry, checkout):
    mode, blob = entry
    content = git(upstream, "cat-file", "blob", blob)
    if checkout:
        object_id = check_output(["git", "hash-object", "-w", "--stdin"], input=content).decode().strip()
        content = git(checkout, "cat-file", "--filters", f"--path={path.as_posix()}", object_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    path.chmod(int(mode, 8) & 0o777)


def git(upstream, *arguments):
    return check_output(["git", "-C", str(upstream), *arguments])


if __name__ == "__main__":
    main()
