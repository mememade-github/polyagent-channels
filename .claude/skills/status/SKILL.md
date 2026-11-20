---
name: status
description: Show repository status, worktrees, WIP, upstream, and marker ages
user-invocable: true
allowed-tools: Bash, Read, Glob, Grep
---

# Status

Resolve the repository root and vendor marker directory:

```bash
WORKSPACE_ROOT="${CLAUDE_PROJECT_DIR:-${CODEX_PROJECT_DIR:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}}"
if [ "${ANTIGRAVITY_AGENT:-}" = "1" ]; then
  WORKSPACE_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
  STATE_DIR="$WORKSPACE_ROOT/.claude"; MARKER_PREFIX=".last-verification."
elif [ -n "${CLAUDE_PROJECT_DIR:-}" ]; then
  STATE_DIR="$WORKSPACE_ROOT/.claude"; MARKER_PREFIX=".last-verification."
elif [ -n "${CODEX_PROJECT_DIR:-}" ] || [ "${CODEX_CI:-}" = "1" ] || [ -n "${CODEX_THREAD_ID:-}" ]; then
  STATE_DIR="$WORKSPACE_ROOT/.codex/state"; MARKER_PREFIX="last-verification."
else
  case "${AGENT_VENDOR:-}" in
    claude|antigravity) STATE_DIR="$WORKSPACE_ROOT/.claude"; MARKER_PREFIX=".last-verification." ;;
    codex) STATE_DIR="$WORKSPACE_ROOT/.codex/state"; MARKER_PREFIX="last-verification." ;;
    *) echo "ERROR: cannot identify Claude, Codex, or Antigravity host; set AGENT_VENDOR=claude|codex|antigravity" >&2; exit 2 ;;
  esac
fi
```

Use `$WORKSPACE_ROOT` for every path. Run `bash "$WORKSPACE_ROOT/scripts/git/git-status.sh" --brief`; report a missing upstream explicitly. The shared script reports the workspace repository and nested Git boundaries, including worktrees; dependency directories are excluded. List `git -C "$WORKSPACE_ROOT" worktree list`, read each `wip/*/README.md`, and list marker ages with:

```bash
for marker in "$STATE_DIR"/"$MARKER_PREFIX"*; do
  [ -f "$marker" ] || continue
  age=$(( $(date +%s) - $(stat -c '%Y' "$marker" 2>/dev/null || echo 0) ))
  echo "$(basename "$marker") ${age}s"
done
```

Summarize repository state, unpushed commits, worktrees, active WIP, and stale markers.
