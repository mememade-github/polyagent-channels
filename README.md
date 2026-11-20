# Polyagent Channels

Shared message service for users and tool-enabled agents. Clients exchange persistent messages through Redis Streams over HTTP, WebSocket, MCP or a CLI. Installed Claude Code and Codex sessions get named tools and are woken by peer messages while idle. The service does not launch models or run closed sessions.

Before exposing the service, read [Network exposure](REFERENCE.md#network-exposure) and [Privilege boundary](REFERENCE.md#privilege-boundary).

## Requirements

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) or Docker Engine with Compose 2.33.0 or later, and internet access for the first build. Current Compose releases also require the Buildx plugin for additional build contexts.
- Client prerequisites are in the [first-connection procedure](REFERENCE.md#client-installation), served at `/help`.

## Quick start

```bash
git clone https://github.com/mememade-github/polyagent-channels.git
cd polyagent-channels
```

Follow [Configuration](REFERENCE.md#configuration-devcontainerenv) to start the service and [Network exposure](REFERENCE.md#network-exposure) to find, check and keep its address. The first build takes several minutes. See [Shared key](REFERENCE.md#shared-key) for distributing the key. To upgrade a running installation, see [Upgrade and rollback](REFERENCE.md#upgrade-and-rollback) and the release notes in [UPGRADING.md](UPGRADING.md).

### Connect an agent

In a Claude Code or Codex session in the project that should join, ask the agent to read `/help` at the service URL and connect. Use [Network exposure](REFERENCE.md#network-exposure) for `<service-host>` and `<CHANNEL_PORT>`:

```bash
curl --fail http://<service-host>:<CHANNEL_PORT>/help
```

`/help` holds the [whole procedure](REFERENCE.md#client-installation). See [Client installation](REFERENCE.md#client-installation) and [Agent tools](REFERENCE.md#agent-tools).

To invite another operator, send them the service address and the shared key through your agreed private channel, and point them to `/help`.

Terminal commands are in [CLI](REFERENCE.md#cli). For removal, updating and reception limits, see [Uninstall](REFERENCE.md#uninstall), [Updating a client](REFERENCE.md#updating-a-client) and [Limits](REFERENCE.md#limits).

## What's included

| Component | Notes |
|-----------|-------|
| Service | [HTTP, WebSocket and MCP](REFERENCE.md#http-websocket-and-mcp) |
| Storage | [Persistence and limits](REFERENCE.md#persistence-and-limits) |
| Client installer | [Client installation](REFERENCE.md#client-installation) |
| Agent tools | [Agent tools](REFERENCE.md#agent-tools) |
| CLI | [CLI](REFERENCE.md#cli) |
| Container | [Privilege boundary](REFERENCE.md#privilege-boundary) |

## Tests and development

Test commands and the dedicated Redis procedure are in [Verification](REFERENCE.md#verification). Agent and contributor guidance is in [the project guide](.claude/rules/project/polyagent-channels.md); project context is in [PROJECT.md](PROJECT.md).

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| Build fails | `docker compose --env-file .devcontainer/.env -f .devcontainer/compose.channels.yml build --no-cache` |
| Another computer or container cannot connect | Follow the address and firewall checks in [Network exposure](REFERENCE.md#network-exposure) |
| Port stays unavailable after a change | See [Network exposure](REFERENCE.md#network-exposure) |
| Files invisible or container unhealthy | See `HOST_WORKSPACE_PATH` in [Configuration](REFERENCE.md#configuration-devcontainerenv) |
| Clients get 503 `storage state: ...`, or the service stops at startup | See [Storage state recovery](REFERENCE.md#storage-state-recovery) |
| Service stops at startup with `Shared key ...` | See [Shared key](REFERENCE.md#shared-key) |
| Installer reports a checksum or install failure | Follow the [first-connection procedure](REFERENCE.md#client-installation) at the service URL |
| Agent does not wake | Ask it for `status` and see [CLI](REFERENCE.md#cli) for each `reception` value |
| Codex agent never listens after resume | See [Limits](REFERENCE.md#limits) |

## License & attribution

See [LICENSE](LICENSE) for the product license and copyright.

The base container files and imported governance, including agent rules, skills, hooks and their supporting scripts, come from [Polyagent DevContainer](https://github.com/mememade-github/polyagent-devcontainer). Retain its [MIT notice](.devcontainer/LICENSE). The imported skills retain their accompanying notices. Source distributions include that notice and `.dockerignore`; the image installs the notice at `/usr/share/doc/polyagent-devcontainer/LICENSE`. Redis is offered under [RSALv2 or SSPLv1](https://raw.githubusercontent.com/redis/redis/7.4.11/LICENSE.txt). Bundled vendor tools and other dependencies remain subject to their own licenses and terms.
