#!/bin/bash
set -e

CODEX_NPM_PREFIX="${CODEX_NPM_PREFIX:-${HOME}/.npm-global}"
CODEX_REAL_BIN="${CODEX_REAL_BIN:-${CODEX_NPM_PREFIX}/bin/codex}"
CODEX_LOCK="${CODEX_LOCK:-${CODEX_NPM_PREFIX}/.codex-update.lock}"

codex_real_version() {
    [ -x "$CODEX_REAL_BIN" ] || return 0
    "$CODEX_REAL_BIN" --version 2>/dev/null | awk '{print $2}' || true
}

codex_latest_version() {
    command -v npm >/dev/null 2>&1 || return 0
    npm view @openai/codex version 2>/dev/null || true
}

codex_healthy() {
    [ -x "$CODEX_REAL_BIN" ] && "$CODEX_REAL_BIN" --version >/dev/null 2>&1
}

# Reinstall @openai/codex without destroying a working install. The real binary
# ships as a nested platform optional dep (@openai/codex-<os>-<arch>); npm's
# default in-place reinstall renames the existing package dir aside, which on
# 9p/overlay filesystems fails with ENOTEMPTY and leaves the platform binary
# uninstalled -- a half-install that only throws at run time. So move the current
# package aside ourselves first: a plain rename to a fresh name never hits
# ENOTEMPTY, and npm then installs into an empty target and never performs its
# own failing rename. If the install does not yield a healthy codex, restore the
# preserved package -- a failed reinstall must never leave the prefix with no
# codex at all (npm view can succeed and npm install still fail).
codex_clean_install() {
    pkg="$CODEX_NPM_PREFIX/lib/node_modules/@openai/codex"
    bak="$CODEX_NPM_PREFIX/.codex-pkg-bak"
    rm -rf "$bak" "$CODEX_NPM_PREFIX"/lib/node_modules/@openai/.codex-* 2>/dev/null || true
    [ -e "$pkg" ] && mv "$pkg" "$bak"
    if npm install -g --prefix "$CODEX_NPM_PREFIX" @openai/codex@latest && codex_healthy; then
        rm -rf "$bak" 2>/dev/null || true
        return 0
    fi
    echo "codex launcher: reinstall failed; keeping previous install" >&2
    rm -rf "$pkg" 2>/dev/null || true
    mkdir -p "$(dirname "$pkg")"
    [ -e "$bak" ] && mv "$bak" "$pkg"
    return 1
}

codex_update_if_needed() {
    [ "${SKIP_CODEX_UPDATE:-}" = "1" ] && return 0
    command -v npm >/dev/null 2>&1 || return 0
    mkdir -p "$CODEX_NPM_PREFIX"

    current="$(codex_real_version)"
    latest="$(codex_latest_version)"
    # Offline guard: registry unreachable but a working binary exists -> do
    # nothing. MUST precede codex_clean_install and MUST test -x (not
    # codex_healthy): if offline AND half-installed, a clean install would
    # delete the old package and then fail (no registry), removing codex
    # entirely. Never delete while offline.
    if [ -z "$latest" ] && [ -x "$CODEX_REAL_BIN" ]; then
        return 0
    fi
    # Version drift OR half-install (unhealthy) -> clean reinstall. Do not let a
    # failed reinstall abort under set -e: codex_clean_install restores the prior
    # install on failure, and the functional gate below is the real arbiter.
    if [ -z "$latest" ] || ! codex_healthy || { [ -n "$current" ] && [ "$current" != "$latest" ]; }; then
        codex_clean_install || true
    fi
    # Functional gate: npm can exit 0 yet leave a half-install on 9p, so judge
    # by actually running the binary. This return propagates through
    # --update-only to setup-env's WARN + npm-log-tail path.
    codex_healthy
}

codex_update_locked() {
    if command -v flock >/dev/null 2>&1; then
        ( flock 9; codex_update_if_needed ) 9>"$CODEX_LOCK"
    else
        codex_update_if_needed
    fi
}

mkdir -p "$CODEX_NPM_PREFIX"
if [ "${1:-}" = "--update-only" ]; then
    codex_update_locked
    exit $?
fi

codex_update_locked >/dev/null 2>&1 || true

if [ ! -x "$CODEX_REAL_BIN" ]; then
    echo "codex launcher: missing executable: $CODEX_REAL_BIN" >&2
    exit 127
fi

exec "$CODEX_REAL_BIN" "$@"
