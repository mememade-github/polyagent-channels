#!/bin/bash
# =============================================================================
# verify-template.sh — on-demand checks of this template's tools and hook
# scripts. NOT a per-commit check.
#
# What it observes:
#   [1/3] tools on this runner — which agent CLIs and helpers are present and
#                                answer --version, and whether a Docker daemon
#                                answers; an absent tool is listed, not failed
#   [2/3] fast checks          — scripts/meta/completion-checker.sh, run once
#   [3/3] hook scripts         — the Claude and Codex hooks and the Antigravity
#                                adapter, fed synthetic JSON input in fixture
#                                repositories; shows each script's exit code
#                                or answer for that input only
#
# What it does not observe: a normal container start, the healthcheck, or a
# real agent session (rule loading, hook invocation and blocking by the agent).
# Observe those separately. No section creates Docker objects.
#
# Output: PASS, FAIL and NOT RUN lines, then a summary naming what failed and
# what did not run. Exit 0: every required check ran and passed. Exit 1: a
# check failed. Exit 2: nothing failed, but a required check could not run.
# =============================================================================
set -u
PROJECT_DIR="${PROJECT_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
FAILED=""; NOTRUN=""; ABSENT=""
pass() { echo "PASS: $1"; }
fail() { echo "FAIL: $1"; FAILED="$FAILED; $1"; }
notrun() { echo "NOT RUN: $1"; NOTRUN="$NOTRUN; $1"; }
absent() { echo "ABSENT: $1"; ABSENT="$ABSENT; $1"; }
TMP=$(mktemp -d "${TMPDIR:-/tmp}/polyagent-checks.XXXXXX") || { echo "FATAL: mktemp failed" >&2; exit 1; }
trap 'rm -rf "$TMP"' EXIT

echo "=============================================="
echo "  Polyagent template checks (tools, fast checks, hook scripts)"
echo "  PROJECT_DIR: $PROJECT_DIR"
echo "=============================================="

# --- [1/3] Tools on this runner ---
echo ""
echo "=== [1/3] Tools on this runner ==="
if command -v docker >/dev/null 2>&1; then
    docker info >/dev/null 2>&1 && pass "docker daemon answers" || absent "docker CLI present, daemon not reachable"
else
    absent "docker CLI not installed"
fi
if command -v claude >/dev/null 2>&1 || [ -x "$HOME/.local/bin/claude" ]; then
    { claude --version >/dev/null 2>&1 || "$HOME/.local/bin/claude" --version >/dev/null 2>&1; } \
        && pass "claude CLI runs" || fail "claude CLI present but broken"
else
    absent "claude CLI not on this runner"
fi
CODEX="$HOME/.npm-global/bin/codex"
command -v codex >/dev/null 2>&1 && CODEX=$(command -v codex)
if [ -x "$CODEX" ]; then
    SKIP_CODEX_UPDATE=1 "$CODEX" --version >/dev/null 2>&1 && pass "codex CLI runs" || fail "codex CLI present but broken"
else
    absent "codex CLI not on this runner"
fi
if command -v agy >/dev/null 2>&1 || [ -x "$HOME/.local/bin/agy" ]; then
    { agy --version >/dev/null 2>&1 || "$HOME/.local/bin/agy" --version >/dev/null 2>&1; } \
        && pass "agy CLI runs" || fail "agy CLI present but broken"
else
    absent "agy CLI not on this runner"
fi
for T in node python3; do
    if command -v "$T" >/dev/null 2>&1; then
        "$T" --version >/dev/null 2>&1 && pass "$T runs" || fail "$T present but broken"
    else
        absent "$T not on this runner"
    fi
done
UV="$HOME/.local/bin/uv"
command -v uv >/dev/null 2>&1 && UV=$(command -v uv)
if [ -x "$UV" ]; then
    "$UV" --version >/dev/null 2>&1 && pass "uv runs" || fail "uv present but broken"
else
    absent "uv not on this runner"
fi

# --- [2/3] Fast checks ---
echo ""
echo "=== [2/3] Fast checks ==="
FC="$PROJECT_DIR/scripts/meta/completion-checker.sh"
if [ -f "$FC" ]; then
    FC_RC=0
    FC_OUT=$(bash "$FC" 2>&1) || FC_RC=$?
    printf '%s\n' "$FC_OUT"
    if [ "$FC_RC" -eq 0 ]; then
        pass "completion-checker.sh (only the checks its OK line names)"
    else
        fail "completion-checker.sh exited $FC_RC"
    fi
else
    notrun "completion-checker.sh not found at $FC"
fi

# --- [3/3] Hook scripts on synthetic input ---
echo ""
echo "=== [3/3] Hook scripts on synthetic input ==="
mkrepo() {   # fixture repo with a born `main` branch (plumbing only, no porcelain)
    git init -q -b main "$1" &&
    T=$(git -C "$1" mktree </dev/null) &&
    C=$(git -C "$1" -c user.email=fixture@example.invalid -c user.name=fixture commit-tree "$T" -m seed) &&
    git -C "$1" update-ref refs/heads/main "$C"
}
try() {   # try <want_rc> <label> <hook> <project_dir> <command>
    IN=$(jq -n --arg c "$5" '{tool_input:{command:$c}}') || { fail "$2 (could not build input)"; return; }
    printf '%s' "$IN" | CLAUDE_PROJECT_DIR="$4" CODEX_PROJECT_DIR="$4" bash "$3" >/dev/null 2>&1
    rc=$?
    if [ "$rc" -eq "$1" ]; then pass "$2"; else fail "$2 (want exit $1, got $rc)"; fi
}
R="$TMP/repo"; SC="$TMP/storedcred"
if ! command -v git >/dev/null 2>&1 || ! command -v jq >/dev/null 2>&1; then
    notrun "hook scripts: git and jq are required"
elif ! mkrepo "$R" >/dev/null 2>&1 || ! mkrepo "$SC" >/dev/null 2>&1; then
    notrun "hook scripts: could not create fixture repositories"
else
    mkdir -p "$R/.claude" || fail "hook fixtures: mkdir failed"
    CRED="ghp_""aaaaaaaaaaaaaaaaaaaaaaaaaa"   # fragment-built fake token; matches the gate's shape check only
    for V in claude codex; do
        if [ "$V" = claude ]; then HOOKS="$PROJECT_DIR/.claude/hooks"; else HOOKS="$PROJECT_DIR/.codex/hooks"; fi
        PPG="$HOOKS/pre-push-gate.sh"
        try 2 "$V pre-push: raw credential-shaped text blocked"      "$PPG" "$R" "curl -H token $CRED https://example.com"
        try 0 "$V pre-push: plain push allowed"                      "$PPG" "$R" 'git push origin main'
        # Session-start hook: context-only, valid JSON, exit 0.
        OUT=$(printf '{"source":"startup"}' | CLAUDE_PROJECT_DIR="$R" CODEX_PROJECT_DIR="$R" bash "$HOOKS/session-start.sh" 2>/dev/null); rc=$?
        if [ "$rc" -eq 0 ] && printf '%s' "$OUT" | jq -er '.hookSpecificOutput.additionalContext | length > 0' >/dev/null 2>&1; then
            pass "$V session-start: context JSON emitted, exit 0"
        else
            fail "$V session-start: context JSON (rc=$rc)"
        fi
    done
    # Antigravity: run the commands registered in the real .agents/hooks.json from
    # the fixture's .agents/ (agy's hook working directory). The fixture links the
    # real adapter and Claude hooks.
    AH="$PROJECT_DIR/.agents/hooks.json"
    mkdir -p "$R/.agents" &&
        ln -s "$PROJECT_DIR/.agents/hooks" "$R/.agents/hooks" &&
        ln -s "$PROJECT_DIR/.claude/hooks" "$R/.claude/hooks" ||
        fail "agy fixtures: could not link the adapter and Claude hooks"
    ptu() { jq -cn --arg c "$1" --arg d "$R" '{toolCall: {name: "run_command", args: {CommandLine: $c, Cwd: $d}}}'; }
    agy_try() {   # agy_try <want> <label> <PreToolUse|SessionStart> [command]
        if [ "$3" = PreToolUse ]; then
            IN=$(ptu "$4") || { fail "$2 (could not build input)"; return; }
            HC=$(jq -r '[.[] | (.PreToolUse? // [])[] | select(.matcher == "run_command") | .hooks[].command] | first // empty' "$AH" 2>/dev/null)
        else
            IN='{}'
            HC=$(jq -r --arg e "$3" '[.[] | (.[$e]? // [])[] | .command] | first // empty' "$AH" 2>/dev/null)
        fi
        hrc=$?
        if [ "$hrc" -ne 0 ]; then fail "$2 (could not read .agents/hooks.json, jq=$hrc)"; return; fi
        if [ -z "$HC" ]; then fail "$2 (no $3 command in .agents/hooks.json)"; return; fi
        OUT=$(cd "$R/.agents" && printf '%s' "$IN" | sh -c "$HC" 2>/dev/null); rc=$?
        if [ "$3" = SessionStart ]; then
            GOT=$(printf '%s' "$OUT" | jq -er '.injectSteps[0].systemMessage.systemMessage | select(length > 0) | "injectSteps"' 2>/dev/null)
        else
            GOT=$(printf '%s' "$OUT" | jq -er '.decision' 2>/dev/null)
        fi
        jrc=$?
        if [ "$rc" -eq 0 ] && [ "$jrc" -eq 0 ] && [ "$GOT" = "$1" ]; then pass "$2"; else fail "$2 (want $1, got '${GOT:-invalid JSON}', rc=$rc, jq=$jrc)"; fi
    }
    agy_try deny "agy pre-push: raw credential-shaped text denied"     PreToolUse "curl -H token $CRED https://example.com"
    agy_try ask  "agy pre-push: plain push asks"                       PreToolUse 'git push origin main'
    agy_try ask  "agy pre-tool-use: unrelated command asks"            PreToolUse 'ls -la'
    agy_try injectSteps "agy session-start: context injected"          SessionStart
    # Sanctioned variable-reference credential helper passes the stored-config scan;
    # a literal token in stored config blocks.
    if git -C "$R" config credential.helper '!f(){ . "$HOME"/.tokens.env >/dev/null 2>&1; echo username=x-access-token; echo "password=${GITHUB_PAT}"; };f'; then
        try 0 "claude pre-push: variable-reference helper allowed" "$PROJECT_DIR/.claude/hooks/pre-push-gate.sh" "$R" 'git push origin main'
        try 0 "codex pre-push: variable-reference helper allowed"  "$PROJECT_DIR/.codex/hooks/pre-push-gate.sh" "$R" 'git push origin main'
    else
        fail "pre-push: variable-reference helper cases (git config failed)"
    fi
    if git -C "$SC" config credential.helper "!f(){ echo password=$CRED; };f"; then
        try 2 "claude pre-push: literal token in stored config blocked" "$PROJECT_DIR/.claude/hooks/pre-push-gate.sh" "$SC" 'git push origin main'
        try 2 "codex pre-push: literal token in stored config blocked"  "$PROJECT_DIR/.codex/hooks/pre-push-gate.sh" "$SC" 'git push origin main'
    else
        fail "pre-push: stored-token cases (git config failed)"
    fi
    # A push-shaped command whose repository cannot be found is not checked:
    # one stderr line, exit 1 from the Claude gate and 0 from the Codex gate.
    try 1 "claude pre-push: push in a missing directory not checked (exit 1)" "$PROJECT_DIR/.claude/hooks/pre-push-gate.sh" "$R" "git -C $TMP/missing push origin main"
    try 0 "codex pre-push: push in a missing directory not checked (exit 0)"  "$PROJECT_DIR/.codex/hooks/pre-push-gate.sh" "$R" "git -C $TMP/missing push origin main"
fi

# --- Summary ---
echo ""
echo "=============================================="
[ -z "$ABSENT" ] || echo "  Absent on this runner (not required): ${ABSENT#; }"
[ -z "$FAILED" ] || echo "  FAILED: ${FAILED#; }"
[ -z "$NOTRUN" ] || echo "  NOT RUN (required): ${NOTRUN#; }"
echo "  Not observed by this script: normal container start, healthcheck, real agent sessions."
if [ -n "$FAILED" ]; then
    echo "  RESULT: failed"; echo "=============================================="; exit 1
elif [ -n "$NOTRUN" ]; then
    echo "  RESULT: incomplete (a required check did not run)"; echo "=============================================="; exit 2
fi
echo "  RESULT: every required check ran and passed (only the checks listed above)"
echo "=============================================="
