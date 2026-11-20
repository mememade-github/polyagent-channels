#!/bin/bash
# completion-checker.sh — fast, environment-independent pre-commit verification.
# Seconds-scale checks only: no docker, no network, no canonical checkout path;
# works in fresh clones, CI, and temp checkouts. All checks pass -> touch both
# vendors' per-branch markers (existence + age contract read by the pre-commit
# gates) and exit 0. Any failure -> name it and exit 1 (no marker is written).
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT" || { echo "FAIL: cannot cd to $ROOT" >&2; exit 1; }
FAIL=0
fail() { echo "FAIL: $1" >&2; FAIL=1; }
while IFS= read -r f; do
  bash -n "$f" 2>/dev/null || fail "bash -n $f"
done < <(git ls-files '*.sh')
for f in .claude/settings.json .codex/hooks.json .agents/rules.json .agents/hooks.json; do
  jq empty "$f" 2>/dev/null || fail "invalid JSON: $f"
done
if command -v python3 >/dev/null 2>&1; then
  python3 -c 'import sys
try: import tomllib
except ImportError: sys.exit(0)
tomllib.load(open(sys.argv[1], "rb"))' .codex/config.toml 2>/dev/null || fail "TOML parse: .codex/config.toml"
fi
# The public template checks internal identifiers. Private projects own a tracked
# pattern list instead; optional local identifiers are never committed.
LEAK='products/[A-Za-z0-9._/-]*[A-Z][A-Z0-9_]*_ROOT|gitlab[.]local|cp[0-9]{3}[.]|172[.]10[.]'
if [ -f scripts/meta/leak-patterns.txt ]; then
  LEAK=$(grep -vE '^[[:space:]]*(#|$)' scripts/meta/leak-patterns.txt | paste -sd'|' -)
  [ -n "$LEAK" ] || fail "empty scripts/meta/leak-patterns.txt"
fi
if [ -f .env/leak-patterns ]; then
  EXTRA=$(grep -vE '^[[:space:]]*(#|$)' .env/leak-patterns | paste -sd'|' -)
  [ -z "$EXTRA" ] || LEAK="$LEAK|$EXTRA"
fi
# Report filenames only: a detected credential must not be copied to the log.
SCAN_RC=0
HITS=$(git grep -l -I -i -E "$LEAK" -- ':!.devcontainer/verify-template.sh' ':!scripts/meta/completion-checker.sh' ':!scripts/meta/leak-patterns.txt' 2>/dev/null) || SCAN_RC=$?
case "$SCAN_RC" in
  0) printf '%s\n' "$HITS" >&2; fail "internal-leak pattern in tracked files" ;;
  1) ;;
  *) fail "tracked-content scan failed (exit $SCAN_RC)" ;;
esac
bash scripts/meta/karpathy-consistency-check.sh "$ROOT" >/dev/null 2>&1 || fail "karpathy-consistency-check (run it directly for detail)"
DRY=$(bash scripts/sync-agents-mirror.sh --dry 2>&1) || fail "sync-agents-mirror --dry errored"
printf '%s\n' "$DRY" | grep -q '^Dry run complete\. 0 change(s) detected\.$' || fail "mirror drift — run: bash scripts/sync-agents-mirror.sh"
# Only projects that own this existing check consume it.
if [ -d .env ] && [ -f scripts/meta/verify-env-sync.sh ]; then
  bash scripts/meta/verify-env-sync.sh >/dev/null 2>&1 || fail "env-sync: run scripts/meta/verify-env-sync.sh for detail"
fi
[ "$FAIL" -eq 0 ] || exit 1
BRANCH=$(git symbolic-ref --quiet --short HEAD 2>/dev/null || echo HEAD)
SAFE=$(printf '%s' "$BRANCH" | tr '/' '-')
mkdir -p .claude .codex/state
touch ".claude/.last-verification.$SAFE" ".codex/state/last-verification.$SAFE"
echo "OK: all checks passed; verification markers written for branch '$BRANCH'."
