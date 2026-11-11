import contextlib
import stat

import pytest

from polyagent_channels.auth import load_key


def test_first_start_creates_private_key(monkeypatch, tmp_path):
    key_path = tmp_path / '.env/channel.key'
    monkeypatch.setenv('CHANNEL_KEY_FILE', str(key_path))
    key = load_key()
    assert (bool(key), stat.S_IMODE(key_path.stat().st_mode), stat.S_IMODE(key_path.parent.stat().st_mode)) == (True, 0o600, 0o700)


def private_key_file(tmp_path, content):
    tmp_path.chmod(0o700)
    key_path = tmp_path / 'channel.key'
    key_path.write_text(content)
    return key_path


def test_admin_key_is_preserved(monkeypatch, tmp_path):
    monkeypatch.setenv('CHANNEL_KEY_FILE', str(private_key_file(tmp_path, 'synthetic-admin-key-0123456789abcdef\n')))
    assert load_key() == 'synthetic-admin-key-0123456789abcdef'


@pytest.mark.parametrize('content', ['synthetic two words in a key of length\n', 'synthetic-short-key\n'])
def test_key_no_client_accepts_fails_startup(monkeypatch, tmp_path, content):
    monkeypatch.setenv('CHANNEL_KEY_FILE', str(private_key_file(tmp_path, content)))
    with pytest.raises(RuntimeError, match='at least 32 characters with no whitespace'):
        load_key()


def open_key_directory(monkeypatch, tmp_path):
    shared = tmp_path / 'shared'
    shared.mkdir()
    shared.chmod(0o755)
    monkeypatch.setenv('CHANNEL_KEY_FILE', str(shared / 'channel.key'))
    return shared


def test_existing_open_directory_fails_startup(monkeypatch, tmp_path):
    open_key_directory(monkeypatch, tmp_path)
    with pytest.raises(RuntimeError, match='must have mode 0700'):
        load_key()


def test_existing_open_directory_keeps_its_mode(monkeypatch, tmp_path):
    shared = open_key_directory(monkeypatch, tmp_path)
    with contextlib.suppress(RuntimeError):
        load_key()
    assert stat.S_IMODE(shared.stat().st_mode) == 0o755


def test_empty_key_fails_startup(monkeypatch, tmp_path):
    monkeypatch.setenv('CHANNEL_KEY_FILE', str(private_key_file(tmp_path, '')))
    with pytest.raises(RuntimeError, match='empty'):
        load_key()


@pytest.mark.parametrize('scope_type', ['http', 'websocket'])
@pytest.mark.parametrize('authorizations', [[], [b'Bearer wrong'],
                                          [b'Bearer shared', b'Bearer shared'],
                                          [b'Bearer wrong', b'Bearer shared']])
def test_protected_transports_reject_missing_wrong_or_duplicate_bearer(scope_type, authorizations):
    import asyncio
    from unittest.mock import AsyncMock

    from polyagent_channels.auth import CredentialAuth

    application, send = AsyncMock(), AsyncMock()
    scope = {'type': scope_type, 'method': 'GET', 'path': '/status',
             'headers': [(b'authorization', value) for value in authorizations]}
    asyncio.run(CredentialAuth(application, 'shared', 32)(scope, AsyncMock(), send))
    assert application.await_count == 0


def test_shared_key_rotation_invalidates_previous_key(server_module):
    from starlette.testclient import TestClient

    from polyagent_channels.auth import CredentialAuth

    with TestClient(CredentialAuth(server_module.inner, 'replacement', server_module.MAX_BODY)) as client:
        previous = client.get('/status', headers={'Authorization': 'Bearer synthetic-admin-key'})
        replacement = client.get('/status', headers={'Authorization': 'Bearer replacement'})
    assert (previous.status_code, replacement.status_code, replacement.json()) == (401, 200, {'status': 'ready'})
