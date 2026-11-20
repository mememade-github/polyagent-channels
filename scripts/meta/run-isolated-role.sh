#!/bin/bash
# Run one Codex refine role (audit|modify|evaluate) in a fresh isolated process:
# read-only sandbox where available, isolated user config, output only outside
# the repo or gitignored. Report gates fail closed for the /refine contract.
set -euo pipefail

ROLE="${1:-}"; PROJECT_ROOT="${2:-}"; PROMPT_FILE="${3:-}"; OUTPUT_FILE="${4:-}"
CODEX_BIN="${CODEX_BIN:-codex}"
case "$ROLE" in
    audit|modify|evaluate) ;;
    *) echo "Usage: $0 <audit|modify|evaluate> <project-root> <prompt-file> [output-file]" >&2; exit 2 ;;
esac
git -C "$PROJECT_ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1 ||
    { echo "ERROR: not a git repository: $PROJECT_ROOT" >&2; exit 2; }
PROJECT_ROOT=$(git -C "$PROJECT_ROOT" rev-parse --show-toplevel)
[ -f "$PROMPT_FILE" ] || { echo "ERROR: prompt file missing: $PROMPT_FILE" >&2; exit 2; }
[ "$ROLE" != "evaluate" ] || [ -n "$OUTPUT_FILE" ] || { echo "ERROR: evaluate requires an output-file (report gates are mandatory)." >&2; exit 2; }
if [ -n "$OUTPUT_FILE" ]; then
    OUTPUT_FILE=$(realpath -m -- "$OUTPUT_FILE")
    case "$OUTPUT_FILE" in
        "$PROJECT_ROOT"/*)
            git -C "$PROJECT_ROOT" check-ignore -q "${OUTPUT_FILE#"$PROJECT_ROOT/"}" ||
                { echo "ERROR: in-repo output must be gitignored: $OUTPUT_FILE" >&2; exit 2; } ;;
    esac
    mkdir -p "$(dirname "$OUTPUT_FILE")"
    [ "$ROLE" != "evaluate" ] || : > "$OUTPUT_FILE"  # a stale report must not pass the gates
fi

RUN_ROOT="$PROJECT_ROOT"; ISOLATED_ROOT=""; FINAL_OUTPUT=""
cleanup() { [ -z "$ISOLATED_ROOT" ] || rm -rf "$ISOLATED_ROOT"; [ -z "$FINAL_OUTPUT" ] || rm -f "$FINAL_OUTPUT"; }
trap cleanup EXIT
if [ "$ROLE" = "evaluate" ]; then
    ISOLATED_ROOT=$(mktemp -d); RUN_ROOT="$ISOLATED_ROOT"  # outside the repo: no recursive AGENTS.md auto-load
fi

ARGS=(exec --ephemeral --ignore-user-config --disable hooks -C "$RUN_ROOT")
[ "$ROLE" != "evaluate" ] || ARGS+=(--skip-git-repo-check)
if [ -f /.dockerenv ]; then
    ARGS+=(--dangerously-bypass-approvals-and-sandbox)  # DevContainer: bubblewrap unavailable
elif [ "$ROLE" = "modify" ]; then
    ARGS+=(--sandbox workspace-write)
elif [ "$ROLE" = "evaluate" ]; then
    ARGS+=(--sandbox workspace-write --add-dir "$(dirname "$OUTPUT_FILE")")
else
    ARGS+=(--sandbox read-only)
fi

if [ "$ROLE" = "evaluate" ]; then
    FINAL_OUTPUT=$(mktemp); ARGS+=(-o "$FINAL_OUTPUT")
    "$CODEX_BIN" "${ARGS[@]}" - < "$PROMPT_FILE" >/dev/null
    [ -s "$OUTPUT_FILE" ] || { echo "ERROR: evaluator did not write a report: $OUTPUT_FILE" >&2; exit 1; }
    jq -e '.checks_total | type == "number" and . >= 1' "$OUTPUT_FILE" >/dev/null ||
        { echo "ERROR: evaluator report must be valid JSON with checks_total >= 1." >&2; exit 1; }
    jq -e '.score | type == "number" and . >= 0 and . <= 1' "$FINAL_OUTPUT" >/dev/null ||
        { echo "ERROR: evaluator final message must be valid JSON with score in [0,1]." >&2; exit 1; }
    cat "$FINAL_OUTPUT"
else
    [ -z "$OUTPUT_FILE" ] || ARGS+=(-o "$OUTPUT_FILE")
    "$CODEX_BIN" "${ARGS[@]}" - < "$PROMPT_FILE"
fi
