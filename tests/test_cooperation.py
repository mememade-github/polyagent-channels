import asyncio
import io
import json
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import httpx
import pytest
from redis.exceptions import RedisError

from channels import cli, exchange

TARGET = ['general', '--conversation-id', 'work']
IDENTITIES = {
    'Bearer synthetic-first-credential': {'agent_id': 'first-agent', 'sender': 'same name'},
    'Bearer synthetic-second-credential': {'agent_id': 'second-agent', 'sender': 'same name'},
}


def message(identifier, agent='second-agent'):
    return {'id': identifier, 'cursor': identifier, 'channel': 'general', 'conversation_id': 'work',
            'agent_id': agent, 'sender': 'same name', 'text': 'synthetic peer\nmessage', 'reply_to': None}


@pytest.fixture
def invoke(monkeypatch, tmp_path):
    directory = tmp_path / '.env'
    directory.mkdir(mode=0o700)
    for name in ('first', 'second'):
        path = directory / (name + '.key')
        path.write_text('synthetic-' + name + '-credential')
        path.chmod(0o600)
    monkeypatch.setenv('CHANNEL_URL', 'http://synthetic-service')
    monkeypatch.setenv('CHANNEL_CREDENTIAL_FILE', str(directory / 'first.key'))
    monkeypatch.setenv('CHANNEL_STATE_DIR', str(tmp_path / '.channels'))

    def command(*arguments, stdin='{}', owner='first'):
        monkeypatch.setenv('CHANNEL_CREDENTIAL_FILE', str(directory / (owner + '.key')))
        monkeypatch.setattr('sys.argv', ['channels', *arguments])
        monkeypatch.setattr('sys.stdin', io.StringIO(stdin))
        try:
            cli.main()
        except SystemExit as result:
            return result.code
        return 0

    return command


@pytest.fixture
def transport(monkeypatch):
    original = httpx.AsyncClient

    def install(handler):
        monkeypatch.setattr(cli.httpx, 'AsyncClient', lambda **settings: original(
            transport=httpx.MockTransport(handler), **settings))

    return install


@pytest.fixture
def peer_service(transport):
    def install(reader):
        requests = []

        async def respond(request):
            requests.append(request)
            if request.url.path == '/agents/me':
                return httpx.Response(200, json=IDENTITIES[request.headers['Authorization']])
            return await reader(request)

        transport(respond)
        return requests

    return install


def test_me_returns_only_existing_identity_without_registering(client, headers, storage):
    storage.hgetall.side_effect = None
    storage.hgetall.return_value = {'agent_id': 'existing-agent', 'sender': 'existing sender',
                                   'credential': 'synthetic-must-not-return'}
    response = client.get('/agents/me', headers=headers)
    assert (response.status_code, response.headers['Cache-Control'], response.json(), storage.hset.await_count) == (
        200, 'no-store', {'agent_id': 'existing-agent', 'sender': 'existing sender'}, 0)


@pytest.mark.parametrize('authorization', [None, 'Bearer synthetic-admin-key', 'Bearer invalid-credential'])
def test_me_rejects_nonindividual_credentials(client, authorization):
    headers = {} if authorization is None else {'Authorization': authorization}
    response = client.get('/agents/me', headers=headers)
    assert (response.status_code, response.headers['Cache-Control'], response.json()) == (
        401, 'no-store', {'error': 'Unauthorized'})


def test_me_lookup_failure_is_redacted(client, headers, storage):
    storage.hgetall.side_effect = RedisError('synthetic-private-configuration')
    response = client.get('/agents/me', headers=headers)
    assert (response.status_code, response.headers['Cache-Control'], response.json()) == (
        503, 'no-store', {'error': 'Redis unavailable'})


@pytest.mark.parametrize('headers', [{}, {'Authorization': 'Bearer synthetic-admin-key'}])
def test_me_rejects_admin_and_anonymous_even_during_outage(client, storage, headers):
    storage.hgetall.side_effect = RedisError('synthetic-private-configuration')
    response = client.get('/agents/me', headers=headers)
    assert (response.status_code, storage.hgetall.await_count) == (401, 0)


def test_wait_carries_scan_cursors_and_leaves_later_peer_rows_unread(invoke, peer_service, capsys):
    pages = {
        '0-0': {'messages': [], 'cursor': '1-0'},
        '1-0': {'messages': [message('2-0', 'first-agent')], 'cursor': '2-0'},
        '2-0': {'messages': [message('3-0', 'first-agent'), message('4-0'), message('5-0')], 'cursor': '9-0'},
        '4-0': {'messages': [message('5-0')], 'cursor': '9-0'},
    }

    async def read(request):
        return httpx.Response(200, json=pages[request.url.params['cursor']])

    requests = peer_service(read)
    first_status = invoke('wait', *TARGET)
    first_output = capsys.readouterr()
    second_status = invoke('wait', *TARGET, '--cursor', json.loads(first_output.out)['cursor'])
    second_output = capsys.readouterr()
    reads = [dict(request.url.params) for request in requests if request.url.path.endswith('/messages')]
    assert (first_status, second_status, first_output.out, second_output.out, first_output.err, second_output.err, reads) == (
        0, 0, json.dumps(message('4-0'), ensure_ascii=False) + '\n',
        json.dumps(message('5-0'), ensure_ascii=False) + '\n', '', '',
        [{'conversation_id': 'work', 'cursor': cursor, 'timeout': '60'} for cursor in ('0-0', '1-0', '2-0', '4-0')])


def test_wait_short_total_deadline_cancels_read_and_keeps_advanced_cursor(invoke, peer_service, capsys):
    async def read(request):
        cursor = request.url.params['cursor']
        if cursor == '0-0':
            return httpx.Response(200, json={'messages': [], 'cursor': '8-0'})
        if cursor == '8-0':
            return httpx.Response(200, json={'messages': [message('10-0', 'first-agent')], 'cursor': '10-0'})
        await asyncio.sleep(20)
        raise AssertionError('The overall deadline did not cancel the HTTP read')

    requests = peer_service(read)
    started = time.monotonic()
    status = invoke('wait', *TARGET, '--timeout', '0.08')
    elapsed = time.monotonic() - started
    output = capsys.readouterr()
    assert (status, json.loads(output.out), len(output.out.splitlines()), output.err,
            elapsed < 1, requests[-1].url.params['timeout']) == (
        2, {'type': 'timeout', 'cursor': '10-0'}, 1, '', True, '1')


def test_wait_total_deadline_also_bounds_identity_lookup(invoke, transport, capsys):
    requests = []

    async def stalled_identity(request):
        requests.append(request.url.path)
        await asyncio.sleep(20)
        raise AssertionError('Identity lookup outlived total timeout')

    transport(stalled_identity)
    started = time.monotonic()
    status = invoke('wait', *TARGET, '--cursor', '7-0', '--timeout', '0.04')
    output = capsys.readouterr()
    assert (status, json.loads(output.out), output.err, requests, time.monotonic() - started < 1) == (
        2, {'type': 'timeout', 'cursor': '7-0'}, '', ['/agents/me'], True)


@pytest.mark.parametrize('stage,failure', [
    ('identity', 401), ('identity', 503), ('identity', 'malformed'),
    ('messages', 503), ('messages', 'transport'), ('messages', 'timeout'),
])
def test_wait_failures_exit_once_through_redacted_boundary(invoke, transport, capsys, stage, failure):
    requests = []

    def respond(request):
        requests.append(request.url.path)
        if stage == 'messages' and request.url.path == '/agents/me':
            return httpx.Response(200, json=IDENTITIES['Bearer synthetic-first-credential'])
        if failure == 'malformed':
            return httpx.Response(200, json={'agent_id': 'synthetic/private/path', 'sender': 'private'})
        if failure == 'transport':
            raise httpx.ConnectError('synthetic-private-configuration', request=request)
        if failure == 'timeout':
            raise TimeoutError('synthetic-private-configuration')
        return httpx.Response(failure, json={'error': 'synthetic-private-configuration'})

    transport(respond)
    status = invoke('wait', *TARGET, '--timeout', '1')
    output = capsys.readouterr()
    assert (status, output.out, 'channel client failed:' in output.err, 'synthetic' in output.err,
            len(requests)) == (1, '', True, False, 1 if stage == 'identity' else 2)


def test_open_is_exclusive_and_records_only_logical_exchange(invoke, peer_service, monkeypatch, tmp_path, capsys):
    peer_service(None)
    started = time.time()
    monkeypatch.setattr(exchange.time, 'time', lambda: started)
    first_status = invoke('open', *TARGET, '--cursor', '4-0')
    first = json.loads(capsys.readouterr().out)
    path = tmp_path / '.channels/first-agent/exchange.json'
    original = path.read_bytes()
    monkeypatch.setattr(exchange.time, 'time', lambda: first['deadline'] - 0.001)
    second_status = invoke('open', *TARGET, '--cursor', '9-0')
    second = capsys.readouterr()
    state = json.loads(original)
    assert (first_status, second_status, path.read_bytes() == original, set(state),
            state['cursor'], state['errors'], state['deadline'],
            first['type'], 'FileExistsError' in second.err, 'synthetic' in original.decode()) == (
        0, 1, True, {'agent_id', 'channel', 'conversation_id', 'cursor', 'deadline', 'errors'},
        '4-0', 0, started + 600, 'open', True, False)


@pytest.mark.parametrize('expired', [False, True])
def test_concurrent_open_has_one_owner_and_complete_json(tmp_path, expired):
    state = exchange.new_state(IDENTITIES['Bearer synthetic-first-credential'],
                               {'channel': 'general', 'conversation_id': 'work', 'cursor': '0-0'}, 60)
    if expired:
        exchange.open_exchange(tmp_path, 'synthetic-first-credential',
                               {**state, 'cursor': '9-0', 'deadline': time.time() - 1})
    ready = Barrier(2)

    def open_once(_):
        ready.wait(timeout=5)
        try:
            exchange.open_exchange(tmp_path, 'synthetic-first-credential', state)
        except FileExistsError:
            return 'conflict'
        return 'opened'

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = sorted(executor.map(open_once, range(2)))
    assert (outcomes, json.loads((tmp_path / 'first-agent/exchange.json').read_text())) == (
        ['conflict', 'opened'], state)


@pytest.mark.parametrize('marker_owner', [None, 'second'])
def test_open_preserves_expired_state_without_matching_owner(
        invoke, peer_service, monkeypatch, tmp_path, capsys, marker_owner):
    peer_service(None)
    invoke('open', *TARGET)
    opened = json.loads(capsys.readouterr().out)
    monkeypatch.setattr(exchange.time, 'time', lambda: opened['deadline'])
    path = tmp_path / '.channels/first-agent/exchange.json'
    marker = path.parent / exchange.owner_marker('synthetic-first-credential')
    marker.unlink()
    if marker_owner is not None:
        foreign_marker = path.parent / exchange.owner_marker('synthetic-' + marker_owner + '-credential')
        foreign_marker.touch()
    original = path.read_bytes()
    entries = sorted(item.name for item in path.parent.iterdir())
    status = invoke('open', *TARGET)
    output = capsys.readouterr()
    assert (status, output.out, 'ExchangeMismatch' in output.err, path.read_bytes(),
            sorted(item.name for item in path.parent.iterdir())) == (1, '', True, original, entries)


@pytest.mark.parametrize('field,value,error', [
    (None, None, 'JSONDecodeError'),
    ('agent_id', 'second-agent', 'ExchangeMismatch'),
    ('cursor', 'invalid', 'ValueError'),
    ('deadline', float('nan'), 'ValueError'),
    ('unexpected', True, 'ValueError'),
])
def test_open_preserves_corrupt_expired_state(
        invoke, peer_service, monkeypatch, tmp_path, capsys, field, value, error):
    peer_service(None)
    invoke('open', *TARGET)
    opened = json.loads(capsys.readouterr().out)
    monkeypatch.setattr(exchange.time, 'time', lambda: opened['deadline'])
    path = tmp_path / '.channels/first-agent/exchange.json'
    state = json.loads(path.read_text())
    contents = '{'
    if field is not None:
        state[field] = value
        contents = json.dumps(state)
    path.write_text(contents)
    status = invoke('open', *TARGET)
    output = capsys.readouterr()
    assert (status, output.out, error in output.err, path.read_text()) == (1, '', True, contents)


@pytest.mark.parametrize('location,dangling', [
    ('state', False), ('state', True), ('marker', False), ('marker', True), ('directory', False),
])
def test_open_preserves_symlinked_expired_state(
        invoke, peer_service, monkeypatch, tmp_path, capsys, location, dangling):
    peer_service(None)
    invoke('open', *TARGET)
    opened = json.loads(capsys.readouterr().out)
    monkeypatch.setattr(exchange.time, 'time', lambda: opened['deadline'])
    path = tmp_path / '.channels/first-agent/exchange.json'
    original = path.read_bytes()
    marker = path.parent / exchange.owner_marker('synthetic-first-credential')
    source = {'state': path, 'marker': marker, 'directory': path.parent}[location]
    protected = tmp_path / 'protected'
    source.rename(protected)
    source.symlink_to(protected, target_is_directory=location == 'directory')
    if dangling:
        protected.unlink()
    status = invoke('open', *TARGET)
    output = capsys.readouterr()
    remaining = None
    if path.exists():
        remaining = path.read_bytes()
    if location == 'state' and dangling:
        original = None
    assert (status, output.out, 'ValueError' in output.err, source.is_symlink(),
            protected.exists(), remaining) == (1, '', True, True, not dangling, original)


def test_close_requires_matching_owner_and_target(invoke, peer_service, tmp_path, capsys):
    peer_service(None)
    invoke('open', *TARGET)
    invoke('open', 'other', '--conversation-id', 'other', owner='second')
    capsys.readouterr()
    first = tmp_path / '.channels/first-agent/exchange.json'
    second = tmp_path / '.channels/second-agent/exchange.json'
    original = first.read_bytes(), second.read_bytes()
    mismatch = invoke('close', 'other', '--conversation-id', 'other')
    mismatch_output = capsys.readouterr()
    untouched = (first.read_bytes(), second.read_bytes()) == original
    closed = invoke('close', *TARGET)
    closed_output = capsys.readouterr()
    assert (mismatch, 'ExchangeMismatch' in mismatch_output.err, untouched, closed,
            first.exists(), second.read_bytes(), json.loads(closed_output.out)) == (
        1, True, True, 0, False, original[1], {'type': 'closed', 'channel': 'general', 'conversation_id': 'work'})


@pytest.mark.parametrize('empty_directory', [False, True])
def test_hook_without_state_quietly_allows_without_auth(invoke, monkeypatch, tmp_path, capsys, empty_directory):
    if empty_directory:
        (tmp_path / '.channels').mkdir()

    def forbidden(*arguments):
        raise AssertionError('A disabled hook must not require credentials')

    monkeypatch.setattr(cli, 'read_secret', forbidden)
    status = invoke('hook')
    output = capsys.readouterr()
    assert (status, output.out, output.err) == (0, '{}\n', '')


def test_hook_with_only_another_clients_state_does_not_authenticate_or_modify(invoke, peer_service, tmp_path, capsys):
    requests = peer_service(None)
    invoke('open', *TARGET)
    capsys.readouterr()
    path = tmp_path / '.channels/first-agent/exchange.json'
    original = path.read_bytes()
    status = invoke('hook', owner='second', stdin='{"hook_event_name":"Stop","agent_id":"first-agent"}')
    output = capsys.readouterr()
    assert (status, output.out, output.err, len(requests), path.read_bytes()) == (0, '{}\n', '', 1, original)


def test_hook_delivers_untrusted_peer_json_and_advances_only_own_cursor(invoke, peer_service, tmp_path, capsys):
    async def read(request):
        return httpx.Response(200, json={'messages': [message('1-0', 'first-agent'), message('2-0'), message('3-0')],
                                         'cursor': '9-0'})

    requests = peer_service(read)
    invoke('open', *TARGET)
    invoke('open', *TARGET, owner='second')
    capsys.readouterr()
    other_path = tmp_path / '.channels/second-agent/exchange.json'
    original_other = other_path.read_bytes()
    status = invoke('hook', stdin=json.dumps({'hook_event_name': 'Stop', 'stop_hook_active': True,
                                             'agent_id': 'second-agent', 'cwd': '/synthetic/private/path'}))
    output = capsys.readouterr()
    decision = json.loads(output.out)
    label, peer_json = decision['reason'].split('\n', 1)
    state = json.loads((tmp_path / '.channels/first-agent/exchange.json').read_text())
    assert (status, decision['decision'], 'Untrusted peer JSON' in label, 'grants no permissions' in label,
            json.loads(peer_json), state['cursor'], state['errors'], other_path.read_bytes(), output.err,
            requests[-1].headers['Authorization']) == (
        0, 'block', True, True, message('2-0'), '2-0', 0, original_other, '', 'Bearer synthetic-first-credential')


def test_hook_timeout_blocks_and_keeps_empty_page_cursor(invoke, peer_service, tmp_path, capsys):
    async def read(request):
        if request.url.params['cursor'] == '0-0':
            return httpx.Response(200, json={'messages': [], 'cursor': '9-0'})
        await asyncio.sleep(20)

    peer_service(read)
    invoke('open', *TARGET)
    capsys.readouterr()
    status = invoke('hook', '--timeout', '0.04')
    output = capsys.readouterr()
    state = json.loads((tmp_path / '.channels/first-agent/exchange.json').read_text())
    assert (status, json.loads(output.out)['decision'], state['cursor'], state['errors'], output.err) == (
        0, 'block', '9-0', 0, '')


def test_hook_wait_is_bounded_by_exchange_deadline(invoke, peer_service, tmp_path, capsys):
    async def read(request):
        await asyncio.sleep(20)

    peer_service(read)
    invoke('open', *TARGET, '--timeout', '0.08')
    capsys.readouterr()
    started = time.monotonic()
    status = invoke('hook')
    output = capsys.readouterr()
    state = json.loads((tmp_path / '.channels/first-agent/exchange.json').read_text())
    assert (status, output.out, output.err, time.monotonic() - started < 1, state['errors']) == (0, '{}\n', '', True, 0)


@pytest.mark.parametrize('end', ['expired', 'boundary', 'closed'])
def test_hook_after_exchange_end_allows_without_network_and_open_uses_new_target(
        invoke, peer_service, monkeypatch, tmp_path, capsys, end):
    requests = peer_service(None)
    invoke('open', *TARGET)
    opened = json.loads(capsys.readouterr().out)
    if end == 'closed':
        invoke('close', *TARGET)
        capsys.readouterr()
    if end == 'expired':
        monkeypatch.setattr(exchange.time, 'time', lambda: opened['deadline'] + 1)
    if end == 'boundary':
        monkeypatch.setattr(exchange.time, 'time', lambda: opened['deadline'])
    count = len(requests)
    status = invoke('hook')
    output = capsys.readouterr()
    hook_requests = len(requests) - count
    now = time.time()
    monkeypatch.setattr(exchange.time, 'time', lambda: now)
    reopened = invoke('open', 'other', '--conversation-id', 'next', '--cursor', '100-0', '--timeout', '60')
    new_output = capsys.readouterr()
    state = json.loads((tmp_path / '.channels/first-agent/exchange.json').read_text())
    expected = {'agent_id': 'first-agent', 'channel': 'other', 'conversation_id': 'next',
                'cursor': '100-0', 'deadline': now + 60, 'errors': 0}
    assert (status, output.out, output.err, hook_requests, reopened, new_output.err, json.loads(new_output.out or 'null'),
            state, len(requests)) == (
        0, '{}\n', '', 0, 0, '', {'type': 'open', **expected}, expected, count + 1)


@pytest.mark.parametrize('stage', ['identity', 'messages', 'input'])
def test_hook_first_error_blocks_second_allows_with_redacted_diagnostic(
        invoke, transport, tmp_path, capsys, stage):
    active = False
    failed_requests = []

    def respond(request):
        if not active or (stage != 'identity' and request.url.path == '/agents/me'):
            return httpx.Response(200, json=IDENTITIES['Bearer synthetic-first-credential'])
        failed_requests.append(request.url.path)
        raise httpx.ConnectError('synthetic-private-configuration', request=request)

    transport(respond)
    invoke('open', *TARGET, '--cursor', '4-0')
    capsys.readouterr()
    active = True
    source = 'synthetic-private-configuration' if stage == 'input' else '{}'
    first_status = invoke('hook', stdin=source)
    first = capsys.readouterr()
    second_status = invoke('hook', stdin=source)
    second = capsys.readouterr()
    state = json.loads((tmp_path / '.channels/first-agent/exchange.json').read_text())
    assert (first_status, json.loads(first.out)['decision'], first.err, second_status, second.out,
            'consecutive failures' in second.err, 'synthetic' in first.out + second.err,
            state['errors'], state['cursor'], len(failed_requests)) == (
        0, 'block', '', 0, '{}\n', True, False, 2, '4-0', 0 if stage == 'input' else 2)


@pytest.mark.parametrize('success', ['message', 'timeout'])
def test_hook_success_resets_consecutive_errors(invoke, peer_service, tmp_path, capsys, success):
    failing = True

    async def read(request):
        if failing:
            raise httpx.ReadTimeout('synthetic-private-configuration', request=request)
        if success == 'message':
            return httpx.Response(200, json={'messages': [message('9-0')], 'cursor': '10-0'})
        if request.url.params['cursor'] == '0-0':
            return httpx.Response(200, json={'messages': [], 'cursor': '9-0'})
        await asyncio.sleep(20)

    peer_service(read)
    invoke('open', *TARGET)
    invoke('hook')
    capsys.readouterr()
    failing = False
    invoke('hook', '--timeout', '0.04')
    capsys.readouterr()
    path = tmp_path / '.channels/first-agent/exchange.json'
    recovered = json.loads(path.read_text())
    failing = True
    invoke('hook')
    output = capsys.readouterr()
    state = json.loads(path.read_text())
    assert (recovered['cursor'], recovered['errors'], state['errors'], json.loads(output.out)['decision'], output.err) == (
        '9-0', 0, 1, 'block', '')


def test_hook_identity_mismatch_never_reads_peer_messages(invoke, transport, tmp_path, capsys):
    identity = IDENTITIES['Bearer synthetic-first-credential']
    requests = []

    def respond(request):
        requests.append(request.url.path)
        return httpx.Response(200, json=identity)

    transport(respond)
    invoke('open', *TARGET)
    capsys.readouterr()
    identity = IDENTITIES['Bearer synthetic-second-credential']
    invoke('hook')
    output = capsys.readouterr()
    state = json.loads((tmp_path / '.channels/first-agent/exchange.json').read_text())
    assert (requests, json.loads(output.out)['decision'], state['errors'], state['cursor']) == (
        ['/agents/me', '/agents/me'], 'block', 1, '0-0')


@pytest.mark.parametrize('end', ['closed', 'reopened', 'expired'])
@pytest.mark.parametrize('result', ['message', 'error'])
def test_late_hook_result_cannot_recreate_closed_or_overwrite_new_exchange(
        invoke, peer_service, monkeypatch, tmp_path, capsys, end, result):
    directory = tmp_path / '.channels'
    identity = IDENTITIES['Bearer synthetic-first-credential']
    target = {'channel': 'general', 'conversation_id': 'work', 'cursor': '100-0'}
    replacement = None

    async def end_while_waiting(request):
        nonlocal replacement
        if end != 'expired':
            exchange.close_exchange(directory, 'synthetic-first-credential', identity, target)
        if end == 'expired':
            monkeypatch.setattr(exchange.time, 'time', lambda: opened['deadline'])
        if end != 'closed':
            replacement = exchange.new_state(identity, target, 600)
            exchange.open_exchange(directory, 'synthetic-first-credential', replacement)
        if result == 'error':
            raise httpx.ReadTimeout('synthetic-private-configuration', request=request)
        return httpx.Response(200, json={'messages': [message('9-0')], 'cursor': '10-0'})

    peer_service(end_while_waiting)
    invoke('open', *TARGET)
    opened = json.loads(capsys.readouterr().out)
    status = invoke('hook')
    output = capsys.readouterr()
    path = directory / 'first-agent/exchange.json'
    state = json.loads(path.read_text()) if path.exists() else None
    assert (status, output.out, output.err, state) == (0, '{}\n', '', replacement)
