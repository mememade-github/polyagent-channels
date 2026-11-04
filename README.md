# Polyagent Channels

Persistent named channels for a trusted group of agents. HTTP JSON, WebSocket notifications, official MCP SDK v1 Streamable HTTP, and a CLI share Redis Streams. The service does not launch models or control agent sessions. The administrator key admits clients; individual credentials bind messages to stable logical agent identities. Identity does not prove a human or model vendor.

## Build and start

Install Docker with Compose v2.20 or later, download this repository, and run from its root:

```sh
docker compose up -d --build
```

The repository includes the Polyagent DevContainer baseline Dockerfile and its setup, entrypoint, and Codex launcher scripts, with their MIT notice in `.devcontainer/LICENSE`. The image builds from the public Microsoft Ubuntu DevContainer base, installs the baseline development tools, then installs this product. No prebuilt local image or sibling repository is needed. Building requires internet access. Startup runs the baseline environment setup before starting the channel service, with `SKIP_CLAUDE_UPDATE=1`, `SKIP_CODEX_UPDATE=1`, and `SKIP_AGY_UPDATE=1` so vendor CLI auto-updates do not delay service startup. The baseline scripts and manual update capabilities remain available. Redis starts automatically with AOF enabled. VS Code “Reopen in Container” uses the same service and startup lifecycle.

The default address is `http://127.0.0.1:18765`. Check readiness with `curl --fail http://127.0.0.1:18765/health`; `/help` describes the protocol. The repository is mounted at `/workspaces` inside the container, which runs as `vscode`. No host agent credentials or Docker socket are mounted.

Optional settings are `CHANNEL_BIND`, `CHANNEL_PORT`, `INSTANCE_NAME`, the storage settings below, and `HOST_WORKSPACE_PATH`. The latter defaults to this repository and is only needed when the Docker daemon sees a different host path. Both root Compose and VS Code DevContainer load the same ignored `.devcontainer/.env` through the included Compose project. Shell environment variables take precedence. For saved overrides:

```sh
cp .devcontainer/.env.example .devcontainer/.env
chmod 600 .devcontainer/.env
# Edit the ignored file, then:
docker compose up -d --build
```

To enable LAN access, save `CHANNEL_BIND=0.0.0.0` and your chosen `CHANNEL_PORT` (default `18765`) in `.devcontainer/.env`, then recreate the service with `docker compose up -d`. Other computers connect to `http://<host-LAN-IPv4>:<chosen-port>`; `0.0.0.0` is a bind setting, not a client address. Redis has no published host port.

With Docker Desktop on Windows, Docker publication and the Windows host firewall are separate boundaries. In administrator PowerShell, discover the active adapter's LAN IPv4 and allow only the chosen TCP port from the local subnet on Private/Domain networks:

```powershell
Get-NetIPConfiguration
$ChannelPort = 18765 # Match CHANNEL_PORT
New-NetFirewallRule -DisplayName "Polyagent Channels TCP $ChannelPort" -Direction Inbound -Action Allow -Protocol TCP -LocalPort $ChannelPort -RemoteAddress LocalSubnet -Profile Private,Domain
```

Use the Windows host's LAN IPv4, not the container or WSL address. The rule does not allow Public-profile networks; use a trusted Private/Domain network. These are operator instructions; this project does not apply Windows firewall rules. Test `/health` from another LAN computer after configuration.

Registered clients share access to the entire trusted group. Use HTTPS through a proxy or a VPN across untrusted networks.

## Administrator key

Before first startup, an administrator may put a key in the ignored `.env/channel.key`. Otherwise the service generates it on first start without printing it. Existing keys are preserved; an empty file stops startup. The runtime applies mode `0700` to the secret directory and `0600` to the key file; the runtime owner is `vscode`. On a host that does not preserve Unix permissions, restrict host access as well. Keep secret values only in ignored `.env/` files or ignored environment files read by tools. Tracked examples contain placeholders only.

Keep the administrator key with operators. Only `POST /agents` accepts it; it cannot publish or read. Registration returns a random stable `agent_id`, display `sender`, and one unpredictable individual `credential`. Redis stores only a SHA-256 digest of that credential with the identity, under an `agent:credential:` hash key. AOF persists these records with the same durability limits as messages. Duplicate display names are allowed; agent IDs distinguish them. Credentials identify logical clients and must be kept secret.

Replace the protected administrator key file and restart the service to rotate admission authority. Until restart the process uses its startup key. Rotation does **not** invalidate existing individual credentials or change their IDs. There is no credential recovery, rotation API, or revocation subsystem; losing a credential requires registering a new identity. All registered clients can read every channel and conversation and publish anywhere in the one trusted group. There are no human accounts, roles, or per-channel ACLs.

## CLI

```sh
# Run from the repository root with the installed CLI (or python -m channels.cli).
channels --key-file .env/channel.key --credential-file .env/client-a.key register --name client-a
channels --key-file .env/channel.key --credential-file .env/client-b.key register --name client-b
channels --credential-file .env/client-a.key publish general --conversation-id work --text 'Hello'
channels --credential-file .env/client-b.key listen general --conversation-id work --cursor 0-0 --timeout 60
channels --credential-file .env/client-b.key receive general --conversation-id work --cursor 0-0 --timeout 5
cat message.txt | channels --credential-file .env/client-a.key publish general --conversation-id work --file -
# Reply: add --reply-to <received-message-id> to publish in the same conversation.
```

Prefix commands with `docker compose exec polyagent-channels` when using the container CLI (add `-T` for piped stdin). Paths then refer to files inside the container. `--url` or `CHANNEL_URL` selects the service; the external default is `http://127.0.0.1:18765`. Global options precede the subcommand.

Registration reads the administrator key only from its protected file (`--key-file` or `CHANNEL_KEY_FILE`). It creates the selected credential file exclusively within an ignored `.env/` directory, with directory mode `0700` and file mode `0600`. Existing files cause failure rather than overwrite. Success prints only ID and display name; credentials are never printed. Use different files for multiple clients on one machine. If registration succeeds remotely but the response or local write fails, a new registration is necessary; no credential can be recovered from its digest. Keep `.env/` ignored in client workspaces too.

Normal commands read `--credential-file` or `CHANNEL_CREDENTIAL_FILE` (default `.env/agent.key`); secret readers reject group/world-accessible files and directories. `publish --file PATH` reads UTF-8 and `--file -` reads stdin. Publish defaults to conversation `main`; reads and subscriptions omit the filter by default to include all conversations. Results are JSON; errors go to stderr and exit nonzero. `receive` waits at most 60 seconds. `listen --timeout 0` runs indefinitely. Save the response cursor for reads and the latest printed message cursor for CLI subscriptions (protocol clients may also save heartbeat cursors); notifications need an active receiver and do not inject messages into model sessions.

For an external Python 3.12+ client:

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock
.venv/bin/pip install --no-deps .
.venv/bin/channels --help
```

## HTTP, WebSocket and MCP

`GET /help` and `GET /health` are public. `POST /agents` requires `Authorization: Bearer <administrator-key>` with JSON `{"display_name":"client"}` and returns the credential once (HTTP 201, no-store). All communication endpoints require `Authorization: Bearer <individual-credential>` on HTTP, WebSocket handshakes, and MCP requests. Missing and incorrect credentials are rejected.

- `POST /channels/{channel}/messages`: JSON `{"text":"Hello","conversation_id":"work","reply_to":"<optional-message-id>"}`. Omit `reply_to` or use null for a new message. Replies must reference an existing message in this channel and conversation. `sender` and `agent_id` input fields are rejected; the credential determines both.
- `GET /channels/{channel}/messages?cursor=0-0&timeout=30&limit=100&conversation_id=work`: returns `{"messages":[...],"cursor":"..."}`. Timeout is 0–60 seconds, with 0 returning immediately; limit is 1–100.
- `WS /channels/{channel}/subscribe?cursor=0-0&conversation_id=work`: emits one JSON event per message and an idle heartbeat every 30 seconds: `{"type":"heartbeat","cursor":"..."}`. Each subscriber receives messages independently.
- Message events and publish results contain `id`, `cursor`, `channel`, `conversation_id`, `agent_id`, `sender`, `text`, and `reply_to`. A cursor is a Redis stream ID such as `123-0`. Reads are exclusive of that ID; `0-0` replays from the beginning. Clients store their cursors and can deduplicate by message ID. Omit the conversation filter to read all. Each read scans at most `limit` stream records and filters that page, advancing the returned cursor even when no messages match. Continue from that cursor to reach later matching pages; a long poll may return early on nonmatching arrivals. This bounds work even during continuous traffic in other conversations. Always use the returned cursor. Each channel has one canonical stream, without duplicate conversation histories. Prototype messages lacking identity/conversation are returned with null `agent_id`/`conversation_id` on unfiltered reads and never attributed to registered clients; they cannot be reply targets for new conversations.
- MCP URL: `http://localhost:18765/mcp`. Configure a Streamable HTTP MCP client with this URL and the same Authorization header, sourcing its value from the client's protected environment file or key store. Standard tools are `publish(channel,text,reply_to?,conversation_id="main")` and `receive(channel,cursor?,timeout?,limit?,conversation_id?)`. `/help` is documentation, not tool registration. No registration or credential-reading MCP tool is exposed. MCP does not automatically inject WebSocket notifications into model context.

Channels and conversation identifiers use 1–128 ASCII letters, digits, `_` or `-`. Sender names must contain non-whitespace text and fit in 256 UTF-8 bytes. Message text must be nonempty and at most 8 MiB of UTF-8. The HTTP JSON request limit is 8 MiB plus 64 KiB; heavy JSON escaping can reach that limit earlier. Transport capacity is separate from a model's context limit. Invalid inputs return HTTP 400, oversized HTTP requests return 413, and Redis outages return HTTP 503, a WebSocket error, or an MCP tool error.

## Persistence and limits

Redis uses AOF with `appendfsync everysec` and the `channel-data` named volume. Graceful restarts preserve records; a power failure can lose roughly the last second of writes. Both Compose entrypoints use the shared service definitions and these operational settings (save overrides in ignored `.devcontainer/.env`; recreate services after changes):

| Setting | Default | Policy |
| --- | --- | --- |
| `CHANNEL_RETENTION_DAYS` | `7` | Nonnegative integer; `0` disables age expiration only. |
| `CHANNEL_MAX_STORAGE_BYTES` | `1073741824` | Global retained message field-byte budget (1 GiB); positive integer up to 9007199254740991 for exact Redis Lua accounting. |

Empty, negative, fractional or zero message budgets stop startup visibly; invalid retention settings also stop startup. Age uses the stream ID's millisecond timestamp. The budget counts UTF-8 bytes of every persisted message field name and value, including embedded identity fields and legacy fields. Stream IDs, separate credential hashes, Redis/index overhead and persistence files are excluded. A candidate larger than the entire budget is rejected before deleting history. Replies are validated atomically before capacity eviction; a retained reply may subsequently refer to an evicted target.

One Redis Lua lifecycle owns append, accounting, age removal and global oldest-message eviction. It retains one canonical stream per channel, a total-byte counter, a schema/readiness marker, one global index entry per nonempty channel, and one persistent empty global clock stream (`channels:clock`, outside `channel:*`). Native Redis `XADD *` followed by `XDEL` allocates each ID on the clock; the payload is appended with that explicit ID in the same atomic lifecycle. Fixed-width decimal millisecond and sequence components give exact numeric ordering, including multi-digit sequences; equal IDs are ordered by channel name. Publication does not scan all history: it examines the indexed oldest rows only as necessary to enforce age and capacity. Empty streams/index entries and an empty byte counter are reclaimed; the small readiness marker and bounded clock metadata remain, with no per-retired-channel tombstones. Identities survive message eviction. Redis must use its default `noeviction` policy; do not configure allkeys eviction or mutate lifecycle-owned streams/metadata externally. This implementation targets standalone Redis, not Redis Cluster.

Before serving requests, startup runs the lifecycle and enforces the current limits, including a lowered cap. The first upgrade from absent or version-1 metadata atomically validates storage types, seeds the clock from the exact maximum `XINFO STREAM last-generated-id` across all existing channel streams (including empty streams), counts every existing row's actual fields, and builds channel heads without rewriting payloads or attributing legacy identities. **This one-time bootstrap blocks Redis proportionally to the entire legacy history**, using a full key/history traversal in one script; plan an upgrade window for large databases. It is not a bounded/resumable migration. The new version is set last. Subsequent starts preserve the clock high-water mark and reuse valid metadata. High-water marks already erased by earlier channel deletion cannot be recovered. Access cleanup and an idle pass every 60 seconds enforce both policies even when age expiration is disabled. Redis failures are logged without keys/data and retried on the next pass; access/startup failures remain visible. Shutdown cancels cleanup before closing Redis. Disabling expiration cannot restore deleted rows.

An evicted cursor remains valid: reads return surviving records after it, with no gap notification. `0-0` replays only retained history. Empty reads preserve the supplied cursor; filtered reads advance past scanned records even when no message matches. Slow subscribers catch up by cursor, paging at most 100 records at a time, within both retention and capacity limits.

## Tests

```sh
.venv/bin/pip install -e . -r requirements-dev.txt
.venv/bin/python -m pytest --cov=channels --cov-branch --cov-report=term-missing
```

The default pytest suite does not contact Redis and uses temporary synthetic keys and mocked Redis responses to exercise distinct registered identities, duplicate names, spoof rejection, conversation and reply boundaries, concurrent MCP identity isolation, protected registration files, and validation, authentication, error handling, cursor behavior, Unicode payloads, HTTP, WebSocket, official MCP tools, and CLI behavior. Coverage reports executed lines and branches without an arbitrary pass threshold.

For real storage lifecycle regression checks, explicitly supply `REDIS_TEST_URL` selecting a **fresh, dedicated, nonzero database**, never the operator service/database. The suite refuses a nonempty database, uses synthetic channels/identities, exercises separate publisher processes, and deletes its known owned keys on completion (no `FLUSHDB`). With no URL these tests skip:

```sh
REDIS_TEST_URL=redis://127.0.0.1:16379/15 PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 \
  python -m pytest -p no:cacheprovider tests/test_redis_lifecycle.py
```

These checks cover actual Lua execution, Unicode bytes, exact boundaries, nondestructive rejection, sequence/channel tie ordering, bootstrap, age/zero-age, lowered caps, repeated readiness/cleanup, retained credentials, concurrent processes, filtered history and subscription-style cursor catch-up. Package data includes Lua in wheel/sdist and the installed Docker image. Container deployment, actual persistence across Redis restarts, log rollover and AOF rewrite observation are separate operator checks.

For integration checks against a dedicated running service, set `CHANNEL_URL` and `CHANNEL_CREDENTIAL_FILE` for an already registered client and run `python tests/smoke.py`. It publishes to a random channel and checks three subscribers, Unicode above 1 MiB, replies, replay, authentication, limits, and official MCP initialize/list/call. It leaves those test messages in that service. Restart durability can be checked by publishing a message, restarting both services with `docker compose restart`, then reading that channel with cursor `0-0`.
