"""Exercise the installed CLI's control-socket exchange against a stand-in app server."""
import json
import queue
import select
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from threading import Thread

import pytest
from websockets.exceptions import ConnectionClosed, ConnectionClosedOK
from websockets.sync.server import unix_serve

from polyagent_channels import codex_listen

TEST_SECONDS = 10
CLI = [sys.executable, '-m', 'polyagent_channels.cli']


@pytest.fixture(autouse=True)
def control_socket(tmp_path, monkeypatch):
    home = tmp_path / ('codex-home-' + 'x' * 80)
    directory = home / 'app-server-control'
    directory.mkdir(parents=True)
    monkeypatch.setenv('CODEX_HOME', str(home))
    socket = tmp_path / 'control.sock'
    (directory / 'app-server-control.sock').symlink_to(socket)
    return socket


@contextmanager
def app_server(control_socket, handler):
    server = unix_serve(handler, str(control_socket))
    thread = Thread(target=server.serve_forever)
    try:
        thread.start()
        yield
    finally:
        server.shutdown()
        join_started_thread(thread)


def join_started_thread(thread):
    if thread.ident is not None:
        thread.join()


def receive(connection):
    return json.loads(connection.recv(timeout=TEST_SECONDS))


def answer(connection, request, **outcome):
    connection.send(json.dumps({'id': request['id'], **outcome}))


def loaded_threads(connection, threads):
    initialize = receive(connection)
    answer(connection, initialize, result={})
    initialized = receive(connection)
    listing = receive(connection)
    answer(connection, listing, result={'data': threads})
    return [initialize, initialized, listing]


def answer_read(connection, source):
    request = receive(connection)
    answer(connection, request, result={'thread': {'source': source}})
    return request


def run_listener():
    return subprocess.run([*CLI, '--config', '/missing/config.json', 'codex-listen'],
                          capture_output=True, text=True, timeout=TEST_SECONDS)


def test_calls_channels_status_once_per_loaded_thread(control_socket):
    requests = queue.Queue()

    def handle(connection):
        handshake = loaded_threads(connection, ['first', 'second'])
        first_read = answer_read(connection, 'vscode')
        first = receive(connection)
        answer(connection, first, result={})
        second_read = answer_read(connection, {'customSource': {}})
        second = receive(connection)
        answer(connection, second, result={})
        with pytest.raises(ConnectionClosedOK):
            receive(connection)
        requests.put(handshake + [first_read, first, second_read, second])

    with app_server(control_socket, handle):
        process = run_listener()

    handshake, initialized, listing, first_read, first, second_read, second = requests.get(timeout=TEST_SECONDS)
    assert (process.returncode, json.loads(process.stdout), handshake['method'], initialized, listing,
            first_read, first, second_read, second) == (
        0, {'first': True, 'second': True}, 'initialize', {'method': 'initialized'},
        {'id': 2, 'method': 'thread/loaded/list', 'params': {}},
        {'id': 3, 'method': 'thread/read', 'params': {'threadId': 'first', 'includeTurns': False}},
        {'id': 4, 'method': 'mcpServer/tool/call',
         'params': {'threadId': 'first', 'server': 'channels', 'tool': 'status', 'arguments': {}}},
        {'id': 5, 'method': 'thread/read', 'params': {'threadId': 'second', 'includeTurns': False}},
        {'id': 6, 'method': 'mcpServer/tool/call',
         'params': {'threadId': 'second', 'server': 'channels', 'tool': 'status', 'arguments': {}}},
    )


def test_ignores_notifications_and_replies_with_other_ids(control_socket):
    def handle(connection):
        loaded_threads(connection, ['thread'])
        answer_read(connection, 'vscode')
        request = receive(connection)
        connection.send(json.dumps({'method': 'thread/started'}))
        connection.send(json.dumps({'id': 99, 'error': {'message': 'unrelated'}}))
        answer(connection, request, result={})

    with app_server(control_socket, handle):
        process = run_listener()

    assert (process.returncode, json.loads(process.stdout)) == (0, {'thread': True})


def test_thread_error_does_not_stop_the_next_call(control_socket):
    def handle(connection):
        loaded_threads(connection, ['gone', 'open'])
        answer_read(connection, 'vscode')
        answer(connection, receive(connection), error={'message': 'thread not found'})
        answer_read(connection, 'vscode')
        answer(connection, receive(connection), result={})

    with app_server(control_socket, handle):
        process = run_listener()

    assert (process.returncode, json.loads(process.stdout)) == (0, {'gone': False, 'open': True})


def test_does_not_call_status_in_a_sub_agent_thread(control_socket):
    requests = queue.Queue()

    def handle(connection):
        loaded_threads(connection, ['child', 'parent'])
        child_read = answer_read(connection, {'subAgent': {'thread_spawn': {'parent_thread_id': 'parent'}}})
        parent_read = answer_read(connection, 'vscode')
        status = receive(connection)
        answer(connection, status, result={})
        requests.put((child_read['method'], parent_read['method'], status['params']['threadId']))

    with app_server(control_socket, handle):
        process = run_listener()

    assert (process.returncode, json.loads(process.stdout), requests.get(timeout=TEST_SECONDS)) == (
        0, {'parent': True}, ('thread/read', 'thread/read', 'parent'))


def test_read_error_skips_only_that_thread(control_socket):
    requests = queue.Queue()

    def handle(connection):
        loaded_threads(connection, ['gone', 'open'])
        gone_read = receive(connection)
        answer(connection, gone_read, error={'message': 'thread not found'})
        open_read = answer_read(connection, 'vscode')
        status = receive(connection)
        answer(connection, status, result={})
        requests.put((gone_read['method'], open_read['method'], status['params']['threadId']))

    with app_server(control_socket, handle):
        process = run_listener()

    assert (process.returncode, json.loads(process.stdout), requests.get(timeout=TEST_SECONDS)) == (
        0, {'open': True}, ('thread/read', 'thread/read', 'open'))


def test_missing_socket_succeeds(control_socket):
    process = run_listener()
    assert (process.returncode, process.stdout, process.stderr) == (0, '{}\n', '')


def test_failed_exchange_exits_with_one_diagnostic(control_socket):
    def handle(connection):
        answer(connection, receive(connection), error={'message': 'initialize failed'})

    with app_server(control_socket, handle):
        process = run_listener()

    assert (process.returncode, process.stdout, process.stderr.splitlines()) == (
        1, '', ['codex-listen failed: initialize returned an error'])


def test_wait_for_reply_is_bounded(control_socket, monkeypatch, capsys):
    def handle(connection):
        receive(connection)
        with pytest.raises(ConnectionClosedOK):
            receive(connection)

    monkeypatch.setattr(codex_listen, 'PASS_SECONDS', 0.2)
    with app_server(control_socket, handle):
        status = codex_listen.run()

    captured = capsys.readouterr()
    assert (status, captured.out, len(captured.err.splitlines())) == (1, '', 1)


@pytest.fixture
def adapter_process(tmp_path):
    archive = tmp_path / 'harness.pyz'
    archive.write_text(
        'import sys\n'
        'from types import SimpleNamespace\n'
        'from polyagent_channels import cli, stdio_tools\n'
        'if sys.argv[-1] == "mcp":\n'
        '    stdio_tools.wait_for_first_report = lambda options, session: None\n'
        '    stdio_tools.serve(SimpleNamespace(client="codex", config="unused"))\n'
        'elif sys.argv[-1] == "codex-listen":\n'
        '    cli.main()\n'
        'elif sys.argv[-1] == "status":\n'
        '    print("{\\"synthetic\\": true}")\n')
    with subprocess.Popen([sys.executable, str(archive), 'mcp'], stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0) as process:
        yield process


@pytest.fixture
def adapter(adapter_process):
    try:
        yield adapter_process
    finally:
        adapter_process.stdin.close()
        adapter_process.wait(timeout=TEST_SECONDS)


def send_adapter(process, message):
    process.stdin.write(json.dumps(message).encode() + b'\n')


def read_adapter(process):
    ready, _, _ = select.select([process.stdout], [], [], TEST_SECONDS)
    if not ready:
        raise TimeoutError('no stdio response')
    return json.loads(process.stdout.readline())


def initialize_adapter(process):
    send_adapter(process, {'id': 1, 'method': 'initialize'})
    read_adapter(process)


def expect_no_connection(connections):
    with pytest.raises(queue.Empty):
        connections.get(timeout=0.5)


def test_adapter_starts_pass_after_initialized(control_socket, adapter):
    connections = queue.Queue()

    def handle(connection):
        connections.put(receive(connection))

    with app_server(control_socket, handle):
        initialize_adapter(adapter)
        expect_no_connection(connections)
        send_adapter(adapter, {'method': 'notifications/initialized'})
        first = connections.get(timeout=TEST_SECONDS)

    assert first['method'] == 'initialize'


def test_adapter_reaps_completed_startup_pass_while_idle(control_socket, adapter):
    child_paths = queue.Queue()
    adapter_pid = adapter.pid

    def handle(connection):
        children = Path(f'/proc/{adapter_pid}/task/{adapter_pid}/children')
        initialize = receive(connection)
        child_pid, = children.read_text().split()
        child_paths.put(Path('/proc') / child_pid)
        answer(connection, initialize, result={})
        receive(connection)
        answer(connection, receive(connection), result={'data': []})

    with app_server(control_socket, handle):
        initialize_adapter(adapter)
        send_adapter(adapter, {'method': 'notifications/initialized'})
        child = child_paths.get(timeout=TEST_SECONDS)
        wait_for_process_to_disappear(child)

    assert (child.exists(), adapter.poll()) == (False, None)


def wait_for_process_to_disappear(process_path):
    deadline = time.monotonic() + TEST_SECONDS
    while process_path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)


def test_adapter_answers_a_tool_call_while_startup_waits(control_socket, adapter):
    requests = queue.Queue()
    replies = queue.Queue()

    def handle(connection):
        loaded_threads(connection, ['own-thread'])
        answer_read(connection, 'vscode')
        request = receive(connection)
        requests.put(request)
        answer(connection, request, result=replies.get(timeout=TEST_SECONDS))

    with app_server(control_socket, handle):
        initialize_adapter(adapter)
        send_adapter(adapter, {'method': 'notifications/initialized'})
        requests.get(timeout=TEST_SECONDS)
        send_adapter(adapter, {'id': 2, 'method': 'tools/call', 'params': {
            'name': 'status', 'arguments': {}, '_meta': {'threadId': 'own-thread'}}})
        reply = read_adapter(adapter)
        replies.put(reply['result'])

    assert reply == {'jsonrpc': '2.0', 'id': 2, 'result': {
        'content': [{'type': 'text', 'text': '{"synthetic": true}\n'}], 'isError': False}}


def test_adapter_exit_terminates_pending_startup_pass(control_socket, adapter):
    connected = queue.Queue()
    disconnected = queue.Queue()

    def handle(connection):
        connected.put(receive(connection))
        with pytest.raises(ConnectionClosed):
            receive(connection)
        disconnected.put(True)

    with app_server(control_socket, handle):
        initialize_adapter(adapter)
        send_adapter(adapter, {'method': 'notifications/initialized'})
        connected.get(timeout=TEST_SECONDS)
        adapter.stdin.close()
        adapter.wait(timeout=TEST_SECONDS)
        stopped = disconnected.get(timeout=TEST_SECONDS)

    assert stopped
