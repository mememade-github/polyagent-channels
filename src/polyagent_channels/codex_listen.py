"""Ask the local Codex app server to start channel reception in its loaded threads."""
import json
import os
import sys
import time
from pathlib import Path

from websockets.exceptions import WebSocketException
from websockets.sync.client import unix_connect

OPEN_SECONDS = 5
PASS_SECONDS = 90
CLOSE_SECONDS = 1


def run():
    try:
        threads = listen()
    except (OSError, WebSocketException, ValueError, KeyError, TypeError, AttributeError) as error:
        detail = ' '.join(str(error).splitlines()) or type(error).__name__
        print('codex-listen failed: ' + detail, file=sys.stderr)
        return 1
    print(json.dumps(threads))
    return 0


def listen():
    home = Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex')
    socket = home / 'app-server-control/app-server-control.sock'
    try:
        socket_path = socket.resolve(strict=True)
    except FileNotFoundError:
        return {}
    with unix_connect(str(socket_path), open_timeout=OPEN_SECONDS, close_timeout=CLOSE_SECONDS) as connection:
        return listen_to_loaded_threads(connection)


def listen_to_loaded_threads(connection):
    deadline = time.monotonic() + PASS_SECONDS
    client = {'name': 'channels', 'title': 'Polyagent Channels', 'version': '1'}
    result(connection, deadline, {'id': 1, 'method': 'initialize', 'params': {'clientInfo': client}})
    connection.send(json.dumps({'method': 'initialized'}))
    threads = result(connection, deadline, {'id': 2, 'method': 'thread/loaded/list', 'params': {}})['data']
    outcomes = {}
    for index, thread in enumerate(threads):
        outcomes.update(listen_to_thread(connection, deadline, 3 + 2 * index, thread))
    return outcomes


def listen_to_thread(connection, deadline, number, thread):
    parameters = {'threadId': thread, 'includeTurns': False}
    response = request(connection, deadline, {'id': number, 'method': 'thread/read', 'params': parameters})
    if 'error' in response:
        return {}
    source = response['result']['thread']['source']
    if isinstance(source, dict) and 'subAgent' in source:
        return {}
    return {thread: status_answered(connection, deadline, number + 1, thread)}


def result(connection, deadline, message):
    response = request(connection, deadline, message)
    if 'error' in response:
        raise ValueError(message['method'] + ' returned an error')
    return response['result']


def status_answered(connection, deadline, number, thread):
    parameters = {'threadId': thread, 'server': 'channels', 'tool': 'status', 'arguments': {}}
    response = request(connection, deadline, {'id': number, 'method': 'mcpServer/tool/call', 'params': parameters})
    return 'error' not in response


def request(connection, deadline, message):
    connection.send(json.dumps(message))
    reply = receive(connection, deadline)
    while reply.get('id') != message['id']:
        reply = receive(connection, deadline)
    return reply


def receive(connection, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError('timed out waiting for a control socket reply')
    return json.loads(connection.recv(timeout=remaining))
