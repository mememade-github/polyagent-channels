"""Shared-key file handling and transport authentication."""
import hmac
import os
import re
import secrets
from pathlib import Path

from starlette.responses import JSONResponse


ERRORS = {400: 'invalid Content-Length', 401: 'Unauthorized', 411: 'Content-Length required',
          413: 'request exceeds size limit', 503: 'Redis unavailable'}
KEY_MIN_LENGTH = 32
KEY_CHARACTERS = '[!-~]+'  # printable ASCII without space: clients send the key in an HTTP header


def prepare_key_directory(directory):
    """Create a private key directory; one that already exists must be private, since others may share it."""
    if not directory.exists():
        directory.mkdir(parents=True, mode=0o700)
    if directory.stat().st_mode & 0o077:
        raise RuntimeError(f'Shared key directory {directory} must have mode 0700')


def checked_key(key):
    """Refuse a key no client can send, rather than start with it."""
    if not key:
        raise RuntimeError('Shared key file is empty')
    if len(key) < KEY_MIN_LENGTH or not re.fullmatch(KEY_CHARACTERS, key):
        raise RuntimeError(f'Shared key must be at least {KEY_MIN_LENGTH} printable ASCII characters with no whitespace')
    return key


def load_key():
    path = Path(os.environ.get('CHANNEL_KEY_FILE', '.env/channel.key'))
    prepare_key_directory(path.parent)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(descriptor, 'w') as key_file:
            key_file.write(secrets.token_urlsafe(48) + '\n')
    path.chmod(0o600)
    return checked_key(path.read_text().strip())


class CredentialAuth:
    def __init__(self, application, key, max_body):
        self.application = application
        self.authorization = ('Bearer ' + key).encode()
        self.max_body = max_body

    async def __call__(self, scope, receive, send):
        if scope['type'] not in ('http', 'websocket'):
            return await self.application(scope, receive, send)
        if scope['type'] == 'http' and scope['path'] in ('/help', '/help/reference', '/health', '/harness', '/harness.sha256'):
            return await self.application(scope, receive, send)
        body_status = self.mcp_body_status(scope)
        if body_status:
            return await self.reject(scope, receive, send, body_status)
        authorizations = [value for name, value in scope.get('headers', []) if name == b'authorization']
        supplied = authorizations[0] if len(authorizations) == 1 else b''
        if not hmac.compare_digest(supplied, self.authorization):
            return await self.reject(scope, receive, send)
        return await self.application(scope, receive, send)

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
        headers = {'Cache-Control': 'no-store'}
        await JSONResponse({'error': ERRORS[status]}, status_code=status, headers=headers)(scope, receive, send)
