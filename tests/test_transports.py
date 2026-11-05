import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from redis.exceptions import RedisError
from starlette.websockets import WebSocketDisconnect

from channels.auth import CredentialAuth


@pytest.mark.parametrize('authorization', [None, 'Bearer wrong'])
def test_http_rejects_missing_or_wrong_key(client, authorization):
    headers = {} if authorization is None else {'Authorization': authorization}
    assert client.get('/channels/general/messages', headers=headers).status_code == 401


def test_mcp_requires_authentication(client):
    assert client.post('/mcp', json={}).status_code == 401


def test_mcp_rejects_oversized_request(client, headers, server_module):
    assert client.post('/mcp', headers=headers, content=b'x' * (server_module.MAX_BODY + 1)).status_code == 413


def test_mcp_rejects_oversized_request_before_authentication(client, server_module):
    assert client.post('/mcp', content=b'x' * (server_module.MAX_BODY + 1)).status_code == 413


def test_mcp_rejects_chunked_request_with_411(client, headers):
    assert client.post('/mcp', headers=headers, content=iter([b'{}'])).status_code == 411


def test_mcp_rejects_missing_content_length_with_411(client, headers):
    request = client.build_request('POST', '/mcp', headers=headers, content=b'{}')
    del request.headers['Content-Length']
    assert client.send(request).status_code == 411


@pytest.mark.parametrize('headers,status', [
    *(([(b'content-length', value)], 400) for value in (b'abc', b'', b'-1', b'+5', b'1_0', b' 7 ',
                                                        b'9' * 4301, b'0' * 4999 + b'2', b'1' * 21)),
    ([(b'content-length', b'99999999'), (b'content-length', b'2')], 400), ([(b'content-length', b'2')] * 2, None),
    ([(b'content-length', b'2'), (b'transfer-encoding', b'chunked')], 411), ([], 411),
    ([(b'content-length', b'33')], 413), ([(b'content-length', b'32')], None),
    ([(b'content-length', b'0' * 20)], None), ([(b'content-length', b'0002')], None), ([(b'content-length', b'00033')], 413)])
def test_mcp_body_status_validates_content_length(headers, status):
    assert CredentialAuth(None, 'k', 32).mcp_body_status({'type': 'http', 'method': 'POST', 'path': '/mcp', 'headers': headers}) == status


@pytest.mark.parametrize('headers,status,error', [([(b'content-length', b'33')], 413, 'request exceeds size limit'),
                                                  ([(b'content-length', b'abc')], 400, 'invalid Content-Length'),
                                                  ([(b'transfer-encoding', b'chunked')], 411, 'Content-Length required')])
def test_mcp_guard_rejects_without_calling_application(headers, status, error):
    application, send = AsyncMock(), AsyncMock()
    asyncio.run(CredentialAuth(application, 'k', 32)({'type': 'http', 'method': 'POST', 'path': '/mcp', 'headers': headers}, AsyncMock(), send))
    start, body = (message.args[0] for message in send.await_args_list)
    assert (start['status'], json.loads(body['body']), application.await_count) == (status, {'error': error}, 0)


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
    response = client.post('/channels/general/messages', headers=headers, json={'text': 'hello', 'session_id': 'synthetic-session', 'sender': 'writer'})
    assert response.json() == {'channel': 'general', 'sender': 'writer', 'session_id': 'synthetic-session', 'conversation_id': 'main', 'text': 'hello', 'reply_to': None, 'id': '1-0', 'cursor': '1-0'}


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
        'params': {'name': 'publish', 'arguments': {'channel': 'general', 'text': 'hello', 'session_id': 'synthetic-session'}},
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

    monkeypatch.setattr(server_module, 'sweep_history', sweep)
    with TestClient(server_module.inner):
        assert started.wait(1)
    assert stopped.is_set()
    storage.aclose.assert_awaited_once()


def test_production_app_wires_guard_with_message_body_limit(server_module):
    app = server_module.app
    assert isinstance(app, CredentialAuth) and app.application is server_module.inner
    assert app.max_body == server_module.MAX_BODY == server_module.MAX_TEXT + 65536


@pytest.mark.parametrize('scope_type,method,path', [('http', 'GET', '/mcp'), ('http', 'DELETE', '/mcp'),
                                                    ('http', 'POST', '/agents'),
                                                    ('http', 'POST', '/channels/general/messages'),
                                                    ('websocket', None, '/mcp')])
def test_mcp_body_status_ignores_other_methods_paths_and_websockets(scope_type, method, path):
    scope = {'type': scope_type, 'path': path, 'headers': []}
    if method is not None:
        scope['method'] = method
    assert CredentialAuth(None, 'k', 32).mcp_body_status(scope) is None


def test_removed_registration_routes_are_unavailable(client, headers):
    assert client.post('/agents', headers=headers, json={'display_name': 'client'}).status_code == 404
    assert client.get('/agents/me', headers=headers).status_code == 404
