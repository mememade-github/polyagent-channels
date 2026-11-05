import asyncio
import io
import json
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import httpx
import pytest

from channels import cli, exchange
from channels.client_config import apply_config


TARGET = {'channel': 'general', 'conversation_id': 'work', 'cursor': '0-0'}


def state(session='native-a', timeout=600):
    return exchange.new_state(session, TARGET, timeout)


def test_shared_key_sessions_have_independent_state(tmp_path):
    for session in ('native-a', 'native-b'):
        exchange.open_exchange(tmp_path, session, state(session))
    assert exchange.owned_state(tmp_path, 'native-a') != exchange.owned_state(tmp_path, 'native-b')


def test_duplicate_open_serialized(tmp_path):
    barrier = Barrier(2)
    def open_once():
        barrier.wait()
        try:
            exchange.open_exchange(tmp_path, 'native-a', state())
            return 'opened'
        except FileExistsError:
            return 'duplicate'
    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(lambda ignored: open_once(), range(2))) == ['duplicate', 'opened']


def test_expiry_reopen_and_close_target(tmp_path):
    expired = {**state(), 'deadline': time.time() - 1}
    exchange.open_exchange(tmp_path, 'native-a', expired)
    exchange.open_exchange(tmp_path, 'native-a', state())
    with pytest.raises(exchange.ExchangeMismatch):
        exchange.close_exchange(tmp_path, 'native-a', {**TARGET, 'conversation_id': 'other'})
    exchange.close_exchange(tmp_path, 'native-a', TARGET)
    assert exchange.owned_state(tmp_path, 'native-a') is None


def test_compare_and_swap_preserves_new_state(tmp_path):
    original = state()
    exchange.open_exchange(tmp_path, 'native-a', original)
    path = exchange.owned_state(tmp_path, 'native-a')
    exchange.update_state(path, original, {**original, 'cursor': '1-0'})
    assert not exchange.update_state(path, original, {**original, 'cursor': '2-0'})


def test_wait_ignores_own_rows_and_empty_pages():
    pages = iter([
        {'messages': [{'id': '1-0', 'session_id': 'native-a'}], 'cursor': '1-0'},
        {'messages': [], 'cursor': '2-0'},
        {'messages': [{'id': '3-0', 'session_id': 'native-b'}, {'id': '4-0', 'session_id': 'native-c'}], 'cursor': '4-0'},
    ])
    cursors = []
    def handler(request):
        cursors.append(request.url.params['cursor'])
        return httpx.Response(200, json=next(pages))
    async def run():
        async with httpx.AsyncClient(base_url='http://synthetic/', transport=httpx.MockTransport(handler)) as client:
            return await exchange.wait_for_peer(client, {**TARGET, 'session_id': 'native-a'}, 1)
    result = asyncio.run(run())
    assert result['cursor'] == '3-0' and cursors == ['0-0', '1-0', '2-0']


def test_legacy_history_readable():
    assert exchange.first_peer([{'id': '1-0', 'agent_id': 'old-agent'}], 'native-a')['agent_id'] == 'old-agent'


def test_hook_empty_polls_wait_until_deadline(tmp_path):
    exchange.open_exchange(tmp_path, 'native-a', {**state(), 'deadline': time.time() + .08})
    path = exchange.owned_state(tmp_path, 'native-a')
    polls = []
    async def handler(request):
        polls.append(request)
        await asyncio.sleep(.01)
        return httpx.Response(200, json={'messages': [], 'cursor': '0-0'})
    async def run():
        async with httpx.AsyncClient(base_url='http://synthetic/', transport=httpx.MockTransport(handler)) as client:
            return await exchange.hook_decision(client, path)
    started = time.monotonic()
    result = asyncio.run(run())
    assert result == {} and len(polls) > 1 and time.monotonic() - started >= .06


def test_hook_stdin_owns_state_and_diagnostic(tmp_path, monkeypatch, capsys):
    directory = tmp_path / 'state'
    exchange.open_exchange(directory, 'callback-native', {**state('callback-native'), 'deadline': time.time() - 1})
    selected = cli.command_parser().parse_args(['--session-id', 'wrong', '--state-dir', str(directory), 'hook'])
    apply_config(selected, [])
    monkeypatch.setenv('CODEX_THREAD_ID', 'also-wrong')
    monkeypatch.setattr('sys.stdin', io.StringIO(json.dumps({'session_id': 'callback-native', 'hook_event_id': 'event:1', 'body': 'never persist'})))
    cli.run_hook(selected)
    diagnostic = json.loads((exchange.session_directory(directory, 'callback-native') / 'last-hook.json').read_text())
    assert json.loads(capsys.readouterr().out) == {} and diagnostic['session_id'] == 'callback-native' and diagnostic['callback_id'] == 'event:1' and 'body' not in diagnostic


def test_no_state_hook_needs_no_key(tmp_path, monkeypatch, capsys):
    selected = cli.command_parser().parse_args(['--state-dir', str(tmp_path), 'hook'])
    apply_config(selected, [])
    monkeypatch.setattr('sys.stdin', io.StringIO('{"session_id":"new-session"}'))
    monkeypatch.setattr(cli, 'read_secret', lambda path: pytest.fail('must not read a key'))
    cli.run_hook(selected)
    assert json.loads(capsys.readouterr().out) == {}


@pytest.mark.parametrize('identifier', ['', '../other', 'x/y', 'é', 'a' * 257])
def test_invalid_session_rejected(identifier):
    with pytest.raises(ValueError):
        exchange.validate_session_id(identifier)


@pytest.mark.parametrize('timeout', [0, .5, 601, float('inf')])
def test_open_timeout_bounded(timeout):
    with pytest.raises(ValueError):
        state(timeout=timeout)


def test_native_adapters_exchange_through_http_contract(tmp_path, monkeypatch, capsys):
    secret_directory = tmp_path / '.env'
    secret_directory.mkdir(mode=0o700)
    key = secret_directory / 'channel.key'
    key.write_text('synthetic-shared-key')
    key.chmod(0o600)
    config = tmp_path / 'config.json'
    config.write_text('{"url":"http://synthetic-service"}')
    rows = []
    routes = []
    def handler(request):
        if request.headers.get('Authorization') != 'Bearer synthetic-shared-key':
            return httpx.Response(401)
        routes.append((request.method, request.url.path))
        if request.url.path == '/status':
            return httpx.Response(200, json={'status': 'ready'})
        if request.method == 'POST':
            row = {**json.loads(request.content), 'id': str(len(rows) + 1) + '-0'}
            rows.append(row)
            return httpx.Response(200, json=row)
        cursor = int(request.url.params['cursor'].split('-')[0])
        return httpx.Response(200, json={'messages': rows[cursor:], 'cursor': str(len(rows)) + '-0'})
    original_sync, original_async = httpx.Client, httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(cli.httpx, 'Client', lambda **settings: original_sync(transport=transport, **settings))
    monkeypatch.setattr(cli.httpx, 'AsyncClient', lambda **settings: original_async(transport=transport, **settings))
    monkeypatch.setenv('CODEX_THREAD_ID', 'raw.codex:thread')
    monkeypatch.setenv('CLAUDE_CODE_SESSION_ID', 'raw.claude:session')
    def invoke(adapter, *arguments, stdin=None):
        selected = cli.command_parser().parse_args(['--config', str(config), '--client', adapter, *arguments])
        apply_config(selected, [])
        if stdin is not None:
            monkeypatch.setattr('sys.stdin', io.StringIO(json.dumps(stdin)))
        cli.run(selected)
        return json.loads(capsys.readouterr().out)
    invoke('codex', 'open')
    invoke('claude', 'open')
    invoke('codex', 'publish', '--text', 'own text')
    invoke('claude', 'publish', '--text', 'peer text')
    callback = invoke('codex', 'hook', stdin={'session_id': 'raw.codex:thread', 'hook_event_id': 'callback:1'})
    own_status = invoke('codex', 'status')
    other_status = invoke('claude', 'status')
    invoke('codex', 'ack', '--message-id', '2-0')
    invoke('codex', 'close')
    assert (callback['decision'] == 'block' and 'peer text' in callback['reason'] and 'own text' not in callback['reason']
            and own_status['last_callback']['callback_id'] == 'callback:1'
            and other_status['last_callback'] is None and other_status['exchange'] == 'live'
            and rows[0]['session_id'] == 'raw.codex:thread' and rows[1]['sender'] == 'raw.claude:session'
            and all('/agents' not in path for method, path in routes))


def test_consecutive_hook_errors_stop_without_leaking_details(tmp_path, capsys):
    exchange.open_exchange(tmp_path, 'native-a', state())
    path = exchange.owned_state(tmp_path, 'native-a')
    async def handler(request):
        raise httpx.ConnectError('synthetic-secret-detail')
    async def invoke():
        async with httpx.AsyncClient(base_url='http://synthetic/', transport=httpx.MockTransport(handler)) as client:
            return await exchange.hook_decision(client, path)
    first = asyncio.run(invoke())
    second = asyncio.run(invoke())
    assert (first['decision'] == 'block' and second == {} and exchange.read_state(path)['errors'] == 2
            and 'synthetic-secret-detail' not in json.dumps(first) + capsys.readouterr().err)


def test_hook_cycles_save_cursor_without_empty_model_polls(tmp_path, monkeypatch):
    exchange.open_exchange(tmp_path, 'native-a', state())
    path = exchange.owned_state(tmp_path, 'native-a')
    calls = []
    async def wait(client, target, timeout):
        calls.append((target['cursor'], timeout))
        if len(calls) == 1:
            return {'type': 'timeout', 'cursor': '7-0'}
        assert exchange.read_state(path)['cursor'] == '7-0'
        return {'id': '8-0', 'cursor': '8-0', 'session_id': 'native-b', 'text': 'peer'}
    monkeypatch.setattr(exchange, 'wait_for_peer', wait)
    result = asyncio.run(exchange.hook_decision(None, path))
    assert result['decision'] == 'block' and calls == [('0-0', 30), ('7-0', 30)]


def test_hook_close_during_poll_cancels_without_error(tmp_path, monkeypatch):
    exchange.open_exchange(tmp_path, 'native-a', state())
    path = exchange.owned_state(tmp_path, 'native-a')
    calls = []
    async def wait(client, target, timeout):
        calls.append(timeout)
        exchange.close_exchange(tmp_path, 'native-a', TARGET)
        return {'type': 'timeout', 'cursor': '9-0'}
    monkeypatch.setattr(exchange, 'wait_for_peer', wait)
    assert asyncio.run(exchange.hook_decision(None, path)) == {} and calls == [30] and not path.exists()


def test_two_topics_replay_ack_and_close_independently(tmp_path):
    first = state()
    second = exchange.new_state('native-a', {**TARGET, 'conversation_id': 'second'}, 600)
    exchange.open_exchange(tmp_path, 'native-a', first)
    exchange.open_exchange(tmp_path, 'native-a', second)
    path = exchange.owned_state(tmp_path, 'native-a', second)
    row = {'id': '9-0', 'session_id': 'native-b', 'text': 'synthetic work'}
    async def deliver():
        async with httpx.AsyncClient(base_url='http://synthetic/', transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={'messages': [row], 'cursor': '9-0'}))) as client:
            return await exchange.hook_decision(client, path)
    initial = asyncio.run(deliver())
    pending = exchange.read_state(path)
    with pytest.raises(ValueError, match='pending_ack_required'):
        exchange.close_exchange(tmp_path, 'native-a', second)
    exchange.update_state(path, pending, {**pending, 'deadline': time.time() - 1})
    reopened = exchange.open_exchange(tmp_path, 'native-a', second)
    replay = asyncio.run(deliver())
    exchange.ack_exchange(tmp_path, 'native-a', second, '9-0')
    exchange.close_exchange(tmp_path, 'native-a', second)
    assert (reopened['cursor'] == '0-0' and reopened['pending'] == '9-0'
            and 'synthetic work' in initial['reason'] and 'synthetic work' in replay['reason']
            and exchange.list_states(tmp_path, 'native-a') == [exchange.owned_state(tmp_path, 'native-a', first)])


def test_concurrent_topics_no_head_of_line_and_cancel_cleanup(tmp_path, monkeypatch):
    paths = []
    for name in ('slow', 'fast', 'expired'):
        topic = exchange.new_state('native-a', {**TARGET, 'conversation_id': name}, 600)
        if name == 'expired':
            topic['deadline'] = time.time() - 1
        exchange.open_exchange(tmp_path, 'native-a', topic)
        paths.append(exchange.owned_state(tmp_path, 'native-a', topic))
    cancelled = []
    async def wait(client, target, duration):
        if target['conversation_id'] == 'fast':
            await asyncio.sleep(.01)
            return {'id': '4-0', 'cursor': '4-0', 'session_id': 'native-b', 'text': 'ready'}
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(target['conversation_id'])
    monkeypatch.setattr(exchange, 'wait_for_peer', wait)
    result = asyncio.run(exchange.hook_topics(None, paths, 1))
    assert 'fast' in result['reason'] and cancelled == ['slow']


def test_pending_eviction_is_visible_and_never_acknowledged(tmp_path):
    topic = {**state(), 'pending': '5-0'}
    exchange.open_exchange(tmp_path, 'native-a', topic)
    path = exchange.owned_state(tmp_path, 'native-a', topic)
    async def run():
        async with httpx.AsyncClient(base_url='http://synthetic/', transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={'messages': [], 'cursor': '0-0'}))) as client:
            return await exchange.hook_decision(client, path)
    result = asyncio.run(run())
    assert 'evicted' in result['reason'] and exchange.read_state(path)['pending'] == '5-0'


def test_topic_path_validation_and_small_bound(tmp_path):
    for number in range(16):
        exchange.open_exchange(tmp_path, 'native-a', exchange.new_state(
            'native-a', {**TARGET, 'conversation_id': 'topic_' + str(number)}, 600))
    with pytest.raises(ValueError, match='topic_limit'):
        exchange.open_exchange(tmp_path, 'native-a', exchange.new_state(
            'native-a', {**TARGET, 'conversation_id': 'overflow'}, 600))
    path = exchange.list_states(tmp_path, 'native-a')[0]
    wrong = path.with_name('topic-' + '0' * 64 + '.json')
    wrong.write_text(path.read_text())
    with pytest.raises(exchange.ExchangeMismatch):
        exchange.read_state(wrong)


def test_stop_stdin_routes_two_same_vendor_sessions_with_two_topics(tmp_path, monkeypatch, capsys):
    directory = tmp_path / 'state'
    for session in ('same-vendor-a', 'same-vendor-b'):
        for name in ('first', 'second'):
            exchange.open_exchange(directory, session, exchange.new_state(
                session, {**TARGET, 'conversation_id': name}, 600))
    key_dir = tmp_path / '.env'
    key_dir.mkdir(mode=0o700)
    key = key_dir / 'channel.key'
    key.write_text('synthetic-key')
    key.chmod(0o600)
    async def handler(request):
        if request.url.params['conversation_id'] == 'first':
            await asyncio.Event().wait()
        return httpx.Response(200, json={'messages': [
            {'id': '8-0', 'session_id': 'same-vendor-b', 'text': 'second topic result'}], 'cursor': '8-0'})
    monkeypatch.setattr(cli, 'exchange_client', lambda options, secret: httpx.AsyncClient(
        base_url='http://synthetic/', transport=httpx.MockTransport(handler)))
    options = cli.command_parser().parse_args(['--client', 'codex', '--session-id', 'wrong-session',
        '--state-dir', str(directory), '--key-file', str(key), 'hook'])
    monkeypatch.setattr('sys.stdin', io.StringIO(json.dumps({'session_id': 'same-vendor-a'})))
    cli.run_hook(options)
    decision = json.loads(capsys.readouterr().out)
    selected = [exchange.read_state(path) for path in exchange.list_states(directory, 'same-vendor-a')]
    other = [exchange.read_state(path) for path in exchange.list_states(directory, 'same-vendor-b')]
    assert ('second topic result' in decision['reason'] and 'second' in decision['reason']
            and sum(topic['pending'] is not None for topic in selected) == 1
            and all(topic['pending'] is None for topic in other))
