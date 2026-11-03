from unittest.mock import AsyncMock

import pytest
from redis.exceptions import RedisError
from starlette.websockets import WebSocketDisconnect


@pytest.mark.parametrize('authorization', [None, 'Bearer wrong'])
def test_http_rejects_missing_or_wrong_key(client, authorization):
    headers = {} if authorization is None else {'Authorization': authorization}
    assert client.get('/channels/general/messages', headers=headers).status_code == 401


def test_mcp_requires_authentication(client):
    assert client.post('/mcp', json={}).status_code == 401


def test_websocket_requires_authentication(client):
    with pytest.raises(WebSocketDisconnect) as failure, client.websocket_connect('/channels/general/subscribe'):
        pass
    assert failure.value.code == 1008


def test_help_is_public(client):
    assert 'Authorization: Bearer' in client.get('/help').text


def test_health_checks_storage(client):
    assert client.get('/health').json() == {'status': 'ready'}


def test_health_exposes_storage_failure(client, storage):
    storage.ping.side_effect = RedisError('synthetic outage')
    assert client.get('/health').status_code == 503


def test_http_publish_returns_message(client, headers):
    response = client.post('/channels/general/messages', headers=headers, json={'text': 'hello'})
    assert response.json() == {'channel': 'general', 'sender': 'writer', 'agent_id': 'synthetic-agent', 'conversation_id': 'main', 'text': 'hello', 'reply_to': None, 'id': '1-0', 'cursor': '1-0'}


def test_http_read_uses_cursor(client, headers):
    assert client.get('/channels/general/messages?cursor=2-0', headers=headers).json() == {'messages': [], 'cursor': '2-0'}


@pytest.mark.parametrize('payload', ['{broken', '[]', '{}', '{"sender":"writer","text":"hello","extra":true}'])
def test_http_rejects_invalid_message_objects(client, headers, payload):
    assert client.post('/channels/general/messages', headers=headers, content=payload).status_code == 400


def test_http_rejects_invalid_query(client, headers):
    assert client.get('/channels/general/messages?timeout=invalid', headers=headers).status_code == 400


def test_http_rejects_oversized_request(client, headers, server_module, monkeypatch):
    monkeypatch.setattr(server_module, 'MAX_BODY', 32)
    assert client.post('/channels/general/messages', headers=headers, content='x' * 33).status_code == 413


def test_http_exposes_storage_failure(client, headers, storage):
    storage.xread.side_effect = RedisError('synthetic outage')
    assert client.get('/channels/general/messages', headers=headers).status_code == 503


def test_websocket_reports_invalid_cursor(client, headers):
    with client.websocket_connect('/channels/general/subscribe?cursor=bad', headers=headers) as websocket:
        assert 'Invalid cursor' in websocket.receive_json()['error']


def test_websocket_reports_storage_failure(client, headers, storage):
    storage.xread.side_effect = RedisError('synthetic outage')
    with client.websocket_connect('/channels/general/subscribe', headers=headers) as websocket:
        assert websocket.receive_json() == {'error': 'Redis unavailable'}


def test_websocket_sends_message_and_advances_cursor(client, headers, server_module, monkeypatch):
    read_messages = AsyncMock(side_effect=[{'messages': [{'id': '5-0', 'text': 'hello'}], 'cursor': '5-0'}, WebSocketDisconnect()])
    monkeypatch.setattr(server_module, 'receive', read_messages)
    with client.websocket_connect('/channels/general/subscribe', headers=headers) as websocket:
        message = websocket.receive_json()
    assert message == {'id': '5-0', 'text': 'hello'} and read_messages.call_args.args == ('general', '5-0', 30)


def test_websocket_sends_idle_heartbeat(client, headers, server_module, monkeypatch):
    monkeypatch.setattr(server_module, 'receive', AsyncMock(side_effect=[{'messages': [], 'cursor': '0-0'}, WebSocketDisconnect()]))
    with client.websocket_connect('/channels/general/subscribe', headers=headers) as websocket:
        assert websocket.receive_json() == {'type': 'heartbeat', 'cursor': '0-0'}


def test_mcp_lists_standard_tools(client, headers):
    response = client.post('/mcp', headers={**headers, 'Accept': 'application/json, text/event-stream'},
                           json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})
    assert {tool['name'] for tool in response.json()['result']['tools']} == {'publish', 'receive'}


def test_mcp_publish_calls_shared_storage(client, headers):
    response = client.post('/mcp', headers={**headers, 'Accept': 'application/json, text/event-stream'}, json={
        'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
        'params': {'name': 'publish', 'arguments': {'channel': 'general', 'text': 'hello'}},
    })
    assert not response.json()['result']['isError']


def test_mcp_validation_failure_is_tool_error(client, headers):
    response = client.post('/mcp', headers={**headers, 'Accept': 'application/json, text/event-stream'}, json={
        'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
        'params': {'name': 'publish', 'arguments': {'channel': 'general', 'text': ''}},
    })
    assert response.json()['result']['isError']


def test_retention_task_starts_and_stops_before_redis_closes(server_module, storage, monkeypatch):
    import threading

    import anyio
    from starlette.testclient import TestClient

    started, stopped = threading.Event(), threading.Event()

    async def sweep():
        started.set()
        try:
            await anyio.sleep_forever()
        finally:
            assert storage.aclose.await_count == 0
            stopped.set()

    monkeypatch.setattr(server_module, 'RETENTION_DAYS', 30)
    monkeypatch.setattr(server_module, 'sweep_history', sweep)
    with TestClient(server_module.inner):
        assert started.wait(1)
    assert stopped.is_set()
    storage.aclose.assert_awaited_once()
