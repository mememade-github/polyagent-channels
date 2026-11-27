---
name: status
description: Show repository status, worktrees, WIP, and upstream
user-invocable: true
allowed-tools: Bash, Read, Glob, Grep
---

# Status

Resolve the repository root:

```bash
if [ "${ANTIGRAVITY_AGENT:-}" = "1" ]; then
  WORKSPACE_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
else
  WORKSPACE_ROOT="${CLAUDE_PROJECT_DIR:-${CODEX_PROJECT_DIR:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}}"
fi
```

Use `$WORKSPACE_ROOT` for every path. Run `bash "$WORKSPACE_ROOT/scripts/git/git-status.sh" --brief`; report a missing upstream explicitly. The shared script reports the workspace repository and nested Git boundaries, including worktrees; dependency directories are excluded. List `git -C "$WORKSPACE_ROOT" worktree list` and read each `wip/*/README.md`.

Summarize repository state, unpushed commits, worktrees, and active WIP.
