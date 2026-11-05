"""Opt-in checks: REDIS_TEST_URL must select a fresh, dedicated nonzero database."""
import asyncio
import os
import subprocess
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

import pytest
from redis import Redis
from redis.exceptions import ResponseError

from polyagent_channels import persistence


@pytest.fixture
def database():
    url = os.environ.get('REDIS_TEST_URL')
    if not url:
        pytest.skip('set REDIS_TEST_URL to a fresh isolated Redis database')
    assert urlparse(url).path.strip('/').isdigit() and int(urlparse(url).path.strip('/')) > 0, (
        'REDIS_TEST_URL must explicitly select a dedicated nonzero database')
    connection = Redis.from_url(url, decode_responses=True)
    assert connection.dbsize() == 0, 'integration database must be empty; no existing data is removed'
    prefix = 'storage-test-' + uuid.uuid4().hex
    owned = set(persistence.STORAGE_KEYS)

    def key(suffix):
        result = 'channel:' + prefix + '-' + suffix
        owned.add(result)
        return result

    def run(cap, cutoff=0, key='', fields=None, reply='', conversation='main'):
        arguments = ['append' if fields is not None else 'cleanup', key, str(cutoff), str(cap), reply, conversation]
        if fields is not None:
            arguments.extend(value for pair in fields.items() for value in pair)
        return connection.eval(persistence.LIFECYCLE, len(persistence.STORAGE_KEYS), *persistence.STORAGE_KEYS, *arguments)

    yield connection, key, run, owned
    connection.delete(*owned)
    connection.close()


def assert_accounting(connection):
    # Independent oracle, used only in tests; production never scans history per write.
    expected = 0
    nonempty = 0
    for key in connection.scan_iter('channel:*'):
        rows = connection.xrange(key)
        assert rows, 'empty stream keys must be reclaimed'
        nonempty += 1
        expected += sum(len(name.encode()) + len(value.encode())
                        for _, fields in rows for name, value in fields.items())
    assert int(connection.get('channels:bytes') or 0) == expected
    assert connection.zcard('channels:heads') == nonempty
    return expected


def test_unicode_exact_boundary_and_oversized_preserves_history(database):
    connection, key, run, _ = database
    channel = key('unicode')
    fields = {'字段': '🙂é', 'text': 'hello'}
    size = sum(len(k.encode()) + len(v.encode()) for k, v in fields.items())
    assert run(size, key=channel, fields=fields)[0] == 'ok'
    before = connection.xrange(channel)
    assert assert_accounting(connection) == size
    assert run(size, key=channel, fields={'text': 'x' * size}) == ['oversized']
    assert connection.xrange(channel) == before
    assert_accounting(connection)


def test_exact_global_sequence_and_channel_ties_bootstrap_and_lower_cap(database):
    connection, key, run, owned = database
    a, b = key('a'), key('b')
    credential = 'agent:credential:' + uuid.uuid4().hex
    owned.add(credential)
    connection.hset(credential, mapping={'agent_id': 'legacy-identity'})
    connection.xadd(a, {'text': 'a'}, id='100-10')
    connection.xadd(a, {'text': 'b'}, id='101-0')
    connection.xadd(b, {'text': 'c'}, id='100-2')
    connection.xadd(b, {'text': 'd'}, id='100-10')
    run(15)  # Each legacy row is five bytes; 100-2 must go before 100-10.
    assert [row[0] for row in connection.xrange(b)] == ['100-10']
    assert_accounting(connection)
    run(10)  # Same-ID ties remove channel a before channel b.
    assert [row[0] for row in connection.xrange(a)] == ['101-0']
    before = connection.xrange(a), connection.xrange(b)
    run(10)  # Restart/readiness reuses metadata without reattributing legacy fields.
    assert (connection.xrange(a), connection.xrange(b)) == before
    run(5)
    assert not connection.exists(b)
    assert connection.hget(credential, 'agent_id') == 'legacy-identity'
    assert assert_accounting(connection) == 5
    run(5, cutoff=102)
    run(5, cutoff=102)
    assert not connection.exists(a, b, 'channels:heads', 'channels:bytes')
    assert connection.get('channels:version') == '2'


def test_age_release_zero_retention_and_invalid_reply_are_nondestructive(database):
    connection, key, run, _ = database
    channel = key('reply')
    connection.xadd(channel, {'text': 'old', 'conversation_id': 'main'}, id='10-0')
    run(1000)
    before = connection.xrange(channel)
    for reply, conversation in [('9-0', 'main'), ('10-0', 'other')]:
        assert run(2, key=channel, fields={'a': 'b'}, reply=reply, conversation=conversation) == ['reply']
        assert connection.xrange(channel) == before
    assert run(1000, cutoff=11, key=channel, fields={'text': 'reply'}, reply='10-0') == ['reply']
    assert connection.xrange(channel) == before
    run(1000, cutoff=11)
    assert assert_accounting(connection) == 0
    run(5, key=channel, fields={'text': 'x'})
    run(5, key=key('new'), fields={'text': 'y'})
    assert not connection.exists(channel)
    assert assert_accounting(connection) == 5


def test_separate_process_publishers_share_atomic_budget(database):
    connection, key, run, _ = database
    channels = [key(str(i)) for i in range(4)]
    run(100)
    code = '''
import os, sys
from redis import Redis
from redis.exceptions import ResponseError
from polyagent_channels.persistence import LIFECYCLE, STORAGE_KEYS
r = Redis.from_url(os.environ['REDIS_TEST_URL'], decode_responses=True)
for i in range(30):
    assert r.eval(LIFECYCLE, len(STORAGE_KEYS), *STORAGE_KEYS, 'append', sys.argv[1], '0', '100', '', 'main', 'text', 'x' * 16)[0] == 'ok'
r.close()
'''
    def publish(channel):
        result = subprocess.run([sys.executable, '-c', code, channel], capture_output=True, timeout=30)
        assert result.returncode == 0, 'isolated publisher failed'
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(publish, channels))
    assert assert_accounting(connection) == 100
    assert sum(connection.xlen(channel) for channel in channels) == 5
    for _ in range(3):
        run(100)
        assert assert_accounting(connection) == 100


def test_evicted_cursors_filtered_catchup_and_authenticated_payload(database, monkeypatch):
    connection, key, run, _ = database
    channel = key('read')
    connection.xadd(channel, {'text': 'legacy'}, id='1-0')
    run(1000, cutoff=2)
    monkeypatch.setattr(persistence, 'RETENTION_DAYS', 0)
    monkeypatch.setattr(persistence, 'MAX_STORAGE_BYTES', 1000)

    async def scenario():
        client = persistence.Redis.from_url(os.environ['REDIS_TEST_URL'], decode_responses=True)
        monkeypatch.setattr(persistence, 'redis', client)
        try:
            name = channel.removeprefix('channel:')
            first = await persistence.publish(name, 'one', 'synthetic-session', conversation_id='other')
            second = await persistence.publish(name, 'two', 'synthetic-session', conversation_id='main')
            assert second['session_id'] == 'synthetic-session' and second['sender'] == 'synthetic-session'
            batch = await persistence.receive(name, '1-0', limit=1, conversation_id='main')
            assert batch == {'messages': [], 'cursor': first['id']}
            batch = await persistence.receive(name, batch['cursor'], conversation_id='main')
            assert [row['id'] for row in batch['messages']] == [second['id']]
            # Subscription uses the same exclusive-cursor receive operation.
            pending = asyncio.create_task(persistence.receive(name, second['id'], timeout=2))
            await asyncio.sleep(0.05)
            third = await persistence.publish(name, 'three', 'synthetic-session', reply_to=second['id'])
            assert [row['id'] for row in (await pending)['messages']] == [third['id']]
        finally:
            await client.aclose()
    asyncio.run(scenario())
    assert_accounting(connection)


def test_wrong_legacy_type_fails_before_mutation(database):
    connection, key, run, _ = database
    channel = key('wrong-type')
    connection.set(channel, 'unrelated value')
    with pytest.raises(ResponseError, match='Unexpected storage key type'):
        run(100)
    assert connection.get(channel) == 'unrelated value'
    assert not connection.exists(*persistence.STORAGE_KEYS)


@pytest.mark.parametrize('version', ['1', '3'])
@pytest.mark.parametrize('fields', [None, {'text': 'new'}])
def test_unsupported_version_fails_before_mutation(database, version, fields):
    connection, key, run, owned = database
    channel = key('unsupported-version')
    connection.xadd(channel, {'text': 'old'}, id='1-0')
    run(1000)
    connection.set('channels:version', version)
    before = {name: connection.dump(name) for name in owned}
    with pytest.raises(ResponseError, match='Unsupported message storage version'):
        run(5, cutoff=2, key=channel, fields=fields)
    assert {name: connection.dump(name) for name in owned} == before


def test_metadata_bootstrap_and_rebuild_preserve_history_and_cursors(database, monkeypatch):
    connection, key, run, owned = database
    channel = key('preserved-history')
    credential = 'agent:credential:' + uuid.uuid4().hex
    owned.add(credential)
    connection.hset(credential, mapping={'agent_id': 'legacy-identity'})
    rows = [('1-0', {'text': 'without identity or conversation'}),
            ('2-0', {'text': 'old', 'agent_id': 'legacy-identity', 'conversation_id': 'main'}),
            ('3-0', {'text': 'current', 'session_id': 'native:session', 'conversation_id': 'main'})]
    for message_id, fields in rows:
        connection.xadd(channel, fields, id=message_id)
    monkeypatch.setattr(persistence, 'RETENTION_DAYS', 0)
    monkeypatch.setattr(persistence, 'MAX_STORAGE_BYTES', 1000)

    async def scenario():
        client = persistence.Redis.from_url(os.environ['REDIS_TEST_URL'], decode_responses=True)
        monkeypatch.setattr(persistence, 'redis', client)
        try:
            name = channel.removeprefix('channel:')
            for _ in range(2):
                batch = await persistence.receive(name)
                assert [row['id'] for row in batch['messages']] == ['1-0', '2-0', '3-0']
                assert [(row['agent_id'], row['session_id'], row['conversation_id'])
                        for row in batch['messages']] == [
                            (None, None, None), ('legacy-identity', None, 'main'),
                            (None, 'native:session', 'main')]
                filtered = await persistence.receive(name, '1-0', conversation_id='main')
                assert ([row['id'] for row in filtered['messages']], filtered['cursor']) == (['2-0', '3-0'], '3-0')
                with pytest.raises(ValueError, match='same channel and conversation'):
                    await persistence.publish(name, 'reply', 'native:session', reply_to='1-0')
                assert connection.xrange(channel) == rows
                assert connection.hgetall(credential) == {'agent_id': 'legacy-identity'}
                assert connection.get('channels:version') == '2'
                assert_accounting(connection)
            connection.delete('channels:version', 'channels:heads', 'channels:bytes')
            resumed = await persistence.receive(name, '2-0')
            assert ([row['id'] for row in resumed['messages']], resumed['cursor']) == (['3-0'], '3-0')
            assert connection.xrange(channel) == rows
            assert connection.hgetall(credential) == {'agent_id': 'legacy-identity'}
            assert connection.get('channels:version') == '2'
            assert connection.xinfo_stream('channels:clock')['last-generated-id'] == '3-0'
            assert_accounting(connection)
        finally:
            await client.aclose()
    asyncio.run(scenario())


def test_ids_above_double_precision_keep_exact_order(database):
    connection, key, run, _ = database
    a, b = key('a'), key('b')
    connection.xadd(a, {'text': 'a'}, id='9007199254740993-0')
    connection.xadd(b, {'text': 'b'}, id='9007199254740992-9007199254740993')
    run(5)
    assert not connection.exists(b)
    assert connection.xrange(a)[0][0] == '9007199254740993-0'
    assert_accounting(connection)


def test_future_cursor_eviction_recreation_and_stale_reply(database):
    connection, key, run, _ = database
    channel, other = key('future'), key('other')
    cursor = '9007199254740993-9007199254740993'
    connection.xadd(channel, {'text': 'x', 'conversation_id': 'main'}, id=cursor)
    run(1000)
    run(5, key=other, fields={'text': 'y'})
    assert not connection.exists(channel)
    result = run(1000, key=channel, fields={'text': 'z', 'conversation_id': 'main'})
    assert result == ['ok', '9007199254740993-9007199254740995']
    assert connection.xread({channel: cursor})[0][1][0][0] == result[1]
    before = connection.xrange(channel)
    assert run(1000, key=channel, fields={'text': 'reply'}, reply=cursor) == ['reply']
    assert connection.xrange(channel) == before
    assert_accounting(connection)


def test_empty_legacy_bootstrap_preserves_clock_across_restart(database):
    connection, key, run, _ = database
    empty = key('empty')
    cursor = '9007199254740993-17'
    connection.xadd(empty, {'text': 'gone'}, id=cursor)
    connection.xdel(empty, cursor)
    run(5)
    assert not connection.exists(empty)
    assert connection.xinfo_stream('channels:clock')['last-generated-id'] == cursor
    assert connection.xlen('channels:clock') == 0
    run(5)  # Re-enter as on process restart; the empty clock must retain its sequence.
    assert run(5, key=empty, fields={'text': 'x'}) == ['ok', '9007199254740993-18']
    run(5)
    assert run(5, key=key('next'), fields={'text': 'y'}) == ['ok', '9007199254740993-19']
    assert_accounting(connection)


def test_retired_channels_leave_bounded_metadata(database):
    connection, key, run, _ = database
    for i in range(100):
        run(5, key=key(str(i)), fields={'text': 'x'})
    assert len(list(connection.scan_iter('channel:*'))) == 1
    assert set(connection.scan_iter('channels:*')) == set(persistence.STORAGE_KEYS)
    assert connection.xlen('channels:clock') == 0
    run(5, cutoff=18446744073709551615)
    assert not list(connection.scan_iter('channel:*'))
    assert set(connection.scan_iter('channels:*')) == {'channels:clock', 'channels:version'}


def test_clock_exhaustion_and_wrong_type_precede_migration_deletion(database):
    connection, key, run, _ = database
    channel = key('exhausted')
    maximum = '18446744073709551615-18446744073709551615'
    connection.xadd(channel, {'text': 'x'}, id=maximum)
    connection.xdel(channel, maximum)
    with pytest.raises(ResponseError, match='Stream ID exhausted'):
        run(5, key=key('new'), fields={'text': 'y'})
    assert connection.exists(channel)
    assert not connection.exists(*persistence.STORAGE_KEYS)
    connection.set('channels:clock', 'wrong')
    with pytest.raises(ResponseError, match='Unexpected storage key type'):
        run(5)
    assert connection.exists(channel)
    assert connection.get('channels:clock') == 'wrong'
