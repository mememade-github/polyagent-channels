import asyncio
import io
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from channels import cli


def test_parser_accepts_bounded_read():
    options = cli.command_parser().parse_args(['receive', 'general', '--cursor', '2-0', '--timeout', '5'])
    assert (options.command, options.cursor, options.timeout) == ('receive', '2-0', 5)


def test_publish_reads_unicode_file(tmp_path):
    message_file = tmp_path / 'message.txt'
    message_file.write_text('hello🙂', encoding='utf-8')
    options = SimpleNamespace(text=None, file=str(message_file), conversation_id='main', reply_to=None)
    assert cli.publish_payload(options)['text'] == 'hello🙂'


def test_publish_reads_standard_input(monkeypatch):
    monkeypatch.setattr('sys.stdin', io.StringIO('stdin message'))
    options = SimpleNamespace(text=None, file='-', conversation_id='main', reply_to=None)
    assert cli.publish_payload(options)['text'] == 'stdin message'


def test_run_prints_publish_result(monkeypatch, tmp_path, capsys):
    key_file = tmp_path / 'key'
    key_file.write_text('synthetic-client-key')
    key_file.chmod(0o600)
    monkeypatch.setattr(cli, 'request_messages', lambda *parameters: httpx.Response(200, json={'id': '1-0'}))
    options = SimpleNamespace(credential_file=str(key_file), url='http://localhost', channel='general', command='publish')
    cli.run(options)
    assert json.loads(capsys.readouterr().out) == {'id': '1-0'}


def test_run_rejects_empty_key(tmp_path):
    key_file = tmp_path / 'key'
    key_file.touch(mode=0o600)
    with pytest.raises(ValueError, match='empty'):
        cli.run(SimpleNamespace(command='receive', credential_file=str(key_file)))


def test_run_exposes_http_error(monkeypatch, tmp_path):
    key_file = tmp_path / 'key'
    key_file.write_text('synthetic-client-key')
    key_file.chmod(0o600)
    monkeypatch.setattr(cli, 'request_messages', lambda *parameters: httpx.Response(503, json={'error': 'Redis unavailable'}))
    options = SimpleNamespace(credential_file=str(key_file), url='http://localhost', channel='general', command='receive')
    with pytest.raises(ValueError, match='HTTP 503'):
        cli.run(options)


def test_main_exits_with_error_for_missing_key(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr('sys.argv', ['channels', '--credential-file', str(tmp_path / 'absent'), 'receive', 'general'])
    with pytest.raises(SystemExit) as failure:
        cli.main()
    assert failure.value.code == 1 and 'FileNotFoundError' in capsys.readouterr().err


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
    options = SimpleNamespace(command=command, cursor='2-0', timeout=5, text='hello', file=None, conversation_id='main', reply_to=None)
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


def test_registration_saves_secret_privately_without_printing_or_overwriting(monkeypatch, tmp_path, capsys):
    administrator_file = tmp_path / '.env/admin.key'
    administrator_file.parent.mkdir(mode=0o700)
    administrator_file.write_text('synthetic-administrator')
    administrator_file.chmod(0o600)
    credential_file = administrator_file.parent / 'client.key'
    original_client = httpx.Client

    def respond(request):
        assert request.headers['Authorization'] == 'Bearer synthetic-administrator'
        assert json.loads(request.content) == {'display_name': 'client'}
        return httpx.Response(201, json={'agent_id': 'issued-id', 'sender': 'client', 'credential': 'synthetic-issued-secret'})

    monkeypatch.setattr(cli.httpx, 'Client', lambda **settings: original_client(transport=httpx.MockTransport(respond), **settings))
    options = SimpleNamespace(command='register', key_file=administrator_file, credential_file=credential_file,
                              name='client', url='http://localhost')
    cli.run(options)
    assert credential_file.read_text().strip() == 'synthetic-issued-secret'
    assert credential_file.stat().st_mode & 0o777 == 0o600
    assert credential_file.parent.stat().st_mode & 0o777 == 0o700
    assert 'synthetic-issued-secret' not in capsys.readouterr().out
    with pytest.raises(FileExistsError):
        cli.run(options)
    assert credential_file.read_text().strip() == 'synthetic-issued-secret'


def test_outer_cli_boundary_redacts_exception_values(monkeypatch, capsys):
    monkeypatch.setattr('sys.argv', ['channels', 'receive', 'general'])
    monkeypatch.setattr(cli, 'run', lambda options: (_ for _ in ()).throw(ValueError('synthetic-secret-must-not-print')))
    with pytest.raises(SystemExit):
        cli.main()
    assert 'synthetic-secret-must-not-print' not in capsys.readouterr().err


def test_registration_failure_removes_only_new_file(monkeypatch, tmp_path):
    administrator_file = tmp_path / '.env/admin.key'
    administrator_file.parent.mkdir(mode=0o700)
    administrator_file.write_text('synthetic-admin')
    administrator_file.chmod(0o600)
    destination = administrator_file.parent / 'new-client.key'
    original_client = httpx.Client
    monkeypatch.setattr(cli.httpx, 'Client', lambda **settings: original_client(
        transport=httpx.MockTransport(lambda request: httpx.Response(401)), **settings))
    options = SimpleNamespace(key_file=administrator_file, credential_file=destination, name='client', url='http://localhost')
    with pytest.raises(ValueError, match='HTTP 401'):
        cli.register_client(options)
    assert not destination.exists() and administrator_file.read_text() == 'synthetic-admin'


def test_secret_reader_rejects_public_permissions(tmp_path):
    path = tmp_path / 'key'
    path.write_text('synthetic-secret')
    path.chmod(0o644)
    with pytest.raises(ValueError, match='permissions'):
        cli.read_secret(path)
