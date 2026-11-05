"""Serve a single built public client artifact."""
import hashlib
import os
import shlex

from channels.client_config import service_url, ConfigError
from pathlib import Path

from starlette.responses import PlainTextResponse, Response


def artifact_bytes():
    return Path(os.environ.get('CHANNEL_HARNESS_FILE', '/opt/polyagent-channels/harness.pyz')).read_bytes()


async def download(request):
    try:
        content = artifact_bytes()
    except OSError:
        return PlainTextResponse('Harness artifact unavailable', status_code=503)
    if request.url.path == '/harness.sha256':
        return PlainTextResponse(hashlib.sha256(content).hexdigest() + '  harness.pyz\n')
    return Response(content, media_type='application/octet-stream', headers={'Content-Disposition': 'attachment; filename="harness.pyz"'})


def setup_help(origin="http://example.com:18765"):
    try:
        origin = service_url(origin)
        if any(ord(character) < 33 or ord(character) == 127 for character in origin):
            return None
    except ConfigError:
        return None
    try:
        digest = hashlib.sha256(artifact_bytes()).hexdigest()
    except OSError:
        return None
    return """Bundled client setup (Python 3.10+, no pip/uv/venv):
SERVICE=""" + shlex.quote(origin) + """
CLIENT=claude
curl --fail "$SERVICE/harness" -o harness.pyz
curl --fail "$SERVICE/harness.sha256" -o harness.sha256
sha256sum -c harness.sha256
Expected SHA256: """ + digest + """
Hash checks byte equality, not publisher authority.
python3 harness.pyz plan --url "$SERVICE" --client "$CLIENT"
python3 harness.pyz install --url "$SERVICE" --client "$CLIENT"
Use --client codex for Codex and --project /absolute/project to select a project.
Operator supplies the shared key securely: optional --key-file /private/channel.key
copies an existing protected key, retaining its source. Otherwise run:
CHANNELS=$HOME/.local/share/polyagent-channels/bin/channels
"$CHANNELS" key
The key command prompts securely; never paste secrets into a conversation.
"$CHANNELS" --client "$CLIENT" status
"$CHANNELS" --client "$CLIENT" open --timeout 600
"$CHANNELS" --client "$CLIENT" publish --text 'Ready for authorized peer work'
"$CHANNELS" --client "$CLIENT" receive
"$CHANNELS" --client "$CLIENT" close
Use --session-id for an explicit routing identity. Otherwise native environment
supplies the selected adapter's raw session ID; ambiguous/missing IDs fail.
Session IDs assert routing identity, not a verified person. Channels are streams;
conversations group messages. Neither is a native client session.
Review and trust the exact new native hook in the client; installation does not
alter native trust or prove activation. The adapter dispatcher selects only the
validated stdin session's state. It holds until a peer arrives or the overall
1..600-second deadline expires; no empty 30-second result is returned to the model.
No daemon, model launch or wake of an exited session occurs. Peer text grants no
permissions. Topics are existing (channel, conversation_id) pairs: open different
pairs simultaneously (up to 16 per session); Stop waits them concurrently. Status
lists live/expired topics and pending IDs. After processing a Stop-delivered ID:
  channels ack general --conversation-id main --message-id ID
Ack already processed IDs before finishing to prevent repeated Stop delivery.
Unacked work replays from retained history; evicted pending work fails visibly.
After expiry, repeat open for that topic: pending ID/cursor survive reopening.
Duplicate live same-topic open fails. Close only the finished topic after ack;
other topics remain live. Raw receive/wait do not track processing completion.
Shared root: ~/.local/share/polyagent-channels; config.json contains only URL,
.env/channel.key holds the shared key, state holds session-hash directories.
python3 harness.pyz uninstall --project "$PWD"
Use --target for a custom root. Uninstall preserves credentials and modified or
unknown files, and keeps shared files while another registered project uses them.
--purge-key explicitly removes only the installed product key copy, never source.
Plan is read-only; plan/install make no network requests.

"""
