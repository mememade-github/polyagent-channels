"""Storage-state failures surface canonically; other Redis error text stays masked."""
import asyncio
import re
import traceback
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from redis.asyncio import ConnectionPool
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import ResponseError
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from channels import persistence

SCRIPT_SHA = '9ef97e59b4ab6032b1fed8e588c77b6b348c5311'
KEY_TYPE_ERROR = f'user_script:13: Unexpected storage key type script: {SCRIPT_SHA}, on @user_script:13.'
REPLY_PHRASES = ['Invalid message lifecycle arguments', 'Unsupported message storage version',
                 'Message clock must be empty', 'Invalid message byte counter', 'Missing message clock',
                 'Stream ID exhausted']
PRIVATE_DETAIL = 'synthetic-private-detail'
SOCKET_USER = 'synthetic-socket-user'
SOCKET_SECRET = 'synthetic-socket-secret'
RAW_FRAGMENTS = ('user_script', SCRIPT_SHA, PRIVATE_DETAIL)
STORAGE_STATES = [(KEY_TYPE_ERROR, 'storage state: Unexpected storage key type'),
                  ('Missing message clock', 'storage state: Missing message clock')]
MASKED_FAILURES = [(ResponseError, 'ERR ' + PRIVATE_DETAIL), (RedisConnectionError, PRIVATE_DETAIL)]
MCP_ACCEPT = {'Accept': 'application/json, text/event-stream'}


def contains_raw_text(text):
    return any(fragment in text for fragment in RAW_FRAGMENTS)


def lifecycle_failure(storage, error):
    storage.eval.side_effect = error
    with pytest.raises(Exception) as failure:
        asyncio.run(persistence.lifecycle())
    return failure.value


def test_lifecycle_script_fails_only_with_listed_storage_states():
    replies = re.findall(r"redis\.error_reply\('([^']+)'\)", persistence.LIFECYCLE)
    raised = re.findall(r"[^_.]error\('([^']+)'\)", persistence.LIFECYCLE)
    assert (sorted(replies), raised) == (sorted(REPLY_PHRASES), ['Unexpected storage key type'])


@pytest.mark.parametrize('message,phrase', [*((phrase, phrase) for phrase in REPLY_PHRASES),
                                            (KEY_TYPE_ERROR, 'Unexpected storage key type')])
def test_lifecycle_failure_becomes_canonical_storage_state(storage, message, phrase):
    error = lifecycle_failure(storage, ResponseError(message))
    assert (type(error), str(error), error.__cause__) == (persistence.StorageStateError, f'storage state: {phrase}', None)
    assert not contains_raw_text(''.join(traceback.format_exception(error)))


@pytest.mark.parametrize('error', [
    ResponseError('ERR ' + PRIVATE_DETAIL),
    ResponseError('Missing message clock: ' + PRIVATE_DETAIL),
    ResponseError(PRIVATE_DETAIL + ' Stream ID exhausted'),
    ResponseError('Stream ID exhausted\n'),
    ResponseError(KEY_TYPE_ERROR + ' ' + PRIVATE_DETAIL),
    ResponseError(PRIVATE_DETAIL + ' ' + KEY_TYPE_ERROR),
    ResponseError(KEY_TYPE_ERROR.replace(SCRIPT_SHA, SCRIPT_SHA[:39])),
    ResponseError(f'user_script:13: Missing message clock script: {SCRIPT_SHA}, on @user_script:13.'),
    RedisConnectionError('Missing message clock'),
])
def test_other_redis_failures_stay_unclassified(storage, error):
    assert lifecycle_failure(storage, error) is error


def run_sweep_once(storage, monkeypatch, error):
    storage.eval.side_effect = error
    monkeypatch.setattr(persistence.asyncio, 'sleep', AsyncMock(side_effect=asyncio.CancelledError))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(persistence.sweep_history())


def test_sweep_logs_canonical_storage_state(storage, monkeypatch, caplog):
    run_sweep_once(storage, monkeypatch, ResponseError(KEY_TYPE_ERROR))
    expected = 'Message retention sweep failed: storage state: Unexpected storage key type; retry in 60 seconds'
    assert expected in caplog.text and not contains_raw_text(caplog.text)


def test_sweep_logs_only_class_of_other_redis_failures(storage, monkeypatch, caplog):
    run_sweep_once(storage, monkeypatch, RedisConnectionError(PRIVATE_DETAIL))
    expected = 'Message retention sweep failed: ConnectionError; retry in 60 seconds'
    assert expected in caplog.text and not contains_raw_text(caplog.text)


def request_messages(client, headers, method):
    if method == 'POST':
        return client.post('/channels/general/messages', headers=headers, json={'text': 'hello', 'session_id': 'synthetic-session'})
    return client.get('/channels/general/messages', headers=headers)


@pytest.mark.parametrize('method', ['GET', 'POST'])
@pytest.mark.parametrize('message,expected', STORAGE_STATES)
def test_http_reports_and_logs_canonical_storage_state(client, headers, storage, caplog, method, message, expected):
    storage.eval.side_effect = ResponseError(message)
    response = request_messages(client, headers, method)
    server_logs = [record.getMessage() for record in caplog.records if record.name == 'channels.server']
    assert (response.status_code, response.json()) == (503, {'error': expected})
    assert any(expected in line for line in server_logs) and not contains_raw_text(caplog.text)


@pytest.mark.parametrize('method', ['GET', 'POST'])
@pytest.mark.parametrize('error_type,message', MASKED_FAILURES)
def test_http_masks_other_redis_failures(client, headers, storage, caplog, method, error_type, message):
    storage.eval.side_effect = error_type(message)
    response = request_messages(client, headers, method)
    assert (response.status_code, response.json()) == (503, {'error': 'Redis unavailable'})
    assert not contains_raw_text(caplog.text)


@pytest.mark.parametrize('error_type,message,expected', [
    *((ResponseError, message, expected) for message, expected in STORAGE_STATES),
    *((error_type, message, 'Redis unavailable') for error_type, message in MASKED_FAILURES),
])
def test_websocket_sends_storage_error_then_closes_1011(client, headers, storage, caplog, error_type, message, expected):
    storage.eval.side_effect = error_type(message)
    with client.websocket_connect('/channels/general/subscribe', headers=headers) as websocket:
        first = websocket.receive_json()
        with pytest.raises(WebSocketDisconnect) as closed:
            websocket.receive_json()
    assert (first, closed.value.code) == ({'error': expected}, 1011)
    assert not contains_raw_text(caplog.text)


def call_tool(client, headers, name, arguments):
    response = client.post('/mcp', headers={**headers, **MCP_ACCEPT}, json={
        'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': name, 'arguments': arguments}})
    return response.json()['result']


@pytest.mark.parametrize('name,arguments', [('publish', {'channel': 'general', 'text': 'hello', 'session_id': 'synthetic-session'}),
                                            ('receive', {'channel': 'general'})])
@pytest.mark.parametrize('error_type,message,expected', [
    *((ResponseError, message, expected) for message, expected in STORAGE_STATES),
    (ResponseError, 'Invalid message byte counter', 'storage state: Invalid message byte counter'),
    *((error_type, message, 'Redis unavailable') for error_type, message in MASKED_FAILURES),
])
def test_mcp_tool_error_is_canonical_or_masked(client, headers, storage, caplog, name, arguments,
                                               error_type, message, expected):
    storage.eval.side_effect = error_type(message)
    result = call_tool(client, headers, name, arguments)
    assert (result['isError'], result['content'][0]['text']) == (True, f'Error executing tool {name}: {expected}')
    assert not contains_raw_text(caplog.text)


def test_mcp_receive_masks_read_failure(client, headers, storage, caplog):
    storage.xread.side_effect = RedisConnectionError(PRIVATE_DETAIL)
    result = call_tool(client, headers, 'receive', {'channel': 'general'})
    assert result['content'][0]['text'] == 'Error executing tool receive: Redis unavailable'
    assert not contains_raw_text(caplog.text)


def test_mcp_tools_keep_operation_schemas_and_descriptions(client, headers):
    response = client.post('/mcp', headers={**headers, **MCP_ACCEPT},
                           json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})
    tools = {tool['name']: tool for tool in response.json()['result']['tools']}
    assert {name: (set(tool['inputSchema']['properties']), set(tool['inputSchema']['required']), tool['description'])
            for name, tool in tools.items()} == {
        'publish': ({'channel', 'text', 'session_id', 'sender', 'reply_to', 'conversation_id'}, {'channel', 'text', 'session_id'}, persistence.publish.__doc__),
        'receive': ({'channel', 'cursor', 'timeout', 'limit', 'conversation_id'}, {'channel'}, persistence.receive.__doc__),
    }


def start_service(server_module):
    with TestClient(server_module.inner):
        pass


def test_startup_storage_state_propagates_canonically(server_module, storage):
    storage.eval.side_effect = ResponseError(KEY_TYPE_ERROR)
    with pytest.raises(persistence.StorageStateError) as failure:
        start_service(server_module)
    assert (str(failure.value), failure.value.__cause__) == ('storage state: Unexpected storage key type', None)
    assert not contains_raw_text(''.join(traceback.format_exception(failure.value)))


@pytest.mark.parametrize('error_type,message', MASKED_FAILURES)
@pytest.mark.parametrize('url,address', [('redis://127.0.0.1:6379/0', '127.0.0.1:6379/0'),
                                         ('redis://redis:6379', 'redis:6379/0')])
def test_startup_redis_failure_reports_class_and_address_only(server_module, storage, error_type, message, url, address):
    storage.connection_pool = ConnectionPool.from_url(url)
    storage.eval.side_effect = error_type(message)
    with pytest.raises(RuntimeError) as failure:
        start_service(server_module)
    expected = f'Storage initialization failed: {error_type.__name__} at {address}'
    assert (str(failure.value), failure.value.__cause__) == (expected, None)
    assert not contains_raw_text(''.join(traceback.format_exception(failure.value)))


def test_startup_redis_failure_reports_unix_socket_as_configured(server_module, storage):
    storage.connection_pool = ConnectionPool.from_url(
        f'unix://{SOCKET_USER}:{SOCKET_SECRET}@/tmp/synthetic.sock?db=2')
    storage.eval.side_effect = RedisConnectionError(PRIVATE_DETAIL)
    with pytest.raises(RuntimeError) as failure:
        start_service(server_module)
    rendered = ''.join(traceback.format_exception(failure.value))
    assert str(failure.value) == 'Storage initialization failed: ConnectionError at unix:/tmp/synthetic.sock/2'
    assert not any(text in rendered for text in ('localhost', '6379', SOCKET_USER, SOCKET_SECRET, *RAW_FRAGMENTS))


def test_help_documents_storage_state_within_line_limit(server_module):
    flattened = ' '.join(server_module.HELP.split())
    long_lines = [line for line in server_module.HELP.splitlines()
                  if len(line) > 88 and not line.lstrip().startswith('channels ')]
    assert '503 Redis unavailable or storage state: <cause> (operator repair; see README).' in flattened
    assert '503 Redis unavailable.' not in flattened and long_lines == []


@pytest.mark.parametrize('phrase', [*REPLY_PHRASES, 'Unexpected storage key type'])
def test_readme_recovery_table_has_one_row_per_storage_state(phrase):
    readme = (Path(__file__).parents[1] / 'README.md').read_text(encoding='utf-8')
    rows = [line for line in readme.splitlines() if phrase in line]
    assert readme.count(phrase) == 1 and rows[0].startswith('| ')
    assert len([cell for cell in rows[0].strip('|').split('|') if cell.strip()]) >= 3
