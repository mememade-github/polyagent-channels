import asyncio
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from channels import persistence


@pytest.mark.parametrize('session_id', ['', 'has/slash', 'has space', '한글', 'x' * 257, None, 123])
def test_invalid_session_rejected_before_storage(storage, session_id):
    with pytest.raises(ValueError, match='session_id'):
        asyncio.run(persistence.publish('general', 'hello', session_id))
    storage.eval.assert_not_awaited()


@pytest.mark.parametrize('sender', ['', '  ', '🙂' * 65, 123])
def test_invalid_sender_rejected_before_storage(storage, sender):
    with pytest.raises(ValueError, match='sender'):
        asyncio.run(persistence.publish('general', 'hello', 'native:session', sender=sender))
    storage.eval.assert_not_awaited()


@pytest.mark.parametrize('session_id', ['native:session._-42', 'a' * 256])
def test_session_is_asserted_and_sender_defaults(client, headers, session_id):
    response = client.post('/channels/general/messages', headers=headers,
                           json={'session_id': session_id, 'text': 'hello'})
    assert (response.status_code, response.json()['session_id'], response.json()['sender']) == (200, session_id, session_id)


def test_sender_override_is_allowed_but_agent_id_is_not(client, headers):
    response = client.post('/channels/general/messages', headers=headers,
                           json={'session_id': 'native', 'sender': '표시🙂', 'text': 'hello'})
    assert response.json()['sender'] == '표시🙂'
    assert client.post('/channels/general/messages', headers=headers,
                       json={'session_id': 'native', 'agent_id': 'old', 'text': 'hello'}).status_code == 400


def test_missing_session_is_rejected(client, headers):
    assert client.post('/channels/general/messages', headers=headers, json={'text': 'hello'}).status_code == 400


def test_shared_auth_never_looks_up_redis_identity(client, headers, storage):
    storage.hgetall.side_effect = AssertionError('credential lookup forbidden')
    assert client.get('/status', headers=headers).json() == {'status': 'ready'}
    storage.hgetall.assert_not_awaited()


@pytest.mark.parametrize('authorization', [None, 'Bearer wrong'])
def test_status_requires_shared_key(client, authorization):
    headers = {} if authorization is None else {'Authorization': authorization}
    assert client.get('/status', headers=headers).status_code == 401


def test_status_reports_storage_outage(client, headers, storage):
    from redis.exceptions import RedisError

    storage.ping.side_effect = RedisError('private failure detail')
    response = client.get('/status', headers=headers)
    assert (response.status_code, response.json()) == (503, {'error': 'Redis unavailable'})


def test_concurrent_mcp_calls_keep_asserted_sessions(client, headers):
    def call(session_id):
        response = client.post('/mcp', headers={**headers, 'Accept': 'application/json, text/event-stream'},
                               json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                                     'params': {'name': 'publish', 'arguments': {
                                         'channel': 'general', 'session_id': session_id,
                                         'text': session_id, 'conversation_id': 'work'}}})
        result = response.json()['result']
        assert not result.get('isError'), result
        return json.loads(result['content'][0]['text'])

    with ThreadPoolExecutor(max_workers=4) as executor:
        messages = list(executor.map(call, ['codex:1', 'claude:2'] * 4))
    assert all(message['session_id'] == message['text'] and message['conversation_id'] == 'work'
               for message in messages)


def test_reply_validation_keeps_channel_and_conversation(storage):
    storage.eval.return_value = ['reply']
    with pytest.raises(ValueError, match='same channel and conversation'):
        asyncio.run(persistence.publish('other-channel', 'reply', 'session', '1-0', 'work'))
    assert storage.eval.call_args.args[7:12] == ('channel:other-channel', '0', '1073741824', '1-0', 'work')


def test_mixed_legacy_and_session_history_is_preserved(storage):
    legacy = {'conversation_id': 'work', 'agent_id': 'old-agent', 'sender': 'old', 'text': 'old'}
    current = {'conversation_id': 'work', 'session_id': 'native', 'sender': 'native', 'text': 'new'}
    storage.xread.return_value = [('channel:general', [('1-0', legacy), ('2-0', current)])]
    result = asyncio.run(persistence.receive('general', conversation_id='work'))
    assert [(row['agent_id'], row['session_id'], row['text']) for row in result['messages']] == [
        ('old-agent', None, 'old'), (None, 'native', 'new')]
    storage.hset.assert_not_awaited()
    storage.delete.assert_not_awaited()


def test_filtered_read_advances_over_nonmatching_and_legacy_rows(storage):
    storage.xread.return_value = [('channel:general', [
        ('1-0', {'sender': 'legacy'}),
        ('2-0', {'conversation_id': 'other', 'session_id': 'other'}),
        ('3-0', {'conversation_id': 'work', 'session_id': 'peer'}),
    ])]
    result = asyncio.run(persistence.receive('general', conversation_id='work'))
    assert ([row['id'] for row in result['messages']], result['cursor']) == (['3-0'], '3-0')
