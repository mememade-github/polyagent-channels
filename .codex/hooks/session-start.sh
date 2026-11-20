#!/bin/bash
# SessionStart hook: inject project context (branch, memory, WIP, environment).
# Context-only: never blocks a session (always exit 0).
set -u

INPUT=$(cat)
SOURCE=$(echo "$INPUT" | jq -r '.source // "startup"' 2>/dev/null || echo "startup")
PROJECT_DIR="${CODEX_PROJECT_DIR:-.}"
STATE_DIR="$PROJECT_DIR/.codex/state"
GIT_ROOT=$(git -C "$PROJECT_DIR" rev-parse --show-toplevel 2>/dev/null || echo "$PROJECT_DIR")
CONTEXT=""
mkdir -p "$STATE_DIR" 2>/dev/null || true

# 1. Git status summary
if command -v git >/dev/null 2>&1 && [ -e "$GIT_ROOT/.git" ]; then
  BRANCH=$(git -C "$GIT_ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "unknown")
  DIRTY=$(git -C "$GIT_ROOT" status --porcelain 2>/dev/null | wc -l | tr -d ' ')
  CONTEXT="${CONTEXT}Git branch: ${BRANCH} (${DIRTY} uncommitted changes)\n"
fi

# 2. Known issues from auto memory
if [ -f "$GIT_ROOT/MEMORY.md" ]; then
  CONTEXT="${CONTEXT}Known issues from MEMORY.md:\n$(head -20 "$GIT_ROOT/MEMORY.md" | sed 's/^/  /')\n"
fi

# 3. Active WIP tasks — resume only when the current request permits
if [ -d "$GIT_ROOT/wip" ]; then
  WIP_DIRS=("$GIT_ROOT"/wip/*/)
  if [ -d "${WIP_DIRS[0]}" ]; then
    CONTEXT="${CONTEXT}Active WIP tasks:\n"
    for d in "${WIP_DIRS[@]}"; do
      CONTEXT="${CONTEXT}  - $(basename "$d")\n"
      [ -f "$d/README.md" ] && CONTEXT="${CONTEXT}$(head -5 "$d/README.md" | sed 's/^/    /')\n"
    done
    CONTEXT="${CONTEXT}\nWIP tasks detected. Follow the current request; when it permits resuming saved work, read the relevant WIP README.md and continue its first actionable item.\n"
  fi
fi

# 4. Environment quick check
ENV_ISSUES=""
[ ! -S /var/run/docker.sock ] && ENV_ISSUES="${ENV_ISSUES}  - Docker socket not available\n"
[ ! -f "$GIT_ROOT/.devcontainer/.env" ] && ENV_ISSUES="${ENV_ISSUES}  - .devcontainer/.env missing (copy .devcontainer/.env.example)\n"
[ -f /.dockerenv ] && ! command -v agy >/dev/null 2>&1 && ENV_ISSUES="${ENV_ISSUES}  - agy not found in container; see UPGRADING.md\n"
[ -n "$ENV_ISSUES" ] && CONTEXT="${CONTEXT}Environment issues:\n${ENV_ISSUES}"

# 5. Stale markers: existence+age semantics — prune anything older than 7 days.
find "$STATE_DIR" -maxdepth 1 \( -name 'last-verification.*' \
  -o -name 'refinement-active' -o -name 'stop-blocked-refinement.*' \) \
  -type f -mtime +7 -delete 2>/dev/null

# 6. Environment info
if [ -f /.dockerenv ]; then
  OS_INFO=$(. /etc/os-release 2>/dev/null && echo "$NAME $VERSION_ID" || echo "Linux")
  CONTEXT="${CONTEXT}Environment: Dev Container (${OS_INFO})\n"
else
  CONTEXT="${CONTEXT}Environment: Host ($(uname -s))\n"
fi
CONTEXT="${CONTEXT}Hook source: ${SOURCE}\nUser: $(whoami)\n"

jq -n --arg ctx "$(echo -e "$CONTEXT")" \
  '{hookSpecificOutput: {hookEventName: "SessionStart", additionalContext: $ctx}}' || true

exit 0
