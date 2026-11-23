# PROJECT.md — Polyagent Channels

> Product scope and prerequisites: [README.md](README.md)
> Governance: [base guide](AGENTS.md) and [Polyagent Channels development guide](.claude/rules/project/polyagent-channels.md). Clients that do not load `.claude/rules/project/` should read the project guide directly.
> Commands: [REFERENCE.md](REFERENCE.md)

## Overview

Version: [project.version in pyproject.toml](pyproject.toml).

## Environment

Service dependencies and the Python requirement are in [pyproject.toml](pyproject.toml) and [requirements.lock](requirements.lock); the container and Redis definitions are in [compose.channels.yml](.devcontainer/compose.channels.yml) and [Dockerfile.channels](.devcontainer/Dockerfile.channels).

See [Configuration](REFERENCE.md#configuration-devcontainerenv), [Shared key](REFERENCE.md#shared-key), [Disk use](REFERENCE.md#disk-use), [Privilege boundary](REFERENCE.md#privilege-boundary), [Network exposure](REFERENCE.md#network-exposure) and [Client installation](REFERENCE.md#client-installation).

## Known limits

- **Git environment and filesystems for Codex installs**: use Git from the environment that installed the client. The filter requires the Git common directory and `.codex` to share a filesystem. See [What installation adds](REFERENCE.md#what-installation-adds).
- **Container access through the host LAN address**: verified on Windows Docker Desktop only; macOS Docker Desktop and Linux Engine remain unverified.
- **Room membership and session state**: [Reception](REFERENCE.md#reception) describes normal joining. An open session that already saved a position in a topic by publishing there does not start listening when its project joins that topic; `connect` in that session or a new session does. Sessions of a project that left every topic stop their receivers; they observe a later join only at their next turn. A session with no state for a topic follows another session's join for the same member from the stream end at follow time, so its default reads skip intervening messages that the joining session can read while retained.
- **Recipients**: display names and session IDs share one matching space: a session ID can also match another installation's name. The CLI accepts a temporary `connect --name` announcement as a recipient, but that session's receiver matches its installed name, so the accepted send may not wake it. The recipient check lists every announced session, so the check costs more as sessions grow. See [recipient rules](REFERENCE.md#reception) and [CLI publishing](REFERENCE.md#cli).
- **Large modules**: [setup.py](src/polyagent_channels/setup.py) and [cli.py](src/polyagent_channels/cli.py) exceed the guide's small-entity aim, and seven functions exceed 40 lines: `harness.setup_help`, `setup.install`, `setup.uninstall`, `setup.managed_binding`, `cli.command_parser`, `cli.run` and `cli.main`. Splitting the client bindings out of `setup.py` and shortening these functions, without behavior change, remains separate from this release.
- **Redis advisory**: Redis 7.4.11 falls under CVE-2025-46686, which the supplier disputes: an authenticated client can make Redis allocate memory with a multi-bulk command of many arguments, and the record lists versions through 8.0.3. See [Network exposure](REFERENCE.md#network-exposure) for Redis access; the service itself sends fixed commands. The risk is accepted. Moving to Redis 8, with its AOF and license changes, is evaluated for 0.2.0.
- **Remaining guide exceptions**: listed with their limits in [Specific guide conflicts](.claude/rules/project/polyagent-channels.md#specific-guide-conflicts).

## Distribution

- **GitHub** (origin): `mememade-github/polyagent-channels`
- **Attribution**: [README.md § License & attribution](README.md#license--attribution)
- **Package**: [pyproject.toml](pyproject.toml); [MANIFEST.in](MANIFEST.in) lists source-distribution files

---

*Last updated: 2026-11-19*
