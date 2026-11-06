import asyncio
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


@pytest.fixture(autouse=True)
def clean_native_environment(monkeypatch):
    monkeypatch.delenv('CODEX_THREAD_ID', raising=False)
    monkeypatch.delenv('CLAUDE_CODE_SESSION_ID', raising=False)


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
        process.wait(timeout=3)
        output, errors = process.communicate(timeout=1)
    finally:
        if process.poll() is None:
            process.terminate()
            process.communicate(timeout=2)
    assert process.returncode == exit_code and json.loads(output) == {}
    assert ('native hook event JSON required; do not invoke manually' in errors) if exit_code else errors == ''


@pytest.mark.parametrize('missing_config', [False, True])
def test_empty_hook_input_returns_failure_even_when_config_is_missing(tmp_path, missing_config):
    arguments = ['--config', str(tmp_path / 'missing.json')] if missing_config else []
    result = subprocess.run([sys.executable, '-m', 'polyagent_channels.cli', '--state-dir', str(tmp_path / 'state'),
                             *arguments, 'hook'], input='', capture_output=True, text=True, timeout=3)
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
    saved = subprocess.run(command, input=original + '\n', capture_output=True, text=True, timeout=3)
    reused = subprocess.run(command, input=original, capture_output=True, text=True, timeout=3)
    refused = subprocess.run(command, input=different, capture_output=True, text=True, timeout=3)
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
                            input=content, capture_output=True, text=True, timeout=3)
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


def test_listener_rejects_negative_timeout():
    with pytest.raises(ValueError, match='nonnegative'):
        asyncio.run(cli.listen_until_timeout(SimpleNamespace(timeout=-1), 'http://localhost', {}))


def test_listener_bounded_timeout_exits_normally(monkeypatch):
    monkeypatch.setattr(cli, 'listen', AsyncMock(side_effect=TimeoutError))
    assert asyncio.run(cli.listen_until_timeout(SimpleNamespace(timeout=1, cursor='0-0', conversation_id=None), 'http://localhost', {})) is None


@pytest.mark.parametrize('command', ['publish', 'receive'])
def test_http_client_uses_expected_transport(monkeypatch, command):
    captured = []

    def respond(request):
        captured.append(request)
        return httpx.Response(200, json={'messages': []})

    original_client = httpx.Client
    monkeypatch.setattr(cli.httpx, 'Client', lambda **settings: original_client(transport=httpx.MockTransport(respond), **settings))
    options = SimpleNamespace(command=command, cursor='2-0', timeout=5, text='hello', file=None, conversation_id='main', reply_to=None, session_id='test-session', sender=None)
    cli.request_messages(options, 'http://localhost/channels/general', {})
    assert captured[0].method == ('POST' if command == 'publish' else 'GET')


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

    monkeypatch.setattr(cli, 'connect', lambda *parameters, **settings: SyntheticSocket())
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
    hook = setup.native_hook(tmp_path, 'codex')
    hook.parent.mkdir()
    command = shlex.join([str(wrapper), '--client', 'codex', 'hook'])
    hook.write_bytes(setup.hook_document(hook, command)[0])
    manifest = {'version': 1, 'files': {}, 'hooks': [
        {'project': str(tmp_path), 'client': 'codex', 'path': str(hook), 'command': command}]}
    (tmp_path / 'install-manifest.json').write_text(json.dumps(manifest))
    requests = []
    messages = [{'id': '1-0', 'session_id': 'old-peer', 'conversation_id': 'main', 'text': 'old work'}]
    presence = []
    on_snapshot = []
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
            for action in on_snapshot:
                action()
            return httpx.Response(200, json={'cursor': cursor, 'time_ms': 1000})
        if request.method == 'POST':
            topic = exchange.owned_state(tmp_path / 'state', 'native-self', {'channel': 'general', 'conversation_id': 'main'})
            assert topic is not None, 'Reception must be open before a peer can reply to this publish'
            row = {**json.loads(request.content), 'id': str(len(messages) + 1) + '-0'}
            messages.append(row)
            return httpx.Response(200, json=row)
        cursor = request.url.params['cursor']
        conversation = request.url.params.get('conversation_id')
        result = [row for row in messages if int(row['id'].split('-')[0]) > int(cursor.split('-')[0])
                  and (conversation is None or row['conversation_id'] == conversation)]
        return httpx.Response(200, json={'messages': result, 'cursor': messages[-1]['id'] if messages else cursor})
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
                           directory=tmp_path / 'state', key=key, on_snapshot=on_snapshot, hook=hook, root=tmp_path)


@pytest.mark.parametrize('missing', ['client', 'project', 'hook_file'])
def test_missing_binding_does_not_claim_native_approval_is_the_next_step(connection_client, missing):
    session = connection_client
    session.invoke('connect')
    diagnostic = exchange.session_directory(session.directory, 'native-self') / 'last-hook.json'
    diagnostic.write_text(json.dumps({'timestamp': time.time(), 'state': 'waiting',
                                      'session_id': 'native-self', 'callback_id': 'old-binding'}))
    manifest_path = session.root / 'install-manifest.json'
    manifest = json.loads(manifest_path.read_text())
    if missing == 'client':
        manifest['hooks'][0]['client'] = 'claude'
    if missing == 'project':
        manifest['hooks'][0]['project'] = str(session.root / 'another-project')
    if missing == 'hook_file':
        session.hook.unlink()
    manifest_path.write_text(json.dumps(manifest))
    before = manifest_path.read_bytes()
    result = session.invoke('connect')
    status = session.invoke('status')
    assert (result['reception'] == status['reception'] == 'hook_missing'
            and 'explicit installer' in result['next_action']
            and '/hooks' not in result['next_action']
            and manifest_path.read_bytes() == before)


@pytest.mark.parametrize('damage', ['command', 'timeout', 'json', 'manifest', 'matcher', 'wrapper'])
def test_invalid_binding_is_distinct_from_an_unobserved_callback(connection_client, damage):
    session = connection_client
    document = json.loads(session.hook.read_text())
    if damage == 'command':
        document['hooks']['Stop'][0]['hooks'][0]['command'] = 'unrelated-command'
    if damage == 'timeout':
        document['hooks']['Stop'][0]['hooks'][0]['timeout'] = 1
    if damage == 'matcher':
        document['hooks']['Stop'][0]['matcher'] = 'restricted'
    session.hook.write_text(json.dumps(document) if damage != 'json' else '{')
    if damage == 'manifest':
        (session.root / 'install-manifest.json').write_text('{')
    if damage == 'wrapper':
        (session.root / 'bin/channels').write_text('#!/bin/sh\nexit 0\n')
    before = session.hook.read_bytes()
    result = session.invoke('status')
    assert (result['reception'] == 'hook_invalid' and 'Inspect the reported hook file' in result['next_action']
            and session.hook.read_bytes() == before)


def test_binding_inspection_accepts_unrelated_hooks_and_settings(connection_client):
    session = connection_client
    document = json.loads(session.hook.read_text())
    document['custom-setting'] = True
    document['hooks']['Stop'].append({'hooks': [{'type': 'command', 'command': 'other-hook'}]})
    session.hook.write_text(json.dumps(document))
    before = session.hook.read_bytes()
    result = session.invoke('connect')
    assert (result['reception'] == 'hook_unobserved' and result['hook_binding']['status'] == 'installed'
            and session.hook.read_bytes() == before)


def test_missing_installer_points_to_download_instead_of_a_nonexistent_command(connection_client):
    session = connection_client
    (session.root / 'harness.pyz').unlink()
    session.hook.unlink()
    result = session.invoke('status')
    assert (result['reception'] == 'hook_missing' and result['install_command'] is None
            and 'http://synthetic-service/help' in result['next_action'])


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
            and shlex.split(command) == [client, '--resume' if client == 'claude' else 'resume', 'native-self']
            and result['hook_binding']['project'] == str(session.root))
    selected.session_id = 'explicit-other-session'
    assert cli.reception_status(selected)['resume_command'] is None


def test_connect_reuses_key_preserves_pending_and_is_idempotent(connection_client, monkeypatch):
    session = connection_client
    monkeypatch.setattr(cli.getpass, 'getpass', lambda prompt: pytest.fail('an existing protected key must be reused'))
    first = session.invoke('connect', '--name', 'reviewer')
    path = exchange.owned_state(session.directory, 'native-self')
    original = exchange.read_state(path)
    exchange.write_state(path, {**original, 'pending': '2-0', 'deadline': time.time() - 1})
    second = session.invoke('connect')
    assert (first['reception'] == 'hook_unobserved' and second['topic']['cursor'] == '1-0'
            and second['topic']['pending'] == '2-0'
            and sum(request.url.path.endswith('/cursor') for request in session.requests) == 1
            and session.presence[0]['name'] == 'reviewer' and 'name' not in session.presence[1])


def test_connect_missing_key_prompts_once_without_printing_it(connection_client, monkeypatch):
    session = connection_client
    session.key.unlink()
    prompts = []
    monkeypatch.setattr(cli.getpass, 'getpass', lambda prompt: prompts.append(prompt) or 'synthetic-connection-key')
    result = session.invoke('connect')
    assert len(prompts) == 1 and 'synthetic-connection-key' not in json.dumps(result)


def test_publish_opens_reception_before_sending_and_keeps_pre_reply_cursor(connection_client):
    session = connection_client
    session.on_snapshot.append(lambda: session.messages.append({
        'id': '2-0', 'session_id': 'current-peer', 'conversation_id': 'main', 'text': 'racing peer message'}))
    published = session.invoke('publish', '--text', 'current task')
    session.messages.append({'id': '4-0', 'session_id': 'current-peer', 'conversation_id': 'main', 'text': 'current reply'})
    received = session.invoke('receive', '--timeout', '0')
    paths = [request.url.path for request in session.requests]
    assert (published['reception'] == 'hook_unobserved' and published['next_action']
            and paths.index('/channels/general/cursor') < paths.index('/channels/general/messages')
            and [row['text'] for row in received['messages']] == ['racing peer message', 'current task', 'current reply']
            and exchange.read_state(exchange.owned_state(session.directory, 'native-self'))['cursor'] == '1-0')


def test_receive_defaults_to_current_main_and_history_discovery_is_explicit(connection_client):
    session = connection_client
    current = session.invoke('receive', '--timeout', '0')
    session.messages.append({'id': '2-0', 'session_id': 'other-peer', 'conversation_id': 'other', 'text': 'unrelated task'})
    main = session.invoke('receive', '--cursor', '0-0', '--timeout', '0')
    all_topics = session.invoke('receive', '--all-conversations', '--cursor', '0-0', '--timeout', '0')
    assert (current['messages'] == [] and current['ack_required'] is False
            and 'only IDs previously delivered by Stop' in current['next_action']
            and [row['text'] for row in main['messages']] == ['old work']
            and len(all_topics['messages']) == 2)


def test_open_defaults_fresh_and_reopen_preserves_pending_while_history_is_explicit(connection_client):
    session = connection_client
    opened = session.invoke('open', '--timeout', '60')
    path = exchange.owned_state(session.directory, 'native-self')
    saved = {**exchange.read_state(path), 'deadline': time.time() - 1, 'pending': '2-0'}
    exchange.write_state(path, saved)
    resumed = session.invoke('open')
    historical = session.invoke('open', '--conversation-id', 'historical', '--cursor', '0-0')
    assert (opened['cursor'] == '1-0' and resumed['cursor'] == '1-0' and resumed['pending'] == '2-0'
            and historical['cursor'] == '0-0'
            and sum(request.url.path.endswith('/cursor') for request in session.requests) == 1)


@pytest.mark.parametrize('callback_state', ['waiting', 'callback_observed', 'peer', 'expired'])
def test_stale_callback_is_distinct_from_current_reception(connection_client, callback_state):
    session = connection_client
    session.invoke('connect')
    path = exchange.session_directory(session.directory, 'native-self') / 'last-hook.json'
    path.write_text(json.dumps({'timestamp': time.time() - 120, 'state': callback_state,
                               'session_id': 'native-self', 'callback_id': 'old'}))
    result = session.invoke('connect')
    assert result['reception'] == 'callback_stale' and result['callback_age_seconds'] >= 120 and 'stale' in result['next_action']


def test_connect_callback_probe_is_once_and_does_not_consume_peer_work(connection_client):
    session = connection_client
    session.invoke('connect')
    session.messages.append({'id': '2-0', 'session_id': 'peer', 'conversation_id': 'main', 'text': 'new peer work'})
    observed = session.invoke('hook', stdin={'session_id': 'native-self', 'hook_event_id': 'probe:1'})
    before_delivery = exchange.read_state(exchange.owned_state(session.directory, 'native-self'))
    delivered = session.invoke('hook', stdin={'session_id': 'native-self', 'hook_event_id': 'delivery:1'})
    assert ('Connection callback observed' in observed['reason'] and before_delivery['pending'] is None
            and 'new peer work' in delivered['reason']
            and exchange.read_state(exchange.owned_state(session.directory, 'native-self'))['pending'] == '2-0'
            and any(row['state'] == 'waiting' and row['ttl_seconds'] == 90 for row in session.presence))


def test_peers_lists_service_announcements_and_close_retires_last_topic(connection_client):
    session = connection_client
    session.invoke('connect')
    peers = session.invoke('peers')
    session.invoke('close')
    assert (peers['sessions'][0]['client'] == 'codex' and session.requests[-1].url.path == '/sessions'
            and session.presence[-1]['state'] == 'closed' and session.presence[-1]['ttl_seconds'] == 1)
