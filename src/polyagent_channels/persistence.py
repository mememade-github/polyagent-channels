"""Redis Streams persistence and shared message validation."""
import asyncio
import logging
import os
import re
import time
from importlib.resources import files

from polyagent_channels.identity import (is_cursor_form, is_identifier, validate_sender, validate_session_id,
                                         within_cursor_range)

from redis.asyncio import Redis
from redis.exceptions import RedisError, ResponseError


DEFAULT_REDIS_URL = 'redis://127.0.0.1:6379/0'
DEFAULT_RETENTION_DAYS = 7
DEFAULT_MAX_STORAGE_BYTES = 1073741824


def retention_days():
    value = os.environ.get('CHANNEL_RETENTION_DAYS', str(DEFAULT_RETENTION_DAYS))
    if not re.fullmatch(r'[0-9]+', value):
        raise ValueError('CHANNEL_RETENTION_DAYS must be a nonnegative integer (0 disables expiration)')
    return int(value)


def max_storage_bytes():
    value = os.environ.get('CHANNEL_MAX_STORAGE_BYTES', str(DEFAULT_MAX_STORAGE_BYTES))
    if not re.fullmatch(r'[0-9]+', value) or not 0 < int(value) <= 2**53 - 1:
        raise ValueError('CHANNEL_MAX_STORAGE_BYTES must be a positive integer <= 9007199254740991')
    return int(value)


# configure_storage() replaces these from the environment when the service starts; importing reads nothing.
MAX_STORAGE_BYTES = DEFAULT_MAX_STORAGE_BYTES
LIFECYCLE = files('polyagent_channels').joinpath('lifecycle.lua').read_text(encoding='utf-8')
STORAGE_KEYS = ('channels:heads', 'channels:bytes', 'channels:version', 'channels:clock')
RETENTION_DAYS = DEFAULT_RETENTION_DAYS
RETENTION_SWEEP_SECONDS = 60
MAX_TEXT = 8 * 1024 * 1024
STORAGE_STATE_REPLIES = frozenset({
    'Invalid message lifecycle arguments', 'Unsupported message storage version', 'Message clock must be empty',
    'Invalid message byte counter', 'Missing message clock', 'Stream ID exhausted',
})
# Lua error() adds the script position; Redis and redis-py add the script SHA and source suffix.
STORAGE_TYPE_ERROR = re.compile(
    r'user_script:[0-9]+: Unexpected storage key type script: [0-9a-f]{40}, on @user_script:[0-9]+\.')
redis = None


def configure_storage():
    """Read the storage settings and create the Redis client; the service calls this once when it starts."""
    global redis, RETENTION_DAYS, MAX_STORAGE_BYTES
    RETENTION_DAYS, MAX_STORAGE_BYTES = retention_days(), max_storage_bytes()
    redis = Redis.from_url(os.environ.get('REDIS_URL', DEFAULT_REDIS_URL), decode_responses=True,
                           max_connections=256, socket_connect_timeout=3, socket_timeout=65)


def storage_address():
    """Connection target exactly as configured: no library defaults, and never credentials."""
    settings = redis.connection_pool.connection_kwargs
    database = settings.get('db', 0)
    if 'path' in settings:
        return f"unix:{settings['path']}/{database}"
    return f"{settings.get('host', '')}:{settings.get('port', '')}/{database}"


async def ping_storage():
    await redis.ping()


async def close_storage():
    await redis.aclose()


class StorageStateError(RedisError):
    """Service-owned Redis metadata needs operator repair (see the REFERENCE.md recovery table)."""

    def __init__(self, phrase):
        super().__init__(f'storage state: {phrase}')


def storage_state_phrase(message):
    if message in STORAGE_STATE_REPLIES:
        return message
    if STORAGE_TYPE_ERROR.fullmatch(message):
        return 'Unexpected storage key type'
    return None


def retention_cutoff():
    return max(0, int(time.time() * 1000) - RETENTION_DAYS * 86_400_000) if RETENTION_DAYS else 0


async def lifecycle(key='', fields=None, reply_to=None, conversation_id=''):
    arguments = ['append' if fields is not None else 'cleanup', key,
                 str(retention_cutoff()), str(MAX_STORAGE_BYTES), reply_to or '', conversation_id]
    if fields is not None:
        arguments.extend(value for pair in fields.items() for value in pair)
    try:
        result = await redis.eval(LIFECYCLE, len(STORAGE_KEYS), *STORAGE_KEYS, *arguments)
    except ResponseError as error:
        phrase = storage_state_phrase(str(error))
        if phrase is None:
            raise
        raise StorageStateError(phrase) from None
    if result[0] == 'oversized':
        raise ValueError('message exceeds CHANNEL_MAX_STORAGE_BYTES')
    if result[0] == 'reply':
        raise ValueError('reply_to must exist in the same channel and conversation')
    return result[1]


async def sweep_history():
    while True:
        try:
            await lifecycle()
        except RedisError as error:
            # Only canonical storage states carry text; other Redis errors log their class alone.
            reason = error if isinstance(error, StorageStateError) else type(error).__name__
            logging.getLogger(__name__).error('Message retention sweep failed: %s; retry in 60 seconds', reason)
        await asyncio.sleep(RETENTION_SWEEP_SECONDS)


def channel_key(channel):
    if not is_identifier(channel):
        raise ValueError('Invalid channel: use 1..128 ASCII letters, digits, _ or -')
    return 'channel:' + channel


def validate_cursor(cursor):
    if not is_cursor_form(cursor):
        raise ValueError('Invalid cursor: expected milliseconds-sequence (e.g. 0-0)')
    if not within_cursor_range(cursor):
        raise ValueError('Cursor component exceeds unsigned 64-bit range')


def validate_conversation(conversation_id):
    channel_key(conversation_id)


async def server_time_ms():
    seconds, microseconds = await redis.time()
    return seconds * 1000 + microseconds // 1000


async def channel_cursor(channel: str) -> dict:
    """Observe the current stream end without changing retained messages."""
    rows = await redis.xrevrange(channel_key(channel), count=1)
    return {'cursor': rows[0][0] if rows else '0-0', 'time_ms': await server_time_ms()}


async def lease_session(session_id: str, state: str, ttl_seconds: int,
                        client: str | None = None, name: str | None = None) -> dict:
    """Record a bounded, asserted observation, not model liveness or verified identity."""
    validate_session_id(session_id)
    if not isinstance(state, str) or state not in {'connected', 'waiting', 'active', 'closed'}:
        raise ValueError('state must be connected, waiting, active or closed')
    if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 600:
        raise ValueError('ttl_seconds must be an integer in 1..600')
    for field, value, limit in (('client', client, 64), ('name', name, 256)):
        if value is not None and (not isinstance(value, str) or not value.strip()
                                  or len(value.encode('utf-8')) > limit):
            raise ValueError(f'{field} must be nonempty and <={limit} UTF-8 bytes')
    record = {'session_id': session_id, 'state': state, 'ttl_seconds': ttl_seconds}
    if client is not None:
        record['client'] = client
    if name is not None:
        record['name'] = name
    key = 'channels:session:' + session_id
    async with redis.pipeline(transaction=True) as transaction:
        transaction.hset(key, mapping=record)
        transaction.expire(key, ttl_seconds)
        transaction.hgetall(key)
        transaction.pttl(key)
        transaction.time()
        results = await transaction.execute()
    return session_record(*results[-3:])


def session_record(fields, remaining_ms, now):
    seconds, microseconds = now
    expires_at_ms = seconds * 1000 + microseconds // 1000 + remaining_ms
    ttl_seconds = int(fields['ttl_seconds'])
    return {**fields, 'client': fields.get('client'), 'name': fields.get('name'),
            'ttl_seconds': ttl_seconds, 'last_seen_ms': expires_at_ms - ttl_seconds * 1000,
            'expires_at_ms': expires_at_ms}


async def list_sessions() -> dict:
    """List unexpired lease observations; absent sessions may simply be disconnected."""
    records = {}
    async for key in redis.scan_iter(match='channels:session:*', count=100):
        async with redis.pipeline(transaction=True) as transaction:
            transaction.hgetall(key)
            transaction.pttl(key)
            transaction.time()
            value, remaining_ms, now = await transaction.execute()
        if value and remaining_ms > 0:
            record = session_record(value, remaining_ms, now)
            records[record['session_id']] = record
    now = await server_time_ms()
    return {'sessions': [records[key] for key in sorted(records)
                         if records[key]['expires_at_ms'] > now and records[key]['state'] != 'closed']}


async def publish(channel: str, text: str, session_id: str, reply_to: str | None = None,
                  conversation_id: str = 'main', sender: str | None = None) -> dict:
    """Publish with an asserted routing session, not a verified person identity."""
    validate_session_id(session_id)
    if sender is None:
        sender = session_id
    validate_sender(sender)
    key = channel_key(channel)
    validate_conversation(conversation_id)
    if not isinstance(text, str) or not text:
        raise ValueError('text must be a nonempty string')
    if len(text.encode('utf-8')) > MAX_TEXT:
        raise ValueError('text exceeds 8 MiB UTF-8 limit')
    if reply_to is not None:
        validate_cursor(reply_to)
    fields = {'session_id': session_id, 'sender': sender, 'channel': channel, 'conversation_id': conversation_id, 'text': text}
    if reply_to is not None:
        fields['reply_to'] = reply_to
    message_id = await lifecycle(key, fields, reply_to, conversation_id)
    return {**fields, 'reply_to': reply_to, 'id': message_id, 'cursor': message_id}


async def receive(channel: str, cursor: str = '0-0', timeout: int = 0, limit: int = 100,
                  conversation_id: str | None = None) -> dict:
    """Read one bounded stream page, optionally filtered; cursor includes skipped rows."""
    key = channel_key(channel)
    validate_cursor(cursor)
    if conversation_id is not None:
        validate_conversation(conversation_id)
    if not 0 <= timeout <= 60 or not 1 <= limit <= 100:
        raise ValueError('timeout must be 0..60; limit must be 1..100')
    await lifecycle(key)
    rows = await redis.xread({key: cursor}, count=limit, block=timeout * 1000 if timeout else None)
    cutoff = retention_cutoff()
    messages = []
    for _, entries in rows:
        for message_id, fields in entries:
            cursor = message_id
            if int(message_id.split('-')[0]) < cutoff:
                continue
            message = {**fields, 'session_id': fields.get('session_id'), 'agent_id': fields.get('agent_id'),
                       'conversation_id': fields.get('conversation_id'),
                       'id': message_id, 'cursor': message_id, 'reply_to': fields.get('reply_to')}
            if conversation_id is None or message['conversation_id'] == conversation_id:
                messages.append(message)
    return {'messages': messages, 'cursor': cursor}
