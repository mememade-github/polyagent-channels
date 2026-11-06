"""Publish, read and listen to channels without launching or controlling agents."""
import argparse
import getpass
import asyncio
import contextlib
import json
import math
import os
import select
import shlex
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlencode

import httpx
from websockets.asyncio.client import connect

from polyagent_channels import exchange
from polyagent_channels.client_config import ConfigError, apply_config


class HookInputRequired(ValueError):
    """The callback was invoked without a complete native hook event."""


def read_hook_input():
    failure = 'native hook event JSON required; do not invoke manually'
    if sys.stdin.isatty():
        raise HookInputRequired(failure)
    try:
        descriptor = sys.stdin.fileno()
    except (AttributeError, OSError, ValueError):
        try:
            return json.load(sys.stdin)
        except (ValueError, TypeError):
            raise HookInputRequired(failure) from None
    deadline = time.monotonic() + 1
    content = bytearray()
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([descriptor], [], [], remaining)[0]:
            raise HookInputRequired(failure)
        chunk = os.read(descriptor, 65536)
        if not chunk:
            raise HookInputRequired(failure)
        content.extend(chunk)
        try:
            return json.loads(content)
        except (ValueError, UnicodeError):
            # A pipe may split a valid event across reads; the overall deadline remains bounded.
            continue


def command_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config')
    parser.add_argument('--client', choices=('claude', 'codex'))
    parser.add_argument('--session-id')
    parser.add_argument('--project', type=Path, default=Path.cwd(), help='Project whose native hook binding is inspected; default current directory')
    parser.add_argument('--url', default=os.getenv('CHANNEL_URL', 'http://127.0.0.1:18765'))
    parser.add_argument('--key-file', default=os.getenv('CHANNEL_KEY_FILE'))
    parser.add_argument('--state-dir', default=os.getenv('CHANNEL_STATE_DIR'))
    commands = parser.add_subparsers(dest='command', required=True)
    key = commands.add_parser('key', help='Save the shared key from stdin, a protected file or hidden prompt')
    key_source = key.add_mutually_exclusive_group()
    key_source.add_argument('--source-file')
    key_source.add_argument('--stdin', action='store_true', help='Read the shared key from process stdin until EOF')
    key.add_argument('--replace-key', action='store_true')
    connection = commands.add_parser('connect', help='Connect this session and open automatic reception for a topic')
    connection.add_argument('channel', nargs='?', default='general')
    connection.add_argument('--conversation-id', default='main')
    connection.add_argument('--name', help='Display name; asserted by this session, not verified identity')
    connection.add_argument('--timeout', type=exchange.positive_seconds, default=exchange.EXCHANGE_TIMEOUT)
    commands.add_parser('peers', help='List unexpired session announcements; these do not prove a peer is online')
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
        reader.add_argument('--conversation-id', default='main' if name == 'receive' else None)
        reader.add_argument('--all-conversations', action='store_const', const=None, dest='conversation_id',
                            help='Explicitly discover messages across conversations')
        reader.add_argument('--cursor', default=None if name == 'receive' else '0-0',
                            help='Explicit history cursor; 0-0 opts into all retained history')
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
            command.add_argument('--cursor', default=None if name == 'open' else '0-0',
                                 help='Open defaults to the fresh or saved cursor; 0-0 explicitly reads retained history')
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


def key_input(options):
    if options.source_file:
        return read_secret(options.source_file)
    if getattr(options, 'stdin', False):
        return sys.stdin.read().strip()
    return getpass.getpass('Shared channel key: ').strip()


def save_key(options, report=True):
    secret = key_input(options)
    if not secret or any(character.isspace() for character in secret):
        raise ValueError('key_invalid')
    path = Path(options.key_file)
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError('key_permissions')
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.parent.stat().st_mode & 0o077:
        raise ValueError('key_permissions')
    descriptor, temporary = tempfile.mkstemp(prefix='.channel-key-', dir=path.parent)
    status = 'key_saved'
    try:
        with os.fdopen(descriptor, 'w') as output:
            output.write(secret + '\n')
            output.flush()
            os.fsync(output.fileno())
        if options.replace_key:
            os.replace(temporary, path)
        if not options.replace_key:
            try:
                # Publish a complete file without exposing an empty O_EXCL-created key.
                os.link(temporary, path)
            except FileExistsError:
                if read_secret(path) != secret:
                    raise ValueError('key_exists_different: use key --replace-key to replace the existing key') from None
                status = 'key_unchanged'
    finally:
        Path(temporary).unlink(missing_ok=True)
    if report:
        print(json.dumps({'status': status, 'key_path': str(path)}))


def connection_key(options):
    try:
        return read_secret(options.key_file)
    except ValueError as error:
        if not str(error).startswith('key_missing:'):
            raise
    options.source_file = None
    options.replace_key = False
    save_key(options, report=False)
    return read_secret(options.key_file)


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
    if options.command not in ('close', 'ack') and options.cursor is not None:
        exchange.validate_cursor(options.cursor)
        target['cursor'] = options.cursor
    return target


def exchange_client(options, key):
    return httpx.AsyncClient(base_url=options.url.rstrip('/') + '/', timeout=70,
                             headers={'Authorization': 'Bearer ' + key})


def selected_adapter(options):
    adapter = options.client
    if adapter is None:
        selected = [client for client, name in (('claude', 'CLAUDE_CODE_SESSION_ID'), ('codex', 'CODEX_THREAD_ID'))
                    if os.environ.get(name) == options.session_id]
        adapter = selected[0] if len(selected) == 1 else 'cli'
    return adapter


def presence_payload(options, state='connected', ttl_seconds=600):
    adapter = selected_adapter(options)
    payload = {'session_id': options.session_id, 'client': adapter, 'state': state, 'ttl_seconds': ttl_seconds}
    if getattr(options, 'name', None) is not None:
        payload['name'] = options.name
    return payload


def announce_presence(options, key, state='connected', ttl_seconds=600):
    with httpx.Client(timeout=10) as client:
        response = client.post(options.url.rstrip('/') + '/sessions',
                               headers={'Authorization': 'Bearer ' + key}, json=presence_payload(options, state, ttl_seconds))
        response.raise_for_status()


def ensure_topic(options, key):
    target = {'channel': options.channel, 'conversation_id': options.conversation_id or 'main'}
    for value in target.values():
        exchange.validate_identifier(value)
    path = exchange.owned_state(options.state_dir, options.session_id, target)
    previous = exchange.read_state(path) if path else None
    if previous and previous['deadline'] > time.time() and options.command != 'connect':
        return previous
    cursor = previous['cursor'] if previous else None
    if cursor is None:
        with httpx.Client(timeout=10) as client:
            response = client.get(options.url.rstrip('/') + '/channels/' + options.channel + '/cursor',
                                  headers={'Authorization': 'Bearer ' + key})
            response.raise_for_status()
            cursor = response.json()['cursor']
        exchange.validate_cursor(cursor)
    timeout = options.timeout if options.command == 'connect' else exchange.EXCHANGE_TIMEOUT
    state = exchange.new_state(options.session_id, {**target, 'cursor': cursor}, timeout)
    return exchange.open_exchange(options.state_dir, options.session_id, state, reuse=True)


def binding_diagnostics(options):
    from polyagent_channels.client_config import DEFAULT_ROOT
    from polyagent_channels.setup import canonical_path, hook_binding_status, native_resume_command
    adapter = selected_adapter(options)
    if adapter == 'cli':
        return {}
    root = canonical_path(Path(options.config).parent) if options.config else canonical_path(DEFAULT_ROOT)
    project = canonical_path(options.project)
    binding = hook_binding_status(root, project, adapter)
    install = shlex.join(['python3', str(root / 'harness.pyz'), 'install', '--target', str(root),
                          '--url', options.url, '--client', adapter, '--project', str(project)])
    return {'hook_binding': binding, 'install_command': install if (root / 'harness.pyz').is_file() else None,
            'resume_command': native_resume_command(adapter, Path.cwd(), options.session_id)}


def reception_status(options):
    result = {'reception': 'not_open', 'next_action': 'Run connect for the topic before waiting for peer replies.',
              'native_activation': 'Local callback diagnostics do not verify native hook trust or activation.',
              **binding_diagnostics(options)}
    binding = result.get('hook_binding')
    if binding and binding['status'] != 'installed':
        result['reception'] = binding['status']
        prefix = 'This project/client has no installed Stop binding. '
        if binding['status'] == 'hook_invalid':
            prefix = 'This project/client Stop binding differs from its installation record or cannot be read. Inspect the reported hook file. '
        installation = 'Download the installer following ' + options.url + '/help for this project/client. '
        if result['install_command']:
            installation = 'Run the explicit installer: ' + result['install_command'] + '. '
        result['next_action'] = prefix + installation + 'Reuse the existing protected key. '
        result['next_action'] += 'Then restart/resume this session, review the native hook, and run connect after activation.'
        if result['resume_command']:
            result['next_action'] += ' Resume command: ' + result['resume_command']
        else:
            result['next_action'] += ' Obtain the current session ID from its native client before restarting; do not guess or resume another session.'
        return result
    if not options.session_id:
        result['next_action'] = 'Select --client in its native session, or provide --session-id.'
        return result
    topics = [exchange.read_state(path) for path in exchange.list_states(options.state_dir, options.session_id)]
    target_channel = getattr(options, 'channel', None)
    if target_channel is not None:
        target_conversation = getattr(options, 'conversation_id', None) or 'main'
        topics = [topic for topic in topics if topic['channel'] == target_channel and topic['conversation_id'] == target_conversation]
    if not topics:
        return result
    if not any(topic['deadline'] > time.time() for topic in topics):
        result.update(reception='expired', next_action='Run connect for the same topic to resume its saved cursor and pending work.')
        return result
    diagnostic = exchange.session_directory(options.state_dir, options.session_id) / 'last-hook.json'
    callback = None
    if diagnostic.exists() and not diagnostic.is_symlink():
        candidate = json.loads(diagnostic.read_text())
        if candidate.get('session_id') == options.session_id:
            callback = candidate
    if callback:
        timestamp = callback.get('timestamp')
        age = time.time() - timestamp if type(timestamp) in (int, float) and math.isfinite(timestamp) else None
        result['callback_age_seconds'] = round(age, 3) if age is not None else None
        if age is None or not 0 <= age <= 90:
            result.update(reception='callback_stale', next_action='The last callback observation is stale or has an invalid timestamp. '
                          'Review the Channels command in /hooks, enable project hooks if required, then yield to an actual native Stop event. '
                          'A past callback does not establish current reception or native trust.')
            return result
    if callback and callback.get('state') == 'waiting':
        result.update(reception='waiting', next_action='The callback reports waiting for peer data; process Stop-delivered content, reply, then ack its ID.')
        return result
    if callback and callback.get('state') in ('callback_observed', 'peer', 'expired'):
        result.update(reception='callback_observed', next_action='A local callback was observed; native trust is not verified. '
                      'Process any Stop-delivered peer content, reply and ack its ID, then yield to Stop while this topic remains open.')
        return result
    result.update(reception='hook_unobserved', next_action='Review the exact Channels Stop command in /hooks and enable project hooks if required. '
                  'If it is not loaded, restart and resume this same session. Finish local work, then yield to an actual native Stop event; '
                  'installation and a direct hook invocation do not prove activation.')
    if result.get('resume_command'):
        result['next_action'] += ' Resume command: ' + result['resume_command']
    return result


def connection_probe(options, consume=False):
    directory = exchange.session_directory(options.state_dir, options.session_id)
    if directory.is_symlink() or Path(options.state_dir).is_symlink():
        raise ValueError('session_state_invalid')
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / 'connection-probe.json'
    with exchange.locked_state(path):
        if not consume:
            exchange.write_state(path, {'session_id': options.session_id})
            return
        if not path.exists():
            return False
        if path.is_symlink() or json.loads(path.read_text()) != {'session_id': options.session_id}:
            raise ValueError('session_probe_invalid')
        path.unlink()
        return True


async def run_exchange(options, key):
    target = {**exchange_target(options), 'session_id': options.session_id}
    async with exchange_client(options, key) as client:
        if options.command == 'wait':
            result = await exchange.wait_for_peer(client, target, options.timeout)
            print(json.dumps(result, ensure_ascii=False))
            return 2 if result.get('type') == 'timeout' else 0
        response = await client.get('status')
        response.raise_for_status()
        if options.command == 'open' and 'cursor' not in target:
            previous = exchange.owned_state(options.state_dir, options.session_id, target)
            if previous is not None:
                target['cursor'] = exchange.read_state(previous)['cursor']
            if previous is None:
                response = await client.get('channels/' + options.channel + '/cursor')
                response.raise_for_status()
                target['cursor'] = response.json()['cursor']
            exchange.validate_cursor(target['cursor'])
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
    live = any(exchange.read_state(path)['deadline'] > time.time()
               for path in exchange.list_states(options.state_dir, options.session_id))
    announce_presence(options, key, 'connected' if live else 'closed', 90 if live else 1)
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
        event = read_hook_input()
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
            record_hook(options, 'callback_observed', callback_id)
            if connection_probe(options, consume=True):
                announce_presence(options, key)
                print(json.dumps({'decision': 'block', 'reason': 'Connection callback observed for this session. '
                                  'Continue the already authorized task, or finish local work and yield to Stop to wait for peer messages. '
                                  'This diagnostic does not verify native hook trust; do not invoke the hook manually to claim activation.'}))
                return
            async def wait():
                async with exchange_client(options, key) as client:
                    async def heartbeat():
                        response = await client.post('sessions', json=presence_payload(options, 'waiting', 90))
                        response.raise_for_status()
                        record_hook(options, 'waiting', callback_id)
                    try:
                        return await exchange.hook_topics(client, live, options.timeout, heartbeat)
                    finally:
                        response = await client.post('sessions', json=presence_payload(options))
                        response.raise_for_status()
            decision = asyncio.run(wait())
            diagnostic = 'peer' if 'Untrusted peer JSON' in decision.get('reason', '') else 'expired'
            if any(path.exists() and exchange.read_state(path)['errors'] for path in live):
                diagnostic = 'error'
    except HookInputRequired as error:
        print('channel hook failed: ' + str(error), file=sys.stderr)
        print('{}')
        return 1
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
        event = read_hook_input()
        options.session_id = exchange.validate_session_id(event.get('session_id'))
        if Path(options.config).is_file():
            record_hook(options, 'config_error')
    except HookInputRequired as error:
        print('channel hook failed: ' + str(error), file=sys.stderr)
        print('{}')
        return 1
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
              'service': 'ready',
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
    print(json.dumps({**result, **reception_status(options)}))


def run(options):
    if options.command == 'key':
        save_key(options)
        return
    if options.command == 'hook':
        return run_hook(options)
    options.session_id = select_session(options, required=options.command in ('connect', 'publish', 'open', 'close', 'wait', 'ack')
                                        or (options.command == 'receive' and options.cursor is None))
    key = connection_key(options) if options.command == 'connect' else read_secret(options.key_file)
    if options.command == 'connect':
        announce_presence(options, key)
        state = ensure_topic(options, key)
        connection_probe(options)
        print(json.dumps({'type': 'connected', 'session_id': options.session_id, 'topic': state,
                          **reception_status(options)}))
        return
    if options.command == 'peers':
        with httpx.Client(timeout=10) as client:
            response = client.get(options.url.rstrip('/') + '/sessions', headers={'Authorization': 'Bearer ' + key})
            response.raise_for_status()
        print(json.dumps(response.json(), ensure_ascii=False))
        return
    if options.session_id:
        announce_presence(options, key)
    if options.command == 'status':
        return client_status(options, key)
    if options.command in ('wait', 'open', 'close', 'ack'):
        return asyncio.run(run_exchange(options, key))
    if options.command == 'publish' or (options.command == 'receive' and options.cursor is None):
        state = ensure_topic(options, key)
        if options.command == 'receive':
            options.cursor = state['cursor']
    headers = {'Authorization': 'Bearer ' + key}
    channel_url = options.url.rstrip('/') + '/channels/' + options.channel
    if options.command == 'listen':
        asyncio.run(listen_until_timeout(options, channel_url, headers))
        return
    response = request_messages(options, channel_url, headers)
    if response.is_error:
        raise ValueError('unauthorized401' if response.status_code == 401 else f'HTTP {response.status_code}')
    result = {**response.json(), **reception_status(options)}
    if options.command == 'receive':
        result.update(delivery='history_read', ack_required=False)
        result['next_action'] = ('This receive call creates no pending delivery; ack only IDs previously delivered by Stop. '
                                 + result['next_action'])
    print(json.dumps(result, ensure_ascii=False))


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
                status = run_hook_config_error(options, error.code)
                if status:
                    sys.exit(status)
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
