#!/bin/bash
# claude-hooks-adapter.sh — run the Claude hooks under Antigravity (agy).
#
# Antigravity reads .agents/hooks.json and runs each handler with the working
# directory set to .agents/. This adapter translates the agy hook contract to
# the Claude hook contract, calls the unmodified .claude/hooks/*.sh, and
# translates the result back. It never executes the tool command itself.
#
# Usage: bash hooks/claude-hooks-adapter.sh <session-start|pre-tool-use|stop>
# Event mapping is documented in REFERENCE.md, section Hooks.
# For pre-tool-use, an empty {} blocks commands; emit ask on a non-denial.
#
# Exit status is always 0 with one valid JSON object on stdout: that is the
# agy response format, not a claim that a gate succeeded. The fail-open policy
# of the Claude hooks is unchanged (their exit 0 -> ask; only exit 2 -> deny).
EVENT="${1:-}"

fallback() {
  case "$EVENT" in
    pre-tool-use) printf '%s\n' '{"decision":"ask"}' ;;
    stop) printf '%s\n' '{"decision":"allow"}' ;;
    session-start) printf '%s\n' '{"injectSteps":[]}' ;;
    *) printf '%s\n' '{}' ;;
  esac
  exit 0
}

INPUT=$(cat 2>/dev/null) || INPUT=""
command -v jq >/dev/null 2>&1 || fallback
ROOT=$(git rev-parse --show-toplevel 2>/dev/null) ||
  ROOT=$(cd "$(dirname "$0")/../.." 2>/dev/null && pwd) || fallback
HOOKS="$ROOT/.claude/hooks"

case "$EVENT" in
  pre-tool-use)
    CMD=$(printf '%s' "$INPUT" | jq -r '.toolCall.args.CommandLine // empty' 2>/dev/null) || fallback
    [ -n "$CMD" ] || fallback
    CWD=$(printf '%s' "$INPUT" | jq -r '.toolCall.args.Cwd // empty' 2>/dev/null) || CWD=""
    PROJ="$ROOT"
    RUN_DIR="$ROOT"
    if [ -n "$CWD" ] && [ -d "$CWD" ]; then
      RUN_DIR="$CWD"
      P=$(git -C "$CWD" rev-parse --show-toplevel 2>/dev/null) && PROJ="$P"
    fi
    CLAUDE_INPUT=$(jq -n --arg c "$CMD" '{tool_name: "Bash", tool_input: {command: $c}}') || fallback
    for GATE in pre-commit-gate pre-push-gate; do
      [ -f "$HOOKS/$GATE.sh" ] || continue
      ERR=$(cd "$RUN_DIR" && printf '%s' "$CLAUDE_INPUT" |
        CLAUDE_PROJECT_DIR="$PROJ" timeout 5 bash "$HOOKS/$GATE.sh" 2>&1 >/dev/null)
      if [ $? -eq 2 ]; then
        jq -cn --arg r "$ERR" '{decision: "deny", reason: $r}' || fallback
        exit 0
      fi
    done
    fallback
    ;;
  stop)
    REASON=$(printf '%s' "$INPUT" | jq -r '.terminationReason // empty' 2>/dev/null) || fallback
    [ "$REASON" = "NO_TOOL_CALL" ] || fallback
    [ -f "$HOOKS/refinement-gate.sh" ] || fallback
    OUT=$(cd "$ROOT" && CLAUDE_PROJECT_DIR="$ROOT" timeout 10 bash "$HOOKS/refinement-gate.sh" </dev/null 2>/dev/null)
    DECISION=$(printf '%s' "$OUT" | jq -r '.decision // empty' 2>/dev/null) || fallback
    [ "$DECISION" = "block" ] || fallback
    printf '%s' "$OUT" | jq -c '{decision: "continue", reason: (.reason // "")}' 2>/dev/null || fallback
    ;;
  session-start)
    [ -f "$HOOKS/session-start.sh" ] || fallback
    OUT=$(cd "$ROOT" && printf '%s' '{"source":"startup"}' |
      CLAUDE_PROJECT_DIR="$ROOT" timeout 10 bash "$HOOKS/session-start.sh" 2>/dev/null)
    CTX=$(printf '%s' "$OUT" | jq -r '.hookSpecificOutput.additionalContext // empty' 2>/dev/null) || fallback
    [ -n "$CTX" ] || fallback
    jq -cn --arg m "$CTX" '{injectSteps: [{systemMessage: {systemMessage: $m}}]}' || fallback
    ;;
  *)
    fallback
    ;;
esac
exit 0
