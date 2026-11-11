# PROJECT.md — Polyagent Channels

> Persistent message channels for users and tool-enabled agents on a trusted network.
> Governance: [CLAUDE.md](CLAUDE.md) (Claude) · [AGENTS.md](AGENTS.md) (Codex and other agents)
> Commands: [REFERENCE.md](REFERENCE.md)

## Overview

A shared message service. Clients exchange persistent messages through Redis Streams over HTTP+JSON, WebSocket, MCP Streamable HTTP or the `channels` CLI. Installed Claude Code and Codex sessions get named tools (`publish`, `receive`, `ack`, `peers`, `connect`, `close`, `status`) and are woken by peer messages while idle. One shared Bearer key authorizes every client; there are no accounts, roles or per-channel ACLs. The service does not launch models or run closed sessions.

Version `0.0.0a0` (tag `v0.0.0-alpha.0`).

## Tech Stack

| Category | Technology |
|----------|-----------|
| Container | Docker Compose (v2.20+), DevContainer spec; inherited Polyagent DevContainer image |
| Runtime | Python 3.12 service; Python 3.10+ client installer (`harness.pyz`) |
| Service | Starlette, FastMCP (Streamable HTTP), WebSocket |
| Storage | Redis 7.4.11 standalone, AOF `everysec`, Lua lifecycle script |
| Clients | Claude Code, Codex CLI, `channels` CLI |
| Tests | pytest (mocked Redis by default; opt-in real-Redis suite) |

## Environment

- **Configuration**: ignored `.devcontainer/.env` (see [REFERENCE.md § Configuration](REFERENCE.md#configuration-devcontainerenv)); defaults need no file.
- **Shared key**: ignored `.env/channel.key`, generated on first start (directory 0700, file 0600); at least 32 printable ASCII characters without whitespace.
- **Persistent volume**: `channel-data` (Redis AOF).
- **Default address**: `http://127.0.0.1:18765` on the Docker host (container port 8080).
- **Client root**: `~/.local/share/polyagent-channels` (installer, wrapper, URL-only config, session state).

## Known limits

- **Large modules**: `setup.py` (1040 lines) and `cli.py` (971 lines) exceed the guide's small-entity aim, and six functions exceed 40 lines: `setup.install`, `setup.uninstall`, `setup.managed_binding`, `cli.command_parser`, `cli.run` and `cli.main`. Splitting the client bindings out of `setup.py` and shortening these functions, without behavior change, is planned for 0.1.x.
- **Remaining guide exceptions**: listed with their limits in [AGENTS.md § Specific guide conflicts](AGENTS.md#specific-guide-conflicts).

## Distribution

- **GitHub** (origin): `mememade-github/polyagent-channels`
- **Upstream**: container files derive from `mememade-github/polyagent-devcontainer` (MIT notice in `.devcontainer/LICENSE`)
- **Package**: `polyagent-channels` (`pyproject.toml`); `MANIFEST.in` lists the documents and notice files for source distributions

---

*Last updated: 2026-11-11*
