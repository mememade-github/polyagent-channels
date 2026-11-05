"""Publish, read and listen to channels without launching or controlling agents."""
import argparse
import getpass
import asyncio
import contextlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlencode

import httpx
from websockets.asyncio.client import connect

from polyagent_channels import exchange
from polyagent_channels.client_config import ConfigError, apply_config


def command_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config')
    parser.add_argument('--client', choices=('claude', 'codex'))
    parser.add_argument('--session-id')
    parser.add_argument('--url', default=os.getenv('CHANNEL_URL', 'http://127.0.0.1:18765'))
    parser.add_argument('--key-file', default=os.getenv('CHANNEL_KEY_FILE'))
    parser.add_argument('--state-dir', default=os.getenv('CHANNEL_STATE_DIR'))
    commands = parser.add_subparsers(dest='command', required=True)
    key = commands.add_parser('key', help='Save the shared key from a protected file or hidden prompt')
    key.add_argument('--source-file')
    key.add_argument('--replace-key', action='store_true')
    publisher = commands.add_parser('publish', help='Publish text, --file PATH or --file - for stdin')
    publisher.add_argument('channel', nargs='?', default='general')
    publisher.add_argument('--conversation-id', default='main')
    source = publisher.add_mutually_exclusive_group(required=True)
    source.add_argument('--text')
    source.add_argument('--file')
    publisher.add_argument('--reply-to')
    publisher.add_argument('--sender')
    for name in ('receive', 'listen'):
        reader = commands.add_parser(name, help='Read after cursor' if name == 'receive' else 'Active WebSocket notifications')
        reader.add_argument('channel', nargs='?', default='general')
        reader.add_argument('--conversation-id', help='Omit to read all conversations')
        reader.add_argument('--cursor', default='0-0')
        reader.add_argument('--timeout', type=int, default=30, help='seconds; listen 0 means unlimited')
    commands.add_parser('status', help='Probe shared-key access and this session callback status')
    add_exchange_commands(commands)
    return parser


def add_exchange_commands(commands):
    for name in ('wait', 'open', 'close', 'ack'):
        command = commands.add_parser(name, help={
            'wait': 'Wait for one other-client message',
            'open': 'Open one bounded cooperation exchange for this logical client',
            'close': 'Close only the matching owned exchange',
            'ack': 'Mark a pending message processed for this topic',
        }[name])
        command.add_argument('channel', nargs='?', default='general')
        command.add_argument('--conversation-id', default='main')
        if name not in ('close', 'ack'):
            command.add_argument('--cursor', default='0-0')
        if name == 'ack':
            command.add_argument('--message-id', required=True)
        if name == 'wait':
            command.add_argument('--timeout', type=exchange.seconds, default=0, help='total seconds; 0 means unlimited')
        if name == 'open':
            command.add_argument('--timeout', type=exchange.positive_seconds, default=exchange.EXCHANGE_TIMEOUT,
                                 help='overall exchange seconds; default 600, must be positive')
    hook = commands.add_parser('hook', help='Read Stop hook JSON on stdin; act only on this client\'s open exchange')
    hook.add_argument('--timeout', type=exchange.positive_seconds, default=exchange.HOOK_TIMEOUT,
                      help='total wait seconds per callback; default 600, bounded by exchange deadline')


def publish_payload(options):
    text = options.text
    if options.file == '-':
        text = sys.stdin.read()
    if options.file and options.file != '-':
        text = Path(options.file).read_text(encoding='utf-8')
    sender = options.sender if options.sender is not None else options.session_id
    if not sender.strip() or len(sender.encode('utf-8')) > 256:
        raise ValueError('sender_invalid')
    return {'session_id': options.session_id, 'sender': sender, 'text': text, 'reply_to': options.reply_to, 'conversation_id': options.conversation_id}


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
    with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
        await asyncio.wait_for(listen(channel_url, headers, options.cursor, options.conversation_id), options.timeout or None)


def read_secret(path):
    path = Path(path)
    try:
        if path.is_symlink() or path.parent.is_symlink() or path.parent.stat().st_mode & 0o077 or path.stat().st_mode & 0o077 or not path.is_file():
            raise ValueError('key_permissions')
        secret = path.read_text(encoding='utf-8').strip()
    except FileNotFoundError:
        raise ValueError('key_missing: ' + str(path)) from None
    except OSError:
        raise ValueError('key_unreadable') from None
    if not secret or any(character.isspace() for character in secret):
        raise ValueError('key_invalid')
    return secret


def save_key(options):
    secret = read_secret(options.source_file) if options.source_file else getpass.getpass('Shared channel key: ').strip()
    if not secret or any(character.isspace() for character in secret):
        raise ValueError('key_invalid')
    path = Path(options.key_file)
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError('key_permissions')
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.parent.stat().st_mode & 0o077:
        raise ValueError('key_permissions')
    if not options.replace_key:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, 'w') as output:
            output.write(secret + '\n')
    else:
        descriptor, temporary = tempfile.mkstemp(prefix='.channel-key-', dir=path.parent)
        try:
            with os.fdopen(descriptor, 'w') as output:
                output.write(secret + '\n')
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)
    print(json.dumps({'status': 'key_saved', 'key_path': str(path)}))


def select_session(options, required=True):
    if options.session_id is not None:
        return exchange.validate_session_id(options.session_id)
    names = {'codex': 'CODEX_THREAD_ID', 'claude': 'CLAUDE_CODE_SESSION_ID'}
    values = [os.environ[name] for client, name in names.items()
              if (options.client is None or options.client == client) and os.environ.get(name)]
    if len(values) > 1 and not required:
        return None
    if len(values) > 1:
        raise ValueError('session_ambiguous: select --client or --session-id')
    if values:
        return exchange.validate_session_id(values[0])
    if required:
        raise ValueError('session_missing: use --client with its native environment or --session-id')
    return None


def exchange_target(options):
    target = {'channel': options.channel, 'conversation_id': options.conversation_id}
    exchange.validate_identifier(target['channel'])
    exchange.validate_identifier(target['conversation_id'])
    if options.command not in ('close', 'ack'):
        exchange.validate_cursor(options.cursor)
        target['cursor'] = options.cursor
    return target


def exchange_client(options, key):
    return httpx.AsyncClient(base_url=options.url.rstrip('/') + '/', timeout=70,
                             headers={'Authorization': 'Bearer ' + key})


async def run_exchange(options, key):
    target = {**exchange_target(options), 'session_id': options.session_id}
    async with exchange_client(options, key) as client:
        if options.command == 'wait':
            result = await exchange.wait_for_peer(client, target, options.timeout)
            print(json.dumps(result, ensure_ascii=False))
            return 2 if result.get('type') == 'timeout' else 0
        response = await client.get('status')
        response.raise_for_status()
    if options.command == 'open':
        state = exchange.new_state(options.session_id, target, options.timeout)
        state = exchange.open_exchange(options.state_dir, options.session_id, state)
        print(json.dumps({'type': 'open', **state}))
        return 0
    if options.command == 'ack':
        exchange.ack_exchange(options.state_dir, options.session_id, target, options.message_id)
        print(json.dumps({'type': 'acknowledged', **target, 'message_id': options.message_id}))
        return 0
    exchange.close_exchange(options.state_dir, options.session_id, target)
    print(json.dumps({'type': 'closed', **target}))
    return 0


def record_hook(options, state, callback_id=None):
    if not options.session_id:
        return
    directory = exchange.session_directory(options.state_dir, options.session_id)
    if directory.is_symlink() or Path(options.state_dir).is_symlink():
        raise ValueError('state_invalid')
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    diagnostic = {'timestamp': time.time(), 'state': state, 'session_id': options.session_id,
                  'callback_id': callback_id}
    exchange.write_state(directory / 'last-hook.json', diagnostic)


def run_hook(options):
    decision = {}
    callback_id = None
    diagnostic = 'absent'
    options.session_id = None
    path = None
    state = None
    try:
        event = json.load(sys.stdin)
        if not isinstance(event, dict):
            raise ValueError('hook_input_invalid')
        options.session_id = exchange.validate_session_id(event.get('session_id'))
        candidate = event.get('hook_event_id', event.get('hook_id', event.get('id')))
        if candidate is not None:
            try:
                callback_id = exchange.validate_session_id(candidate)
            except ValueError:
                pass
        if event.get('hook_event_name', 'Stop') != 'Stop':
            print('{}')
            return
        paths = exchange.list_states(options.state_dir, options.session_id)
        live = []
        for path in paths:
            state = exchange.read_state(path)
            if state['deadline'] <= time.time():
                continue
            # Native Claude marks Stop-hook continuations; pending is still work,
            # but replay belongs to the next initial Stop, not this continuation.
            if event.get('stop_hook_active') is True and state['pending'] is not None:
                continue
            live.append(path)
        diagnostic = 'expired' if paths else 'absent'
        if live:
            key = read_secret(options.key_file)
            async def wait():
                async with exchange_client(options, key) as client:
                    return await exchange.hook_topics(client, live, options.timeout)
            decision = asyncio.run(wait())
            diagnostic = 'peer' if 'Untrusted peer JSON' in decision.get('reason', '') else 'expired'
            if any(path.exists() and exchange.read_state(path)['errors'] for path in live):
                diagnostic = 'error'
    except Exception:
        diagnostic = 'error'
        print('channel hook failed: check key, session input, state and service', file=sys.stderr)
        if path is not None and state is not None:
            decision = exchange.hook_error(path, state)
    try:
        record_hook(options, diagnostic, callback_id)
    except Exception:
        print('channel hook diagnostic unavailable', file=sys.stderr)
    print(json.dumps(decision, ensure_ascii=False))


def run_hook_config_error(options, code):
    from polyagent_channels.client_config import DEFAULT_ROOT
    root = Path(options.config).expanduser().absolute().parent if options.config else DEFAULT_ROOT
    options.state_dir = options.state_dir or str(root / 'state')
    options.session_id = None
    try:
        event = json.load(sys.stdin)
        options.session_id = exchange.validate_session_id(event.get('session_id'))
        if Path(options.config).is_file():
            record_hook(options, 'config_error')
    except Exception:
        pass
    print('{}')
    print('channel hook configuration failed: ' + code, file=sys.stderr)


def client_status(options, key):
    with httpx.Client(timeout=10) as client:
        response = client.get(options.url.rstrip('/') + '/status', headers={'Authorization': 'Bearer ' + key})
        response.raise_for_status()
        if response.json() != {'status': 'ready'}:
            raise ValueError('status_invalid')
    session_status = 'selected' if options.session_id else 'session_missing'
    if options.session_id is None and options.client is None and all(os.environ.get(name) for name in ('CODEX_THREAD_ID', 'CLAUDE_CODE_SESSION_ID')):
        session_status = 'session_ambiguous'
    result = {'status': 'ready', 'session_id': options.session_id,
              'session': session_status,
              'key_path': options.key_file, 'exchange': 'absent', 'last_callback': None}
    if options.session_id:
        directory = exchange.session_directory(options.state_dir, options.session_id)
        result['state_path'] = str(directory)
        topics = []
        for path in exchange.list_states(options.state_dir, options.session_id):
            state = exchange.read_state(path)
            topics.append({**state, 'state': 'expired' if state['deadline'] <= time.time() else 'live',
                           'processing': 'pending' if state['pending'] else 'idle'})
        result['topics'] = topics
        if topics:
            result['exchange'] = 'live' if any(topic['state'] == 'live' for topic in topics) else 'expired'
        diagnostic = directory / 'last-hook.json'
        if diagnostic.exists() and not diagnostic.is_symlink():
            data = json.loads(diagnostic.read_text())
            if set(data) == {'timestamp', 'state', 'session_id', 'callback_id'} and data['session_id'] == options.session_id:
                result['last_callback'] = data
    print(json.dumps(result))


def run(options):
    if options.command == 'key':
        save_key(options)
        return
    if options.command == 'hook':
        run_hook(options)
        return
    options.session_id = select_session(options, required=options.command in ('publish', 'open', 'close', 'wait', 'ack'))
    key = read_secret(options.key_file)
    if options.command == 'status':
        return client_status(options, key)
    if options.command in ('wait', 'open', 'close', 'ack'):
        return asyncio.run(run_exchange(options, key))
    headers = {'Authorization': 'Bearer ' + key}
    channel_url = options.url.rstrip('/') + '/channels/' + options.channel
    if options.command == 'listen':
        asyncio.run(listen_until_timeout(options, channel_url, headers))
        return
    response = request_messages(options, channel_url, headers)
    if response.is_error:
        raise ValueError('unauthorized401' if response.status_code == 401 else f'HTTP {response.status_code}')
    print(json.dumps(response.json(), ensure_ascii=False))


def main():
    options = command_parser().parse_args()
    try:
        from polyagent_channels.setup import lifecycle_lock
        from polyagent_channels.client_config import DEFAULT_ROOT
        root = DEFAULT_ROOT
        if options.state_dir:
            root = Path(options.state_dir).expanduser().absolute().parent
        if options.config:
            root = Path(options.config).expanduser().absolute().parent
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock = lifecycle_lock(root)
        with lock:
            try:
                apply_config(options, sys.argv[1:])
            except ConfigError as error:
                if options.command != 'hook':
                    raise
                run_hook_config_error(options, error.code)
                return
            status = run(options)
            if status:
                sys.exit(status)
    except ConfigError as error:
        print('channel configuration failed: ' + error.code, file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        pass
    except httpx.HTTPStatusError as error:
        print('channel client failed: ' + ('unauthorized401' if error.response.status_code == 401 else 'HTTP ' + str(error.response.status_code)), file=sys.stderr)
        sys.exit(1)
    except httpx.TransportError:
        print('channel client failed: service_unreachable', file=sys.stderr)
        sys.exit(1)
    except ValueError as error:
        safe = str(error)
        allowed = ('key_', 'session_', 'sender_', 'status_', 'unauthorized401', 'HTTP ')
        print('channel client failed: ' + (safe if safe.startswith(allowed) else 'invalid_input_or_state'), file=sys.stderr)
        sys.exit(1)
    except Exception:
        print('channel client failed: check key permissions, input, state and service', file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
