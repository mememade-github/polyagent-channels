#!/bin/bash
# completion-checker.sh — fast checks of this template's tracked files.
# Checks the checkout that contains this script (a worktree's copy checks that
# worktree). Seconds-scale only: no docker, no network, no canonical checkout
# path; works in fresh clones and temp checkouts.
# Exit 0: every check that ran passed; a check that could not run for lack of a
# tool (the TOML parse without python3 tomllib) is named as not checked.
# Exit 1: a check failed, its input could not be read, or jq (required for the
# JSON parse) is missing; each failure is named.
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT" || { echo "FAIL: cannot cd to $ROOT" >&2; exit 1; }
CUR=0; PASSED=""; FAILED=""; SKIPPED=""
fail() { echo "FAIL: $1" >&2; CUR=1; }
begin() { CUR=0; }
end() { if [ "$CUR" -eq 0 ]; then PASSED="$PASSED, $1"; else FAILED="$FAILED, $1"; fi; }

begin
# NUL-separated, so paths arrive as they are, not quoted per core.quotepath.
SH_FILES=()
while IFS= read -r -d '' f; do SH_FILES+=("$f"); done < <(git ls-files -z -- '*.sh')
if wait "$!"; then
  for f in ${SH_FILES[@]+"${SH_FILES[@]}"}; do
    bash -n "$f" 2>/dev/null || fail "bash -n $f"
  done
else
  fail "git ls-files failed; shell syntax not checked"
fi
end "shell syntax"

begin
if command -v jq >/dev/null 2>&1; then
  for f in .claude/settings.json .codex/hooks.json .agents/rules.json .agents/hooks.json; do
    jq empty "$f" 2>/dev/null || fail "invalid JSON: $f"
  done
else
  fail "jq not found; JSON parse not run"
fi
end "JSON parse"

# tomllib is in Python 3.11 and later; exit 3 marks only its absence.
if ! command -v python3 >/dev/null 2>&1; then
  SKIPPED="$SKIPPED, TOML parse (no python3)"
else
  TOML_RC=0
  python3 -c 'import sys
try:
    import tomllib
except ModuleNotFoundError:
    sys.exit(3)
with open(sys.argv[1], "rb") as f:
    tomllib.load(f)' .codex/config.toml 2>/dev/null || TOML_RC=$?
  case "$TOML_RC" in
    0) PASSED="$PASSED, TOML parse" ;;
    3) SKIPPED="$SKIPPED, TOML parse (python3 has no tomllib)" ;;
    *) begin; fail "TOML parse: .codex/config.toml (exit $TOML_RC)"; end "TOML parse" ;;
  esac
fi

# The public template checks internal identifiers. Private projects own a tracked
# pattern list instead; optional local identifiers are never committed.
# A pattern file that exists as a directory entry must be readable.
begin
LEAK='products/[A-Za-z0-9._/-]*[A-Z][A-Z0-9_]*_ROOT|gitlab[.]local|cp[0-9]{3}[.]|172[.]10[.]'
PATTERNS="default patterns"; LOCAL="none"
read_patterns() {   # <file>: joined patterns on stdout; exit 1 none, 2 unreadable, 3 join failed
  local lines rc=0
  lines=$(grep -vE '^[[:space:]]*(#|$)' "$1") || rc=$?
  [ "$rc" -eq 0 ] || return "$(( rc == 1 ? 1 : 2 ))"
  printf '%s\n' "$lines" | paste -sd'|' - || return 3
}
if [ -e scripts/meta/leak-patterns.txt ] || [ -L scripts/meta/leak-patterns.txt ]; then
  PATTERNS="scripts/meta/leak-patterns.txt"
  PRC=0; LEAK=$(read_patterns scripts/meta/leak-patterns.txt) || PRC=$?
  case "$PRC" in
    0) ;;
    1) fail "no patterns in scripts/meta/leak-patterns.txt" ;;
    2) fail "cannot read scripts/meta/leak-patterns.txt" ;;
    *) fail "could not join patterns from scripts/meta/leak-patterns.txt" ;;
  esac
fi
if [ -e .env/leak-patterns ] || [ -L .env/leak-patterns ]; then
  LOCAL="present"
  PRC=0; EXTRA=$(read_patterns .env/leak-patterns) || PRC=$?
  case "$PRC" in
    0) LEAK="$LEAK|$EXTRA" ;;
    1) LOCAL="present, no patterns" ;;
    2) fail "cannot read .env/leak-patterns" ;;
    *) fail "could not join patterns from .env/leak-patterns" ;;
  esac
fi
if [ "$CUR" -eq 0 ]; then
  # git grep can exit 1 (no match) after failing to read a file, so any stderr
  # is a failed scan. Its messages are not shown: they can quote the patterns.
  # Report filenames only: a detected credential must not be copied to the log.
  SCAN_ERR=$(mktemp) || { echo "FAIL: mktemp failed" >&2; exit 1; }
  trap 'rm -f "$SCAN_ERR"' EXIT
  SCAN_RC=0
  HITS=$(git grep -l -I -i -E -e "$LEAK" -- ':!scripts/meta/completion-checker.sh' ':!scripts/meta/leak-patterns.txt' 2>"$SCAN_ERR") || SCAN_RC=$?
  if [ "$SCAN_RC" -gt 1 ] || [ -s "$SCAN_ERR" ]; then
    fail "tracked-content scan failed (exit $SCAN_RC; git messages not shown)"
  elif [ "$SCAN_RC" -eq 0 ]; then
    printf '%s\n' "$HITS" >&2; fail "internal-leak pattern in tracked files"
  fi
else
  echo "FAIL: leak scan not run" >&2
fi
end "leak scan"

begin
DRC=0; DRY=$(bash scripts/sync-agents-mirror.sh --dry 2>&1) || DRC=$?
if [ "$DRC" -ne 0 ]; then
  # Only the ERROR lines; running the sync shows its full output.
  printf '%s\n' "$DRY" | grep '^ERROR:' >&2
  fail "sync-agents-mirror.sh --dry exited $DRC; mirror not compared (run it for detail)"
elif ! printf '%s\n' "$DRY" | grep -q '^Dry run complete\. 0 change(s) detected\.$'; then
  fail "mirror drift — run: bash scripts/sync-agents-mirror.sh"
fi
end "mirror drift"

# Only projects that own this existing check consume it.
if [ -d .env ] && [ -f scripts/meta/verify-env-sync.sh ]; then
  begin
  bash scripts/meta/verify-env-sync.sh >/dev/null 2>&1 || fail "env-sync: run scripts/meta/verify-env-sync.sh for detail"
  end "env-sync"
fi

echo "Leak patterns: $PATTERNS; local .env/leak-patterns: $LOCAL"
[ -z "$SKIPPED" ] || echo "NOT CHECKED: ${SKIPPED#, }"
if [ -n "$FAILED" ]; then
  echo "FAILED: ${FAILED#, }${PASSED:+; passed: ${PASSED#, }}"
  exit 1
fi
echo "OK: passed: ${PASSED#, }."
