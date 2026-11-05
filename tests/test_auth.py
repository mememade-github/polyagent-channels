import stat

import pytest

from channels.auth import load_key


def test_first_start_creates_private_key(monkeypatch, tmp_path):
    key_path = tmp_path / '.env/channel.key'
    monkeypatch.setenv('CHANNEL_KEY_FILE', str(key_path))
    key = load_key()
    assert (bool(key), stat.S_IMODE(key_path.stat().st_mode), stat.S_IMODE(key_path.parent.stat().st_mode)) == (True, 0o600, 0o700)


def test_admin_key_is_preserved(monkeypatch, tmp_path):
    key_path = tmp_path / 'channel.key'
    key_path.write_text('synthetic-admin-key\n')
    monkeypatch.setenv('CHANNEL_KEY_FILE', str(key_path))
    assert load_key() == 'synthetic-admin-key'


def test_empty_key_fails_startup(monkeypatch, tmp_path):
    key_path = tmp_path / 'channel.key'
    key_path.touch()
    monkeypatch.setenv('CHANNEL_KEY_FILE', str(key_path))
    with pytest.raises(RuntimeError, match='empty'):
        load_key()


@pytest.mark.parametrize('scope_type', ['http', 'websocket'])
@pytest.mark.parametrize('authorizations', [[], [b'Bearer wrong'],
                                          [b'Bearer shared', b'Bearer shared'],
                                          [b'Bearer wrong', b'Bearer shared']])
def test_protected_transports_reject_missing_wrong_or_duplicate_bearer(scope_type, authorizations):
    import asyncio
    from unittest.mock import AsyncMock

    from channels.auth import CredentialAuth

    application, send = AsyncMock(), AsyncMock()
    scope = {'type': scope_type, 'method': 'GET', 'path': '/status',
             'headers': [(b'authorization', value) for value in authorizations]}
    asyncio.run(CredentialAuth(application, 'shared', 32)(scope, AsyncMock(), send))
    assert application.await_count == 0


def test_shared_key_rotation_invalidates_previous_key(server_module):
    from starlette.testclient import TestClient

    from channels.auth import CredentialAuth

    with TestClient(CredentialAuth(server_module.inner, 'replacement', server_module.MAX_BODY)) as client:
        previous = client.get('/status', headers={'Authorization': 'Bearer synthetic-admin-key'})
        replacement = client.get('/status', headers={'Authorization': 'Bearer replacement'})
    assert (previous.status_code, replacement.status_code, replacement.json()) == (401, 200, {'status': 'ready'})
