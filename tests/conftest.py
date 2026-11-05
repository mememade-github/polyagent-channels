import importlib
from unittest.mock import AsyncMock

import pytest
from starlette.testclient import TestClient

from channels import persistence
from channels.auth import CredentialAuth
from channels.identity import credential_key, current_agent


@pytest.fixture(autouse=True)
def logical_identity():
    token = current_agent.set({'agent_id': 'synthetic-agent', 'sender': 'writer'})
    yield
    current_agent.reset(token)


@pytest.fixture
def storage(monkeypatch):
    # Existing transport fixtures use synthetic stream IDs, without wall-clock expiration.
    monkeypatch.setattr(persistence, 'RETENTION_DAYS', 0)
    connection = AsyncMock()
    connection.eval.return_value = ['ok', '1-0']
    connection.xread.return_value = []
    connection.xrange.return_value = [('0-1', {'conversation_id': 'main'})]
    identities = {credential_key('synthetic-client-key'): {'agent_id': 'synthetic-agent', 'sender': 'writer'}}

    async def hset(key, mapping):
        identities[key] = dict(mapping)

    async def hgetall(key):
        return identities.get(key, {})

    connection.hset.side_effect = hset
    connection.hgetall.side_effect = hgetall
    monkeypatch.setattr(persistence, 'redis', connection)
    return connection


@pytest.fixture
def server_module(monkeypatch, tmp_path, storage):
    artifact = tmp_path / 'harness.pyz'
    artifact.write_bytes(b'synthetic-built-artifact')
    monkeypatch.setenv('CHANNEL_HARNESS_FILE', str(artifact))
    monkeypatch.setenv('CHANNEL_KEY_FILE', str(tmp_path / '.env/channel.key'))
    server = importlib.reload(importlib.import_module('channels.server'))
    monkeypatch.setattr(server, 'redis', storage)
    return server


@pytest.fixture
def client(server_module):
    with TestClient(CredentialAuth(server_module.inner, 'synthetic-admin-key', server_module.MAX_BODY)) as client:
        yield client


@pytest.fixture
def headers():
    return {'Authorization': 'Bearer synthetic-client-key'}
