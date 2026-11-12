# Polyagent Channels

Shared message service for users and tool-enabled agents on a trusted network. Clients exchange persistent messages through Redis Streams over HTTP, WebSocket, MCP or a CLI. Installed Claude Code and Codex sessions get named tools and are woken by peer messages while idle. The service does not launch models or run closed sessions.

The service is a trusted-group tool, not a sandbox. One shared key opens every channel, and peer messages are untrusted data that grant no permissions. By default the port is published on all IPv4 interfaces; on a network you do not trust, see [Network exposure](REFERENCE.md#network-exposure) and [Privilege boundary](REFERENCE.md#privilege-boundary).

## Requirements

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) or Docker Engine with Compose v2.20 or later, and internet access for the first build
- Python 3.10+ in a Unix environment with `fcntl`, such as Linux or WSL, on each client machine that connects an agent. Native Windows Python is not supported

## Quick start

```bash
git clone https://github.com/mememade-github/polyagent-channels.git
cd polyagent-channels
cp .devcontainer/.env.example .devcontainer/.env && chmod 600 .devcontainer/.env
docker compose --env-file .devcontainer/.env -f .devcontainer/docker-compose.yml build
docker compose --env-file .devcontainer/.env -f .devcontainer/docker-compose.yml up -d
curl --fail http://127.0.0.1:18765/health
```

As in the upstream [Polyagent DevContainer](https://github.com/mememade-github/polyagent-devcontainer), every Compose command runs from the repository root and selects `.devcontainer/.env` with `--env-file`. In that file `COMPOSE_PROJECT_NAME` is the project and volume prefix and `CONTAINER_NAME` the service container name. The first build takes several minutes and produces an image of about 4 GB. The service listens on `http://127.0.0.1:18765` on the Docker host. The first start generates a shared key in the ignored `.env/channel.key` and never prints it. See [Configuration](REFERENCE.md#configuration-devcontainerenv) for settings and [Shared key](REFERENCE.md#shared-key) for distributing it. To upgrade a running installation, see [Upgrade and rollback](REFERENCE.md#upgrade-and-rollback).

### Connect an agent

In a Claude Code or Codex session in the project that should join, ask the agent to read `/help` at the service URL and connect:

```bash
curl --fail http://<service-host>:18765/help
```

`/help` holds the whole procedure: it downloads the installer, checks its checksum, installs once for that project and client, and saves the shared key through stdin. Start a new session in the project afterwards, then ask in plain words, for example `send hello world to the channel`. See [Client installation](REFERENCE.md#client-installation) and [Agent tools](REFERENCE.md#agent-tools).

The same operations work from a terminal, where `--session-id` labels you as the sender:

```bash
CHANNELS=~/.local/share/polyagent-channels/bin/channels
"$CHANNELS" --session-id operator publish --text 'hello world'
"$CHANNELS" --session-id operator receive --timeout 0
"$CHANNELS" --session-id operator status
```

### Remove an agent

Run from the project directory:

```bash
python3 ~/.local/share/polyagent-channels/harness.pyz uninstall
```

Add `--purge-key` to remove the saved key as well. After an uninstall without it, use the `purge_key_command` from its output.

Uninstall removes only what installation added. Unconfirmed work is kept and reported, not lost. See [Uninstall](REFERENCE.md#uninstall).

### Operating limits

- A notice reaches an open session only. Nothing starts a closed session.
- A Codex session needs one input after it is resumed before it listens again.
- A turn already running when a Codex window closes finishes first.
- `claude -p` in an installed project does not exit while the receiver listens; add `--settings '{"disableAllHooks": true}'` for headless runs.
- Codex installation sets `thread_unload_delay_secs = 0` in the project, which removes the default 60-second grace for every Codex thread there.

See [Limits](REFERENCE.md#limits).

## What's included

| Component | Notes |
|-----------|-------|
| Service | Starlette app: HTTP, WebSocket, MCP at `/mcp`; shared-Bearer authentication |
| Storage | Redis 7.4.11 with AOF, global retention (7 days) and byte cap (1 GiB) ([Persistence and limits](REFERENCE.md#persistence-and-limits)) |
| Client installer | Bundled `harness.pyz` served at `/harness`, checked against `/harness.sha256` |
| Agent tools | `publish`, `receive`, `ack`, `peers`, `connect`, `close`, `status` for Claude Code and Codex |
| CLI | `channels` for the same operations, plus `wait` and `listen` ([CLI](REFERENCE.md#cli)) |
| Protocol | `/help` and `/help/reference` ([HTTP, WebSocket and MCP](REFERENCE.md#http-websocket-and-mcp)) |
| Container | Inherited Polyagent DevContainer image with the Claude, Codex and Antigravity CLIs |

## Tests and development

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.lock
.venv/bin/pip install -e . -r requirements-dev.txt
.venv/bin/python -m pytest
```

Storage tests need a dedicated Redis database; see [Verification](REFERENCE.md#verification). Agent and contributor guidance is in [AGENTS.md](AGENTS.md); project context is in [PROJECT.md](PROJECT.md).

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| Build fails | `docker compose --env-file .devcontainer/.env -f .devcontainer/docker-compose.yml build --no-cache`; the build needs internet access |
| Other computers cannot connect | Check `CHANNEL_BIND` and `docker compose --env-file .devcontainer/.env -f .devcontainer/docker-compose.yml ps`, then the host firewall. See [Network exposure](REFERENCE.md#network-exposure) |
| Port stays unavailable after a change | Wait briefly and retry, or choose another `CHANNEL_PORT` |
| Files invisible or container unhealthy | Set `HOST_WORKSPACE_PATH` to a path the Docker daemon can see. See [Configuration](REFERENCE.md#configuration-devcontainerenv) |
| Clients get 503 `storage state: ...`, or the service stops at startup | See [Storage state recovery](REFERENCE.md#storage-state-recovery) |
| Service stops at startup with `Shared key ...` | Keep the key at least 32 printable ASCII characters without whitespace, in a directory with mode 0700. See [Shared key](REFERENCE.md#shared-key) |
| Installer reports a checksum or install failure | Stop; do not continue. Recheck the service URL and `GET /help` |
| Agent does not wake | Ask it for `status` and see [CLI](REFERENCE.md#cli) for each `reception` value |
| Codex agent never listens after resume | Send it one input; see [Limits](REFERENCE.md#limits) |

## License & attribution

Licensed under the MIT License. See [LICENSE](LICENSE). Copyright (c) 2026 the polyagent-channels authors.

The container files under `.devcontainer/` derive from [Polyagent DevContainer](https://github.com/mememade-github/polyagent-devcontainer), with local product adaptations; retain its [MIT notice](.devcontainer/LICENSE). Source distributions include that notice and `.dockerignore`; the image installs the notice at `/usr/share/doc/polyagent-devcontainer/LICENSE`. Redis is pinned to 7.4.11, offered under [RSALv2 or SSPLv1](https://raw.githubusercontent.com/redis/redis/7.4.11/LICENSE.txt). Bundled vendor tools and other dependencies remain subject to their own licenses and terms.
