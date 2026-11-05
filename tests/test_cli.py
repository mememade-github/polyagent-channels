import asyncio
import io
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from polyagent_channels import cli
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
    with pytest.raises(FileExistsError):
        cli.save_key(selected)
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
