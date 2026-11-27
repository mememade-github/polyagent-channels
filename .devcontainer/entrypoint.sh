#!/bin/bash
# =============================================================================
# Polyagent DevContainer — Container Entrypoint
# =============================================================================
# Runs on every container start. Both `docker compose up -d` and VS Code's
# "Reopen in Container" path through this script, so environment setup is
# always applied.
#
# VS Code additionally runs postCreateCommand (setup-env.sh) once when the
# container is created. The two runs can overlap in time; only the Codex
# update is serialized (codex-launcher's flock).
# =============================================================================

# Run environment setup (non-fatal if it errors out)
if [ -x "/usr/local/bin/setup-env.sh" ]; then
    /usr/local/bin/setup-env.sh 2>&1 || echo "[entrypoint] WARN: setup-env.sh exited with error (non-fatal)"
fi

# Execute the passed command (default: sleep infinity)
exec "$@"
