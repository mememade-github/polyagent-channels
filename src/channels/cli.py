"""Publish, read and listen to channels without launching or controlling agents."""
import argparse
import asyncio
import contextlib
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlencode

import httpx
from websockets.asyncio.client import connect


def command_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default=os.getenv('CHANNEL_URL', 'http://127.0.0.1:18765'))
    parser.add_argument('--key-file', default=os.getenv('CHANNEL_KEY_FILE', '.env/channel.key'), help='Protected administrator key; registration only')
    parser.add_argument('--credential-file', default=os.getenv('CHANNEL_CREDENTIAL_FILE', '.env/agent.key'))
    commands = parser.add_subparsers(dest='command', required=True)
    registration = commands.add_parser('register', help='Register with administrator key; save a new private credential file')
    registration.add_argument('--name', required=True)
    publisher = commands.add_parser('publish', help='Publish text, --file PATH or --file - for stdin')
    publisher.add_argument('channel')
    publisher.add_argument('--conversation-id', default='main')
    source = publisher.add_mutually_exclusive_group(required=True)
    source.add_argument('--text')
    source.add_argument('--file')
    publisher.add_argument('--reply-to')
    for name in ('receive', 'listen'):
        reader = commands.add_parser(name, help='Read after cursor' if name == 'receive' else 'Active WebSocket notifications')
        reader.add_argument('channel')
        reader.add_argument('--conversation-id', help='Omit to read all conversations')
        reader.add_argument('--cursor', default='0-0')
        reader.add_argument('--timeout', type=int, default=30, help='seconds; listen 0 means unlimited')
    return parser


def publish_payload(options):
    text = options.text
    if options.file == '-':
        text = sys.stdin.read()
    if options.file and options.file != '-':
        text = Path(options.file).read_text(encoding='utf-8')
    return {'text': text, 'reply_to': options.reply_to, 'conversation_id': options.conversation_id}


def request_messages(options, channel_url, headers):
    with httpx.Client(timeout=70) as client:
        if options.command == 'publish':
            return client.post(channel_url + '/messages', headers=headers, json=publish_payload(options))
        return client.get(channel_url + '/messages', headers=headers,
                          params={key: value for key, value in {'cursor': options.cursor, 'timeout': options.timeout,
                                  'conversation_id': options.conversation_id}.items() if value is not None})


async def listen(channel_url, headers, cursor, conversation_id=None):
    websocket_url = channel_url.replace('http:', 'ws:', 1).replace('https:', 'wss:', 1)
    websocket_url += '/subscribe?' + urlencode({key: value for key, value in {'cursor': cursor, 'conversation_id': conversation_id}.items() if value is not None})
    async with connect(websocket_url, additional_headers=headers, max_size=16 * 1024 * 1024) as websocket:
        async for payload in websocket:
            event = json.loads(payload)
            if 'error' in event:
                raise ValueError(event['error'])
            if event.get('type') != 'heartbeat':
                print(json.dumps(event, ensure_ascii=False), flush=True)


async def listen_until_timeout(options, channel_url, headers):
    if options.timeout < 0:
        raise ValueError('timeout must be nonnegative')
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(listen(channel_url, headers, options.cursor, options.conversation_id), options.timeout or None)


def read_secret(path):
    path = Path(path)
    if path.is_symlink() or path.parent.stat().st_mode & 0o077 or path.stat().st_mode & 0o077:
        raise ValueError('Secret directory/file permissions must be 0700/0600')
    secret = path.read_text().strip()
    if not secret:
        raise ValueError('Credential file is empty')
    return secret


def register_client(options):
    administrator_key = read_secret(options.key_file)
    path = Path(options.credential_file)
    if '.env' not in path.parts or '.env' not in path.resolve().parts:
        raise ValueError('Credential file must be inside an ignored .env directory')
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, 'w') as credential_file, httpx.Client(timeout=70) as client:
            response = client.post(options.url.rstrip('/') + '/agents',
                                   headers={'Authorization': 'Bearer ' + administrator_key},
                                   json={'display_name': options.name})
            if response.is_error:
                raise ValueError(f'Registration failed: HTTP {response.status_code}')
            identity = response.json()
            credential_file.write(identity['credential'] + '\n')
        print(json.dumps({'agent_id': identity['agent_id'], 'sender': identity['sender']}))
    except Exception:
        path.unlink(missing_ok=True)
        raise


def run(options):
    if options.command == 'register':
        register_client(options)
        return
    credential = read_secret(options.credential_file)
    headers = {'Authorization': 'Bearer ' + credential}
    channel_url = options.url.rstrip('/') + '/channels/' + options.channel
    if options.command == 'listen':
        asyncio.run(listen_until_timeout(options, channel_url, headers))
        return
    response = request_messages(options, channel_url, headers)
    if response.is_error:
        raise ValueError(f'HTTP {response.status_code}')
    print(json.dumps(response.json(), ensure_ascii=False))


def main():
    options = command_parser().parse_args()
    try:
        run(options)
    except KeyboardInterrupt:
        pass
    # Outer CLI boundary deliberately omits exception text: transports can include secrets.
    except Exception as error:  # noqa: BLE001 — redact all transport exception text at CLI boundary
        print(f'channel client failed: {type(error).__name__} (check credentials, permissions, input and service)', file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
