"""The stdio adapter exposes named tools and fills in what the model must not supply."""
import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from polyagent_channels import exchange, stdio_tools


def adapter_options(tmp_path, client):
    return SimpleNamespace(client=client, config=str(tmp_path / 'config.json'), state_dir=str(tmp_path / 'state'))


@pytest.fixture(autouse=True)
def installed_archive(tmp_path, monkeypatch):
    archive = tmp_path / 'harness.pyz'
    archive.write_bytes(b'synthetic')
    monkeypatch.setattr(sys, 'argv', [str(archive)])
    return archive


@pytest.fixture
def executed(monkeypatch):
    calls = []
    def run(command, **settings):
        calls.append((command, settings))
        return subprocess.CompletedProcess(command, 0, stdout='{"ok": true}', stderr='')
    monkeypatch.setattr(stdio_tools.subprocess, 'run', run)
    return calls


def test_initialize_lists_named_tools_and_requires_only_text():
    initialized = stdio_tools.respond(None, {'method': 'initialize', 'params': {'protocolVersion': '2025-11-25'}}, None)
    tools = {tool['name']: tool for tool in stdio_tools.respond(None, {'method': 'tools/list'}, None)['tools']}
    assert (initialized['protocolVersion'] == '2025-11-25' and '채널' in initialized['instructions'].splitlines()[0]
            and set(tools) == {'publish', 'receive', 'ack', 'peers', 'connect', 'close', 'status'}
            and tools['ack']['inputSchema']['required'] == ['message_id']
            and tools['publish']['inputSchema']['required'] == ['text']
            and not any(name in json.dumps(tools) for name in ('session_id', '"key"')))


def test_ack_says_it_does_not_replace_an_answer():
    description = next(tool for tool in stdio_tools.TOOLS if tool['name'] == 'ack')['description']
    assert 'never replaces an answer' in description and 'through' not in description


def test_unknown_method_is_a_protocol_error_not_a_crash(tmp_path):
    process = subprocess.run([sys.executable, '-c', 'from polyagent_channels import stdio_tools, cli\n'
                              'from types import SimpleNamespace\nstdio_tools.serve(SimpleNamespace(client="codex"))'],
                             input=json.dumps({'id': 1, 'method': 'server/discover'}) + '\n', capture_output=True, text=True, timeout=15)
    assert json.loads(process.stdout)['error']['code'] == -32601


def test_claude_uses_the_session_announced_for_its_process(tmp_path, monkeypatch, executed):
    options = adapter_options(tmp_path, 'claude')
    monkeypatch.setenv('CLAUDE_CODE_SESSION_ID', 'spawn-time-session')
    stdio_tools.call(options, {'name': 'status', 'arguments': {}}, None)
    pointer = exchange.client_pointer(options.state_dir, os.getppid())
    pointer.parent.mkdir(parents=True)
    pointer.write_text('session-after-clear')
    stdio_tools.call(options, {'name': 'status', 'arguments': {}}, None)
    assert [command[command.index('--session-id') + 1] for command, settings in executed] == ['spawn-time-session', 'session-after-clear']


def test_codex_without_thread_metadata_fails_visibly_and_sends_nothing(tmp_path, executed):
    result = stdio_tools.call(adapter_options(tmp_path, 'codex'), {'name': 'publish', 'arguments': {'text': 'hello'}}, stdio_tools.Receivers())
    assert result['isError'] is True and executed == []


def test_message_text_travels_through_stdin_only(tmp_path, monkeypatch, executed):
    monkeypatch.setenv('CLAUDE_CODE_SESSION_ID', 'session')
    result = stdio_tools.call(adapter_options(tmp_path, 'claude'),
                              {'name': 'publish', 'arguments': {'text': '안녕 secret-text', 'reply_to': '1-0'}}, None)
    command, settings = executed[0]
    assert (result['isError'] is False and settings['input'] == '안녕 secret-text' and 'secret-text' not in ' '.join(command)
            and command[-2:] == ['--reply-to', '1-0'] and settings['env']['PYTHONUTF8'] == '1')


def test_ack_runs_the_confirmation_command_for_the_chosen_topic(tmp_path, monkeypatch, executed):
    monkeypatch.setenv('CLAUDE_CODE_SESSION_ID', 'session')
    stdio_tools.call(adapter_options(tmp_path, 'claude'), {'name': 'ack', 'arguments': {
        'message_id': '5-0', 'channel': 'work', 'conversation_id': 'topic'}}, None)
    assert executed[0][0][-6:] == ['ack', 'work', '--conversation-id', 'topic', '--message-id', '5-0']


@pytest.mark.parametrize('name,arguments,problem', [
    ('publish', {'text': 42}, 'text must be a string'),
    ('publish', {}, 'text is required'),
    ('publish', {'text': 'hi', 'session_id': 'forged'}, 'unknown argument session_id'),
    ('ack', {'message_id': None}, 'message_id must be a string'),
    ('receive', ['general'], 'arguments must be an object'),
    ('missing_tool', {}, 'unknown tool')])
def test_bad_arguments_come_back_as_tool_errors_and_run_nothing(tmp_path, monkeypatch, executed, name, arguments, problem):
    monkeypatch.setenv('CLAUDE_CODE_SESSION_ID', 'session')
    result = stdio_tools.call(adapter_options(tmp_path, 'claude'), {'name': name, 'arguments': arguments}, None)
    assert result['isError'] is True and problem in result['content'][0]['text'] and executed == []


def serve_lines(tmp_path, *lines):
    """Run the real adapter loop in a process, as a client does, with one request per line."""
    code = ('import sys\nfrom types import SimpleNamespace\nfrom polyagent_channels import stdio_tools\n'
            'sys.argv = [sys.argv[1]]\nstdio_tools.serve(SimpleNamespace(client="codex", config="c", state_dir=sys.argv[0]))')
    done = subprocess.run([sys.executable, '-c', code, str(tmp_path / 'harness.pyz')], input='\n'.join(lines) + '\n',
                          capture_output=True, text=True, timeout=20, env={**os.environ, 'PYTHONPATH': 'src'})
    return done.returncode, [json.loads(line) for line in done.stdout.splitlines()]


def test_bad_argument_keeps_the_connection_and_the_next_ping_answers(tmp_path):
    status, replies = serve_lines(tmp_path, json.dumps({'id': 1, 'method': 'tools/call', 'params': {
        'name': 'publish', 'arguments': {'text': 42}, '_meta': {'threadId': 'thread'}}}), json.dumps({'id': 2, 'method': 'ping'}))
    assert status == 0 and replies[0]['result']['isError'] is True and replies[1] == {'jsonrpc': '2.0', 'id': 2, 'result': {}}


def test_malformed_requests_get_their_own_error_and_the_connection_survives(tmp_path):
    status, replies = serve_lines(tmp_path, 'not json', '[1]', json.dumps({'id': 3, 'method': 'tools/call', 'params': 'bad'}),
                                  json.dumps({'id': 4, 'method': 'ping'}))
    assert status == 0 and [reply.get('error', {}).get('code') for reply in replies] == [-32700, -32600, -32602, None]


def test_unexpected_failure_answers_that_request_without_details(tmp_path, monkeypatch):
    def fail(options, params, receivers):
        raise RuntimeError('synthetic-secret-detail')
    monkeypatch.setattr(stdio_tools, 'call', fail)
    reply = stdio_tools.reply_to_line(None, json.dumps({'id': 9, 'method': 'tools/call', 'params': {}}), None)
    assert reply['error'] == {'code': -32603, 'message': 'Internal error'} and 'synthetic-secret-detail' not in json.dumps(reply)


def test_failed_cli_call_reaches_the_model_as_a_tool_error(tmp_path, monkeypatch):
    monkeypatch.setenv('CLAUDE_CODE_SESSION_ID', 'session')
    monkeypatch.setattr(stdio_tools.subprocess, 'run', lambda command, **settings: subprocess.CompletedProcess(
        command, 1, stdout='', stderr='channel client failed: service_unreachable'))
    result = stdio_tools.call(adapter_options(tmp_path, 'claude'), {'name': 'peers', 'arguments': {}}, None)
    assert result['isError'] is True and 'service_unreachable' in result['content'][0]['text']


def test_tool_after_removal_says_so_and_runs_nothing(tmp_path, monkeypatch, executed, installed_archive):
    monkeypatch.setenv('CLAUDE_CODE_SESSION_ID', 'session')
    installed_archive.unlink()
    result = stdio_tools.call(adapter_options(tmp_path, 'claude'), {'name': 'status', 'arguments': {}}, None)
    assert result['isError'] is True and 'removed' in result['content'][0]['text'] and executed == []


def test_codex_first_call_joins_once_and_keeps_one_receiver_per_thread(tmp_path, monkeypatch, executed):
    started = []
    class Receiver:
        def __init__(self, command, **settings):
            started.append(command)
        def poll(self):
            return None
    monkeypatch.setattr(stdio_tools.subprocess, 'Popen', Receiver)
    monkeypatch.setattr(stdio_tools, 'wait_for_first_report', lambda options, session: None)
    options = adapter_options(tmp_path, 'codex')
    receivers = stdio_tools.Receivers()
    for name in ('status', 'peers'):
        stdio_tools.call(options, {'name': name, 'arguments': {}, '_meta': {'threadId': 'thread-a'}}, receivers)
    stdio_tools.call(options, {'name': 'status', 'arguments': {}, '_meta': {'threadId': 'thread-b'}}, receivers)
    joined = [command for command, settings in executed if command[-1] == 'connect']
    assert (len(joined) == 2 and [command[command.index('--session-id') + 1] for command in started] == ['thread-a', 'thread-b']
            and all(command[-2:] == ['watch', '--redeliver'] for command in started))


def test_stopped_receiver_is_restarted_by_the_next_tool_call(tmp_path, monkeypatch, executed):
    started = []
    class Receiver:
        def __init__(self, command, **settings):
            started.append(command)
        def poll(self):
            return 0
    monkeypatch.setattr(stdio_tools.subprocess, 'Popen', Receiver)
    monkeypatch.setattr(stdio_tools, 'wait_for_first_report', lambda options, session: None)
    options = adapter_options(tmp_path, 'codex')
    receivers = stdio_tools.Receivers()
    for ignored in range(2):
        stdio_tools.call(options, {'name': 'status', 'arguments': {}, '_meta': {'threadId': 'thread-a'}}, receivers)
    assert (len(started) == 2 and sum(command[-1] == 'connect' for command, settings in executed) == 1
            and started[0][-1] == '--redeliver' and started[1][-1] == 'watch')
