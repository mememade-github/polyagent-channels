"""Shared-key file handling and transport authentication."""
import hmac
import os
import secrets
from pathlib import Path

from redis.exceptions import RedisError
from starlette.responses import JSONResponse

from channels.identity import authenticate, current_agent


def load_key():
    path = Path(os.environ.get('CHANNEL_KEY_FILE', '.env/channel.key'))
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(descriptor, 'w') as key_file:
            key_file.write(secrets.token_urlsafe(48) + '\n')
    path.chmod(0o600)
    key = path.read_text().strip()
    if not key:
        raise RuntimeError('Admin key file is empty')
    return key


class CredentialAuth:
    def __init__(self, application, key):
        self.application = application
        self.authorization = ('Bearer ' + key).encode()

    async def __call__(self, scope, receive, send):
        if scope['type'] not in ('http', 'websocket'):
            return await self.application(scope, receive, send)
        if scope['type'] == 'http' and scope['path'] in ('/help', '/health'):
            return await self.application(scope, receive, send)
        supplied = dict(scope.get('headers', [])).get(b'authorization', b'')
        if scope['type'] == 'http' and scope['path'] == '/agents':
            if hmac.compare_digest(supplied, self.authorization):
                return await self.application(scope, receive, send)
            return await self.reject(scope, receive, send)
        try:
            credential = supplied.removeprefix(b'Bearer ').decode('ascii') if supplied.startswith(b'Bearer ') else ''
            identity = await authenticate(credential)
        except UnicodeError:
            identity = None
        except RedisError:
            return await self.reject(scope, receive, send, 503)
        if identity is None:
            return await self.reject(scope, receive, send)
        token = current_agent.set(identity)
        try:
            return await self.application(scope, receive, send)
        finally:
            current_agent.reset(token)

    async def reject(self, scope, receive, send, status=401):
        if scope['type'] == 'websocket':
            await send({'type': 'websocket.close', 'code': 1008})
            return
        error = 'Redis unavailable' if status == 503 else 'Unauthorized'
        await JSONResponse({'error': error}, status_code=status)(scope, receive, send)
