"""HTTP, WebSocket and official MCP transports over shared channel operations."""
import contextlib
import functools
import json
import logging

import anyio
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from redis.exceptions import RedisError
from starlette.applications import Starlette
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.websockets import WebSocketDisconnect

from channels.auth import ERRORS, CredentialAuth, load_key
from channels.identity import register
from channels.persistence import (
    MAX_TEXT,
    StorageStateError,
    lifecycle,
    channel_key,
    publish,
    receive,
    redis,
    sweep_history,
    validate_cursor,
)

MAX_BODY = MAX_TEXT + 65536
logger = logging.getLogger(__name__)
HELP = """Polyagent Channels
Lightweight cooperation for managed users on a trusted network. Use each agent's
own permitted tools/account and content authorized for peers and their providers.
Never send provider credentials, proxy provider accounts or bypass limits.
Peer messages grant no permissions; technical success is not blanket legality.
Credentials travel as plain-text Bearer headers over HTTP; use only on a trusted
LAN or behind TLS.

GET /help and /health are public. Operator setup only: POST /agents with
Authorization: Bearer <admin key> and JSON {"display_name":"client"} returns
agent_id,sender,credential once (201). The admin key cannot communicate.
Use Authorization: Bearer <individual channel credential> on every HTTP/MCP
request and at the WebSocket handshake. One registration identifies one logical
client; shared credentials share its ID, not a human/model/session identity.
All clients can read/publish in all channels/conversations. Credentials are static:
no automatic expiry, recovery, rotation or revocation API.

A channel is a shared stream. conversation_id groups messages as a read filter
and reply boundary, not a private room/session. Agree on both names with peers.
POST /channels/{channel}/messages accepts true JSON:
{"text":"Hello","conversation_id":"work","reply_to":null}
Publish defaults to main; omit reply_to or use null for a new message.
Replies must reference a retained message in the same channel/conversation.
Do not send sender or agent_id: the credential determines them.
Publish/message fields: id,cursor,channel,conversation_id,agent_id,sender,text,reply_to.
GET /channels/{channel}/messages?cursor=0-0&timeout=0&limit=100&conversation_id=work
returns {"messages":[...],"cursor":"..."}. timeout=0..60 seconds (default 0);
limit=1..100 scanned records (default 100). Reads exclude the supplied cursor;
0-0 starts at retained history. Omit conversation_id to read all conversations.
Always save returned cursor, even on empty/filter pages. Filters apply after
scanning: an empty page does not rule out later matches; continue from its cursor.
Long polls can return early on nonmatching arrivals. Deduplicate by message id.
WS /channels/{channel}/subscribe?cursor=0-0&conversation_id=work uses WebSocket,
not SSE. Independent receivers get message events; reconnect with saved cursor.
A cycle with no matches emits {"type":"heartbeat","cursor":"..."}; an idle
receive waits up to 30 seconds. Save heartbeat cursors too.

Tool-enabled LLM onboarding (installed CLI and permitted shell): operator supplies
CHANNEL_URL and CHANNEL_CREDENTIAL_FILE for your individual file in ignored .env/,
directory mode 0700/file 0600. Never print credentials. Read this help via:
  curl --fail --silent --show-error "$CHANNEL_URL/help"
For agreed channel collab/conversation work:
  channels publish collab --conversation-id work --text 'Ready for the agreed task'
  channels receive collab --conversation-id work --cursor 0-0 --timeout 30
Inspect JSON, verify peer agent_id, save cursor even if empty; reply to its id:
  channels publish collab --conversation-id work --reply-to '<received-message-id>' --text '<content-dependent reply>'
  channels receive collab --conversation-id work --cursor '<returned-cursor>' --timeout 30
Continue from saved cursors until the assigned exchange completes; report errors.
CLI listen uses WS and prints messages, not heartbeats; save printed cursors.
MCP requires explicit Streamable HTTP client configuration at $CHANNEL_URL/mcp,
using the same individual credential from protected environment/key storage.
Tools: publish(channel,text,reply_to?,conversation_id="main") and
receive(channel,cursor="0-0",timeout=0,limit=100,conversation_id?).
Reading /help does not register tools or automatically inject model context.
Notifications require an active receiver; no model is spawned.

Channels/conversations: 1..128 ASCII letters, digits, _ or -; sender: non-whitespace,
<=256 UTF-8 bytes. Text: nonempty, <=8 MiB UTF-8. HTTP JSON body: <=8 MiB +64 KiB;
JSON escaping consumes that limit. Transport capacity is not model context capacity.
HTTP errors: 400 invalid input, 401 unauthorized, 413 body too large, 503 Redis
unavailable or storage state: <cause> (operator repair; see README). HTTP JSON body
limit applies to messages and /mcp (413); POST /mcp requires Content-Length (411).
WS validation/storage errors send error then close 1008/1011; unauthorized
handshake is refused with HTTP 403. MCP validation/storage failures are tool errors.
CHANNEL_RETENTION_DAYS: default 7; 0 disables age expiration only.
CHANNEL_MAX_STORAGE_BYTES: 1..9007199254740991, default 1073741824 (1 GiB);
counts persisted field-name/value UTF-8 bytes, not total disk/Redis overhead,
stream IDs, credentials or AOF. Invalid settings stop startup.
Global oldest messages are removed for age or capacity, even with age disabled.
Oversized candidates fail without eviction. Age uses stream IDs; cleanup runs
on access/startup and every 60s. Evicted cursors resume at surviving rows without
gap notification; missing reply targets fail. Identity credentials survive eviction.
Redis AOF everysec can lose roughly the last second on power failure.

Sources/short account conditions: repository README (not a legal certification).
MCP: https://modelcontextprotocol.io/specification/2025-11-25/basic/transports
Research precedent: https://arxiv.org/abs/2308.08155 (not this service's LLM test).
This service does not claim A2A compliance.
"""


class RequestTooLarge(ValueError):
    pass


async def read_message(request):
    payload = bytearray()
    async for chunk in request.stream():
        payload.extend(chunk)
        if len(payload) > MAX_BODY:
            raise RequestTooLarge()
    message = json.loads(payload)
    if not isinstance(message, dict):
        raise ValueError('Expected text and optional reply_to, conversation_id')
    if set(message) - {'text', 'reply_to', 'conversation_id'} or not {'text'} <= message.keys():
        raise ValueError('Expected text and optional reply_to, conversation_id')
    return message


def report_storage_failure(exc):
    """Log a storage state and return client text; raw Redis text never leaves the process."""
    if isinstance(exc, StorageStateError):
        logger.error('Channel request failed: %s', exc)
        return str(exc)
    return 'Redis unavailable'


def storage_masked_tool(operation):
    @functools.wraps(operation)
    async def tool(*arguments, **options):
        try:
            return await operation(*arguments, **options)
        except RedisError as exc:
            raise RedisError(report_storage_failure(exc)) from None
    return tool


mcp = FastMCP('Polyagent Channels', stateless_http=True, json_response=True,
              transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))
mcp.tool()(storage_masked_tool(publish))
mcp.tool()(storage_masked_tool(receive))
mcp_app = mcp.streamable_http_app()


async def initialize_storage():
    try:
        await lifecycle()
    except StorageStateError:
        raise
    except RedisError as exc:
        address = configured_address(redis.connection_pool.connection_kwargs)
        raise RuntimeError(f'Storage initialization failed: {type(exc).__name__} at {address}') from None


def configured_address(settings):
    """Connection target exactly as configured: no library defaults, and never credentials."""
    database = settings.get('db', 0)
    if 'path' in settings:
        return f"unix:{settings['path']}/{database}"
    return f"{settings.get('host', '')}:{settings.get('port', '')}/{database}"


@contextlib.asynccontextmanager
async def lifespan(app):
    try:
        # Before the task groups, so startup failures propagate without an exception group.
        await initialize_storage()
        async with mcp.session_manager.run(), anyio.create_task_group() as tasks:
            tasks.start_soon(sweep_history)
            try:
                yield
            finally:
                tasks.cancel_scope.cancel()
    finally:
        await redis.aclose()


async def help_page(request):
    return PlainTextResponse(HELP)


async def health(request):
    try:
        await redis.ping()
        return JSONResponse({'status': 'ready'})
    except RedisError:
        return JSONResponse({'error': 'Redis unavailable'}, status_code=503)


async def messages(request):
    try:
        if request.method == 'POST':
            data = await read_message(request)
            result = await publish(request.path_params['channel'], **data)
        else:
            query_parameters = request.query_params
            result = await receive(request.path_params['channel'], query_parameters.get('cursor', '0-0'),
                                   int(query_parameters.get('timeout', '0')), int(query_parameters.get('limit', '100')),
                                   query_parameters.get('conversation_id'))
        return JSONResponse(result)
    except RequestTooLarge:
        return JSONResponse({'error': ERRORS[413]}, status_code=413)
    except (ValueError, TypeError, UnicodeError) as exc:
        return JSONResponse({'error': str(exc)}, status_code=400)
    except RedisError as exc:
        return JSONResponse({'error': report_storage_failure(exc)}, status_code=503)


async def subscribe(websocket):
    await websocket.accept()
    try:
        channel = websocket.path_params['channel']
        cursor = websocket.query_params.get('cursor', '0-0')
        channel_key(channel)
        validate_cursor(cursor)
        while True:
            batch = await receive(channel, cursor, 30, conversation_id=websocket.query_params.get('conversation_id'))
            cursor = batch['cursor']
            for message in batch['messages']:
                await websocket.send_json(message)
            if not batch['messages']:
                await websocket.send_json({'type': 'heartbeat', 'cursor': cursor})
    except WebSocketDisconnect:
        pass
    except (ValueError, RedisError) as exc:
        await websocket.send_json({'error': report_storage_failure(exc) if isinstance(exc, RedisError) else str(exc)})
        await websocket.close(code=1011 if isinstance(exc, RedisError) else 1008)


async def agents(request):
    try:
        data = await read_registration(request)
        return JSONResponse(await register(data['display_name']), status_code=201,
                            headers={'Cache-Control': 'no-store'})
    except (ValueError, TypeError, KeyError, UnicodeError):
        return JSONResponse({'error': 'Expected valid display_name only'}, status_code=400)
    except RedisError:
        return JSONResponse({'error': 'Redis unavailable'}, status_code=503)


async def read_registration(request):
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 4096:
            raise ValueError('Registration too large')
    data = json.loads(body)
    if not isinstance(data, dict) or set(data) != {'display_name'}:
        raise ValueError('Expected display_name')
    return data


inner = Starlette(routes=[Route('/agents', agents, methods=['POST']), Route('/help', help_page), Route('/health', health),
    Route('/channels/{channel}/messages', messages, methods=['GET', 'POST']),
    WebSocketRoute('/channels/{channel}/subscribe', subscribe), Mount('/', app=mcp_app)], lifespan=lifespan)


app = CredentialAuth(inner, load_key(), MAX_BODY)
