#!/bin/bash
# =============================================================================
# Polyagent DevContainer — Container Entrypoint
# =============================================================================
# Runs on every container start. Both `docker compose up -d` and VS Code's
# "Reopen in Container" path through this script, so environment setup is
# always applied.
# =============================================================================

# Run environment setup (non-fatal if it errors out)
if [ -x "/usr/local/bin/setup-env.sh" ]; then
    /usr/local/bin/setup-env.sh 2>&1 || echo "[entrypoint] WARN: setup-env.sh exited with error (non-fatal)"
fi

exec "$@"
