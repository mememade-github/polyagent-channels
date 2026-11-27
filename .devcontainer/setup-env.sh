#!/bin/bash
# =============================================================================
# Polyagent DevContainer — Environment Setup (postCreateCommand)
# =============================================================================
set -e

export LANG=en_US.UTF-8
export LC_ALL=en_US.UTF-8

STEP_TOTAL=6
STEP=0
step() { STEP=$((STEP + 1)); echo "[${STEP}/${STEP_TOTAL}] $1"; }
workspace_marker_ok() {
    root=$1
    [ -d "$root/.git" ] ||
        [ -f "$root/AGENTS.md" ] ||
        [ -f "$root/CLAUDE.md" ] ||
        [ -f "$root/README.md" ] ||
        [ -d "$root/.devcontainer" ]
}

echo "=============================================="
echo "  Polyagent DevContainer Setup"
echo "=============================================="
echo ""

# =============================================================================
# 1. Docker socket + workspace permissions
# =============================================================================
step "Setting permissions..."

if [ -S /var/run/docker.sock ]; then
    SOCK_GID=$(stat -c %g /var/run/docker.sock 2>/dev/null || true)
    DOCKER_GROUP_GID=$(getent group docker 2>/dev/null | cut -d: -f3 || true)
    if [ -z "$SOCK_GID" ]; then
        echo "      WARN: could not read the group of docker.sock; docker access via the socket may be unavailable" >&2
    elif [ "$SOCK_GID" = "0" ]; then
        # Compatibility measure for a daemon that hands over its socket with
        # group 0 (root), which the container user is not in. Applies only
        # when the socket's group is 0; another group is matched by groupmod
        # below and the socket is left alone. Limits: the socket is shared, so
        # this changes its group for every container that mounts it and, where
        # it is the host's own socket file, for the host, where this group
        # number may belong to another group or to none; it is set only when
        # this script runs, so a socket recreated later or changed by another
        # container is not corrected until the next start; the mode is not
        # changed, so a socket without group write permission stays unusable.
        if ! sudo chgrp docker /var/run/docker.sock 2>/dev/null; then
            echo "      WARN: could not set the group of docker.sock to docker; docker access via the socket may be unavailable" >&2
        fi
    elif [ "$SOCK_GID" != "$DOCKER_GROUP_GID" ]; then
        if ! sudo groupmod -g "$SOCK_GID" docker 2>/dev/null; then
            echo "      WARN: could not align docker group with docker.sock GID; docker access via the socket may be unavailable" >&2
        fi
    fi
fi

WS="/workspaces"
if ! workspace_marker_ok "$WS"; then
    echo "      ERROR: /workspaces has no project marker; set HOST_WORKSPACE_PATH for docker compose entrypoints"
fi
# Compatibility measure for mounts whose files appear owned by a user other
# than the container user (e.g. root:root on 9p/drvfs), where Git refuses the
# repository for dubious ownership. Limit: '*' turns Git's ownership check off
# for every directory this user opens, on any filesystem, not only those mounts.
git config --global safe.directory '*' 2>/dev/null || true

# Include linked worktrees and nested application repos; skip dependency trees.
while IFS= read -r -d '' gitdir; do
    repo=$(dirname "$gitdir")
    git -C "$repo" config core.filemode false || { echo "ERROR: cannot configure $repo" >&2; exit 1; }
done < <(find "$WS" \( -name node_modules -o -name .venv \) -prune -o -name .git -prune -print0)


# Command history
if [ -d /commandhistory ]; then
    export HISTFILE=/commandhistory/.bash_history
    touch "$HISTFILE" 2>/dev/null || true
fi
echo "      Done"

# =============================================================================
# 2. SSH (optional)
# =============================================================================
step "SSH setup..."
SSH_DIR="${HOME}/.ssh"
if [ -d "$SSH_DIR" ]; then
    chmod 700 "$SSH_DIR" 2>/dev/null || true
    find "$SSH_DIR" -type f -name "*.pub" -exec chmod 644 {} \; 2>/dev/null || true
    find "$SSH_DIR" -type f -name "known_hosts*" -exec chmod 644 {} \; 2>/dev/null || true
    find "$SSH_DIR" -type f ! -name "*.pub" ! -name "known_hosts*" ! -name "config" -exec chmod 600 {} \; 2>/dev/null || true
    [ -f "$SSH_DIR/config" ] && chmod 644 "$SSH_DIR/config" 2>/dev/null || true
    if find "$SSH_DIR" -type f -print -quit | grep -q .; then
        echo "      SSH files found"
    else
        echo "      No SSH files (optional)"
    fi
else
    echo "      No SSH (optional)"
fi

# =============================================================================
# 3. Claude CLI — idempotent latest-version sync
# =============================================================================
# Skip with SKIP_CLAUDE_UPDATE=1. REFERENCE.md §Tool versioning policy states
# the output and failure handling of steps 3, 4 and 6.
step "Claude CLI version..."
if ! command -v claude &>/dev/null; then
    echo "      WARN: claude CLI not installed — skipping update"
elif [ "${SKIP_CLAUDE_UPDATE:-}" = "1" ]; then
    echo "      Skipped (SKIP_CLAUDE_UPDATE=1), current: $(claude --version 2>/dev/null)"
else
    BEFORE=$(claude --version 2>/dev/null | awk '{print $1}')
    CLAUDE_UPDATE_LOG=$(mktemp)
    if claude update >"$CLAUDE_UPDATE_LOG" 2>&1; then
        AFTER=$(claude --version 2>/dev/null | awk '{print $1}')
        if [ "$BEFORE" = "$AFTER" ]; then
            echo "      $AFTER (already latest)"
        else
            echo "      $BEFORE -> $AFTER"
        fi
    else
        AFTER=$(claude --version 2>/dev/null | awk '{print $1}')
        echo "      WARN: claude update failed; continuing with ${AFTER:-unknown}"
        tail -20 "$CLAUDE_UPDATE_LOG" | sed 's/^/      claude: /'
    fi
    rm -f "$CLAUDE_UPDATE_LOG"
fi

# =============================================================================
# 4. Codex CLI — idempotent latest-version sync
# =============================================================================
# Skip with SKIP_CODEX_UPDATE=1.
# Do NOT use `codex update`: its built-in updater runs `npm install -g
# @openai/codex` against the default global prefix, which the unprivileged
# vscode user cannot write. The Dockerfile installs Codex to ~/.npm-global, so
# mirror that install command here.
step "Codex CLI version..."
CODEX_NPM_PREFIX="${HOME}/.npm-global"
CODEX_REAL_BIN="${CODEX_NPM_PREFIX}/bin/codex"
CODEX_LAUNCHER="/usr/local/bin/codex-launcher"

install_codex_launcher() {
    if [ ! -x "$CODEX_LAUNCHER" ]; then
        echo "      WARN: Codex launcher preflight missing; rebuild container to close startup race"
        return 0
    fi
    mkdir -p "${HOME}/.local/bin"
    ln -sf "$CODEX_LAUNCHER" "${HOME}/.local/bin/codex" 2>/dev/null || true
    if command -v sudo >/dev/null 2>&1; then
        sudo ln -sf "$CODEX_LAUNCHER" /usr/local/bin/codex 2>/dev/null || true
    else
        ln -sf "$CODEX_LAUNCHER" /usr/local/bin/codex 2>/dev/null || true
    fi
}

codex_version() {
    [ -x "$CODEX_REAL_BIN" ] || return 0
    "$CODEX_REAL_BIN" --version 2>/dev/null | awk '{print $2}' || true
}

install_codex_launcher

if [ ! -x "$CODEX_REAL_BIN" ] && ! command -v npm >/dev/null 2>&1; then
    echo "      WARN: codex CLI not installed — skipping update"
elif [ "${SKIP_CODEX_UPDATE:-}" = "1" ]; then
    echo "      Skipped (SKIP_CODEX_UPDATE=1), current: $(codex_version)"
else
    BEFORE=$(codex_version)
    CODEX_UPDATE_LOG=$(mktemp)
    # Status codes are defined in codex-launcher.sh above codex_update_if_needed.
    CODEX_UPDATE_STATUS=0
    "$CODEX_LAUNCHER" --update-only >"$CODEX_UPDATE_LOG" 2>&1 || CODEX_UPDATE_STATUS=$?
    AFTER=$(codex_version)
    case "$CODEX_UPDATE_STATUS" in
        0)
            if [ "$BEFORE" = "$AFTER" ]; then
                echo "      $AFTER (already latest)"
            else
                echo "      $BEFORE → $AFTER"
            fi
            ;;
        3)
            echo "      WARN: could not check the latest Codex version (npm missing or registry unreachable); continuing with ${AFTER:-unknown}"
            ;;
        4)
            echo "      WARN: Codex reinstall failed; restored previous ${AFTER:-unknown}"
            ;;
        5)
            echo "      WARN: Codex reinstall failed; no working codex is installed"
            ;;
        *)
            echo "      WARN: Codex update failed (status $CODEX_UPDATE_STATUS); continuing with ${AFTER:-unknown}"
            ;;
    esac
    if [ "$CODEX_UPDATE_STATUS" != 0 ] && [ "$CODEX_UPDATE_STATUS" != 3 ]; then
        tail -20 "$CODEX_UPDATE_LOG" | sed 's/^/      npm: /'
    fi
    rm -f "$CODEX_UPDATE_LOG"
fi

# =============================================================================
# 5. Codex CLI — obsolete-key warnings and legacy config-link removal
# =============================================================================
# Current Codex loads a trusted project's .codex/config.toml directly. Never
# copy project policy into ~/.codex/config.toml: that user file is persistent,
# higher-scope state and would become stale when the project template changes.
# Authentication and explicit personal overrides remain in ~/.codex/.
step "Codex project config..."
WORKSPACE_CODEX_CONFIG="/workspaces/.codex/config.toml"
USER_CODEX_CONFIG="${HOME}/.codex/config.toml"
if [ -f "$WORKSPACE_CODEX_CONFIG" ]; then
    if grep -q '^ask_for_approval[[:space:]]*=' "$WORKSPACE_CODEX_CONFIG"; then
        echo "      WARN: project config uses obsolete ask_for_approval; use approval_policy"
    else
        echo "      Project config detected — Codex loads it after project trust"
    fi
else
    echo "      WARN: no project-local .codex/config.toml"
fi

if [ -L "$USER_CODEX_CONFIG" ]; then
    if [ "$(readlink "$USER_CODEX_CONFIG")" = "$WORKSPACE_CODEX_CONFIG" ]; then
        rm -f "$USER_CODEX_CONFIG"
        echo "      Removed legacy ~/.codex/config.toml symlink to project config"
    else
        echo "      WARN: ~/.codex/config.toml is a symlink; replace it with explicit user config"
    fi
elif [ -f "$USER_CODEX_CONFIG" ] && grep -q '^ask_for_approval[[:space:]]*=' "$USER_CODEX_CONFIG"; then
    echo "      WARN: existing user config uses obsolete ask_for_approval; use approval_policy"
fi

# =============================================================================
# 6. Antigravity CLI — idempotent latest-version sync
# =============================================================================
# Skip with SKIP_AGY_UPDATE=1.
step "Antigravity CLI version..."
if ! command -v agy &>/dev/null; then
    echo "      WARN: agy CLI not installed — skipping update"
elif [ "${SKIP_AGY_UPDATE:-}" = "1" ]; then
    echo "      Skipped (SKIP_AGY_UPDATE=1), current: $(agy --version 2>/dev/null)"
else
    BEFORE=$(agy --version 2>/dev/null | awk '{print $1}')
    AGY_UPDATE_LOG=$(mktemp)
    if agy update >"$AGY_UPDATE_LOG" 2>&1; then
        AFTER=$(agy --version 2>/dev/null | awk '{print $1}')
        if [ "$BEFORE" = "$AFTER" ]; then
            echo "      $AFTER (already latest)"
        else
            echo "      $BEFORE -> $AFTER"
        fi
    else
        echo "      WARN: agy update failed; continuing with ${BEFORE:-unknown}"
        tail -20 "$AGY_UPDATE_LOG" | sed 's/^/      agy: /'
    fi
    rm -f "$AGY_UPDATE_LOG"
fi

# =============================================================================
# Project-specific setup (separate file)
# Custom per-project setup goes in setup-env.project.sh.
# =============================================================================
PROJECT_SETUP="/usr/local/bin/setup-env.project.sh"
if [ -f "$PROJECT_SETUP" ]; then
    echo ""
    echo "--- Project Setup ---"
    source "$PROJECT_SETUP"
fi

# =============================================================================
# Done
# =============================================================================
echo ""
echo "=============================================="
echo "  Setup Complete!"
echo "=============================================="
echo ""
echo "Start:  claude"
echo ""
echo "Install additional project tools:"
echo "  Go:      sudo apt install -y golang"
echo "  Rust:    curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh"
echo ""
