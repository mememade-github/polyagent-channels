"""HTTP, WebSocket and official MCP transports over shared channel operations."""
import contextlib
import json

import anyio
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from redis.exceptions import RedisError
from starlette.applications import Starlette
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.websockets import WebSocketDisconnect

from channels.auth import CredentialAuth, load_key
from channels.identity import register
from channels.persistence import (
    MAX_TEXT,
    lifecycle,
    channel_key,
    publish,
    receive,
    redis,
    sweep_history,
    validate_cursor,
)

MAX_BODY = MAX_TEXT + 65536
HELP = """Polyagent Channels
POST /agents with Authorization: Bearer <admin key> and {display_name} registers a client.
HTTP, WebSocket and MCP require Authorization: Bearer <individual credential>.
POST /channels/{channel}/messages: {text,reply_to?,conversation_id?} (default main).
GET /channels/{channel}/messages?cursor=0-0&timeout=0&limit=100&conversation_id=main
WS /channels/{channel}/subscribe?cursor=0-0&conversation_id=main
Omit conversation_id on reads/subscriptions for all conversations.
Messages: id,cursor,channel,conversation_id,agent_id,sender,text,reply_to.
Replies must reference the same channel/conversation. Sender is server bound.
GET returns {messages,cursor}; save returned cursor, including empty filtered reads.
MCP /mcp tools: publish, receive. Registration is operator setup only.
All registered clients can read retained history; no per-channel ACL.
CHANNEL_RETENTION_DAYS: default 7; 0 disables expiration. Invalid values stop startup.
CHANNEL_MAX_STORAGE_BYTES: positive integer, default 1073741824; counts persisted field UTF-8 bytes.
Global oldest messages are removed for age or capacity, including with expiration disabled.
Oversized messages fail without eviction. Message age uses stream IDs; cleanup on access/startup and every 60s.
Evicted cursors resume at surviving rows; expired reply targets are rejected.
Identity credentials do not expire with messages.
Channels/conversations: 1..128 ASCII letters, digits, _ or -. Text <=8 MiB UTF-8.
Notifications require an active receiver; no model is spawned.
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


mcp = FastMCP('Polyagent Channels', stateless_http=True, json_response=True,
              transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))
mcp.tool()(publish)
mcp.tool()(receive)
mcp_app = mcp.streamable_http_app()


@contextlib.asynccontextmanager
async def lifespan(app):
    try:
        async with mcp.session_manager.run(), anyio.create_task_group() as tasks:
            await lifecycle()
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
        return JSONResponse({'error': 'request exceeds size limit'}, status_code=413)
    except (ValueError, TypeError, UnicodeError) as exc:
        return JSONResponse({'error': str(exc)}, status_code=400)
    except RedisError:
        return JSONResponse({'error': 'Redis unavailable'}, status_code=503)


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
        await websocket.send_json({'error': 'Redis unavailable' if isinstance(exc, RedisError) else str(exc)})
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


app = CredentialAuth(inner, load_key())
