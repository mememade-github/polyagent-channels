#!/bin/bash
# Minimal policy tripwire (Codex PreToolUse, matcher: Bash). Blocks a clear top-level
# `git commit` at THIS repo on --no-verify/-n or a missing/stale per-branch marker
# (exists + mtime <= 24h; content ignored). Fail-open: ambiguity -> exit 0; 2 = block.
set -uf
CMD=$(jq -r '.tool_input.command // empty' 2>/dev/null) || exit 0
case "$CMD" in *'<<'*|*'$('*|*'`'*) exit 0 ;; *git*commit*) ;; *) exit 0 ;; esac
CLEAN=$(printf '%s' "$CMD" | sed -e 's/\\./Q/g' -e "s/'[^']*'/Q/g" -e 's/"[^"]*"/Q/g')
case "$CLEAN" in *\'*|*\"*) exit 0 ;; esac
BASE="${CODEX_PROJECT_DIR:-$PWD}"
PROJ=$(git -C "$BASE" rev-parse --show-toplevel 2>/dev/null) || exit 0
INSCOPE=0
while IFS= read -r SEG; do
  set -- $SEG
  [ "${1:-}" = git ] || continue
  shift; D="."
  while [ $# -gt 0 ]; do
    case "$1" in
      -C) D="${2:-}"; shift ;;
      -C?*) D="${1#-C}" ;;
      --git-dir*|--work-tree*) continue 2 ;;
      -c|--namespace|--exec-path|--config-env) shift ;;
      -*) ;;
      *) break ;;
    esac
    [ $# -gt 0 ] && shift
  done
  [ "${1:-}" = commit ] || continue
  shift; NV=0
  while [ $# -gt 0 ]; do
    case "$1" in
      --) break ;;
      --no-verify) NV=1 ;;
      -m|-c|-C|-F|-t|--message|--file|--author|--date|--template|--fixup|--squash|--trailer|--cleanup|--reuse-message|--reedit-message) shift ;;
      --*) ;;
      -?*) case "${1%%[mcCFtuS]*}" in *n*) NV=1 ;; esac; case "$1" in -*[mcCFt]) shift ;; esac ;;
    esac
    [ $# -gt 0 ] && shift
  done
  TARGET=$(cd "$BASE" 2>/dev/null && git -C "${D:-.}" rev-parse --show-toplevel 2>/dev/null) || continue
  [ "$TARGET" = "$PROJ" ] && INSCOPE=1 || continue
  [ "$NV" -eq 1 ] && { echo "Blocked: --no-verify/-n on git commit is not permitted (pre-commit gate)." >&2; exit 2; }
done < <(printf '%s\n' "$CLEAN" | tr ';|&()' '\n')
[ "$INSCOPE" -eq 1 ] || exit 0
BRANCH=$(git -C "$PROJ" rev-parse --abbrev-ref HEAD 2>/dev/null) || exit 0
MARKER="$PROJ/.codex/state/last-verification.$(printf '%s' "$BRANCH" | tr '/' '-')"
if [ -f "$MARKER" ]; then MT=$(stat -c %Y "$MARKER" 2>/dev/null) || exit 0
  [ $(( $(date +%s) - MT )) -le 86400 ] && exit 0; fi
echo "Blocked: verification marker missing or stale (>24h) for branch '$BRANCH'. Run: bash scripts/meta/completion-checker.sh  then retry the commit." >&2
exit 2
