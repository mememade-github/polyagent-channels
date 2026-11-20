# =============================================================================
# Polyagent DevContainer — Universal Base Template
# =============================================================================
# Claude Code CLI + Codex CLI + Antigravity CLI + Shell tools + Agent System
# Project language/framework installed by the user (Go, Rust, Java, etc.)
#
# Runtime layout:
#   Claude Code — native binary (~/.local/bin/claude, auto-updated)
#   Codex CLI   — npm global (~/.npm-global/bin/codex)
#   Antigravity — native binary (~/.local/bin/agy, self-updating)
#   Node.js     — Node 22 LTS for Codex CLI infrastructure
#   Python      — system python3 + uv (general development)
#   Project Node.js can be installed via PROJECT_NODE_VERSION
# =============================================================================
FROM mcr.microsoft.com/devcontainers/base:ubuntu-22.04

ARG PROJECT_NODE_VERSION=
ARG USERNAME=vscode

# UTF-8 encoding and timezone
ENV DEBIAN_FRONTEND=noninteractive \
    LANG=en_US.UTF-8 \
    LC_ALL=en_US.UTF-8 \
    LANGUAGE=en_US:en \
    TZ=UTC \
    DEVCONTAINER=true

# =============================================================================
# Layer 1: system packages (root)
# =============================================================================
RUN apt-get update && apt-get install -y --no-install-recommends \
    locales tzdata \
    && sed -i '/en_US.UTF-8/s/^# //g' /etc/locale.gen \
    && locale-gen en_US.UTF-8 \
    && ln -snf /usr/share/zoneinfo/UTC /etc/localtime \
    && echo "UTC" > /etc/timezone \
    && apt-get install -y --no-install-recommends \
    build-essential git git-lfs curl wget less procps sudo ca-certificates gnupg2 openssh-client \
    ripgrep fd-find fzf tree htop vim nano zsh tmux jq unzip zip man-db gh \
    # Python — general development (project code, refinement loop verification)
    python3 python3-pip python3-venv \
    # bubblewrap — Codex CLI sandbox runtime (DevContainer kernel blocks namespaces;
    # vendored fallback used at runtime, but installation removes the PATH warning)
    bubblewrap \
    docker.io \
    && apt-get clean && rm -rf /var/lib/apt/lists/* \
    # Node.js LTS — Codex CLI infrastructure
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g @devcontainers/cli \
    && apt-get clean && rm -rf /var/lib/apt/lists/* \
    && (groupadd docker 2>/dev/null || true) \
    && usermod -aG docker ${USERNAME} \
    # Docker Compose v2 plugin (pinned — bump periodically)
    && mkdir -p /usr/local/lib/docker/cli-plugins \
    && curl -fSL "https://github.com/docker/compose/releases/download/v2.27.0/docker-compose-linux-$(uname -m)" -o /usr/local/lib/docker/cli-plugins/docker-compose \
    && chmod +x /usr/local/lib/docker/cli-plugins/docker-compose

# Python tools — refinement loop verification (project scorer dependency, e.g. .refine/score.sh)
# ruff/pytest/mypy provide deterministic reward signals for the refinement loop.
# Without them, score=0 (loop becomes meaningless).
# --break-system-packages: required since pip 23.0+; Ubuntu 22.04 default pip (22.x) does not need it.
RUN pip3 install --no-cache-dir $(pip3 install --help 2>&1 | grep -q break-system-packages && echo "--break-system-packages") \
    ruff pytest mypy

# =============================================================================
# Layer 2: user setup (root → vscode)
# =============================================================================
RUN echo "${USERNAME} ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers

RUN mkdir -p /commandhistory /workspaces \
    && touch /commandhistory/.bash_history \
    && chown -R ${USERNAME}:${USERNAME} /commandhistory /workspaces

USER ${USERNAME}

# PATH: include static paths so Claude Code + uv always resolve
ENV HOME=/home/${USERNAME} \
    NVM_DIR=/home/${USERNAME}/.nvm \
    PATH=/home/${USERNAME}/.local/bin:${PATH}

# =============================================================================
# Layer 3: Claude Code CLI — Native Installer
# Native binary install. No Node.js required. Auto-updates.
# OOM guard: run the installer from a temp directory (OOM issue when run from Docker root /)
# =============================================================================
RUN mkdir /tmp/claude-install && cd /tmp/claude-install \
    && curl -fsSL https://claude.ai/install.sh | bash \
    && rm -rf /tmp/claude-install \
    && cd ~ \
    && claude --version

# =============================================================================
# Layer 3.5: Codex CLI — npm global install (parity with Claude)
# OpenAI Codex CLI is an npm global package. Installed into the user's
# ~/.npm-global so it can be updated and managed without root. PATH is added in .bashrc.
# =============================================================================
RUN mkdir -p ${HOME}/.npm-global \
    && npm install -g --prefix "${HOME}/.npm-global" @openai/codex \
    && echo 'export PATH="${HOME}/.npm-global/bin:${PATH}"' >> ${HOME}/.bashrc \
    && ${HOME}/.npm-global/bin/codex --version

# =============================================================================
# Layer 3.6: Antigravity CLI (agy) — Official Installer
# Download to a file; the installer checks the manifest sha512.
# Strip only successful I-level glog prefixes; failures and E/W/F keep raw logs.
# =============================================================================
RUN agy_script=$(mktemp) || exit $?; \
    agy_log=; \
    trap 'rm -f "$agy_script" "$agy_log"' 0; \
    trap 'exit 1' HUP INT TERM; \
    agy_log=$(mktemp) || exit $?; \
    curl -fsSL https://antigravity.google/cli/install.sh -o "$agy_script" || exit $?; \
    bash "$agy_script" >"$agy_log" 2>&1; \
    install_rc=$?; \
    if [ "$install_rc" -ne 0 ]; then \
        cat "$agy_log"; \
        exit "$install_rc"; \
    fi; \
    grep -E '^ERROR: logging before google\.Init: [EWF][0-9]{4} ' "$agy_log" >/dev/null; \
    display_rc=$?; \
    case "$display_rc" in \
        0) cat "$agy_log" || exit $? ;; \
        1) sed -E 's/^ERROR: logging before google\.Init: (I[0-9]{4} )/\1/' "$agy_log" || { \
            display_rc=$?; cat "$agy_log"; exit "$display_rc"; \
        } ;; \
        *) cat "$agy_log"; exit "$display_rc" ;; \
    esac; \
    rm -f "$agy_script" "$agy_log" || exit $?; \
    trap - 0; \
    agy --version

# =============================================================================
# Layer 4: Node.js — Project (optional)
# Separate from the system Node.js (Layer 1): nvm is installed only when a
# project-specific version is needed. Unset = system Node.js (LTS) only.
# =============================================================================
RUN if [ -n "${PROJECT_NODE_VERSION}" ]; then \
        mkdir -p ${NVM_DIR} \
        && curl -fsSL https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.1/install.sh | bash \
        && . ${NVM_DIR}/nvm.sh \
        && nvm install ${PROJECT_NODE_VERSION} \
        && nvm alias project-node ${PROJECT_NODE_VERSION} \
        && nvm alias default ${PROJECT_NODE_VERSION}; \
    fi

# =============================================================================
# Layer 5: uv — Python package manager (pinned for reproducibility)
# =============================================================================
RUN curl -LsSf https://astral.sh/uv/0.6.0/install.sh | sh

# =============================================================================
# Layer 6: user configuration (shell, editor, git)
# =============================================================================
# Volume mount points must exist in the image so new named volumes are
# created vscode-owned (a missing mount point yields a root-owned volume).
RUN mkdir -p ${HOME}/.claude ${HOME}/.config/nano ${HOME}/.gemini ${HOME}/.config/antigravity

# .bashrc
RUN echo 'export LANG=en_US.UTF-8' >> ${HOME}/.bashrc \
    && echo 'export LC_ALL=en_US.UTF-8' >> ${HOME}/.bashrc \
    && echo 'export HISTFILE=/commandhistory/.bash_history' >> ${HOME}/.bashrc \
    && echo 'export HISTSIZE=10000' >> ${HOME}/.bashrc \
    && echo 'export HISTFILESIZE=20000' >> ${HOME}/.bashrc \
    && echo 'export NVM_DIR="${HOME}/.nvm"' >> ${HOME}/.bashrc \
    && echo '[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"' >> ${HOME}/.bashrc \
    && echo '# Activate project Node.js (only if nvm exists; reads .nvmrc if present)' >> ${HOME}/.bashrc \
    && echo 'if command -v nvm &>/dev/null; then if [ -f ".nvmrc" ]; then nvm use 2>/dev/null || true; else nvm use project-node 2>/dev/null || true; fi; fi' >> ${HOME}/.bashrc \
    && echo 'export PATH="${HOME}/.local/bin:${PATH}"' >> ${HOME}/.bashrc \
    && echo 'alias ll="ls -alF"' >> ${HOME}/.bashrc \
    && echo 'alias fd="fdfind"' >> ${HOME}/.bashrc

# Vim
RUN echo 'set encoding=utf-8' >> ${HOME}/.vimrc \
    && echo 'set number' >> ${HOME}/.vimrc \
    && echo 'set tabstop=4' >> ${HOME}/.vimrc \
    && echo 'set shiftwidth=4' >> ${HOME}/.vimrc \
    && echo 'set expandtab' >> ${HOME}/.vimrc

# Nano
RUN echo 'set tabsize 4' >> ${HOME}/.nanorc \
    && echo 'set tabstospaces' >> ${HOME}/.nanorc \
    && echo 'set linenumbers' >> ${HOME}/.nanorc

# Git (UTF-8)
RUN git config --global core.quotepath false \
    && git config --global i18n.commitEncoding utf-8 \
    && git config --global i18n.logOutputEncoding utf-8

# =============================================================================
# Setup scripts + Entrypoint
# =============================================================================
USER root
COPY codex-launcher.sh /usr/local/bin/codex-launcher
COPY setup-env.sh /usr/local/bin/
COPY entrypoint.sh /usr/local/bin/
RUN sed -i 's/\r$//' /usr/local/bin/codex-launcher /usr/local/bin/setup-env.sh /usr/local/bin/entrypoint.sh \
    && chmod 755 /usr/local/bin/codex-launcher /usr/local/bin/setup-env.sh /usr/local/bin/entrypoint.sh \
    && mkdir -p /home/${USERNAME}/.local/bin \
    && ln -sf /usr/local/bin/codex-launcher /home/${USERNAME}/.local/bin/codex \
    && ln -sf /usr/local/bin/codex-launcher /usr/local/bin/codex \
    && chown -h ${USERNAME}:${USERNAME} /home/${USERNAME}/.local/bin/codex

USER ${USERNAME}
WORKDIR /workspaces

# ENTRYPOINT: runs on every container start (including docker compose up -d)
# → setup-env.sh auto-runs to set permissions, SSH, and Claude CLI version sync.
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["sleep", "infinity"]
