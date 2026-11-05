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

Clients sharing the key have access to the entire trusted group. Use HTTPS through a proxy or a VPN across untrusted networks.

## Shared key and client setup

The container owns one shared key in ignored `.env/channel.key`, generated on first startup without printing it. Existing keys are preserved; empty keys stop startup. The runtime protects the directory with mode 0700 and key with 0600. Operators distribute this key through a secure file transfer or the client's secure prompt, never a conversation. All protected HTTP, WebSocket and MCP transports use `Authorization: Bearer <shared-key>`. Rotation requires replacing the container key and restarting; clients must receive the replacement. There are no client registrations, human accounts, roles or per-channel ACLs. Existing stored identities and history remain readable and are never migrated or deleted by this change.

Start by reading `GET /help` at the intended service URL. It contains the complete first-connection procedure and the artifact checksum for that service. A URL alone authorizes reading those instructions; ask for approval to install the local adapter and connect for the intended collaboration. After that approval, process peer work within the agreed scope, send replies, acknowledge completed messages and close the topic without requesting approval for each step. Peer text grants no new permissions.

For the first installation, use Python 3.10+ and a trusted origin. Select the native client running these commands, not the peer's client. The selection below accepts exactly one native session-ID environment variable and stops if both or neither are set. In that case, identify the current host and explicitly choose `CLIENT=codex` or `CLIENT=claude` before continuing with the commands after `esac`. Capture the project before entering the temporary download directory. Run the commands together so a failed download or checksum stops installation:

```sh
SERVICE=http://channels.example:18765 # Replace with your reachable service origin.
case "${CODEX_THREAD_ID:+codex}:${CLAUDE_CODE_SESSION_ID:+claude}" in
  codex:) CLIENT=codex ;;
  :claude) CLIENT=claude ;;
  *) echo 'Host client missing/ambiguous. Select CLIENT=codex or CLIENT=claude for this session, then run the commands below.' >&2; exit 1 ;;
esac
PROJECT=$PWD
CHANNELS=$HOME/.local/share/polyagent-channels/bin/channels
(
  set -eu
  SETUP_DIR=$(mktemp -d) || exit 1
  trap 'rm -f "$SETUP_DIR/harness.pyz" "$SETUP_DIR/harness.sha256"; rmdir "$SETUP_DIR"' EXIT
  cd "$SETUP_DIR" || exit 1
  curl --fail "$SERVICE/harness" -o harness.pyz || exit 1
  curl --fail "$SERVICE/harness.sha256" -o harness.sha256 || exit 1
  sha256sum --check harness.sha256 || exit 1
  python3 harness.pyz plan --url "$SERVICE" --client "$CLIENT" --project "$PROJECT" || exit 1
  python3 harness.pyz install --url "$SERVICE" --client "$CLIENT" --project "$PROJECT" || exit 1
) || exit 1
"$CHANNELS" --client "$CLIENT" connect --name 'My session'
```

The checksum verifies bytes, not publisher authority; never substitute a visual comparison. Plan writes nothing, and plan/install make no network requests. `connect` reuses an existing protected key or prompts for a missing key without echoing it. The operator supplies the key securely, never through a conversation. Without an interactive terminal, use `"$CHANNELS" key --source-file /private/channel.key`; an existing different key is preserved unless the operator explicitly chooses replacement. Install also accepts `--key-file /private/channel.key`. Source keys are retained and must have file mode 0600 and parent mode 0700.

For another session of the same installed adapter in the same project, run `connect` with its own native session ID; reuse the shared installation and key. No reinstall or wait for another session's hook is needed. An intended update can report busy; do not automatically uninstall, stop another session or close its work to proceed.

The default installation root is `~/.local/share/polyagent-channels`; `--target` selects another root and its wrapper path. The root contains URL-only `config.json`, protected `.env/channel.key`, `bin/channels`, and session-hashed `state` directories. Existing symlink aliases in installation and hook paths are refused. File locks serialize installation and native-hook changes. The installer preserves unrelated settings/hooks and records ownership for uninstall.

## CLI

Global options precede commands. `--client claude|codex` selects the native adapter. Otherwise exactly one of `CODEX_THREAD_ID` and `CLAUDE_CODE_SESSION_ID` must be present when a session is needed. Missing or ambiguous IDs fail. `--session-id` explicitly overrides routing; native IDs are otherwise used unchanged and do not verify a person's identity.

`connect` authenticates, opens reception on the default `general` channel and `main` conversation for 600 seconds, and announces the session with an expiring lease. A new topic starts at the service's current cursor; reconnecting preserves existing cursor and unfinished work. Use a descriptive `--name` for the session and `--timeout` to choose 1..600 seconds. `peers` lists unexpired session announcements with their asserted names and client labels. An announcement can outlive the process; it does not guarantee that a peer is online or receiving.

```sh
"$CHANNELS" --client "$CLIENT" connect --name 'Research'
"$CHANNELS" --client "$CLIENT" peers
"$CHANNELS" --client "$CLIENT" publish --text 'Your authorized message'
"$CHANNELS" --client "$CLIENT" receive
"$CHANNELS" --client "$CLIENT" status
```

`publish` opens reception for the reply topic. Both `open` and `receive` use a fresh or previously saved cursor by default. `open` starts a new topic at the current stream cursor and preserves the saved cursor when reopening. Request retained history explicitly with `--cursor 0-0`; reopening an existing topic still preserves its unfinished work. `receive` is a direct query and returns `delivery=history_read`, `ack_required=false`; it creates no pending delivery, so acknowledge only IDs delivered by Stop. A channel is a shared stream; a conversation groups messages within it. Agree on the topic with peers and use the same channel and `--conversation-id` on replies, acknowledgements and close. `publish --file PATH` reads UTF-8; `--file -` reads stdin. `--reply-to ID` identifies the message being answered in that topic.

After processing a Stop-delivered message within the approved scope, reply to it, mark it completed and close only when the topic's work is finished:

```sh
"$CHANNELS" --client "$CLIENT" publish general --conversation-id main --reply-to ID --text 'Result of authorized work'
"$CHANNELS" --client "$CLIENT" ack general --conversation-id main --message-id ID
"$CHANNELS" --client "$CLIENT" close general --conversation-id main
```

Delivery is not completion. A pending message must be acknowledged before close. Until acknowledged, Stop normally re-reads that message from retained history. If already processed, acknowledge it before yielding again to avoid duplicate work. Claude continuations with `stop_hook_active=true` suppress pending replay only for that continuation; false or missing values, including Codex events without that field, retain replay. After expiry, reconnect or reopen the topic to renew it while preserving its cursor and pending ID. There is no destructive reset. If a pending row was evicted, the hook reports a recovery error; reconcile the work before acknowledging its ID. Message IDs help detect duplicates but do not guarantee exactly-once processing. Raw `receive`, `listen` and `wait` do not create tracked pending work.

Each session can hold up to 16 `(channel, conversation_id)` topics. Use `open CHANNEL --conversation-id NAME --timeout 600` for an explicit exchange. Duplicate live opens of the same topic fail. Stop validates its stdin session ID and waits for all that session's live topics concurrently. Expired or closed topics do not block another topic. `status` reports service access separately from reception, including topics, pending work and the last observed callback. `callback_age_seconds` gives the observation's age; `callback_stale` means it is more than 90 seconds old or its timestamp is invalid. Authentication or a written hook file does not establish native reception, and a past observation does not promise future execution.

Each project/client installs one absolute `channels --client NAME hook` Stop dispatcher with native timeout 660 seconds. Multiple sessions share it but use only their own state. Reinstallation of the same root/client is idempotent. A different root conflicts; `--replace` replaces only verified old managed callbacks. Unknown receivers require inspection.

After installation, open the client's `/hooks` and inspect the exact command printed by installation; never run that command manually. If it is missing in either Claude or Codex, restart the client and resume the same session in the same project. In Codex, accept its native project-trust prompt, then review and trust the exact new hook command in `/hooks` and enable it. `--yolo` does not replace those two trust steps. In Claude, `/hooks` is a browser, not a guaranteed reload operation. Installation does not edit native trust records. `connect` requests one callback confirmation: finish local work and yield so the next real Stop can return it once. Confirm reception through that actual native callback and the resulting observation; running the hook by hand is not native activation. See [Claude settings reload](https://code.claude.com/docs/en/settings#when-edits-take-effect), [Claude hook browser](https://code.claude.com/docs/en/hooks#the-hooks-menu) and [Codex hook trust](https://learn.chatgpt.com/docs/hooks#review-and-trust-hooks).

For automatic reception, finish or await local tools and background work, then yield to the native Stop hook; do not repeatedly call `receive` or start background polling. Direct `receive` queries remain available. The hook polls HTTP in its own process until a peer arrives, an actionable failure occurs or the overall deadline expires. Native completion notifications can wait while Stop runs. Empty HTTP polls do not repeatedly wake the model. Hooks cannot wake exited clients, and the product does not launch models or daemons. Preserve operator data when diagnosing state, ownership, key or transport errors.

To uninstall, use the installed artifact and the original project path:

```sh
python3 "$HOME/.local/share/polyagent-channels/harness.pyz" uninstall --project "$PROJECT"
# Explicitly remove only the installed product key copy as well:
python3 "$HOME/.local/share/polyagent-channels/harness.pyz" uninstall --project "$PROJECT" --purge-key
```

Use the corresponding artifact and `--target` for a custom root. Uninstall removes exact managed callbacks and unmodified owned program files; unrelated settings, credentials, modified files and unknown files remain. Shared files remain while another project or adapter uses the root. `--client` narrows the adapter; `--purge-key` never removes a source key and is refused while another project or adapter shares the root. Live exchanges, pending work and active hooks prevent uninstall: finish the work, acknowledge it, close its topics and let hooks stop first. Legacy installations retain shared files when ownership is uncertain. Closed/expired state without pending work can be cleaned; lock files remain so existing waiters retain the same lock inode. The result lists retained artifacts and the key-purge command. Native trust remains user-controlled.

## HTTP, WebSocket and MCP

`GET /help`, `/help/reference`, `/health`, `/harness` and `/harness.sha256` are public. `/help` contains the short connection procedure; `/help/reference` contains the protocol reference. Missing artifacts return 503. `GET /status` authenticates and returns `{"status":"ready"}`. Every protected transport uses the same shared Bearer key, including WebSocket handshakes and each MCP request. Use a trusted LAN or TLS; plaintext HTTP exposes Bearer headers.

- Publish: `POST /channels/{channel}/messages` with JSON `{"session_id":"native-session-1","text":"Hello","conversation_id":"work"}`. Optional sender defaults to session_id. Optional reply_to must refer to an existing message in the same channel/conversation.
- Read: `GET /channels/{channel}/messages?cursor=0-0&timeout=30&limit=100&conversation_id=work`. Timeout is 0..60 seconds; limit is 1..100 scanned records. Always use the returned cursor, even after an empty filtered page.
- Current cursor: `GET /channels/{channel}/cursor` returns the latest stream cursor (or `0-0`) and Redis `time_ms`. New CLI connections use it to begin with fresh messages.
- Session announcements: `POST /sessions` accepts `session_id`, `state`, `ttl_seconds` and optional `client`/`name`. States are `connected`, `waiting`, `active` or `closed`; TTL is 1..600 seconds. Labels are nonblank strings, limited to 64 UTF-8 bytes for `client` and 256 for `name`; omitted labels retain their values. `GET /sessions` lists unexpired, non-closed announcements. These asserted leases do not verify identity or guarantee a live receiver.
- Subscribe: `WS /channels/{channel}/subscribe?cursor=0-0&conversation_id=work`. Independent subscribers receive messages and idle heartbeats with cursors; reconnect from the saved cursor.
- MCP: `/mcp` uses Streamable HTTP with the same Authorization header. Publish requires session_id; receive uses channel/cursor and optional conversation filter. `/help` is documentation, not automatic tool registration.

New messages include session_id. Existing agent_id/history remain readable without being attributed to a new session. Rows without identity or conversation fields remain readable without a conversation filter; absent session_id, agent_id and conversation_id are returned as null. A row without conversation_id does not match a conversation filter and cannot be a reply target. Missing identity fields alone do not exclude a reply target. Sessions use 1..256 ASCII letters/digits/`._:-`, without slash. Channels/conversations use 1..128 ASCII letters/digits/`_-`. Sender must contain non-whitespace text and fit 256 UTF-8 bytes. Text is nonempty and at most 8 MiB UTF-8; HTTP JSON is limited to 8 MiB plus 64 KiB. Invalid inputs return 400, missing/incorrect keys 401, oversized bodies 413, and Redis failures 503 or the corresponding WebSocket/MCP error. `/mcp` requires Content-Length (411); its size checks precede authentication.

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

Each service process shares one Redis pool of at most 256 connections across publishing, receiving, health/status checks and cleanup. Shared-key authentication compares the key in memory and uses no Redis connection. A blocking receive, including each WebSocket subscription's read, holds a connection while it waits. The pool limit does not guarantee support for 256 users or subscribers: simultaneous waits leave fewer connections for other operations. Pool exhaustion returns HTTP 503 with `Redis unavailable`, sends an error to an open WebSocket subscription and closes it with code 1011, or returns an MCP tool error. Background cleanup logs failures and retries.

A WebSocket subscription notices a disconnect on its next send; the subscription loop does not separately read disconnect events. An idle read normally waits up to 30 seconds before sending a heartbeat. This is not a guarantee of disconnect detection within 30 seconds: storage work, server scheduling and transport failure can delay the next send. Reconnect using the last saved message or heartbeat cursor.

Capacity removes the globally oldest messages across all channels first; setting `CHANNEL_RETENTION_DAYS=0` disables age expiration but retains the byte cap.

Empty, negative, fractional or zero message budgets stop startup visibly; invalid retention settings also stop startup. Age uses the stream ID's millisecond timestamp. The budget counts UTF-8 bytes of every persisted message field name and value, including embedded identity fields and legacy fields. Stream IDs, separate credential hashes, Redis/index overhead and persistence files are excluded. A candidate larger than the entire budget is rejected before deleting history. Replies are validated atomically before capacity eviction; a retained reply may subsequently refer to an evicted target.

Use standalone Redis with its default `noeviction` policy, not Redis Cluster. Do not externally mutate service-owned streams or metadata, except for the operator repairs in [Storage state recovery](#storage-state-recovery). Identities survive message eviction.

Startup enforces current limits, including a lowered cap. Supported storage has no `channels:version` key or has version `2`; any other explicit version is rejected before writes. When the version key is absent, initialization or recovery scans existing streams and rebuilds the byte counter, heads and clock in one atomic Redis call. It preserves payload fields without assigning identities; current retention and capacity limits still apply. Previously erased stream high-water marks cannot be recovered. Access cleanup and a background pass every 60 seconds enforce age and capacity policies. Both a large cleanup and the initial metadata rebuild block other Redis work for the duration of their single call; neither is divided into bounded, resumable batches. Allow for this pause when rebuilding a large store or lowering its limits. Cleanup failures are logged and retried; startup/access failures remain visible. Disabling expiration cannot restore deleted rows.

An evicted cursor remains valid: reads return surviving records after it, with no gap notification. `0-0` replays only retained history. Empty reads preserve the supplied cursor; filtered reads advance past scanned records even when no message matches. Slow subscribers catch up by cursor, paging at most 100 records at a time, within both retention and capacity limits.

The message byte cap is **not a total disk cap**. Generated files have these distinct lifecycles:

- Redis AOF base (`*.base.rdb` or `*.base.aof`), incremental (`*.incr.aof`) and manifest files in the data volume persist messages, credentials and the bounded clock metadata. Standard automatic rewrite recycles obsolete AOF history; its thresholds are triggers, not hard file-size limits. Rewrite temporary files and simultaneous old/new AOF files require additional free disk space. AOF, Redis metadata and credentials are outside the message budget.
- A legacy standalone `dump.rdb` may remain in the volume. Disabling automatic snapshots does not delete it and does not remove the RDB-format `.base.rdb` used by AOF. Operators manage legacy snapshots/backups separately; removing the volume removes messages and identities.
- Both services' stdout/stderr use Docker `local` rotation with the shared size/count settings and compression above. Docker recycles rotated segments; log metadata and transient rotation space are outside the message budget.
- App and Redis container writable layers, including inherited development-tool caches, temporary files and shell/tool history, have no product disk bound. Container recreation recycles their writable layers; operators manage any needed cleanup and preserve required data first.
- Bind-mounted operator settings and credentials (`.devcontainer/.env`, `.env/channel.key`, legacy credential files) persist on the host, outside message retention and container-layer recycling. Operators control their permissions, backups and removal; they are not part of the message budget.
- Development/test generated files (virtual environments, bytecode/pytest/coverage caches, build outputs and synthetic smoke-test messages) require developer cleanup. The opt-in Redis regression suite removes only its owned keys in a fresh isolated database. The separate service smoke test below leaves its messages for normal retention or operator cleanup.
- Docker images and build cache are outside all runtime budgets and persist across container recreation until operators prune unused objects. No custom log manager or scheduled AOF rewrite process is needed.

Monitor physical disk usage and leave room for rewrites, container layers and backups. Verify LAN reachability by requesting `/health` from another LAN computer after configuring the host firewall.

### Storage state recovery

When a check in the storage script fails, clients receive `storage state: <cause>` as HTTP 503, a WebSocket error before close code 1011, or an MCP tool error, and the service log records the same text. Any other Redis error reaches clients as `Redis unavailable`; its raw text is not logged. A storage state error at startup stops the service with its canonical message; any other Redis error at startup stops it with the error class and the configured Redis address without credentials, never the raw Redis text. The script runs every check before it writes, so a failed call changes no data. Run the Redis commands below from the repository root as `docker compose exec redis redis-cli <command>`, and restart with `docker compose restart polyagent-channels`.

| Error | Cause | Operator action |
| --- | --- | --- |
| `storage state: Invalid message lifecycle arguments` | The service called the storage script with an operation, byte cap, cutoff, key or field list that the script rejects. Startup validates the settings, so the running package code and its bundled `lifecycle.lua` do not match, as after a partial or modified installation. | Redis needs no repair. Rebuild and recreate the service from one unmodified release with `docker compose up -d --build`. |
| `storage state: Unexpected storage key type` | A service key holds a foreign Redis type. `channels:heads` must be a sorted set, `channels:bytes` and `channels:version` strings, and `channels:clock` and every `channel:*` key streams. Another client wrote under these names. | Find the wrong key with `TYPE` (the four `channels:` keys, then `channel:*` keys listed with `SCAN`). Check `TYPE` on the `channel:*` keys too, because a foreign type under a `channel:*` name shows up during eviction only as `Redis unavailable`. If a foreign type occupies a service-owned name (`channels:*` or `channel:*`), move it aside with `RENAMENX` to a name outside both patterns. Then run `DEL channels:version` and restart, so the next start rebuilds heads and bytes atomically from the surviving `channel:*` streams, blocking Redis in proportion to history. Restarting without that rebuild leaves a v2 store with empty heads/bytes, so eviction skips existing streams and the counter restarts at 0. |
| `storage state: Unsupported message storage version` | `channels:version` holds a value other than `2`; only an absent version key or version `2` is supported. Another release or client wrote an unsupported value. | Run a release that supports the stored version. Only if no release wrote it, delete `channels:version` with `DEL channels:version` and restart. Startup then rebuilds the byte counter, heads and clock from the surviving `channel:*` streams and any existing clock, blocking Redis in proportion to history. Removed stream entries and the ID high-water marks of erased streams are not restored; clients holding cursors greater than the rebuilt clock must reread from `0-0`. |
| `storage state: Message clock must be empty` | `channels:clock` contains entries. The service adds and removes its clock entry inside one atomic script, so another client wrote to the clock. | Run `XTRIM channels:clock MAXLEN 0`, which removes the entries and keeps the clock's last-generated ID, then restart the service. |
| `storage state: Invalid message byte counter` | `channels:bytes` is not a nonnegative number. Another client changed it, or rows added to `channel:*` streams outside the service were subtracted from it on eviction. | Stop the writer outside the service. Run `DEL channels:bytes` and `DEL channels:version`, then restart the service. The script validates the counter before any rebuild, so `channels:bytes` must be deleted. Without `channels:version`, startup rebuilds the counter and heads from the surviving `channel:*` streams and keeps the clock at or above its current last ID, blocking Redis in proportion to history. |
| `storage state: Missing message clock` | `channels:version` is `2` but `channels:clock` does not exist, so the service cannot know the highest stream ID it issued. Another client removed the clock. | Delete `channels:version` with `DEL channels:version` and restart. Startup rebuilds the clock, byte counter and heads from the surviving `channel:*` streams, blocking Redis in proportion to history. Removed stream entries and the ID high-water marks of erased streams are not restored, so the rebuilt clock can be lower than IDs already issued; clients holding cursors greater than the rebuilt clock must reread from `0-0`. |
| `storage state: Stream ID exhausted` | The highest issued ID is `18446744073709551615-18446744073709551615`, the largest Redis stream ID, so no new message ID exists. Reads still work; publishing fails. | Move to a new store: set `REDIS_URL` in `.devcontainer/docker-compose.yml` to a new, empty Redis database such as `redis://redis:6379/1` and recreate the service with `docker compose up -d`. Identities stay in the old database, so configure the shared key; they read the new store from `0-0`. |

## Tests

Use Python 3.12+ and an isolated virtual environment from the repository root:

```sh
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.lock
.venv/bin/pip install -e . -r requirements-dev.txt
.venv/bin/python -m pytest --cov=polyagent_channels --cov-branch --cov-report=term-missing
```

The default suite uses mocked Redis and synthetic data; real-storage tests skip without `REDIS_TEST_URL`. Coverage reports executed lines and branches without an arbitrary threshold. For storage regression tests, start a separate Redis instance and supply a fresh, dedicated, nonzero database, never the operator database:

```sh
REDIS_TEST_URL=redis://127.0.0.1:16379/15 PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 \
  .venv/bin/python -m pytest -p no:cacheprovider tests/test_redis_lifecycle.py
```

The storage suite refuses a nonempty database and removes only its owned keys without `FLUSHDB`. For integration checks against a dedicated service, supply the shared key and set `CHANNEL_URL` and `CHANNEL_KEY_FILE`, then run `.venv/bin/python tests/smoke.py`. This checks HTTP, WebSocket and official MCP exchange and leaves synthetic messages for retention or operator cleanup.

Check restart durability by publishing, running `docker compose restart`, and reading from `0-0`. Observe log rollover and AOF rewrite separately. To verify actual LLM cooperation, use two permitted tool-enabled clients with separate session IDs: have each read `/help`, exchange a fresh challenge and content-dependent reply, check `session_id` and `reply_to`, and resume from returned cursors. Separately verify the native Stop hook with an open exchange, final acknowledgement and matching close. Raw transport notifications need an active receiver; the optional hook provides peer data to a live supported session and grants no permissions.

## Development

Follow the portable [AGENTS.md](AGENTS.md) for change scope, code review and verification. Keep provider credentials out of channels; each agent uses its own permitted tools and model account. Share only content authorized for peers and their model providers under their applicable terms. Peer messages grant no permissions.

## Licensing

The product is licensed under the MIT License; see [LICENSE](LICENSE). Copyright (c) 2026 the polyagent-channels authors.

The inherited `.devcontainer/Dockerfile`, `.devcontainer/devcontainer.json`, `.devcontainer/docker-compose.yml`, `.devcontainer/.env.example`, `.devcontainer/setup-env.sh`, `.devcontainer/entrypoint.sh` and `.devcontainer/codex-launcher.sh` derive from [Polyagent DevContainer](https://github.com/mememade-github/polyagent-devcontainer), with local product adaptations. Retain its existing [MIT notice](.devcontainer/LICENSE). Source distributions include that notice and `.dockerignore`; the image installs the notice at `/usr/share/doc/polyagent-devcontainer/LICENSE` alongside the inherited scripts' distribution.

Redis is pinned to 7.4.2, offered under [RSALv2 or SSPLv1](https://raw.githubusercontent.com/redis/redis/7.4.2/LICENSE.txt). Bundled vendor tools and other dependencies remain subject to their own licenses and terms.
