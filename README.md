# Polyagent Channels

A shared message service for users and tool-enabled agents on a trusted network. HTTP+JSON, WebSocket, MCP Streamable HTTP and CLI clients exchange persistent messages through Redis Streams. It does not launch models or control agent sessions.

[Quick start](#build-and-start) · [First exchange](#cli) · [Integrations](#http-websocket-and-mcp) · [Operations](#persistence-and-limits) · [Tests](#tests) · [Development](AGENTS.md) · [Licensing](#licensing)

## Build and start

Install Docker with Compose v2.20 or later, download this repository, and run from its root:

```sh
docker compose up -d --build
```

The image builds from a public Microsoft Ubuntu DevContainer base and installs the development tools and this product; no sibling repository or local image is needed. Building requires internet access. Redis starts with AOF enabled. Vendor CLI auto-updates are disabled at service startup. VS Code “Reopen in Container” uses the same service lifecycle.

The service image keeps the inherited Polyagent DevContainer shape. It is about 4 GB, the `vscode` user has passwordless sudo (`NOPASSWD:ALL`), and the Claude, Codex and Antigravity (`agy`) CLIs, the Docker CLI and Node are installed. The build fetches the provider installers at their latest, unpinned versions, so a rebuild is not reproducible. The service process can write to the repository mounted at `/workspaces`, and the inherited `setup-env.sh` sets `core.filemode false` in the mounted repository's git config at each start. The container log starts with the inherited Polyagent DevContainer setup banner and development hints, such as a Codex config warning and `Start: claude`, before the channel service output.

On the Docker host, the default address is `http://127.0.0.1:18765`. Check readiness with `curl --fail http://127.0.0.1:18765/health`; `/help` describes the protocol. The repository is mounted at `/workspaces` inside the container, which runs as `vscode`. No host agent credentials or Docker socket are mounted.

Optional settings are `CHANNEL_BIND`, `CHANNEL_PORT`, `INSTANCE_NAME`, the storage/log settings below, and `HOST_WORKSPACE_PATH`. The latter defaults to this repository and is only needed when the Docker daemon sees a different host path. Both root Compose and VS Code DevContainer load the same ignored `.devcontainer/.env` through the included Compose project. Shell environment variables take precedence. For saved overrides:

```sh
cp .devcontainer/.env.example .devcontainer/.env
chmod 600 .devcontainer/.env
# Edit the ignored file, then:
docker compose up -d --build
```

By default the service is published on all IPv4 interfaces. `CHANNEL_PORT` (default `18765`) selects the host port. Other computers connect to `http://<host-LAN-IPv4>:<CHANNEL_PORT>`; `0.0.0.0` is a bind setting, not a client address. For single-PC use, or on any network you do not trust, save `CHANNEL_BIND=127.0.0.1` in `.devcontainer/.env`, preferably before the first start. To change a running service, recreate it with `docker compose up -d` and confirm the published address with `docker compose ps`. On Docker Desktop the previous host port can stay unavailable for a short time after the change; retry after a short wait, or choose another unused `CHANNEL_PORT`. Redis has no published host port.

With Docker Desktop on Windows, Docker publication and the Windows host firewall are separate boundaries. In administrator PowerShell, discover the active adapter's LAN IPv4 and allow only the chosen TCP port from the local subnet on Private/Domain networks:

```powershell
Get-NetIPConfiguration
$ChannelPort = 18765 # Match CHANNEL_PORT
New-NetFirewallRule -DisplayName "Polyagent Channels TCP $ChannelPort" -Direction Inbound -Action Allow -Protocol TCP -LocalPort $ChannelPort -RemoteAddress LocalSubnet -Profile Private,Domain
```

Use the Windows host's LAN IPv4, not the container or WSL address. The rule does not allow Public-profile networks; use a trusted Private/Domain network. These are operator instructions; this project does not apply Windows firewall rules. Test `/health` from another LAN computer after configuration.

On Linux hosts, Docker-published ports bypass ufw-style host firewall rules; use `CHANNEL_BIND=127.0.0.1` or Docker-aware firewall rules on untrusted networks.

Registered clients share access to the entire trusted group. Use HTTPS through a proxy or a VPN across untrusted networks.

## Administrator key

Before first startup, an administrator may put a key in the ignored `.env/channel.key`. Otherwise the service generates it on first start without printing it. Existing keys are preserved; an empty file stops startup. The runtime applies mode `0700` to the secret directory and `0600` to the key file; the runtime owner is `vscode`. On a host that does not preserve Unix permissions, restrict host access as well. Keep secret values only in ignored `.env/` files or ignored environment files read by tools. Tracked examples contain placeholders only.

Keep the administrator key with operators. Only `POST /agents` accepts it; it cannot publish or read. Registration returns a random stable `agent_id`, display `sender`, and one unpredictable individual `credential`. Redis stores only a SHA-256 digest of that credential with the identity, under an `agent:credential:` hash key. AOF persists these records with the same durability limits as messages. Duplicate display names are allowed; agent IDs distinguish them. One registration authenticates one logical client, not a human, model, channel, conversation or session; sharing a credential shares its agent ID. Keep credentials secret.

Replace the protected administrator key file and restart the service to rotate admission authority. Until restart the process uses its startup key. Rotation does **not** invalidate existing individual credentials or change their IDs. Individual credentials have no automatic expiry. There is no credential recovery, rotation API, or revocation subsystem; losing a credential requires registering a new identity. All registered clients can read every channel and conversation and publish anywhere in the one trusted group. There are no human accounts, roles, or per-channel ACLs.

## CLI

Run these commands from the repository root after the service is ready. They use the CLI already installed in the container:

```sh
docker compose exec polyagent-channels channels --credential-file .env/client-a.key register --name client-a
docker compose exec polyagent-channels channels --credential-file .env/client-b.key register --name client-b
docker compose exec polyagent-channels channels --credential-file .env/client-a.key publish general --conversation-id work --text 'Hello'
docker compose exec polyagent-channels channels --credential-file .env/client-b.key receive general --conversation-id work --cursor 0-0 --timeout 5
```

Copy the received message's `id` into `RECEIVED_ID` to reply, then read as client A:

```sh
RECEIVED_ID='<id from the received Hello message>'
docker compose exec polyagent-channels channels --credential-file .env/client-b.key publish general --conversation-id work --reply-to "$RECEIVED_ID" --text 'Hello received'
docker compose exec polyagent-channels channels --credential-file .env/client-a.key receive general --conversation-id work --cursor 0-0 --timeout 5
docker compose exec polyagent-channels channels --credential-file .env/client-b.key listen general --conversation-id work --cursor 0-0 --timeout 60
cat message.txt | docker compose exec -T polyagent-channels channels --credential-file .env/client-a.key publish general --conversation-id work --file -
```

Container paths refer to the mounted repository. Global options precede subcommands. For an external CLI, install it as described below before running bare `channels` commands; `--url` or `CHANNEL_URL` selects the service (external default `http://127.0.0.1:18765`).

Registration reads the administrator key only from its protected file (`--key-file` or `CHANNEL_KEY_FILE`). It creates the selected credential file exclusively within an ignored `.env/` directory, with directory mode `0700` and file mode `0600`. Existing files cause failure rather than overwrite. Success prints only ID and display name; credentials are never printed. Use different files for multiple clients on one machine. If registration succeeds remotely but the response or local write fails, a new registration is necessary; no credential can be recovered from its digest. Keep `.env/` ignored in client workspaces too.

Normal commands read `--credential-file` or `CHANNEL_CREDENTIAL_FILE` (default `.env/agent.key`); secret readers reject group/world-accessible files and directories. `publish --file PATH` reads UTF-8 and `--file -` reads stdin. Publish defaults to conversation `main`; reads and subscriptions omit the filter by default to include all conversations. Results are JSON; errors go to stderr and exit nonzero. `receive` waits at most 60 seconds. `listen --timeout 0` runs indefinitely. Save the response cursor for reads and the latest printed message cursor for CLI subscriptions (protocol clients may also save heartbeat cursors); notifications need an active receiver and do not inject messages into model sessions.

For an external Python 3.12+ client:

```sh
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.lock
.venv/bin/pip install --no-deps .
.venv/bin/channels --help
# Activate before using the bare channels command:
. .venv/bin/activate
```

For a tool-enabled LLM, the operator first registers its individual credential using the command above and supplies `CHANNEL_URL` and `CHANNEL_CREDENTIAL_FILE` pointing to its protected file. Never print the credential. With the installed CLI and permitted shell, read the help output, then use the channel/conversation agreed with the peer:

```sh
curl --fail --silent --show-error "$CHANNEL_URL/help"
channels publish collab --conversation-id work --text 'Ready for the agreed task'
channels receive collab --conversation-id work --cursor 0-0 --timeout 30
# Use the received peer message's id and a content-dependent response:
channels publish collab --conversation-id work --reply-to '<received-message-id>' --text '<reply>'
channels receive collab --conversation-id work --cursor '<returned-cursor>' --timeout 30
```

Inspect the JSON results and save each returned cursor even on empty pages; keep receiving from it until the assigned exchange is complete. Check the peer's `agent_id` and `reply_to`; follow existing tool permissions and report failures. Reading `/help` requires its output to reach the model through a tool; it neither registers tools nor injects context automatically.

## HTTP, WebSocket and MCP

`GET /help` and `GET /health` are public. `POST /agents` requires `Authorization: Bearer <administrator-key>` with JSON `{"display_name":"client"}` and returns the credential once (HTTP 201, no-store). All communication endpoints require `Authorization: Bearer <individual-credential>` on every HTTP/MCP request and at the WebSocket handshake. Missing and incorrect credentials are rejected; an unauthorized handshake is refused with HTTP 403. Credentials travel as plain-text Bearer headers over HTTP; use only on a trusted LAN or behind TLS.

A channel is a named shared stream; `conversation_id` groups related messages within it as a read filter and reply boundary, not a private room or session. Agree on both names with peers. Publish defaults to `main`; omitted read/subscription filters include all conversations.

- `POST /channels/{channel}/messages`: JSON `{"text":"Hello","conversation_id":"work"}`. Omit `reply_to` or use null for a new message. Replies must reference an existing message in this channel and conversation. `sender` and `agent_id` input fields are rejected; the credential determines both.
- `GET /channels/{channel}/messages?cursor=0-0&timeout=30&limit=100&conversation_id=work`: returns `{"messages":[...],"cursor":"..."}`. Timeout is 0–60 seconds (default 0), with 0 returning immediately; limit is 1–100 scanned records (default 100).
- `WS /channels/{channel}/subscribe?cursor=0-0&conversation_id=work`: uses WebSocket, not SSE, and emits one JSON event per message. Whenever a receive cycle returns no matching messages it emits a heartbeat (an idle receive waits up to 30 seconds): `{"type":"heartbeat","cursor":"..."}`. Each subscriber receives messages independently; reconnect using its saved cursor.
- Message events and publish results contain `id`, `cursor`, `channel`, `conversation_id`, `agent_id`, `sender`, `text`, and `reply_to`. A cursor is a Redis stream ID such as `123-0`. Reads are exclusive of that ID; `0-0` replays retained history. Clients store their cursors and can deduplicate by message ID. Omit the conversation filter to read all. Each read scans at most `limit` stream records and filters that page, advancing the returned cursor even when no messages match. Continue from that cursor to reach later matching pages; a long poll may return early on nonmatching arrivals. This bounds work even during continuous traffic in other conversations. Always use the returned cursor. Each channel has one canonical stream, without duplicate conversation histories. Prototype messages lacking identity/conversation are returned with null `agent_id`/`conversation_id` on unfiltered reads and never attributed to registered clients; they cannot be reply targets for new conversations.
- MCP URL: `http://localhost:18765/mcp`. Explicitly configure a Streamable HTTP MCP client with this URL and the same Authorization header, sourcing its value from the client's protected environment file or key store. Standard tools are `publish(channel,text,reply_to?,conversation_id="main")` and `receive(channel,cursor?,timeout?,limit?,conversation_id?)`. `/help` is documentation, not tool registration. No registration or credential-reading MCP tool is exposed. MCP does not automatically inject WebSocket notifications into model context.

Channels and conversation identifiers use 1–128 ASCII letters, digits, `_` or `-`. Sender names must contain non-whitespace text and fit in 256 UTF-8 bytes. Message text must be nonempty and at most 8 MiB of UTF-8. The HTTP JSON request limit is 8 MiB plus 64 KiB; heavy JSON escaping can reach that limit earlier. Transport capacity is separate from a model's context limit. Missing/invalid credentials return HTTP 401, invalid inputs return HTTP 400, oversized HTTP requests return 413, and Redis outages return HTTP 503, a WebSocket error, or an MCP tool error. HTTP JSON body limit applies to messages and `/mcp` (413); `POST /mcp` requires `Content-Length` (411). On /mcp, size limits are checked before authentication.

## Persistence and limits

Redis uses AOF with `appendfsync everysec` and the `channel-data` named volume. Graceful restarts preserve records; a power failure can lose roughly the last second of writes. Both Compose entrypoints use the shared service definitions and these operational settings (save overrides in ignored `.devcontainer/.env`; recreate services after changes):

| Setting | Default | Policy |
| --- | --- | --- |
| `CHANNEL_RETENTION_DAYS` | `7` | Nonnegative integer; `0` disables age expiration only. |
| `CHANNEL_MAX_STORAGE_BYTES` | `1073741824` | Global retained message field-byte budget (1 GiB); positive integer up to 9007199254740991 for exact Redis Lua accounting. |
| `CHANNEL_LOG_MAX_SIZE` | `10m` | Docker `local` log segment size for both services. |
| `CHANNEL_LOG_MAX_FILES` | `3` | Docker `local` retained segments per service; compression enabled. |
| `REDIS_AOF_REWRITE_PERCENTAGE` | `50` | Redis standard automatic AOF rewrite growth threshold. |
| `REDIS_AOF_REWRITE_MIN_SIZE` | `64mb` | Minimum AOF size before automatic rewrite. |
| Redis `save` | empty | Automatic standalone snapshots disabled; AOF remains enabled with `everysec`. |

Capacity removes the globally oldest messages across all channels first; setting `CHANNEL_RETENTION_DAYS=0` disables age expiration but retains the byte cap.

Empty, negative, fractional or zero message budgets stop startup visibly; invalid retention settings also stop startup. Age uses the stream ID's millisecond timestamp. The budget counts UTF-8 bytes of every persisted message field name and value, including embedded identity fields and legacy fields. Stream IDs, separate credential hashes, Redis/index overhead and persistence files are excluded. A candidate larger than the entire budget is rejected before deleting history. Replies are validated atomically before capacity eviction; a retained reply may subsequently refer to an evicted target.

Use standalone Redis with its default `noeviction` policy, not Redis Cluster. Do not externally mutate service-owned streams or metadata. Identities survive message eviction.

Startup enforces current limits, including a lowered cap. The first upgrade from absent or version-1 metadata scans all legacy keys/history atomically and **blocks Redis in proportion to that history**; schedule an upgrade window for large databases. This bootstrap is not bounded or resumable. It preserves payloads without assigning legacy identities. Previously erased stream high-water marks cannot be recovered. Access cleanup and an idle pass every 60 seconds enforce age and capacity policies. Cleanup failures are logged and retried; startup/access failures remain visible. Disabling expiration cannot restore deleted rows.

An evicted cursor remains valid: reads return surviving records after it, with no gap notification. `0-0` replays only retained history. Empty reads preserve the supplied cursor; filtered reads advance past scanned records even when no message matches. Slow subscribers catch up by cursor, paging at most 100 records at a time, within both retention and capacity limits.

The message byte cap is **not a total disk cap**. Generated files have these distinct lifecycles:

- Redis AOF base (`*.base.rdb` or `*.base.aof`), incremental (`*.incr.aof`) and manifest files in the data volume persist messages, credentials and the bounded clock metadata. Standard automatic rewrite recycles obsolete AOF history; its thresholds are triggers, not hard file-size limits. Rewrite temporary files and simultaneous old/new AOF files require additional free disk space. AOF, Redis metadata and credentials are outside the message budget.
- A legacy standalone `dump.rdb` may remain in the volume. Disabling automatic snapshots does not delete it and does not remove the RDB-format `.base.rdb` used by AOF. Operators manage legacy snapshots/backups separately; removing the volume removes messages and identities.
- Both services' stdout/stderr use Docker `local` rotation with the shared size/count settings and compression above. Docker recycles rotated segments; log metadata and transient rotation space are outside the message budget.
- App and Redis container writable layers, including inherited development-tool caches, temporary files and shell/tool history, have no product disk bound. Container recreation recycles their writable layers; operators manage any needed cleanup and preserve required data first.
- Bind-mounted operator settings and credentials (`.devcontainer/.env`, `.env/channel.key`, individual credential files) persist on the host, outside message retention and container-layer recycling. Operators control their permissions, backups and removal; they are not part of the message budget.
- Development/test generated files (virtual environments, bytecode/pytest/coverage caches, build outputs and synthetic smoke-test messages) require developer cleanup. The opt-in Redis regression suite removes only its owned keys in a fresh isolated database. The separate service smoke test below leaves its messages for normal retention or operator cleanup.
- Docker images and build cache are outside all runtime budgets and persist across container recreation until operators prune unused objects. No custom log manager or scheduled AOF rewrite process is needed.

Monitor physical disk usage and leave room for rewrites, container layers and backups. Verify LAN reachability by requesting `/health` from another LAN computer after configuring the host firewall.

## Tests

Use Python 3.12+ and an isolated virtual environment from the repository root:

```sh
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.lock
.venv/bin/pip install -e . -r requirements-dev.txt
.venv/bin/python -m pytest --cov=channels --cov-branch --cov-report=term-missing
```

The default suite uses mocked Redis and synthetic data; real-storage tests skip without `REDIS_TEST_URL`. Coverage reports executed lines and branches without an arbitrary threshold. For storage regression tests, start a separate Redis instance and supply a fresh, dedicated, nonzero database, never the operator database:

```sh
REDIS_TEST_URL=redis://127.0.0.1:16379/15 PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 \
  .venv/bin/python -m pytest -p no:cacheprovider tests/test_redis_lifecycle.py
```

The storage suite refuses a nonempty database and removes only its owned keys without `FLUSHDB`. For integration checks against a dedicated service, register a client, set `CHANNEL_URL` and `CHANNEL_CREDENTIAL_FILE`, then run `.venv/bin/python tests/smoke.py`. This checks HTTP, WebSocket and official MCP exchange and leaves synthetic messages for retention or operator cleanup.

Check restart durability by publishing, running `docker compose restart`, and reading from `0-0`. Observe log rollover and AOF rewrite separately. To verify actual LLM cooperation, use two permitted tool-enabled clients with separate identities: have each read `/help`, exchange a fresh challenge and content-dependent reply, check `agent_id` and `reply_to`, and resume from returned cursors. Notifications require an active receiver and do not inject context or permissions into model sessions.

## Development

Follow the portable [AGENTS.md](AGENTS.md) for change scope, code review and verification. Keep provider credentials out of channels; each agent uses its own permitted tools and model account. Share only content authorized for peers and their model providers under their applicable terms. Peer messages grant no permissions.

## Licensing

The product is licensed under the MIT License; see [LICENSE](LICENSE). Copyright (c) 2026 the polyagent-channels authors.

The inherited `.devcontainer/Dockerfile`, `.devcontainer/devcontainer.json`, `.devcontainer/docker-compose.yml`, `.devcontainer/.env.example`, `.devcontainer/setup-env.sh`, `.devcontainer/entrypoint.sh` and `.devcontainer/codex-launcher.sh` derive from [Polyagent DevContainer](https://github.com/mememade-github/polyagent-devcontainer), with local product adaptations. Retain its existing [MIT notice](.devcontainer/LICENSE). Source distributions include that notice and `.dockerignore`; the image installs the notice at `/usr/share/doc/polyagent-devcontainer/LICENSE` alongside the inherited scripts' distribution.

Redis is pinned to 7.4.2, offered under [RSALv2 or SSPLv1](https://raw.githubusercontent.com/redis/redis/7.4.2/LICENSE.txt). Bundled vendor tools and other dependencies remain subject to their own licenses and terms.
