import asyncio
import importlib
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from channels import persistence
from channels.identity import authenticate, credential_key, current_agent

ADMIN = {'Authorization': 'Bearer synthetic-admin-key'}


def register(client, name='same name'):
    response = client.post('/agents', headers=ADMIN, json={'display_name': name})
    assert response.status_code == 201
    return response.json()


def authorization(identity):
    return {'Authorization': 'Bearer ' + identity['credential']}


def test_registration_is_distinct_and_stores_only_digest(client, storage):
    first, second = register(client), register(client)
    assert first['agent_id'] != second['agent_id']
    assert first['credential'] != second['credential']
    assert first['sender'] == second['sender']
    stored = storage.hset.call_args_list[0]
    assert stored.args == (credential_key(first['credential']),)
    assert stored.kwargs['mapping'] == {'agent_id': first['agent_id'], 'sender': first['sender']}
    assert first['credential'] not in str(stored)
    assert asyncio.run(authenticate(first['credential'])) == stored.kwargs['mapping']


def test_admin_only_registers_individual_only_communicates(client):
    identity = register(client)
    assert client.get('/channels/general/messages', headers=ADMIN).status_code == 401
    assert client.post('/agents', headers=authorization(identity), json={'display_name': 'other'}).status_code == 401


def test_published_identity_is_bound_and_spoof_fields_rejected(client):
    first, second = register(client), register(client)
    for identity in (first, second):
        result = client.post('/channels/general/messages', headers=authorization(identity), json={'text': 'hello'}).json()
        assert (result['agent_id'], result['sender']) == (identity['agent_id'], identity['sender'])
    for field in ('sender', 'agent_id'):
        assert client.post('/channels/general/messages', headers=authorization(first),
                           json={'text': 'hello', field: second[field]}).status_code == 400


@pytest.mark.parametrize('conversation,referenced', [('other', [('1-0', {'conversation_id': 'main'})]), ('main', [])])
def test_reply_must_exist_in_same_channel_and_conversation(storage, conversation, referenced):
    storage.xrange.return_value = referenced
    with pytest.raises(ValueError, match='same channel and conversation'):
        asyncio.run(persistence.publish('other-channel', 'reply', '1-0', conversation))
    storage.xadd.assert_not_awaited()
    assert storage.xrange.call_args.args == ('channel:other-channel',)


def test_filtered_history_scans_nonmatching_pages_and_preserves_matching_cursor(storage):
    storage.xread.side_effect = [
        [('channel:general', [('1-0', {'conversation_id': 'other'})])],
        [('channel:general', [('2-0', {'conversation_id': 'main', 'agent_id': 'agent'})])],
    ]
    first_page = asyncio.run(persistence.receive('general', limit=1, conversation_id='main'))
    assert first_page == {'messages': [], 'cursor': '1-0'}
    result = asyncio.run(persistence.receive('general', first_page['cursor'], limit=1, conversation_id='main'))
    assert [message['id'] for message in result['messages']] == ['2-0']
    assert result['cursor'] == '2-0'
    assert storage.xread.call_args_list[1].args == ({'channel:general': '1-0'},)


def test_empty_filtered_scan_advances_and_legacy_is_unattributed(storage):
    storage.xread.side_effect = [[('channel:general', [('1-0', {'sender': 'old'})])]]
    assert asyncio.run(persistence.receive('general', conversation_id='main')) == {'messages': [], 'cursor': '1-0'}
    storage.xread.side_effect = None
    storage.xread.return_value = [('channel:general', [('1-0', {'sender': 'old'})])]
    message = asyncio.run(persistence.receive('general'))['messages'][0]
    assert message['agent_id'] is None and message['conversation_id'] is None


def test_concurrent_mcp_requests_keep_individual_identity(client, storage):
    identities = [register(client) for _ in range(2)]

    async def delayed_publish(key, fields):
        await asyncio.sleep(0.01)
        assert current_agent.get()['agent_id'] == fields['agent_id']
        return '1-0'

    storage.xadd.side_effect = delayed_publish

    def call(identity):
        response = client.post('/mcp', headers={**authorization(identity), 'Accept': 'application/json, text/event-stream'},
                               json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                                     'params': {'name': 'publish', 'arguments': {
                                         'channel': 'general', 'text': identity['agent_id'], 'conversation_id': 'work'}}})
        result = response.json()['result']
        assert not result.get('isError'), result
        message = json.loads(result['content'][0]['text'])
        return message

    with ThreadPoolExecutor(max_workers=4) as executor:
        messages = list(executor.map(call, identities * 6))
    assert all(message['agent_id'] == message['text'] and message['conversation_id'] == 'work' for message in messages)


def test_websocket_conversation_filter_uses_scan_cursor(client, headers, server_module, monkeypatch):
    from unittest.mock import AsyncMock

    from starlette.websockets import WebSocketDisconnect

    reader = AsyncMock(side_effect=[{'messages': [], 'cursor': '9-0'}, WebSocketDisconnect()])
    monkeypatch.setattr(server_module, 'receive', reader)
    with client.websocket_connect('/channels/general/subscribe?conversation_id=work', headers=headers) as websocket:
        assert websocket.receive_json() == {'type': 'heartbeat', 'cursor': '9-0'}
    assert reader.call_args.args == ('general', '9-0', 30)
    assert reader.call_args.kwargs == {'conversation_id': 'work'}


@pytest.mark.parametrize('payload', [{}, [], {'display_name': ''}, {'display_name': 3},
                                    {'display_name': 'x' * 300}, {'display_name': 'valid', 'agent_id': 'chosen'}])
def test_registration_rejects_invalid_identity(client, payload):
    assert client.post('/agents', headers=ADMIN, json=payload).status_code == 400


def test_registration_limits_body_and_reports_storage_failure(client, storage):
    from redis.exceptions import RedisError

    assert client.post('/agents', headers=ADMIN, content='x' * 4097).status_code == 400
    storage.hset.side_effect = RedisError('synthetic outage')
    assert client.post('/agents', headers=ADMIN, json={'display_name': 'client'}).status_code == 503


def test_credential_lookup_outage_is_visible_without_value(client, headers, storage):
    from redis.exceptions import RedisError

    storage.hgetall.side_effect = RedisError('synthetic-secret-must-not-appear')
    response = client.get('/channels/general/messages', headers=headers)
    assert response.status_code == 503
    assert response.json() == {'error': 'Redis unavailable'}


def test_core_publish_requires_identity(storage):
    token = current_agent.set(None)
    try:
        with pytest.raises(ValueError, match='Individual credential required'):
            asyncio.run(persistence.publish('general', 'hello'))
    finally:
        current_agent.reset(token)
    storage.xadd.assert_not_awaited()


def test_admission_rotation_preserves_registered_identity(server_module, storage, monkeypatch):
    from starlette.testclient import TestClient

    from channels.auth import CredentialAuth

    with TestClient(CredentialAuth(server_module.inner, 'synthetic-admin-key')) as original:
        identity = register(original)
    importlib.reload(server_module)
    monkeypatch.setattr(server_module, 'redis', storage)
    with TestClient(CredentialAuth(server_module.inner, 'synthetic-replacement-admin')) as restarted:
        assert restarted.post('/agents', headers=ADMIN, json={'display_name': 'new'}).status_code == 401
        response = restarted.post('/channels/general/messages', headers=authorization(identity), json={'text': 'after restart'})
        assert response.json()['agent_id'] == identity['agent_id']
        assert restarted.post('/agents', headers={'Authorization': 'Bearer synthetic-replacement-admin'},
                              json={'display_name': 'new'}).status_code == 201


def test_mixed_history_filter_keeps_each_conversation_separate(storage):
    storage.xread.return_value = [('channel:general', [
        ('1-0', {'conversation_id': 'first', 'agent_id': 'a', 'text': 'one'}),
        ('2-0', {'conversation_id': 'second', 'agent_id': 'b', 'text': 'two'}),
        ('3-0', {'conversation_id': 'first', 'agent_id': 'b', 'text': 'three'}),
    ])]
    first = asyncio.run(persistence.receive('general', conversation_id='first'))
    second = asyncio.run(persistence.receive('general', conversation_id='second'))
    assert [message['text'] for message in first['messages']] == ['one', 'three']
    assert [message['text'] for message in second['messages']] == ['two']
    assert first['cursor'] == second['cursor'] == '3-0'


def test_mcp_cannot_override_identity_with_extra_fields(client):
    first, second = register(client), register(client)
    response = client.post('/mcp', headers={**authorization(first), 'Accept': 'application/json, text/event-stream'},
                           json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {
                               'name': 'publish', 'arguments': {'channel': 'general', 'text': 'hello',
                                                               'agent_id': second['agent_id'], 'sender': 'imposter'}}})
    result = response.json()['result']
    # The SDK may reject unknown arguments or discard them; neither can change the identity.
    if not result.get('isError'):
        message = json.loads(result['content'][0]['text'])
        assert (message['agent_id'], message['sender']) == (first['agent_id'], first['sender'])
