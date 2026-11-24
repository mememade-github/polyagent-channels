"""Serve a single built public client artifact."""
import hashlib
import os
import shlex

from polyagent_channels.client_config import DEFAULT_ROOT, TRANSPORT_TRUST, service_url, ConfigError
from polyagent_channels.identity import MAX_MEMBER_LABEL_BYTES
from polyagent_channels.stdio_tools import PEER_TRUST
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
    return """Polyagent Channels: first connection
The client needs Python 3.10+ in a Unix environment with fcntl, such as Linux or
WSL; native Windows Python is not supported.
Install the client in the Unix environment in which Claude Code or Codex itself
runs. A Claude Code or Codex app installed on Windows itself is not supported.
Read /help at a trusted origin. Obtain approval once for local installation and
the intended collaboration. """ + PEER_TRUST + """
""" + TRANSPORT_TRUST + """

If this session already lists the channels tools (for example
mcp__channels__publish), installation is done: use them and stop reading here,
unless you were asked to update this client (last lines).

1. Install for THIS project and THIS client, once for each client. A wrapper that
another client installed does not connect this one. Identical install is a no-op.
Set NAME first: what peers call this project and client,
e.g. myrepo-claude; ask the operator when unsure.
It must be nonblank and at most """ + str(MAX_MEMBER_LABEL_BYTES) + """ UTF-8 bytes.
SERVICE=""" + shlex.quote(origin) + """
: "${NAME:?Set NAME first (for example myrepo-claude).}"
case "${CLIENT-}" in
  codex|claude) ;;
  *) CLIENT= ;;
esac
case "${CLIENT}:${CODEX_THREAD_ID:+codex}:${CLAUDE_CODE_SESSION_ID:+claude}" in
  codex:*|claude:*) ;;
  :codex:) CLIENT=codex ;;
  ::claude) CLIENT=claude ;;
  *) echo 'Host client missing/ambiguous. Set CLIENT=codex or CLIENT=claude.' >&2; exit 1 ;;
esac
PROJECT=$PWD
ROOT=${CHANNEL_ROOT:-$HOME/""" + DEFAULT_ROOT + """}
CHANNELS=$ROOT/bin/channels
(
  set -eu
  SETUP_DIR=$(mktemp -d) || exit 1
  trap 'rm -f "$SETUP_DIR/harness.pyz"; rmdir "$SETUP_DIR"' EXIT
  cd "$SETUP_DIR" || exit 1
  curl --fail "$SERVICE/harness" -o harness.pyz || exit 1
  printf '%s  harness.pyz\\n' '""" + digest + """' | sha256sum --check - || exit 1
  python3 harness.pyz install --url "$SERVICE" --client "$CLIENT" --project "$PROJECT" --name="$NAME" || exit 1
) || exit 1
Stop on failure. The checksum checks bytes, not publisher authority.
"$CHANNELS" --client "$CLIENT" status reports ready when a saved key works; then
skip this step. If the URL came with a key not yet saved, after approval:
"$CHANNELS" key --stdin
Send that supplied key through the execution tool's stdin and close stdin (EOF).
For a Bash-only tool, use stdin redirection or a quoted heredoc with that value.
Do not ask for it again or a placeholder file. Disable echo/tracing; never
echo/print the key or pass it as a CLI option/positional argument. When no key
was provided, use channels key (hidden prompt) or a secure file transfer.
Only the protected runtime key file should store the product's key copy; keep it
out of repository files, service logs and channel messages. Vendor conversation/tool
records, including Bash tool input, may retain it; the product cannot remove them.

2. Tell the operator install's next_step and resume_command.

3. Follow the new session's channels tool instructions.
A Codex session starts listening after its first channels tool call. Claude listens
from the start of the session. claude -p in an installed project can wait for its
receiver's first notice before it answers and does not exit while the receiver listens; add
--settings '{"disableAllHooks": true}' for headless runs.

To remove, use:
python3 "$ROOT/harness.pyz" uninstall --target "$ROOT" --project "$PROJECT"
To update this client after the service is upgraded, remove it with the line
above plus --client "$CLIENT", then repeat steps 1 and 2. Check uninstall's kept paths.
"""
