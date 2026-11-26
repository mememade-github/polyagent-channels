#!/bin/bash
# Run Codex evaluation in a fresh context outside the repository.
# Reports must be outside the repository or gitignored.
# Clear prior report contents before execution.
set -euo pipefail

ROLE="${1:-}"
PROJECT_ROOT="${2:-}"
PROMPT_FILE="${3:-}"
OUTPUT_FILE="${4:-}"
CODEX_BIN="${CODEX_BIN:-codex}"

case "$ROLE" in
    evaluate) ;;
    *)
        echo "Usage: $0 evaluate <project-root> <prompt-file> <output-file>" >&2
        exit 2
        ;;
esac

git -C "$PROJECT_ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1 ||
    { echo "ERROR: not a git repository: $PROJECT_ROOT" >&2; exit 2; }

PROJECT_ROOT=$(git -C "$PROJECT_ROOT" rev-parse --show-toplevel)

[ -f "$PROMPT_FILE" ] ||
    { echo "ERROR: prompt file missing: $PROMPT_FILE" >&2; exit 2; }

[ -n "$OUTPUT_FILE" ] ||
    { echo "ERROR: evaluate requires an output-file." >&2; exit 2; }

OUTPUT_FILE=$(realpath -m -- "$OUTPUT_FILE")

case "$OUTPUT_FILE" in
    "$PROJECT_ROOT"/*)
        git -C "$PROJECT_ROOT" check-ignore -q "${OUTPUT_FILE#"$PROJECT_ROOT/"}" ||
            { echo "ERROR: in-repo output must be gitignored: $OUTPUT_FILE" >&2; exit 2; }
        ;;
esac

mkdir -p "$(dirname "$OUTPUT_FILE")"
: > "$OUTPUT_FILE"

ISOLATED_ROOT=""
cleanup() {
    [ -z "$ISOLATED_ROOT" ] || rm -rf "$ISOLATED_ROOT"
}
trap cleanup EXIT

# Outside the repository: no recursive AGENTS.md auto-load.
ISOLATED_ROOT=$(mktemp -d)

ARGS=(
    exec
    --ephemeral
    --ignore-user-config
    --disable hooks
    -C "$ISOLATED_ROOT"
    --skip-git-repo-check
)

if [ -f /.dockerenv ]; then
    # Existing DevContainer compatibility fallback: bubblewrap unavailable.
    ARGS+=(--dangerously-bypass-approvals-and-sandbox)
else
    ARGS+=(--sandbox workspace-write --add-dir "$(dirname "$OUTPUT_FILE")")
fi

if "$CODEX_BIN" "${ARGS[@]}" - < "$PROMPT_FILE" >/dev/null; then
    :
else
    STATUS=$?
    echo "ERROR: evaluator process failed (exit $STATUS)." >&2
    exit "$STATUS"
fi

[ -s "$OUTPUT_FILE" ] ||
    { echo "ERROR: evaluator did not write a non-empty report: $OUTPUT_FILE" >&2; exit 1; }

cat "$OUTPUT_FILE"
