"""Redis Streams persistence and shared message validation."""
import asyncio
import logging
import os
import re
import time

from redis.asyncio import Redis
from redis.exceptions import RedisError


def retention_days():
    value = os.environ.get('CHANNEL_RETENTION_DAYS', '30')
    if not re.fullmatch(r'[0-9]+', value):
        raise ValueError('CHANNEL_RETENTION_DAYS must be a nonnegative integer (0 disables expiration)')
    return int(value)


RETENTION_DAYS = retention_days()
RETENTION_SWEEP_SECONDS = 60
MAX_TEXT = 8 * 1024 * 1024
redis = Redis.from_url(
    os.environ.get('REDIS_URL', 'redis://127.0.0.1:6379/0'),
    decode_responses=True, max_connections=256,
    socket_connect_timeout=3, socket_timeout=65,
)


def retention_cutoff():
    return max(0, int(time.time() * 1000) - RETENTION_DAYS * 86_400_000) if RETENTION_DAYS else 0


async def trim_history(key):
    cutoff = retention_cutoff()
    if cutoff:
        await redis.xtrim(key, minid=f'{cutoff}-0', approximate=False)


async def sweep_history():
    while True:
        try:
            async for key in redis.scan_iter(match='channel:*', count=100):
                await trim_history(key)
        except RedisError:
            logging.getLogger(__name__).error('Message retention sweep failed: Redis unavailable; retry in 60 seconds')
        await asyncio.sleep(RETENTION_SWEEP_SECONDS)


def channel_key(channel):
    if not isinstance(channel, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', channel):
        raise ValueError('Invalid channel: use 1..128 ASCII letters, digits, _ or -')
    return 'channel:' + channel


def validate_cursor(cursor):
    if not isinstance(cursor, str) or not re.fullmatch(r'[0-9]{1,20}-[0-9]{1,20}', cursor):
        raise ValueError('Invalid cursor: expected milliseconds-sequence (e.g. 0-0)')
    if any(int(n) > 2**64 - 1 for n in cursor.split('-')):
        raise ValueError('Cursor component exceeds unsigned 64-bit range')


def validate_conversation(conversation_id):
    channel_key(conversation_id)


async def publish(channel: str, text: str, reply_to: str | None = None,
                  conversation_id: str = 'main') -> dict:
    """Publish as the authenticated logical client in one conversation."""
    from channels.identity import authenticated_agent

    identity = authenticated_agent()
    key = channel_key(channel)
    validate_conversation(conversation_id)
    if not isinstance(text, str) or not text:
        raise ValueError('text must be a nonempty string')
    if len(text.encode('utf-8')) > MAX_TEXT:
        raise ValueError('text exceeds 8 MiB UTF-8 limit')
    if reply_to is not None:
        validate_cursor(reply_to)
    await trim_history(key)
    if reply_to is not None:
        referenced = await redis.xrange(key, min=reply_to, max=reply_to, count=1)
        if (not referenced or int(reply_to.split('-')[0]) < retention_cutoff()
                or referenced[0][1].get('conversation_id') != conversation_id):
            raise ValueError('reply_to must exist in the same channel and conversation')
    fields = {**identity, 'channel': channel, 'conversation_id': conversation_id, 'text': text}
    if reply_to is not None:
        fields['reply_to'] = reply_to
    message_id = await redis.xadd(key, fields)
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
    await trim_history(key)
    rows = await redis.xread({key: cursor}, count=limit, block=timeout * 1000 if timeout else None)
    cutoff = retention_cutoff()
    messages = []
    for _, entries in rows:
        for message_id, fields in entries:
            cursor = message_id
            if int(message_id.split('-')[0]) < cutoff:
                continue
            message = {**fields, 'agent_id': fields.get('agent_id'),
                       'conversation_id': fields.get('conversation_id'),
                       'id': message_id, 'cursor': message_id, 'reply_to': fields.get('reply_to')}
            if conversation_id is None or message['conversation_id'] == conversation_id:
                messages.append(message)
    return {'messages': messages, 'cursor': cursor}
