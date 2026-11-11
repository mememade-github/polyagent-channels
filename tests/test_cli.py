import asyncio
import contextlib
import io
import json
import shlex
import time
import threading
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from polyagent_channels import cli, exchange, setup
from polyagent_channels.client_config import apply_config


def options(*arguments):
    result = cli.command_parser().parse_args(list(arguments))
    apply_config(result, list(arguments))
    return result


def test_session_selection_uses_raw_native_id(monkeypatch):
    monkeypatch.setenv('CODEX_THREAD_ID', 'raw.codex:123')
    assert cli.select_session(options('publish', '--text', 'hello')) == 'raw.codex:123'


def test_session_ambiguity_requires_choice(monkeypatch):
    monkeypatch.setenv('CODEX_THREAD_ID', 'codex-id')
    monkeypatch.setenv('CLAUDE_CODE_SESSION_ID', 'claude-id')
    with pytest.raises(ValueError, match='session_ambiguous'):
        cli.select_session(options('status'))


def test_adapter_selects_its_native_id(monkeypatch):
    monkeypatch.setenv('CODEX_THREAD_ID', 'codex-id')
    monkeypatch.setenv('CLAUDE_CODE_SESSION_ID', 'claude-id')
    assert cli.select_session(options('--client', 'claude', 'status')) == 'claude-id'


def test_explicit_neutral_override(monkeypatch):
    monkeypatch.setenv('CODEX_THREAD_ID', 'native')
    assert cli.select_session(options('--session-id', 'neutral', 'status')) == 'neutral'


def test_required_session_is_actionable():
    with pytest.raises(ValueError, match='session_missing'):
        cli.select_session(options('publish', '--text', 'hi'))


def test_manual_hook_tty_input_fails_immediately(monkeypatch):
    stream = io.StringIO()
    monkeypatch.setattr(stream, 'isatty', lambda: True)
    monkeypatch.setattr('sys.stdin', stream)
    with pytest.raises(cli.HookInputRequired, match='native hook event JSON required; do not invoke manually'):
        cli.read_hook_input()


@pytest.mark.parametrize('payload,exit_code', [('', 1), ('{"session_id":', 1), ('{"session_id":"native-stdin"}', 0)])
def test_hook_pipe_input_is_bounded_and_complete_event_needs_no_eof(tmp_path, payload, exit_code):
    process = subprocess.Popen([sys.executable, '-m', 'polyagent_channels.cli', '--state-dir', str(tmp_path / 'state'), 'hook'],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        if payload:
            process.stdin.write(payload)
            process.stdin.flush()
        # Keep the writing pipe open, as an accidental direct tool invocation does.
        process.wait(timeout=15)
        output, errors = process.communicate(timeout=1)
    finally:
        if process.poll() is None:
            process.terminate()
            process.communicate(timeout=2)
    assert process.returncode == exit_code and output == ''
    assert ('native hook event JSON required; do not invoke manually' in errors) if exit_code else errors == ''


@pytest.mark.parametrize('missing_config', [False, True])
def test_empty_hook_input_returns_failure_even_when_config_is_missing(tmp_path, missing_config):
    arguments = ['--config', str(tmp_path / 'missing.json')] if missing_config else []
    result = subprocess.run([sys.executable, '-m', 'polyagent_channels.cli', '--state-dir', str(tmp_path / 'state'),
                             *arguments, 'hook'], input='', capture_output=True, text=True, timeout=15)
    assert result.returncode == 1 and 'native hook event JSON required; do not invoke manually' in result.stderr


def test_publish_attaches_session_and_default_sender(monkeypatch):
    monkeypatch.setattr('sys.stdin', io.StringIO('hello🙂'))
    selected = options('--session-id', 'raw-id', 'publish', '--file', '-')
    assert cli.publish_payload(selected) == {'session_id': 'raw-id', 'sender': 'raw-id', 'text': 'hello🙂',
                                             'reply_to': None, 'conversation_id': 'main'}


def test_sender_utf8_limit():
    selected = options('--session-id', 'raw-id', 'publish', '--text', 'hi', '--sender', '🙂' * 65)
    with pytest.raises(ValueError, match='sender_invalid'):
        cli.publish_payload(selected)


def test_config_url_only_and_conventional_paths(tmp_path):
    config = tmp_path / 'config.json'
    config.write_text('{"url":"https://service.example"}')
    selected = options('--config', str(config), 'status')
    assert (selected.url, selected.key_file, selected.state_dir) == (
        'https://service.example', str(tmp_path / '.env/channel.key'), str(tmp_path / 'state'))


def test_key_command_getpass_exclusive_and_replace(tmp_path, monkeypatch, capsys):
    destination = tmp_path / '.env/channel.key'
    selected = options('--key-file', str(destination), 'key')
    monkeypatch.setattr(cli.getpass, 'getpass', lambda prompt: 'synthetic-key')
    cli.save_key(selected)
    cli.save_key(selected)
    assert json.loads(capsys.readouterr().out.splitlines()[-1])['status'] == 'key_unchanged'
    monkeypatch.setattr(cli.getpass, 'getpass', lambda prompt: 'different-key')
    with pytest.raises(ValueError, match='key_exists_different'):
        cli.save_key(selected)
    assert cli.read_secret(destination) == 'synthetic-key'
    selected.replace_key = True
    monkeypatch.setattr(cli.getpass, 'getpass', lambda prompt: 'replacement-key')
    cli.save_key(selected)
    assert cli.read_secret(destination) == 'replacement-key' and 'replacement-key' not in capsys.readouterr().out


def test_key_source_permissions(tmp_path):
    source = tmp_path / 'source'
    source.write_text('synthetic')
    source.chmod(0o644)
    with pytest.raises(ValueError, match='key_permissions'):
        cli.read_secret(source)


def test_key_stdin_saves_reuses_and_preserves_conflicting_key_without_output(tmp_path):
    destination = tmp_path / '.env/channel.key'
    command = [sys.executable, '-m', 'polyagent_channels.cli', '--state-dir', str(tmp_path / 'state'),
               '--key-file', str(destination), 'key', '--stdin']
    original = 'synthetic-process-stdin-key'
    different = 'synthetic-conflicting-stdin-key'
    saved = subprocess.run(command, input=original + '\n', capture_output=True, text=True, timeout=15)
    reused = subprocess.run(command, input=original, capture_output=True, text=True, timeout=15)
    refused = subprocess.run(command, input=different, capture_output=True, text=True, timeout=15)
    output = ''.join(result.stdout + result.stderr for result in (saved, reused, refused))
    assert (saved.returncode == reused.returncode == 0 and refused.returncode == 1
            and json.loads(saved.stdout)['status'] == 'key_saved'
            and json.loads(reused.stdout)['status'] == 'key_unchanged'
            and 'key_exists_different' in refused.stderr
            and cli.read_secret(destination) == original
            and destination.stat().st_mode & 0o077 == destination.parent.stat().st_mode & 0o077 == 0
            and original not in output and different not in output)


@pytest.mark.parametrize('content', ['', 'synthetic first\nsynthetic second'])
def test_key_stdin_rejects_empty_or_multiple_values_without_output(tmp_path, content):
    destination = tmp_path / '.env/channel.key'
    result = subprocess.run([sys.executable, '-m', 'polyagent_channels.cli', '--state-dir', str(tmp_path / 'state'),
                             '--key-file', str(destination), 'key', '--stdin'],
                            input=content, capture_output=True, text=True, timeout=15)
    assert (result.returncode == 1 and 'key_invalid' in result.stderr and not destination.exists()
            and 'synthetic' not in result.stdout + result.stderr)


def test_key_stdin_and_source_file_are_mutually_exclusive(capsys):
    with pytest.raises(SystemExit) as error:
        options('key', '--stdin', '--source-file', '/synthetic/private.key')
    assert error.value.code == 2 and 'not allowed with argument' in capsys.readouterr().err


def test_concurrent_identical_key_writers_never_observe_an_empty_key(tmp_path, monkeypatch):
    destination = tmp_path / '.env/channel.key'
    selected = options('--key-file', str(destination), 'key')
    monkeypatch.setattr(cli.getpass, 'getpass', lambda prompt: 'synthetic-concurrent-key')
    opened, release = threading.Event(), threading.Event()
    original = cli.os.fdopen
    failures = []
    def pause_first_writer(*args, **kwargs):
        if threading.current_thread().name == 'key-writer':
            opened.set()
            assert release.wait(5)
        return original(*args, **kwargs)
    monkeypatch.setattr(cli.os, 'fdopen', pause_first_writer)
    def write_first():
        try:
            cli.save_key(selected, report=False)
        except Exception as error:
            failures.append(type(error).__name__)
    worker = threading.Thread(target=write_first, name='key-writer')
    worker.start()
    try:
        assert opened.wait(5)
        cli.save_key(selected, report=False)
    finally:
        release.set()
        worker.join(5)
    assert not failures and not worker.is_alive() and cli.read_secret(destination) == 'synthetic-concurrent-key'


def test_status_without_session_probes_status_only(monkeypatch, capsys):
    original = httpx.Client
    calls = []
    def handler(request):
        calls.append((request.url.path, request.headers['Authorization']))
        return httpx.Response(200, json={'status': 'ready'})
    monkeypatch.setattr(cli.httpx, 'Client', lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))
    selected = options('status')
    cli.client_status(selected, 'synthetic-key')
    assert calls == [('/status', 'Bearer synthetic-key')] and json.loads(capsys.readouterr().out)['session'] == 'session_missing'


@pytest.mark.parametrize('failure,expected', [
    (httpx.ConnectError('secret must never escape'), 'service_unreachable'),
    (ValueError('key_permissions'), 'key_permissions'),
])
def test_main_redacts_errors(monkeypatch, capsys, failure, expected):
    monkeypatch.setattr('sys.argv', ['channels', 'status'])
    def fail(selected):
        raise failure
    monkeypatch.setattr(cli, 'run', fail)
    with pytest.raises(SystemExit):
        cli.main()
    output = capsys.readouterr().err
    assert expected in output and 'secret must never escape' not in output


def test_main_unauthorized_is_explicit_and_redacted(monkeypatch, capsys):
    request = httpx.Request('GET', 'http://synthetic/status', headers={'Authorization': 'Bearer synthetic-secret'})
    response = httpx.Response(401, request=request)
    def fail(selected):
        response.raise_for_status()
    monkeypatch.setattr(cli, 'run', fail)
    monkeypatch.setattr('sys.argv', ['channels', 'status'])
    with pytest.raises(SystemExit):
        cli.main()
    output = capsys.readouterr().err
    assert 'unauthorized401' in output and 'synthetic-secret' not in output


def test_key_source_file_is_copied_without_output(tmp_path, capsys):
    source = tmp_path / 'source'
    source.write_text('synthetic-source-secret')
    source.chmod(0o600)
    destination = tmp_path / '.env/channel.key'
    cli.save_key(options('--key-file', str(destination), 'key', '--source-file', str(source)))
    assert cli.read_secret(destination) == 'synthetic-source-secret' and 'synthetic-source-secret' not in capsys.readouterr().out


def test_parser_accepts_bounded_read():
    options = cli.command_parser().parse_args(['receive', 'general', '--cursor', '2-0', '--timeout', '5'])
    assert (options.command, options.cursor, options.timeout) == ('receive', '2-0', 5)


def test_publish_reads_unicode_file(tmp_path):
    message_file = tmp_path / 'message.txt'
    message_file.write_text('hello🙂', encoding='utf-8')
    options = SimpleNamespace(text=None, file=str(message_file), conversation_id='main', reply_to=None, session_id='test-session', sender=None)
    assert cli.publish_payload(options)['text'] == 'hello🙂'


def test_listener_follows_until_stopped_by_default():
    assert options('--session-id', 'reader', 'listen').timeout == 0


def test_receive_waits_thirty_seconds_by_default():
    assert options('--session-id', 'reader', 'receive').timeout == 30


def test_listener_rejects_negative_timeout():
    with pytest.raises(ValueError, match='nonnegative'):
        asyncio.run(cli.listen_until_timeout(SimpleNamespace(timeout=-1), 'http://localhost', {}))


def test_listener_bounded_timeout_exits_normally(monkeypatch):
    monkeypatch.setattr(cli, 'listen', AsyncMock(side_effect=TimeoutError))
    assert asyncio.run(cli.listen_until_timeout(SimpleNamespace(timeout=1, cursor='0-0', conversation_id=None), 'http://localhost', {})) is None


def test_publish_posts_the_message(monkeypatch):
    captured = []

    def respond(request):
        captured.append(request)
        return httpx.Response(200, json={'messages': []})

    original_client = httpx.Client
    monkeypatch.setattr(cli.httpx, 'Client', lambda **settings: original_client(transport=httpx.MockTransport(respond), **settings))
    options = SimpleNamespace(command='publish', cursor='2-0', timeout=5, text='hello', file=None, conversation_id='main', reply_to=None, session_id='test-session', sender=None)
    cli.post_message(options, 'http://localhost/channels/general', {})
    assert captured[0].method == 'POST'


REDIRECT_TARGETS = ['ws://127.0.0.1:{port}/subscribe', 'https://127.0.0.1:{port}/subscribe',
                    'http://127.0.0.1:{port}/subscribe', '/same-origin']


async def listen_behind_a_redirect(received, target):
    """The configured address answers 302 with TARGET; the other port records every request it gets."""
    async def other_origin(reader, writer):
        received.append(await reader.read(65536))
        writer.close()

    async def configured(reader, writer):
        await reader.read(65536)
        location = target.format(port=other_port)
        writer.write(f'HTTP/1.1 302 Found\r\nLocation: {location}\r\nContent-Length: 0\r\n\r\n'.encode())
        await writer.drain()
        writer.close()

    other = await asyncio.start_server(other_origin, '127.0.0.1', 0)
    other_port = other.sockets[0].getsockname()[1]
    first = await asyncio.start_server(configured, '127.0.0.1', 0)
    async with other, first:
        await cli.listen(f'http://127.0.0.1:{first.sockets[0].getsockname()[1]}/channels/general',
                         {'Authorization': 'Bearer synthetic-key'}, '0-0')


@pytest.mark.parametrize('target', REDIRECT_TARGETS)
def test_listener_refuses_a_redirect(target):
    with pytest.raises(ValueError, match='HTTP redirect refused'):
        asyncio.run(listen_behind_a_redirect([], target))


@pytest.mark.parametrize('target', REDIRECT_TARGETS)
def test_listener_sends_nothing_to_a_redirect_target(target):
    received = []
    with contextlib.suppress(Exception):
        asyncio.run(listen_behind_a_redirect(received, target))
    assert received == []


@pytest.mark.parametrize('event', [{'id': '1-0', 'text': 'hello'}, {'type': 'heartbeat', 'cursor': '0-0'}, {'error': 'Redis unavailable'}])
def test_websocket_client_handles_message_heartbeat_and_error(monkeypatch, capsys, event):
    class SyntheticSocket:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exception):
            pass

        def __aiter__(self):
            async def events():
                yield json.dumps(event)
            return events()

    monkeypatch.setattr(cli, 'DirectConnect', lambda *parameters, **settings: SyntheticSocket())
    if 'error' in event:
        with pytest.raises(ValueError, match='Redis unavailable'):
            asyncio.run(cli.listen('http://localhost/channels/general', {}, '0-0'))
        return
    asyncio.run(cli.listen('http://localhost/channels/general', {}, '0-0'))
    output = capsys.readouterr().out
    assert output == '' if event.get('type') == 'heartbeat' else json.loads(output) == event


def test_outer_cli_boundary_redacts_exception_values(monkeypatch, capsys):
    monkeypatch.setattr('sys.argv', ['channels', 'receive', 'general'])
    monkeypatch.setattr(cli, 'run', lambda options: (_ for _ in ()).throw(ValueError('synthetic-secret-must-not-print')))
    with pytest.raises(SystemExit):
        cli.main()
    assert 'synthetic-secret-must-not-print' not in capsys.readouterr().err


def test_status_session_is_optional_even_when_native_environments_ambiguous(monkeypatch):
    monkeypatch.setenv('CODEX_THREAD_ID', 'codex-id')
    monkeypatch.setenv('CLAUDE_CODE_SESSION_ID', 'claude-id')
    assert cli.select_session(options('status'), required=False) is None


@pytest.fixture
def connection_client(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    secret_dir = tmp_path / '.env'
    secret_dir.mkdir(mode=0o700)
    key = secret_dir / 'channel.key'
    key.write_text('synthetic-connection-key')
    key.chmod(0o600)
    config = tmp_path / 'config.json'
    config.write_text('{"url":"http://synthetic-service"}')
    wrapper = tmp_path / 'bin/channels'
    wrapper.parent.mkdir()
    wrapper.write_bytes(setup.wrapper_content(Path(sys.executable), tmp_path))
    (tmp_path / 'harness.pyz').write_bytes(b'synthetic-artifact')
    binding = setup.native_binding(tmp_path, 'codex')
    binding.parent.mkdir()
    binding.write_text(setup.CODEX_SETTING + setup.codex_block(wrapper))
    manifest = {'version': 1, 'files': {}, 'hooks': [setup.binding_record(tmp_path, tmp_path, 'codex')]}
    (tmp_path / 'install-manifest.json').write_text(json.dumps(manifest))
    requests = []
    messages = [{'id': '1-0', 'session_id': 'old-peer', 'conversation_id': 'main', 'text': 'old work'}]
    presence = []
    on_snapshot = []
    on_read = []
    def handler(request):
        requests.append(request)
        assert request.headers['Authorization'] == 'Bearer synthetic-connection-key'
        if request.url.path == '/sessions':
            if request.method == 'POST':
                presence.append(json.loads(request.content))
            return httpx.Response(200, json={'sessions': presence})
        if request.url.path == '/status':
            return httpx.Response(200, json={'status': 'ready'})
        if request.url.path.endswith('/cursor'):
            cursor = messages[-1]['id'] if messages else '0-0'
            while on_snapshot:  # each action races with one snapshot only
                on_snapshot.pop(0)()
            return httpx.Response(200, json={'cursor': cursor, 'time_ms': 1000})
        if request.method == 'POST':
            row = {**json.loads(request.content), 'id': str(len(messages) + 1) + '-0'}
            messages.append(row)
            return httpx.Response(200, json=row)
        cursor = request.url.params['cursor']
        conversation = request.url.params.get('conversation_id')
        # Like the service, the limit counts scanned records and the conversation filter applies afterwards.
        scanned = [row for row in messages
                   if int(row['id'].split('-')[0]) > int(cursor.split('-')[0])][:int(request.url.params.get('limit', 100))]
        result = [row for row in scanned if conversation is None or row['conversation_id'] == conversation]
        if on_read:  # records that arrive while one read is in progress
            on_read.pop(0)()
        return httpx.Response(200, json={'messages': result, 'cursor': scanned[-1]['id'] if scanned else cursor})
    original_sync, original_async = httpx.Client, httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(cli.httpx, 'Client', lambda **settings: original_sync(transport=transport, **settings))
    monkeypatch.setattr(cli.httpx, 'AsyncClient', lambda **settings: original_async(transport=transport, **settings))
    def invoke(*arguments, stdin=None):
        selected = options('--config', str(config), '--client', 'codex', '--session-id', 'native-self', *arguments)
        if stdin is not None:
            monkeypatch.setattr('sys.stdin', io.StringIO(json.dumps(stdin)))
        cli.run(selected)
        return json.loads(capsys.readouterr().out)
    return SimpleNamespace(invoke=invoke, requests=requests, messages=messages, presence=presence,
                           directory=tmp_path / 'state', key=key, on_snapshot=on_snapshot, on_read=on_read,
                           binding=binding, root=tmp_path)


@pytest.mark.parametrize('missing', ['client', 'project', 'binding_file'])
def test_missing_binding_points_to_installation_and_changes_nothing(connection_client, missing):
    session = connection_client
    session.invoke('connect')
    diagnostic = exchange.session_directory(session.directory, 'native-self') / 'last-hook.json'
    diagnostic.write_text(json.dumps({'timestamp': time.time(), 'state': 'waiting', 'session_id': 'native-self'}))
    manifest_path = session.root / 'install-manifest.json'
    manifest = json.loads(manifest_path.read_text())
    if missing == 'client':
        manifest['hooks'][0]['client'] = 'claude'
    if missing == 'project':
        manifest['hooks'][0]['project'] = str(session.root / 'another-project')
    if missing == 'binding_file':
        session.binding.unlink()
    manifest_path.write_text(json.dumps(manifest))
    before = manifest_path.read_bytes()
    result = session.invoke('connect')
    status = session.invoke('status')
    assert (result['reception'] == status['reception'] == 'hook_missing'
            and 'explicit installer' in result['next_action']
            and '/hooks' not in result['next_action']
            and manifest_path.read_bytes() == before)


@pytest.mark.parametrize('damage', ['command', 'json_manifest', 'wrapper'])
def test_invalid_binding_is_distinct_from_an_unobserved_receiver(connection_client, damage):
    session = connection_client
    if damage == 'command':
        session.binding.write_text('[mcp_servers.channels]\ncommand = "unrelated-command"\n')
    if damage == 'json_manifest':
        (session.root / 'install-manifest.json').write_text('{')
    if damage == 'wrapper':
        (session.root / 'bin/channels').write_text('#!/bin/sh\nexit 0\n')
    before = session.binding.read_bytes()
    result = session.invoke('status')
    assert (result['reception'] == 'hook_invalid' and 'Inspect the reported file' in result['next_action']
            and session.binding.read_bytes() == before)


def test_binding_inspection_accepts_unrelated_settings(connection_client):
    session = connection_client
    session.binding.write_text('model = "custom"\n' + session.binding.read_text() + '[other]\nvalue = 1\n')
    before = session.binding.read_bytes()
    result = session.invoke('connect')
    assert (result['reception'] == 'hook_unobserved' and result['hook_binding']['status'] == 'installed'
            and session.binding.read_bytes() == before)


def test_missing_installer_points_to_download_instead_of_a_nonexistent_command(connection_client):
    session = connection_client
    (session.root / 'harness.pyz').unlink()
    session.binding.unlink()
    result = session.invoke('status')
    assert (result['reception'] == 'hook_missing' and result['install_command'] is None
            and 'http://synthetic-service/help' in result['next_action'])


def test_session_in_a_subdirectory_uses_the_installed_parent_binding(connection_client):
    session = connection_client
    subdirectory = session.root / 'nested/work'
    subdirectory.mkdir(parents=True)
    session.invoke('connect')
    selected = options('--config', str(session.root / 'config.json'), '--client', 'codex', '--session-id', 'native-self',
                       '--project', str(subdirectory), 'status')
    result = cli.reception_status(selected)
    assert (result['hook_binding']['status'] == 'installed' and result['hook_binding']['project'] == str(session.root)
            and str(subdirectory) not in (result['install_command'] or '') and result['reception'] == 'hook_unobserved')


def test_status_offers_the_removal_command_only_when_something_is_installed(connection_client):
    session = connection_client
    installed = session.invoke('status')['uninstall_command']
    manifest = session.root / 'install-manifest.json'
    manifest.write_text(json.dumps({'version': 1, 'files': {}, 'hooks': []}))
    assert (shlex.split(installed)[2:] == ['uninstall', '--target', str(session.root), '--project', str(session.root)]
            and session.invoke('status')['uninstall_command'] is None)


def test_unobserved_receiver_does_not_order_a_restart(connection_client):
    result = connection_client.invoke('connect')
    assert (result['reception'] == 'hook_unobserved' and result['reception_open'] is True
            and result['next_action'].startswith('Installed, but no background receiver has been observed')
            and 'If it stays unobserved' in result['next_action'])


@pytest.mark.parametrize('client', ['claude', 'codex'])
def test_resume_uses_current_native_session_and_shell_safe_working_directory(connection_client, monkeypatch, client):
    session = connection_client
    current = session.root / "working dir 'quoted' $(false)"
    current.mkdir()
    monkeypatch.chdir(current)
    monkeypatch.setenv('CLAUDE_CODE_SESSION_ID', 'native-self' if client == 'claude' else 'other-session')
    monkeypatch.setenv('CODEX_THREAD_ID', 'native-self' if client == 'codex' else 'other-session')
    selected = options('--config', str(session.root / 'config.json'), '--client', client,
                       '--project', str(session.root), 'status')
    selected.session_id = cli.select_session(selected)
    result = cli.reception_status(selected)
    directory, command = result['resume_command'].split(' && ')
    assert (shlex.split(directory) == ['cd', '--', str(current)]
            and shlex.split(command) == [client, '--resume' if client == 'claude' else 'resume', 'native-self'])
    selected.session_id = 'explicit-other-session'
    assert cli.reception_status(selected)['resume_command'] is None


def test_connect_reuses_key_preserves_position_and_is_idempotent(connection_client, monkeypatch):
    session = connection_client
    monkeypatch.setattr(cli.getpass, 'getpass', lambda prompt: pytest.fail('an existing protected key must be reused'))
    session.invoke('connect', '--name', 'reviewer')
    path = exchange.owned_state(session.directory, 'native-self')
    exchange.write_state(path, {**exchange.read_state(path), 'pending': '2-0', 'deadline': time.time() - 1})
    second = session.invoke('connect')
    assert (second['topic']['cursor'] == '1-0' and second['topic']['pending'] == ['2-0']
            and sum(request.url.path.endswith('/cursor') for request in session.requests) == 1
            and session.presence[0]['name'] == 'reviewer' and 'name' not in session.presence[1])


def test_connect_missing_key_prompts_once_without_printing_it(connection_client, monkeypatch):
    session = connection_client
    session.key.unlink()
    prompts = []
    monkeypatch.setattr(cli.getpass, 'getpass', lambda prompt: prompts.append(prompt) or 'synthetic-connection-key')
    result = session.invoke('connect')
    assert len(prompts) == 1 and 'synthetic-connection-key' not in json.dumps(result)


def test_default_receive_shows_work_again_until_it_is_confirmed(connection_client):
    session = connection_client
    session.on_snapshot.append(lambda: session.messages.append({
        'id': '2-0', 'session_id': 'current-peer', 'conversation_id': 'main', 'text': 'racing peer message'}))
    session.invoke('connect')
    session.invoke('publish', '--text', 'own task')
    session.messages.append({'id': '4-0', 'session_id': 'current-peer', 'conversation_id': 'main', 'text': 'current reply'})
    first = session.invoke('receive', '--timeout', '0')
    resumed = session.invoke('receive', '--timeout', '0')
    session.invoke('ack', '--message-id', '2-0')
    session.invoke('ack', '--message-id', '4-0')
    confirmed = session.invoke('receive', '--timeout', '0')
    saved = exchange.read_state(exchange.owned_state(session.directory, 'native-self'))
    assert ([row['text'] for row in first['messages']] == ['racing peer message', 'current reply']
            and [row['redelivered'] for row in first['messages']] == [False, False]
            and [row['redelivered'] for row in resumed['messages']] == [True, True]
            and first['unconfirmed'] == ['2-0', '4-0'] and 'untrusted' in first['notice']
            and confirmed['messages'] == [] and saved['cursor'] == '4-0' and saved['pending'] == [])


def add_peer_rows(session, conversation_id, text, count):
    first = int(session.messages[-1]['id'].split('-')[0]) + 1
    session.messages.extend({'id': f'{number}-0', 'session_id': 'current-peer', 'conversation_id': conversation_id, 'text': text}
                            for number in range(first, first + count))


def test_receive_reads_past_full_pages_of_other_conversations(connection_client):
    session = connection_client
    session.invoke('connect')
    add_peer_rows(session, 'other', 'elsewhere', 150)
    add_peer_rows(session, 'main', 'question for this session', 1)
    received = session.invoke('receive', '--timeout', '0')
    assert [row['text'] for row in received['messages']] == ['question for this session']


def test_unconfirmed_message_does_not_hide_newer_work_beyond_one_scanned_page(connection_client):
    session = connection_client
    session.invoke('connect')
    add_peer_rows(session, 'main', 'first question', 1)
    session.invoke('receive', '--timeout', '0')
    add_peer_rows(session, 'other', 'elsewhere', 150)
    add_peer_rows(session, 'main', 'second question', 1)
    received = session.invoke('receive', '--timeout', '0')
    assert [(row['text'], row['redelivered']) for row in received['messages']] == [('first question', True), ('second question', False)]


def message_reads(session):
    return sum(request.method == 'GET' and request.url.path.endswith('/messages') for request in session.requests)


def test_receive_stops_at_the_stream_end_it_saw_while_other_conversations_keep_growing(connection_client):
    session = connection_client
    session.invoke('connect')
    add_peer_rows(session, 'main', 'question for this session', 1)
    # Inflow lasts ten reads, so a read that does not stop at the end it saw fails instead of hanging.
    session.on_read.extend([lambda: add_peer_rows(session, 'other', 'elsewhere', 100)] * 10)
    reads_before = message_reads(session)
    session.invoke('receive', '--timeout', '0')
    assert message_reads(session) - reads_before == 1


def test_receive_returns_one_page_and_records_only_what_it_returned(connection_client):
    session = connection_client
    session.invoke('connect')
    add_peer_rows(session, 'main', 'question', 99)
    add_peer_rows(session, 'other', 'elsewhere', 1)
    add_peer_rows(session, 'main', 'question', 100)
    received = session.invoke('receive', '--timeout', '0')
    assert received['unconfirmed'] == [f'{number}-0' for number in range(2, 101)] + ['102-0']


def test_full_page_at_the_stream_end_reports_no_more(connection_client):
    session = connection_client
    session.invoke('connect')
    add_peer_rows(session, 'main', 'question', 100)
    assert session.invoke('receive', '--timeout', '0')['more'] is False


def pin_cursor_behind_a_long_backlog(session):
    """An unconfirmed message keeps reads starting before 500 records of another conversation."""
    session.invoke('connect')
    add_peer_rows(session, 'main', 'first question', 1)
    session.invoke('receive', '--timeout', '0')
    add_peer_rows(session, 'other', 'elsewhere', 500)
    add_peer_rows(session, 'main', 'second question', 1)
    return message_reads(session)


def test_receive_scans_a_bounded_number_of_pages(connection_client, monkeypatch):
    monkeypatch.setattr(cli, 'MAX_SCANNED_PAGES', 3)
    reads_before = pin_cursor_behind_a_long_backlog(connection_client)
    connection_client.invoke('receive', '--timeout', '0')
    assert message_reads(connection_client) - reads_before == 3


def test_receive_bounded_by_pages_reports_more(connection_client, monkeypatch):
    monkeypatch.setattr(cli, 'MAX_SCANNED_PAGES', 3)
    pin_cursor_behind_a_long_backlog(connection_client)
    assert connection_client.invoke('receive', '--timeout', '0')['more'] is True


@pytest.mark.parametrize('status, error', [(401, 'unauthorized401'), (503, 'HTTP 503')])
def test_receive_reports_service_errors(monkeypatch, status, error):
    def respond(request):
        if request.url.path.endswith('/cursor'):
            return httpx.Response(200, json={'cursor': '5-0', 'time_ms': 1})
        return httpx.Response(status)
    original_client = httpx.Client
    monkeypatch.setattr(cli.httpx, 'Client', lambda **settings: original_client(transport=httpx.MockTransport(respond), **settings))
    selected = options('--session-id', 'reader', 'receive', 'general', '--cursor', '0-0')
    with pytest.raises(ValueError, match=error):
        cli.report_page(selected, 'synthetic-key', False)


def test_failure_while_building_the_result_loses_nothing(connection_client, monkeypatch):
    session = connection_client
    session.invoke('connect')
    session.messages.append({'id': '2-0', 'session_id': 'peer', 'conversation_id': 'main', 'text': 'work'})
    def fail(*arguments):
        raise RuntimeError('injected')
    original = cli.read_details
    monkeypatch.setattr(cli, 'read_details', fail)
    with pytest.raises(RuntimeError):
        session.invoke('receive', '--timeout', '0')
    monkeypatch.setattr(cli, 'read_details', original)
    assert [row['text'] for row in session.invoke('receive', '--timeout', '0')['messages']] == ['work']


def test_failure_printing_the_result_leaves_the_work_pending(connection_client, monkeypatch):
    session = connection_client
    session.invoke('connect')
    session.messages.append({'id': '2-0', 'session_id': 'peer', 'conversation_id': 'main', 'text': 'work'})
    selected = options('--config', str(session.root / 'config.json'), '--client', 'codex', '--session-id', 'native-self', 'receive', '--timeout', '0')
    monkeypatch.setattr('builtins.print', lambda *arguments, **settings: (_ for _ in ()).throw(BrokenPipeError()))
    with pytest.raises(BrokenPipeError):
        cli.run(selected)
    monkeypatch.undo()
    saved = exchange.read_state(exchange.owned_state(session.directory, 'native-self'))
    assert saved['pending'] == ['2-0'] and saved['cursor'] == '1-0'


def test_pending_message_removed_by_retention_is_reported_and_never_confirmed(connection_client):
    session = connection_client
    session.invoke('connect')
    session.messages.append({'id': '2-0', 'session_id': 'peer', 'conversation_id': 'main', 'text': 'work'})
    session.invoke('receive', '--timeout', '0')
    session.messages[:] = [row for row in session.messages if row['id'] != '2-0']
    reported = session.invoke('receive', '--timeout', '0')
    saved = exchange.read_state(exchange.owned_state(session.directory, 'native-self'))
    session.invoke('ack', '--message-id', '2-0')
    cleared = exchange.read_state(exchange.owned_state(session.directory, 'native-self'))
    assert (reported['evicted_pending'] == ['2-0'] and 'send them again' in reported['recovery'] and saved['pending'] == ['2-0']
            and cleared['pending'] == [] and cleared['cursor'] == '2-0')


def test_ack_during_another_read_is_not_undone_by_that_read(connection_client, monkeypatch):
    session = connection_client
    session.invoke('connect')
    session.messages.append({'id': '2-0', 'session_id': 'peer', 'conversation_id': 'main', 'text': 'work'})
    session.invoke('receive', '--timeout', '0')
    original = cli.read_details
    async def confirm_while_reading(*arguments):
        result = await original(*arguments)
        exchange.ack_exchange(session.directory, 'native-self', {'channel': 'general', 'conversation_id': 'main'}, '2-0')
        return result
    monkeypatch.setattr(cli, 'read_details', confirm_while_reading)
    session.invoke('receive', '--timeout', '0')
    monkeypatch.setattr(cli, 'read_details', original)
    saved = exchange.read_state(exchange.owned_state(session.directory, 'native-self'))
    assert not saved['pending'] and saved['cursor'] == '2-0' and session.invoke('receive', '--timeout', '0')['messages'] == []


def test_answering_one_question_leaves_the_other_unanswered_one_pending(connection_client):
    session = connection_client
    session.invoke('connect')
    session.messages.extend([{'id': '2-0', 'session_id': 'peer', 'conversation_id': 'main', 'text': 'first question'},
                             {'id': '3-0', 'session_id': 'peer', 'conversation_id': 'main', 'text': 'second question'}])
    session.invoke('receive', '--timeout', '0')
    session.invoke('publish', '--text', 'answer to the second', '--reply-to', '3-0')
    shown = session.invoke('receive', '--timeout', '0')['messages']
    session.invoke('publish', '--text', 'answer to the first', '--reply-to', '2-0')
    finished = session.invoke('receive', '--timeout', '0')['messages']
    saved = exchange.read_state(exchange.owned_state(session.directory, 'native-self'))
    assert [row['text'] for row in shown] == ['first question'] and finished == [] and not saved['pending']


def write_old_state(session, **fields):
    path = exchange.owned_state(session.directory, 'native-self')
    document = json.loads(path.read_text())
    path.write_text(json.dumps({**{key: document[key] for key in exchange.STATE_FIELDS - {'notified', 'delivered', 'pending'}}, **fields}))


def test_batch_state_keeps_every_message_of_its_range_unconfirmed(connection_client):
    session = connection_client
    session.invoke('connect')
    session.messages.extend([{'id': '2-0', 'session_id': 'peer', 'conversation_id': 'main', 'text': 'first'},
                             {'id': '3-0', 'session_id': 'peer', 'conversation_id': 'main', 'text': 'second'}])
    write_old_state(session, notified='3-0', pending='3-0')
    shown = session.invoke('receive', '--timeout', '0')
    session.invoke('ack', '--message-id', '3-0')
    remaining = session.invoke('receive', '--timeout', '0')
    assert ([row['text'] for row in shown['messages']] == ['first', 'second'] and shown['unconfirmed'] == ['2-0', '3-0']
            and [row['text'] for row in remaining['messages']] == ['first'] and remaining['unconfirmed'] == ['2-0'])


def test_batch_state_acknowledged_before_any_read_still_shows_the_earlier_message(connection_client):
    session = connection_client
    session.invoke('connect')
    session.messages.extend([{'id': '2-0', 'session_id': 'peer', 'conversation_id': 'main', 'text': 'first'},
                             {'id': '3-0', 'session_id': 'peer', 'conversation_id': 'main', 'text': 'second'}])
    write_old_state(session, notified='3-0', pending='3-0')
    session.invoke('ack', '--message-id', '3-0')
    assert 'first' in [row['text'] for row in session.invoke('receive', '--timeout', '0')['messages']]


def test_deployed_single_delivery_state_moves_up_without_losing_or_repeating_work(connection_client):
    session = connection_client
    session.invoke('connect')
    session.messages.extend([{'id': '2-0', 'session_id': 'peer', 'conversation_id': 'main', 'text': 'delivered by Stop'},
                             {'id': '3-0', 'session_id': 'peer', 'conversation_id': 'main', 'text': 'not delivered yet'}])
    write_old_state(session, pending='2-0')
    shown = session.invoke('receive', '--timeout', '0')
    session.invoke('ack', '--message-id', '2-0')
    session.invoke('ack', '--message-id', '3-0')
    assert ([(row['text'], row['redelivered']) for row in shown['messages']] == [('delivered by Stop', True), ('not delivered yet', False)]
            and session.invoke('receive', '--timeout', '0')['messages'] == [])


def test_deployed_state_acknowledged_before_any_read_does_not_repeat_it(connection_client):
    session = connection_client
    session.invoke('connect')
    session.messages.append({'id': '2-0', 'session_id': 'peer', 'conversation_id': 'main', 'text': 'delivered by Stop'})
    write_old_state(session, pending='2-0')
    session.invoke('ack', '--message-id', '2-0')
    assert session.invoke('receive', '--timeout', '0')['messages'] == []


def test_rows_with_nothing_to_handle_are_not_shown_twice(connection_client):
    session = connection_client
    session.invoke('connect')
    session.messages.extend([{'id': '2-0', 'session_id': 'native-self', 'conversation_id': 'main', 'text': 'mine'},
                             {'id': '3-0', 'session_id': 'other', 'conversation_id': 'main', 'text': 'their question'},
                             {'id': '4-0', 'session_id': 'peer', 'conversation_id': 'main', 'text': 'their answer', 'reply_to': '3-0'}])
    shown = session.invoke('receive', '--timeout', '0')
    again = session.invoke('receive', '--timeout', '0')
    session.invoke('ack', '--message-id', '3-0')
    finished = session.invoke('receive', '--timeout', '0')
    assert ([row['for_you'] for row in shown['messages']] == [True, False] and shown['unconfirmed'] == ['3-0']
            and [row['text'] for row in again['messages']] == ['their question'] and finished['messages'] == [])


def test_receive_marks_only_replies_to_this_session_for_it(connection_client):
    session = connection_client
    session.invoke('connect')
    session.messages.extend([
        {'id': '2-0', 'session_id': 'native-self', 'conversation_id': 'main', 'text': 'my request'},
        {'id': '3-0', 'session_id': 'other-peer', 'conversation_id': 'main', 'text': 'their request'},
        {'id': '4-0', 'session_id': 'peer', 'conversation_id': 'main', 'text': 'answer to me', 'reply_to': '2-0'},
        {'id': '5-0', 'session_id': 'peer', 'conversation_id': 'main', 'text': 'answer to them', 'reply_to': '3-0'}])
    rows = session.invoke('receive', '--timeout', '0')['messages']
    assert ({row['text']: row['for_you'] for row in rows} == {'their request': True, 'answer to me': True, 'answer to them': False}
            and {row['text'] for row in rows if 'expected' in row} == {'answer to me'})


def test_confirmation_of_my_answer_asks_for_no_further_reply(connection_client):
    session = connection_client
    session.invoke('connect')
    session.messages.extend([
        {'id': '2-0', 'session_id': 'peer', 'conversation_id': 'main', 'text': 'question'},
        {'id': '3-0', 'session_id': 'native-self', 'conversation_id': 'main', 'text': 'answer', 'reply_to': '2-0'},
        {'id': '4-0', 'session_id': 'peer', 'conversation_id': 'main', 'text': 'thanks', 'reply_to': '3-0'}])
    rows = {row['text']: row for row in session.invoke('receive', '--timeout', '0')['messages']}
    assert rows['thanks']['for_you'] is True and 'expected' not in rows['thanks'] and 'expected' not in rows['question']


def test_explicit_history_read_never_moves_the_saved_position(connection_client):
    session = connection_client
    session.invoke('connect')
    before = exchange.read_state(exchange.owned_state(session.directory, 'native-self'))
    history = session.invoke('receive', '--cursor', '0-0', '--timeout', '0')
    after = exchange.read_state(exchange.owned_state(session.directory, 'native-self'))
    assert [row['text'] for row in history['messages']] == ['old work'] and before == after


def test_receive_defaults_to_current_main_and_history_discovery_is_explicit(connection_client):
    session = connection_client
    current = session.invoke('receive', '--timeout', '0')
    session.messages.append({'id': '2-0', 'session_id': 'other-peer', 'conversation_id': 'other', 'text': 'unrelated task'})
    main = session.invoke('receive', '--cursor', '0-0', '--timeout', '0')
    all_topics = session.invoke('receive', '--all-conversations', '--cursor', '0-0', '--timeout', '0')
    assert (current['messages'] == [] and [row['text'] for row in main['messages']] == ['old work']
            and len(all_topics['messages']) == 2 and not exchange.list_states(session.directory, 'native-self')
            and current['reception_open'] is False)


def test_open_defaults_fresh_and_reopen_preserves_position_while_history_is_explicit(connection_client):
    session = connection_client
    opened = session.invoke('open', '--timeout', '60')
    path = exchange.owned_state(session.directory, 'native-self')
    saved = {**exchange.read_state(path), 'deadline': time.time() - 1, 'pending': '2-0'}
    exchange.write_state(path, saved)
    resumed = session.invoke('open')
    historical = session.invoke('open', '--conversation-id', 'historical', '--cursor', '0-0')
    assert (opened['cursor'] == '1-0' and resumed['cursor'] == '1-0' and resumed['pending'] == ['2-0']
            and historical['cursor'] == '0-0'
            and sum(request.url.path.endswith('/cursor') for request in session.requests) == 1)


def test_open_cursor_moves_the_position_publish_saved(connection_client):
    session = connection_client
    session.invoke('publish', '--text', 'hello')
    assert session.invoke('open', '--cursor', '0-0')['cursor'] == '0-0'


def test_open_cursor_shows_history_after_publish(connection_client):
    session = connection_client
    session.invoke('publish', '--text', 'hello')
    session.invoke('open', '--cursor', '0-0')
    assert [row['text'] for row in session.invoke('receive', '--timeout', '0')['messages']] == ['old work']


def test_open_cursor_refuses_to_hide_unconfirmed_work(connection_client):
    session = connection_client
    session.invoke('publish', '--text', 'hello')
    add_peer_rows(session, 'main', 'question', 1)
    session.invoke('receive', '--timeout', '0')
    with pytest.raises(ValueError, match='session_pending_ack_required'):
        session.invoke('open', '--cursor', '0-0')


def publish_to_conversations(session, count):
    for number in range(count):
        session.invoke('publish', '--conversation-id', f'sent-{number}', '--text', 'hello')


def test_topics_only_published_to_leave_room_for_a_new_one(connection_client):
    publish_to_conversations(connection_client, 16)
    assert connection_client.invoke('connect', '--conversation-id', 'new')['type'] == 'connected'


def test_listening_topics_keep_their_slots(connection_client):
    for number in range(16):
        connection_client.invoke('connect', '--conversation-id', f'live-{number}')
    with pytest.raises(ValueError, match='session_topic_limit'):
        connection_client.invoke('connect', '--conversation-id', 'new')


def test_topic_that_gained_unconfirmed_work_is_not_replaced(tmp_path):
    target = {'channel': 'general', 'conversation_id': 'main', 'cursor': '1-0'}
    state = {**exchange.new_state('native-self', target, 60), 'deadline': 1.0}
    exchange.open_exchange(tmp_path, 'native-self', state)
    path = exchange.owned_state(tmp_path, 'native-self')
    exchange.write_state(path, {**state, 'pending': ['2-0']})  # recorded after the replacement candidates were read
    with pytest.raises(ValueError, match='session_topic_limit'):
        exchange.drop_idle(path)


def test_read_of_a_topic_replaced_meanwhile_names_the_cause(tmp_path):
    for number in range(exchange.MAX_SESSION_TOPICS + 1):
        target = {'channel': 'general', 'conversation_id': f'sent-{number}', 'cursor': '1-0'}
        exchange.open_exchange(tmp_path, 'native-self', {**exchange.new_state('native-self', target, 60), 'deadline': 1.0 + number})
    replaced = exchange.session_directory(tmp_path, 'native-self') / exchange.topic_name(
        {'channel': 'general', 'conversation_id': 'sent-0'})
    with pytest.raises(ValueError, match='session_topic_replaced'):
        exchange.settle_read(replaced, '99-0', ['99-0'])


def test_unconfirmed_work_keeps_its_topic(connection_client):
    session = connection_client
    session.invoke('publish', '--text', 'hello')
    add_peer_rows(session, 'main', 'question', 1)
    question = session.messages[-1]['id']
    session.invoke('receive', '--timeout', '0')
    publish_to_conversations(session, 15)
    session.invoke('connect', '--conversation-id', 'new')
    target = {'channel': 'general', 'conversation_id': 'main'}
    assert exchange.read_state(exchange.owned_state(session.directory, 'native-self', target))['pending'] == [question]


def test_open_cursor_on_a_listening_topic_reports_that_first(connection_client):
    session = connection_client
    session.invoke('connect')
    add_peer_rows(session, 'main', 'question', 1)
    session.invoke('receive', '--timeout', '0')
    with pytest.raises(ValueError, match='session_topic_already_live'):
        session.invoke('open', '--cursor', '0-0')


def test_stale_observation_is_distinct_from_listening(connection_client):
    session = connection_client
    session.invoke('connect')
    path = exchange.session_directory(session.directory, 'native-self') / 'last-hook.json'
    path.write_text(json.dumps({'timestamp': time.time() - 120, 'state': 'waiting', 'session_id': 'native-self'}))
    stale = session.invoke('status')
    path.write_text(json.dumps({'timestamp': time.time(), 'state': 'waiting', 'session_id': 'native-self'}))
    listening = session.invoke('status')
    assert (stale['reception'] == 'callback_stale' and stale['callback_age_seconds'] >= 120
            and listening['reception'] == 'waiting' and listening['last_callback']['state'] == 'waiting')


def test_status_and_publish_do_not_announce_presence(connection_client):
    session = connection_client
    session.invoke('status')
    session.invoke('publish', '--text', 'one-way message')
    assert session.presence == []


def test_peers_lists_service_announcements_and_close_retires_last_topic(connection_client):
    session = connection_client
    session.invoke('connect')
    peers = session.invoke('peers')
    session.invoke('close')
    assert (peers['sessions'][0]['client'] == 'codex' and session.requests[-1].url.path == '/sessions'
            and session.presence[-1]['state'] == 'closed' and session.presence[-1]['ttl_seconds'] == 1)


def test_publish_opens_no_reception_and_returns_no_text(connection_client):
    session = connection_client
    result = session.invoke('publish', '--text', 'one-way message')
    live = [path for path in exchange.list_states(session.directory, 'native-self')
            if exchange.read_state(path)['deadline'] > time.time()]
    assert live == [] and result['reception_open'] is False and result['reception'] == 'not_open' and 'text' not in result


def test_publish_keeps_an_open_topic_untouched(connection_client):
    session = connection_client
    session.invoke('open')
    before = {path: path.read_bytes() for path in exchange.list_states(session.directory, 'native-self')}
    result = session.invoke('publish', '--text', 'message')
    assert before == {path: path.read_bytes() for path in exchange.list_states(session.directory, 'native-self')} and result['reception_open'] is True


def longest_state(pending_count):
    """A topic state with the longest identifiers and PENDING_COUNT unconfirmed message IDs."""
    target = {'channel': 'c' * 128, 'conversation_id': 'v' * 128, 'cursor': '1794373367910-0'}
    state = exchange.new_state('s' * 256, target, 60)
    return {**state, 'pending': [f'1794373367910-{number}' for number in range(1, pending_count + 1)]}


def test_two_pages_of_unconfirmed_work_with_the_longest_names_stay_readable(tmp_path):
    state = longest_state(200)
    exchange.open_exchange(tmp_path, state['session_id'], state)
    assert exchange.read_state(exchange.owned_state(tmp_path, state['session_id']))['pending'] == state['pending']


def test_state_too_large_to_read_back_is_refused(tmp_path):
    exchange.open_exchange(tmp_path, 's' * 256, longest_state(1))
    with pytest.raises(ValueError, match='session_state_full'):
        exchange.write_state(exchange.owned_state(tmp_path, 's' * 256), longest_state(1000))


def test_refused_state_leaves_the_saved_one_readable(tmp_path):
    exchange.open_exchange(tmp_path, 's' * 256, longest_state(1))
    path = exchange.owned_state(tmp_path, 's' * 256)
    with contextlib.suppress(ValueError):
        exchange.write_state(path, longest_state(1000))
    assert exchange.read_state(path)['pending'] == ['1794373367910-1']


def test_failed_publish_leaves_no_state(connection_client, monkeypatch):
    session = connection_client
    monkeypatch.setattr(cli, 'post_message', lambda *args: httpx.Response(503))
    with pytest.raises(ValueError, match='HTTP 503'):
        session.invoke('publish', '--text', 'failed request')
    assert exchange.list_states(session.directory, 'native-self') == []


def test_reply_after_publish_is_not_skipped_by_a_later_connect(connection_client):
    session = connection_client
    published = session.invoke('publish', '--text', 'request')
    session.messages.append({'id': '3-0', 'session_id': 'peer', 'conversation_id': 'main', 'text': 'fast reply'})
    connected = session.invoke('connect')
    received = session.invoke('receive', '--timeout', '0')
    assert (connected['topic']['cursor'] == published['id'] and connected['topic']['notified'] == published['id']
            and [row['text'] for row in received['messages']] == ['fast reply'])


def test_receive_after_publish_reads_the_reply_without_joining(connection_client):
    session = connection_client
    session.invoke('publish', '--text', 'request')
    session.messages.append({'id': '3-0', 'session_id': 'peer', 'conversation_id': 'main', 'text': 'reply'})
    received = session.invoke('receive', '--timeout', '0')
    assert [row['text'] for row in received['messages']] == ['reply'] and received['reception_open'] is False


def test_key_stdin_on_a_terminal_reads_without_echo(tmp_path, monkeypatch):
    stream = io.StringIO()
    monkeypatch.setattr(stream, 'isatty', lambda: True)
    monkeypatch.setattr('sys.stdin', stream)
    monkeypatch.setattr(cli.getpass, 'getpass', lambda prompt: 'synthetic-terminal-key')
    assert cli.key_input(options('--key-file', str(tmp_path / 'key'), 'key', '--stdin')) == 'synthetic-terminal-key'
