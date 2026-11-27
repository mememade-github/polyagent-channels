#!/bin/bash
# claude-hooks-adapter.sh — run the Claude hooks under Antigravity (agy).
#
# Antigravity reads .agents/hooks.json and runs each handler with the working
# directory set to .agents/. This adapter translates the agy hook contract to
# the Claude hook contract, calls the unmodified .claude/hooks/*.sh, and
# translates the result back. It never executes the tool command itself.
#
# Usage: bash hooks/claude-hooks-adapter.sh <session-start|pre-tool-use>
# REFERENCE.md §Hooks has the event mapping and the answers. Always exits 0
# with one JSON object on stdout (agy's response format). pre-tool-use answers
# deny for gate exit 2 and ask otherwise, never {} (agy blocks on {}); an
# unchecked run also writes "pre-push gate: not checked: <cause>" to stderr.
EVENT="${1:-}"

fallback() {
  case "$EVENT" in
    pre-tool-use) printf '%s\n' '{"decision":"ask"}' ;;
    session-start) printf '%s\n' '{"injectSteps":[]}' ;;
    *) printf '%s\n' '{}' ;;
  esac
  exit 0
}
unchecked() {
  [ "$EVENT" = pre-tool-use ] && echo "pre-push gate: not checked: $1" >&2
  fallback
}

INPUT=$(cat 2>/dev/null) || INPUT=""
command -v jq >/dev/null 2>&1 || unchecked "jq not found (adapter)"
ROOT=$(git rev-parse --show-toplevel 2>/dev/null) ||
  ROOT=$(cd "$(dirname "$0")/../.." 2>/dev/null && pwd) || unchecked "project root not found (adapter)"
HOOKS="$ROOT/.claude/hooks"

case "$EVENT" in
  pre-tool-use)
    CMD=$(printf '%s' "$INPUT" | jq -r '.toolCall.args.CommandLine // empty' 2>/dev/null) || unchecked "jq could not read the hook input as JSON (adapter)"
    [ -n "$CMD" ] || unchecked "no toolCall.args.CommandLine in the hook input (adapter)"
    CWD=$(printf '%s' "$INPUT" | jq -r '.toolCall.args.Cwd // empty' 2>/dev/null) || unchecked "jq could not read toolCall.args.Cwd (adapter)"
    PROJ="$ROOT"
    RUN_DIR="$ROOT"
    if [ -n "$CWD" ] && [ -d "$CWD" ]; then
      RUN_DIR="$CWD"
      # Outside a repository the gate gets Cwd itself and reports a push there as not checked.
      P=$(git -C "$CWD" rev-parse --show-toplevel 2>/dev/null) && PROJ="$P" || PROJ="$CWD"
    elif [ -n "$CWD" ]; then
      # Cwd is not a directory: the gate still checks the command text, but
      # GIT_DIR names a missing path, so no repository (this one included) is
      # found and a push is reported as not checked.
      PROJ="$CWD"
      export GIT_DIR="$CWD/.git"
    fi
    CLAUDE_INPUT=$(jq -n --arg c "$CMD" '{tool_name: "Bash", tool_input: {command: $c}}') || unchecked "could not build the Claude hook input (adapter)"
    [ -f "$HOOKS/pre-push-gate.sh" ] || unchecked "$HOOKS/pre-push-gate.sh not found (adapter)"
    ERR=$(cd "$RUN_DIR" && printf '%s' "$CLAUDE_INPUT" |
      CLAUDE_PROJECT_DIR="$PROJ" timeout 5 bash "$HOOKS/pre-push-gate.sh" 2>&1 >/dev/null)
    RC=$?
    if [ "$RC" -eq 2 ]; then
      jq -cn --arg r "$ERR" '{decision: "deny", reason: $r}' || unchecked "could not build the deny response (adapter)"
      exit 0
    fi
    [ "$RC" -eq 0 ] && fallback
    LAST=${ERR##*$'\n'}
    unchecked "pre-push-gate.sh exited $RC${LAST:+: ${LAST#pre-push gate: not checked: }} (adapter)"
    ;;
  session-start)
    CTX=""
    if [ -f "$HOOKS/session-start.sh" ]; then
      OUT=$(cd "$ROOT" && printf '%s' '{"source":"startup"}' |
        CLAUDE_PROJECT_DIR="$ROOT" timeout 10 bash "$HOOKS/session-start.sh" 2>/dev/null)
      CTX=$(printf '%s' "$OUT" | jq -r '.hookSpecificOutput.additionalContext // empty' 2>/dev/null) || CTX=""
    fi
    # Antigravity does not follow an instruction to read PROJECT.md, so its
    # contents ride in the session-start message. --rawfile, not --arg: a large
    # file would exceed the single-argument size limit.
    if [ -s "$ROOT/PROJECT.md" ] && [ -r "$ROOT/PROJECT.md" ]; then
      jq -cn --arg c "$CTX" --rawfile p "$ROOT/PROJECT.md" \
        '{injectSteps: [{systemMessage: {systemMessage: ((if $c == "" then "" else $c + "\n" end) + "Project instructions from PROJECT.md at the repository root; follow them as you follow AGENTS.md:\n\n" + $p)}}]}' 2>/dev/null &&
        exit 0
    fi
    [ -n "$CTX" ] || fallback
    jq -cn --arg m "$CTX" '{injectSteps: [{systemMessage: {systemMessage: $m}}]}' || fallback
    ;;
  *)
    fallback
    ;;
esac
exit 0
