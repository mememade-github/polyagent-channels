"""Shared-key file handling and transport authentication."""
import hmac
import os
import secrets
from pathlib import Path

from redis.exceptions import RedisError
from starlette.responses import JSONResponse

from channels.identity import authenticate, current_agent

ERRORS = {400: 'invalid Content-Length', 401: 'Unauthorized', 411: 'Content-Length required',
          413: 'request exceeds size limit', 503: 'Redis unavailable'}


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
    def __init__(self, application, key, max_body):
        self.application = application
        self.authorization = ('Bearer ' + key).encode()
        self.max_body = max_body

    async def __call__(self, scope, receive, send):
        if scope['type'] not in ('http', 'websocket'):
            return await self.application(scope, receive, send)
        if scope['type'] == 'http' and scope['path'] in ('/help', '/health'):
            return await self.application(scope, receive, send)
        body_status = self.mcp_body_status(scope)
        if body_status:
            return await self.reject(scope, receive, send, body_status)
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

    def mcp_body_status(self, scope):
        if (scope['type'], scope.get('method'), scope['path']) != ('http', 'POST', '/mcp'):
            return None
        values = [value for name, value in scope['headers'] if name == b'content-length']
        if any(name == b'transfer-encoding' for name, _ in scope['headers']) or not values:
            return 411
        if len(set(values)) != 1 or not values[0].isdigit() or len(values[0]) > 20:
            return 400
        if int(values[0]) > self.max_body:
            return 413
        return None

    async def reject(self, scope, receive, send, status=401):
        if scope['type'] == 'websocket':
            await send({'type': 'websocket.close', 'code': 1008})
            return
        await JSONResponse({'error': ERRORS[status]}, status_code=status)(scope, receive, send)
