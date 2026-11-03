import asyncio

import pytest
from redis.exceptions import RedisError

from channels.persistence import (
    MAX_TEXT,
    channel_key,
    publish,
    receive,
    validate_cursor,
)


@pytest.mark.parametrize('channel', ['', 'with space', '한글', 'x' * 129])
def test_invalid_channel_is_rejected(channel):
    with pytest.raises(ValueError, match='Invalid channel'):
        channel_key(channel)


@pytest.mark.parametrize('cursor', ['bad', '-1-0', '１-０', None, '18446744073709551616-0'])
def test_invalid_cursor_is_rejected(cursor):
    with pytest.raises(ValueError, match='Cursor|cursor'):
        validate_cursor(cursor)


@pytest.mark.parametrize('fields', [
    {'text': ''}, {'text': None}, {'text': 'x' * (MAX_TEXT + 1)},
    {'text': 'hello', 'reply_to': 'bad'}, {'text': 'hello', 'conversation_id': 'bad name'},
])
def test_invalid_publish_is_rejected_before_storage(storage, fields):
    with pytest.raises(ValueError):
        asyncio.run(publish('general', **fields))
    storage.xadd.assert_not_awaited()


def test_unicode_message_larger_than_one_mib_is_preserved(storage):
    text = '문맥🙂' * 150000
    result = asyncio.run(publish('general', text, '0-1'))
    assert len(text.encode()) > 1024 * 1024 and result == {
        'channel': 'general', 'sender': 'writer', 'agent_id': 'synthetic-agent', 'conversation_id': 'main', 'text': text,
        'reply_to': '0-1', 'id': '1-0', 'cursor': '1-0',
    }


@pytest.mark.parametrize('timeout,limit', [(-1, 100), (61, 100), (0, 0), (0, 101)])
def test_receive_bounds_are_enforced(storage, timeout, limit):
    with pytest.raises(ValueError, match='timeout'):
        asyncio.run(receive('general', timeout=timeout, limit=limit))


def test_empty_read_preserves_cursor(storage):
    assert asyncio.run(receive('general', '3-0')) == {'messages': [], 'cursor': '3-0'}


def test_bounded_wait_passes_exclusive_cursor_to_redis(storage):
    asyncio.run(receive('general', '3-0', timeout=2, limit=5))
    assert storage.xread.call_args.args == ({'channel:general': '3-0'},)
    assert 1900 <= storage.xread.call_args.kwargs['block'] <= 2000


def test_read_normalizes_stream_entries(storage):
    storage.xread.return_value = [('channel:general', [('4-0', {'text': 'reply', 'reply_to': '3-0'})])]
    assert asyncio.run(receive('general', '3-0')) == {
        'messages': [{'id': '4-0', 'cursor': '4-0', 'text': 'reply', 'reply_to': '3-0', 'agent_id': None, 'conversation_id': None}], 'cursor': '4-0',
    }


def test_storage_failure_is_not_hidden(storage):
    storage.xadd.side_effect = RedisError('synthetic outage')
    with pytest.raises(RedisError):
        asyncio.run(publish('general', 'hello'))


@pytest.mark.parametrize('value,expected', [(None, 30), ('0', 0), ('7', 7)])
def test_retention_setting(monkeypatch, value, expected):
    from channels.persistence import retention_days

    monkeypatch.delenv('CHANNEL_RETENTION_DAYS', raising=False)
    if value is not None:
        monkeypatch.setenv('CHANNEL_RETENTION_DAYS', value)
    assert retention_days() == expected


@pytest.mark.parametrize('value', ['-1', '1.5', '', 'forever'])
def test_invalid_retention_is_visible(monkeypatch, value):
    from channels.persistence import retention_days

    monkeypatch.setenv('CHANNEL_RETENTION_DAYS', value)
    with pytest.raises(ValueError, match='CHANNEL_RETENTION_DAYS'):
        retention_days()


@pytest.fixture
def retention(storage, monkeypatch):
    from channels import persistence

    monkeypatch.setattr(persistence, 'RETENTION_DAYS', 1)
    monkeypatch.setattr(persistence.time, 'time', lambda: 172800)
    return storage


def test_retention_filters_expired_rows_and_keeps_conversation_cursor(retention):
    retention.xread.return_value = [('channel:general', [
        ('86399999-9', {'conversation_id': 'main'}),
        ('86400000-0', {'conversation_id': 'main'}),
        ('86400001-0', {'conversation_id': 'other'}),
    ])]
    result = asyncio.run(receive('general', '1-0', conversation_id='main'))
    assert [row['id'] for row in result['messages']] == ['86400000-0']
    assert result['cursor'] == '86400001-0'
    retention.xtrim.assert_awaited_once_with('channel:general', minid='86400000-0', approximate=False)


def test_rows_expiring_during_long_poll_are_excluded(retention, monkeypatch):
    from channels import persistence

    monkeypatch.setattr(persistence.time, 'time', lambda: next(times))
    times = iter([172800, 172801])
    retention.xread.return_value = [('channel:general', [('86400000-0', {'conversation_id': 'main'})])]
    assert asyncio.run(receive('general', timeout=1)) == {'messages': [], 'cursor': '86400000-0'}


def test_reply_to_expired_row_is_rejected_even_if_trim_races(retention):
    retention.xrange.return_value = [('1-0', {'conversation_id': 'main'})]
    with pytest.raises(ValueError, match='reply_to'):
        asyncio.run(publish('general', 'reply', '1-0'))
    retention.xadd.assert_not_awaited()


def test_publish_trims_without_expiring_credentials(retention):
    asyncio.run(publish('general', 'fresh'))
    retention.xtrim.assert_awaited_once_with('channel:general', minid='86400000-0', approximate=False)
    retention.expire.assert_not_awaited()
    retention.hset.assert_not_awaited()


def test_disabled_retention_keeps_old_history(storage):
    storage.xread.return_value = [('channel:general', [('1-0', {'text': 'old'})])]
    assert asyncio.run(receive('general'))['messages'][0]['text'] == 'old'
    asyncio.run(publish('general', 'reply', '0-1'))
    storage.xtrim.assert_not_awaited()


def test_trim_failure_is_visible_to_reader(retention):
    retention.xtrim.side_effect = RedisError('synthetic outage')
    with pytest.raises(RedisError):
        asyncio.run(receive('general'))
    retention.xread.assert_not_awaited()


@pytest.mark.parametrize('outage', [False, True])
def test_idle_sweep_retries_visibly_and_cancels(retention, monkeypatch, caplog, outage):
    from unittest.mock import AsyncMock, Mock

    from channels import persistence

    async def streams(**kwargs):
        if outage:
            raise RedisError('private data must not be logged')
        yield 'channel:idle'

    retention.scan_iter = Mock(side_effect=streams)
    sleep = AsyncMock(side_effect=asyncio.CancelledError)
    monkeypatch.setattr(persistence.asyncio, 'sleep', sleep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(persistence.sweep_history())
    retention.scan_iter.assert_called_once_with(match='channel:*', count=100)
    sleep.assert_awaited_once_with(60)
    if outage:
        assert 'retention sweep failed' in caplog.text
        assert 'private data' not in caplog.text
    else:
        retention.xtrim.assert_awaited_once_with('channel:idle', minid='86400000-0', approximate=False)
