"""Connection observations use isolated fake storage, never a running service."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from redis.exceptions import RedisError

from polyagent_channels import persistence


@pytest.fixture
def session_storage(storage):
    storage.time.return_value = (1700, 123000)
    transaction = MagicMock()
    transaction.__aenter__.return_value = transaction
    transaction.execute = AsyncMock()
    storage.pipeline = MagicMock(return_value=transaction)
    return storage, transaction


def stored_session(session_id='synthetic-session', **updates):
    return {'session_id': session_id, 'state': 'waiting', 'ttl_seconds': '90',
            'last_seen_ms': '1700123', 'expires_at_ms': '1790123', **updates}


@pytest.mark.parametrize('rows,expected', [([], '0-0'), ([('1700000-4', {'text': 'old'})], '1700000-4')])
def test_cursor_reports_stream_end_and_server_time_without_mutation(storage, rows, expected):
    storage.xrevrange.return_value = rows
    storage.time.return_value = (1700, 123000)
    result = asyncio.run(persistence.channel_cursor('general'))
    assert (result, [call[0] for call in storage.mock_calls]) == (
        {'cursor': expected, 'time_ms': 1700123}, ['xrevrange', 'time'])


def test_lease_stores_atomic_expiring_observation_and_preserves_omitted_labels(session_storage):
    storage, transaction = session_storage
    transaction.execute.return_value = [1, True, stored_session(client='codex', name='Synthetic peer'), 90000, (1700, 123000)]
    record = asyncio.run(persistence.lease_session('synthetic-session', 'waiting', 90))
    mapping = transaction.hset.call_args.kwargs['mapping']
    assert (record, storage.pipeline.call_args.kwargs, mapping, transaction.expire.call_args.args) == (
        {'session_id': 'synthetic-session', 'client': 'codex', 'name': 'Synthetic peer', 'state': 'waiting',
         'ttl_seconds': 90, 'last_seen_ms': 1700123, 'expires_at_ms': 1790123},
        {'transaction': True},
        {'session_id': 'synthetic-session', 'state': 'waiting', 'ttl_seconds': 90},
        ('channels:session:synthetic-session', 90))


def test_lease_accepts_client_and_name_labels(session_storage):
    _, transaction = session_storage
    transaction.execute.return_value = [1, True, stored_session(client='cli', name='peer'), 600000, (1700, 123000)]
    asyncio.run(persistence.lease_session('synthetic-session', 'connected', 600, client='cli', name='peer'))
    assert transaction.hset.call_args.kwargs['mapping'] == {
        'session_id': 'synthetic-session', 'client': 'cli', 'name': 'peer', 'state': 'connected',
        'ttl_seconds': 600}


def test_lease_timestamps_follow_delayed_redis_commit(session_storage):
    storage, transaction = session_storage
    storage.time.return_value = (100, 0)
    transaction.execute.return_value = [1, True, stored_session(ttl_seconds='60'), 60000, (161, 0)]
    record = asyncio.run(persistence.lease_session('synthetic-session', 'waiting', 60))
    assert (record['last_seen_ms'], record['expires_at_ms'], storage.time.await_count) == (161000, 221000, 0)


@pytest.mark.parametrize('updates', [
    {'session_id': '../bad'}, {'state': 'online'}, {'state': []}, {'ttl_seconds': True},
    {'ttl_seconds': 0}, {'ttl_seconds': 601}, {'ttl_seconds': 1.5}, {'ttl_seconds': '90'},
    {'client': ''}, {'client': 'x' * 65}, {'client': 1}, {'name': ' '}, {'name': '🙂' * 65},
])
def test_invalid_lease_is_rejected_before_storage(storage, updates):
    arguments = {'session_id': 'synthetic-session', 'state': 'waiting', 'ttl_seconds': 90, **updates}
    with pytest.raises(ValueError):
        asyncio.run(persistence.lease_session(**arguments))
    assert storage.mock_calls == []


def test_list_sessions_excludes_expired_closed_and_expired_during_scan(session_storage, monkeypatch):
    storage, transaction = session_storage
    async def keys(**options):
        for key in ('live', 'expired', 'closed', 'gone', 'live'):
            yield 'channels:session:' + key

    monkeypatch.setattr(storage, 'scan_iter', keys)
    transaction.execute.side_effect = [
        [stored_session('live'), 90000, (1700, 123000)],
        [stored_session('expired'), 0, (1700, 123000)],
        [stored_session('closed', state='closed'), 90000, (1700, 123000)],
        [{}, -2, (1700, 123000)], [stored_session('live'), 90000, (1700, 123000)]]
    storage.time.return_value = (1700, 123000)
    result = asyncio.run(persistence.list_sessions())
    assert result == {'sessions': [{'session_id': 'live', 'client': None, 'name': None, 'state': 'waiting',
                                   'ttl_seconds': 90, 'last_seen_ms': 1700123, 'expires_at_ms': 1790123}]}


@pytest.mark.parametrize('method,path', [('GET', '/channels/general/cursor'), ('GET', '/sessions'), ('POST', '/sessions')])
def test_connection_endpoints_require_shared_key(client, method, path):
    assert client.request(method, path).status_code == 401


def test_cursor_http_returns_latest_position(client, headers, storage):
    storage.xrevrange.return_value = [('1700000-4', {})]
    storage.time.return_value = (1700, 123000)
    assert client.get('/channels/general/cursor', headers=headers).json() == {'cursor': '1700000-4', 'time_ms': 1700123}


def test_sessions_http_creates_observation(client, headers, session_storage):
    _, transaction = session_storage
    transaction.execute.return_value = [1, True, stored_session(), 90000, (1700, 123000)]
    response = client.post('/sessions', headers=headers, json={
        'session_id': 'synthetic-session', 'state': 'waiting', 'ttl_seconds': 90})
    assert (response.status_code, response.json()['expires_at_ms']) == (200, 1790123)


@pytest.mark.parametrize('payload', [None, [], {}, {'session_id': 's', 'state': 'waiting', 'ttl_seconds': 90, 'key': 'bad'}])
def test_sessions_http_rejects_invalid_schema(client, headers, payload):
    assert client.post('/sessions', headers=headers, json=payload).status_code == 400


@pytest.mark.parametrize('path', ['/channels/general/cursor', '/sessions'])
def test_connection_storage_errors_are_masked(client, headers, session_storage, monkeypatch, path):
    storage, transaction = session_storage
    async def keys(**options):
        yield 'channels:session:synthetic-session'

    monkeypatch.setattr(storage, 'scan_iter', keys)
    storage.xrevrange.side_effect = transaction.execute.side_effect = RedisError('private Redis details')
    response = client.get(path, headers=headers)
    assert (response.status_code, response.json()) == (503, {'error': 'Redis unavailable'})


def test_reference_help_is_public(client):
    response = client.get('/help/reference')
    assert (response.status_code, 'POST /channels/{channel}/messages' in response.text) == (200, True)
