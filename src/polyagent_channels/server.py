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

from polyagent_channels.harness import download, setup_help
from polyagent_channels.stdio_tools import ANNOUNCEMENT_NOTICE, PEER_TRUST
from polyagent_channels.client_config import TRANSPORT_TRUST
from polyagent_channels.identity import DEFAULT_CONVERSATION, MAX_IDENTIFIER_LENGTH, MAX_MEMBER_LABEL_BYTES, MAX_SESSION_ID_LENGTH, MAX_SESSION_TOPICS
from polyagent_channels.auth import ERRORS, CredentialAuth, load_key
from polyagent_channels.persistence import (
    MAX_TEXT,
    READ_CURSOR,
    READ_TIMEOUT,
    MAX_READ_TIMEOUT,
    MAX_READ_LIMIT,
    SESSION_STATES,
    MAX_SESSION_TTL,
    MAX_CLIENT_LABEL_BYTES,
    StorageStateError,
    lifecycle,
    channel_key,
    channel_cursor,
    close_storage,
    configure_storage,
    lease_session,
    list_sessions,
    ping_storage,
    publish,
    receive,
    storage_address,
    sweep_history,
    validate_cursor,
)

MAX_BODY = MAX_TEXT + 65536
logger = logging.getLogger(__name__)
HELP = f"""Polyagent Channels protocol reference
GET /help, /help/reference, /health, /harness and /harness.sha256 are public.
GET /harness and /harness.sha256 return 503 when the artifact is unavailable.
All other HTTP/MCP requests and WebSocket handshakes require
Authorization: Bearer <container-owned shared key>. {TRANSPORT_TRUST}
GET /status authenticates and returns {{"status":"ready"}} when Redis answers.
Service readiness does not establish reception or an online peer.
{PEER_TRUST}

POST /channels/{{channel}}/messages accepts JSON:
{{"session_id":"native-session-id","text":"Hello","conversation_id":"work"}}
session_id is required: 1..{MAX_SESSION_ID_LENGTH} ASCII letters/digits/._:- (no slash). Optional sender
defaults to session_id and must be non-whitespace, <={MAX_MEMBER_LABEL_BYTES} UTF-8 bytes. Optional
to names a recipient and has the same limit as sender. Optional reply_to references
retained history in this channel/conversation. conversation_id
defaults to {DEFAULT_CONVERSATION}. Message fields are session_id,sender,channel,
conversation_id,text,reply_to,to,id,cursor; absent to is null when reading.
Existing agent_id/history stay readable.
Rows missing identity/conversation fields remain readable without a conversation
filter; absent session_id,agent_id,conversation_id are null. Missing conversation_id
excludes a row from filtered reads and reply targets; missing identity alone does
not exclude a reply target. Channels/conversations allow 1..{MAX_IDENTIFIER_LENGTH} ASCII letters,
digits,_,-. Text must be nonempty and <={MAX_TEXT // (1024 * 1024)} MiB UTF-8;
JSON is capped at {MAX_TEXT // (1024 * 1024)} MiB +{(MAX_BODY - MAX_TEXT) // 1024} KiB.

GET /channels/{{channel}}/messages accepts cursor,timeout,limit,conversation_id.
Defaults are cursor={READ_CURSOR}, timeout={READ_TIMEOUT}, limit={MAX_READ_LIMIT}. timeout=0..{MAX_READ_TIMEOUT}; limit=1..{MAX_READ_LIMIT} scanned
rows. Omit conversation_id for all conversations. The response is
{{"messages":[...],"cursor":"..."}}. Save its cursor even after empty filtered pages.
Reads exclude the cursor; 0-0 requests retained history.
GET /channels/{{channel}}/cursor returns the latest stream cursor (or 0-0) and
Redis time_ms. It does not read messages or prove peer presence.
WS /channels/{{channel}}/subscribe accepts cursor and conversation_id. Save message
and heartbeat cursors, then reconnect from the saved cursor.
MCP Streamable HTTP: /mcp exposes publish and receive with the fields and
defaults above.
POST /mcp requires Content-Length; oversize checks precede authentication.

POST /sessions announces an asserted session. {ANNOUNCEMENT_NOTICE}
{{"session_id":"native-id","state":"connected","ttl_seconds":600,
 "client":"claude","name":"Research","topics":["general/main"]}}
Required fields: session_id,state,ttl_seconds. States are {','.join(SESSION_STATES)}.
TTL is 1..{MAX_SESSION_TTL} seconds. Optional client is nonblank and <={MAX_CLIENT_LABEL_BYTES} UTF-8 bytes;
optional name is nonblank and <={MAX_MEMBER_LABEL_BYTES} UTF-8 bytes. Optional topics is a list of at most
{MAX_SESSION_TOPICS} channel/conversation_id labels. Omitted client, name and topics retain their values;
topics is returned as a list, or null for an announcement without it.
GET /sessions lists unexpired, non-closed announcements. The CLI peers command
uses this list.

HTTP errors: 400 invalid input, 401 unauthorized, 411 missing MCP Content-Length,
413 oversized body, 503 Redis unavailable or storage state: <cause>
(operator repair; see REFERENCE.md). WebSocket validation/storage errors send an
error then close 1008/1011; unauthorized handshakes are refused. MCP failures
are tool errors. Raw Redis details and keys are not returned.

"""


class RequestTooLarge(ValueError):
    pass


async def read_json(request):
    payload = bytearray()
    async for chunk in request.stream():
        payload.extend(chunk)
        if len(payload) > MAX_BODY:
            raise RequestTooLarge()
    # The parser recurses once per nesting level, so a deeply nested body would otherwise become a server error.
    try:
        return json.loads(payload)
    except RecursionError:
        raise ValueError('JSON nesting is too deep') from None


async def read_message(request):
    message = await read_json(request)
    if not isinstance(message, dict):
        raise ValueError('Expected session_id, text and optional sender, reply_to, conversation_id, to')
    if set(message) - {'session_id', 'sender', 'text', 'reply_to', 'conversation_id', 'to'} or not {'session_id', 'text'} <= message.keys():
        raise ValueError('Expected session_id, text and optional sender, reply_to, conversation_id, to')
    return message


async def read_session(request):
    session = await read_json(request)
    if (not isinstance(session, dict) or set(session) - {'session_id', 'state', 'ttl_seconds', 'client', 'name', 'topics'}
            or not {'session_id', 'state', 'ttl_seconds'} <= session.keys()):
        raise ValueError('Expected session_id, state, ttl_seconds and optional client, name, topics')
    return session


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
NESTING_ERROR = {'jsonrpc': '2.0', 'id': 'server-error',
                 'error': {'code': -32700, 'message': 'Parse error: JSON nesting is too deep'}}


def nested_too_deeply(body):
    try:
        json.loads(body)
    except RecursionError:
        return True
    except ValueError:
        return False  # the MCP library reports other invalid JSON itself
    return False


async def request_messages(receive):
    """Collect the request body messages; CredentialAuth bounds the size of a POST /mcp body."""
    messages = [await receive()]
    while messages[-1].get('more_body'):
        messages.append(await receive())
    return messages


def replayed(messages, receive):
    """A receive callable that hands the collected messages on again before reading further ones."""
    pending = list(messages)

    async def receive_again():
        return pending.pop(0) if pending else await receive()
    return receive_again


class JsonNestingGuard:
    """The MCP library answers only JSONDecodeError with 400, so a deeply nested body would end in its 500."""

    def __init__(self, application):
        self.application = application

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or scope['method'] != 'POST' or scope['path'] != '/mcp':
            return await self.application(scope, receive, send)
        messages = await request_messages(receive)
        if nested_too_deeply(b''.join(message.get('body', b'') for message in messages)):
            return await JSONResponse(NESTING_ERROR, status_code=400)(scope, receive, send)
        return await self.application(scope, replayed(messages, receive), send)


async def initialize_storage():
    try:
        await lifecycle()
    except StorageStateError:
        raise
    except RedisError as exc:
        raise RuntimeError(f'Storage initialization failed: {type(exc).__name__} at {storage_address()}') from None


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
        await close_storage()


async def help_page(request):
    introduction = setup_help(str(request.base_url))
    if introduction is None:
        return PlainTextResponse('Harness artifact unavailable', status_code=503)
    return PlainTextResponse(introduction)


async def reference_page(request):
    return PlainTextResponse(HELP)


async def health(request):
    try:
        await ping_storage()
        return JSONResponse({'status': 'ready'})
    except RedisError:
        return JSONResponse({'error': 'Redis unavailable'}, status_code=503)


async def messages(request):
    try:
        if request.method == 'POST':
            return JSONResponse(await publish(request.path_params['channel'], **await read_message(request)))
        query_parameters = request.query_params
        return JSONResponse(await receive(request.path_params['channel'], query_parameters.get('cursor', READ_CURSOR),
                                          int(query_parameters.get('timeout', READ_TIMEOUT)),
                                          int(query_parameters.get('limit', MAX_READ_LIMIT)),
                                          query_parameters.get('conversation_id')))
    except RequestTooLarge:
        return JSONResponse({'error': ERRORS[413]}, status_code=413)
    except (ValueError, TypeError, UnicodeError) as exc:
        return JSONResponse({'error': str(exc)}, status_code=400)
    except RedisError as exc:
        return JSONResponse({'error': report_storage_failure(exc)}, status_code=503)


async def cursor(request):
    try:
        return JSONResponse(await channel_cursor(request.path_params['channel']))
    except ValueError as exc:
        return JSONResponse({'error': str(exc)}, status_code=400)
    except RedisError as exc:
        return JSONResponse({'error': report_storage_failure(exc)}, status_code=503)


async def sessions(request):
    try:
        if request.method == 'POST':
            return JSONResponse(await lease_session(**await read_session(request)))
        return JSONResponse(await list_sessions())
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
        cursor = websocket.query_params.get('cursor', READ_CURSOR)
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




inner = Starlette(routes=[Route('/status', health),
    Route('/harness', download), Route('/harness.sha256', download), Route('/help', help_page), Route('/health', health),
    Route('/help/reference', reference_page),
    Route('/channels/{channel}/messages', messages, methods=['GET', 'POST']),
    Route('/channels/{channel}/cursor', cursor), Route('/sessions', sessions, methods=['GET', 'POST']),
    WebSocketRoute('/channels/{channel}/subscribe', subscribe), Mount('/', app=JsonNestingGuard(mcp_app))], lifespan=lifespan)


def create_app():
    """Build the service when it starts: importing this module reads no settings and creates no key or client."""
    configure_storage()
    return CredentialAuth(inner, load_key(), MAX_BODY)
