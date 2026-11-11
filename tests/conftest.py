import importlib
import os
import tempfile
from unittest.mock import AsyncMock

import pytest
from starlette.testclient import TestClient

# The service container exports settings for the live deployment, including the path of its shared key, and
# client sessions export their own IDs. The package reads some of them at import time, so clear them before
# any test module imports it; tests that need one set it themselves. REDIS_TEST_URL is the opt-in exception.
AMBIENT_VARIABLES = ('REDIS_URL', 'CLAUDE_PID', 'CLAUDE_PROJECT_DIR', 'CLAUDE_CODE_SESSION_ID', 'CODEX_THREAD_ID')
for name in [name for name in os.environ if name.startswith('CHANNEL_') or name in AMBIENT_VARIABLES]:
    del os.environ[name]
# The default client root is otherwise the real installation under HOME, which installers lock while they run.
CLIENT_ROOT = tempfile.TemporaryDirectory(prefix='polyagent-channels-tests-')
os.environ['CHANNEL_ROOT'] = CLIENT_ROOT.name


@pytest.fixture
def storage(monkeypatch):
    from polyagent_channels import persistence
    # Existing transport fixtures use synthetic stream IDs, without wall-clock expiration.
    monkeypatch.setattr(persistence, 'RETENTION_DAYS', 0)
    connection = AsyncMock()
    connection.eval.return_value = ['ok', '1-0']
    connection.xread.return_value = []
    connection.xrange.return_value = [('0-1', {'conversation_id': 'main'})]
    monkeypatch.setattr(persistence, 'redis', connection)
    return connection


@pytest.fixture
def server_module(monkeypatch, tmp_path, storage):
    artifact = tmp_path / 'harness.pyz'
    artifact.write_bytes(b'synthetic-built-artifact')
    monkeypatch.setenv('CHANNEL_HARNESS_FILE', str(artifact))
    monkeypatch.setenv('CHANNEL_KEY_FILE', str(tmp_path / '.env/channel.key'))
    server = importlib.reload(importlib.import_module('polyagent_channels.server'))
    monkeypatch.setattr(server, 'redis', storage)
    return server


@pytest.fixture
def client(server_module):
    from polyagent_channels.auth import CredentialAuth
    with TestClient(CredentialAuth(server_module.inner, 'synthetic-admin-key', server_module.MAX_BODY)) as client:
        yield client


@pytest.fixture
def headers():
    return {'Authorization': 'Bearer synthetic-admin-key'}
