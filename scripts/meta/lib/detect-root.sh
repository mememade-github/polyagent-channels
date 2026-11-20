#!/bin/bash
# =============================================================================
# lib/detect-root.sh — worktree-aware workspace-root detection (sourced helper)
# =============================================================================
# Sourced by karpathy-consistency-check.sh.
#
# Usage (from a script in scripts/meta/):
#   SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
#   . "$SCRIPT_DIR/lib/detect-root.sh"
#   ROOT="$(detect_root "${1:-}")"
#
# Resolution order:
#   1. explicit $1 argument            (skipped if a transient worktree path)
#   2. $CLAUDE_PROJECT_DIR             (skipped if a transient worktree path)
#   3. git --git-common-dir           (a linked worktree resolves to real ROOT)
#   4. git --show-toplevel
#   5. three levels up from this lib   (scripts/meta/lib -> ROOT)
#
# Why the worktree guard: a transient worktree under .../.claude/worktrees/<name>
# is a checkout of ROOT itself, not a receiver; resolving ROOT to a worktree
# path would make the oracle measure that transient checkout instead of the
# real workspace. Rejecting worktree paths from (1)/(2)
# lets (3) resolve the real workspace root. NOTE: callers may pass ROOT as $1,
# so BOTH the arg and env branches must be guarded.
#
# This file is meant to be SOURCED (no side effects beyond defining detect_root
# and one namespaced variable); it deliberately does not set shell options so it
# inherits the caller's `set` flags.
# =============================================================================

_DETECT_ROOT_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

detect_root() {
    if [ -n "${1:-}" ] && [ -d "${1:-}" ] && [[ "$1" != */.claude/worktrees/* ]]; then echo "$1"; return; fi
    if [ -n "${CLAUDE_PROJECT_DIR:-}" ] && [ -d "$CLAUDE_PROJECT_DIR" ] && [[ "$CLAUDE_PROJECT_DIR" != */.claude/worktrees/* ]]; then echo "$CLAUDE_PROJECT_DIR"; return; fi
    if command -v git >/dev/null 2>&1; then
        local gc tl
        gc=$(git -C "$_DETECT_ROOT_LIB_DIR" rev-parse --git-common-dir 2>/dev/null || true)
        if [ -n "$gc" ] && [ "$gc" != ".git" ]; then (cd "$_DETECT_ROOT_LIB_DIR" && cd "$(dirname "$gc")" && pwd); return; fi
        tl=$(git -C "$_DETECT_ROOT_LIB_DIR" rev-parse --show-toplevel 2>/dev/null || true)
        if [ -n "$tl" ]; then echo "$tl"; return; fi
    fi
    (cd "$_DETECT_ROOT_LIB_DIR/../../.." && pwd)
}
