"""Persistent logical client identities; credentials are stored only as digests."""
import hashlib
import secrets
from contextvars import ContextVar

from channels import persistence

current_agent = ContextVar('current_agent', default=None)


def validate_name(name):
    if not isinstance(name, str) or not name.strip() or len(name.encode('utf-8')) > 256:
        raise ValueError('display_name must be nonempty and <=256 UTF-8 bytes')


def credential_key(credential):
    return 'agent:credential:' + hashlib.sha256(credential.encode()).hexdigest()


async def register(display_name):
    validate_name(display_name)
    agent_id = secrets.token_hex(24)
    credential = secrets.token_urlsafe(48)
    identity = {'agent_id': agent_id, 'sender': display_name}
    await persistence.redis.hset(credential_key(credential), mapping=identity)
    return {**identity, 'credential': credential}


async def authenticate(credential):
    if not credential:
        return None
    identity = await persistence.redis.hgetall(credential_key(credential))
    return identity if identity.get('agent_id') and identity.get('sender') else None


def authenticated_agent():
    identity = current_agent.get()
    if identity is None:
        raise ValueError('Individual credential required')
    return identity
