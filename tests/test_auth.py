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
