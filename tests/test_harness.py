"""Owned synthetic installs, bundled imports and common profile behavior."""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
import time
import zipfile

import httpx
import pytest

from channels import cli, exchange
from channels.client_config import apply_config

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope='module')
def artifact(tmp_path_factory):
    path = tmp_path_factory.mktemp('bundle') / 'harness.pyz'
    runpy.run_path(str(ROOT / 'scripts/build_harness.py'))['build'](path, ROOT)
    return path


def invoke(artifact, *arguments, cwd, interpreter=sys.executable, stdin='{}'):
    environment = {key: value for key, value in os.environ.items() if not key.startswith(('CHANNEL_', 'PYTHON'))}
    return subprocess.run([interpreter, str(artifact), *map(str, arguments)], cwd=cwd,
                          env=environment, input=stdin, text=True, capture_output=True, timeout=15)


def test_plan_no_writes_and_install_preserves_hooks_and_credentials(artifact, tmp_path):
    root = tmp_path / 'shared'
    project = tmp_path / 'project'
    project.mkdir()
    arguments = ['--url', 'http://localhost:18765', '--profile', 'first', '--target', root,
                 '--project', project, '--client', 'claude']
    planned = invoke(artifact, 'plan', *arguments, cwd=tmp_path)
    assert (planned.returncode, root.exists(), list(project.iterdir())) == (0, False, [])
    hook = project / '.claude/settings.local.json'
    hook.parent.mkdir()
    hook.write_text(json.dumps({'unrelated': {'keep': True}, 'hooks': {'Stop': [
        {'matcher': '', 'hooks': [{'type': 'command', 'command': 'other-check'}]}]}}))
    first = invoke(artifact, 'install', *arguments, cwd=tmp_path)
    credential = root / '.env/credentials/first.key'
    credential.write_text('synthetic-existing-credential\n')
    credential.chmod(0o600)
    before = hook.read_bytes()
    second = invoke(artifact, 'install', *arguments, cwd=tmp_path)
    data = json.loads(hook.read_text())
    assert (first.returncode, second.returncode, hook.read_bytes(), credential.read_text(),
            json.loads((root / 'config.json').read_text()), data['unrelated'],
            [item['command'] for row in data['hooks']['Stop'] for item in row['hooks']],
            credential.stat().st_mode & 0o777, credential.parent.stat().st_mode & 0o777) == (
        0, 0, before, 'synthetic-existing-credential\n', {'url': 'http://localhost:18765'}, {'keep': True},
        ['other-check', str(root / 'bin/channels') + ' --profile first hook'], 0o600, 0o700)


@pytest.mark.parametrize('client,relative', [('codex', '.codex/hooks.json'), ('claude', '.claude/settings.local.json')])
def test_siblings_do_not_retarget_and_wrapper_works_from_arbitrary_cwd(artifact, tmp_path, client, relative):
    root = tmp_path / 'root'
    arguments = ['--url', 'http://localhost:18765', '--profile', 'one', '--target', root, '--client', client, '--project', tmp_path]
    first = invoke(artifact, 'install', *arguments, cwd=tmp_path)
    wrapper = root / 'bin/channels'
    result = subprocess.run([str(wrapper), '--profile', 'one', 'hook'], cwd='/', input='{}', text=True, capture_output=True)
    refused = invoke(artifact, 'install', *[str(arg).replace('http://localhost:18765', 'http://localhost:19876') for arg in arguments], cwd=tmp_path)
    assert (first.returncode, result.returncode, result.stdout, refused.returncode, (tmp_path / relative).exists(),
            json.loads((root / 'state/one/last-hook.json').read_text())['state']) == (0, 0, '{}\n', 1, True, 'absent')


def test_profile_paths_ignore_environment_but_explicit_flags_win(tmp_path, monkeypatch):
    config = tmp_path / 'config.json'
    config.write_text('{"url":"http://localhost:18765"}')
    monkeypatch.setenv('CHANNEL_URL', 'http://unselected.invalid')
    monkeypatch.setenv('CHANNEL_CREDENTIAL_FILE', '/unselected')
    monkeypatch.setenv('CHANNEL_STATE_DIR', '/unselected-state')
    results = []
    for profile in ('first', 'second'):
        arguments = ['--config', str(config), '--profile', profile, 'open']
        options = cli.command_parser().parse_args(arguments)
        apply_config(options, arguments)
        results.append((options.url, options.credential_file, options.state_dir, options.channel, options.conversation_id))
    arguments = ['--config', str(config), '--profile', 'first', '--url=http://explicit.test', '--state-dir', '/explicit', 'receive', 'custom', '--conversation-id', 'custom']
    options = cli.command_parser().parse_args(arguments)
    apply_config(options, arguments)
    assert (results, options.url, options.state_dir, options.channel, options.conversation_id) == (
        [('http://localhost:18765', str(tmp_path / '.env/credentials' / (profile + '.key')), str(tmp_path / 'state' / profile), 'general', 'main') for profile in ('first', 'second')],
        'http://explicit.test', '/explicit', 'custom', 'custom')


@pytest.mark.parametrize('content', ['invalid private-input', '{"url":"http://localhost","secret":"private-input"}', '{"url":"http://user:private-input@localhost"}'])
def test_malformed_hook_config_is_redacted_and_bounded(artifact, tmp_path, content):
    config = tmp_path / 'config.json'
    config.write_text(content)
    result = invoke(artifact, '--config', config, '--profile', 'test', 'hook', cwd=tmp_path, stdin='raw-private-input')
    diagnostic = json.loads((tmp_path / 'state/test/last-hook.json').read_text())
    assert (result.returncode, result.stdout, 'private-input' in result.stderr, set(diagnostic), diagnostic['state']) == (0, '{}\n', False, {'timestamp', 'state'}, 'config_error')


def test_bundle_allowlist_resources_and_system_python310_timeout(artifact, tmp_path):
    python = shutil.which('python3.10')
    if not python:
        pytest.skip('system Python 3.10 unavailable')
    with zipfile.ZipFile(artifact) as archive:
        names = archive.namelist()
    script = '''import sys, asyncio
sys.path.insert(0, sys.argv[1])
import httpx, websockets, anyio, certifi, exceptiongroup
from channels import exchange, cli
async def slow():
    await asyncio.sleep(10)
class Client:
    async def get(self, *a, **kw):
        await slow()
async def check():
    result = await exchange.wait_for_peer(Client(), {'cursor':'0-0'}, .01)
    assert result == {'type':'timeout','cursor':'0-0'}
    exchange.IDENTITY_TIMEOUT = .01
    try:
        await exchange.own_identity(Client())
    except asyncio.TimeoutError:
        pass
    else:
        raise AssertionError('identity timeout missing')
    cli.listen = lambda *args: slow()
    from argparse import Namespace
    await cli.listen_until_timeout(Namespace(timeout=.01,cursor='0-0',conversation_id=None), '', {})
asyncio.run(check())
assert __import__('pathlib').Path(certifi.where()).is_file()
print('Python3.10 imports/resources/total/identity/listen timeouts passed')
'''
    result = subprocess.run([python, '-I', '-c', script, str(artifact)], cwd=tmp_path, text=True, capture_output=True, timeout=10)
    assert (result.returncode, {name for name in names if name.startswith('channels/')},
            any(name.endswith(('.pyc', '.so', '.pyd')) or '.env/' in name or '__pycache__' in name or name.startswith(('redis/', 'mcp/', 'starlette/', 'pydantic/')) for name in names),
            sum('license' in name.lower() for name in names) >= 10) == (
        0, {'channels/' + name for name in ('__init__.py', 'cli.py', 'exchange.py', 'client_config.py', 'setup.py')}, False, True), result.stderr


def test_public_artifact_hash_and_missing_are_visible(client, monkeypatch, tmp_path, artifact):
    monkeypatch.setenv('CHANNEL_HARNESS_FILE', str(artifact))
    body = client.get('/harness').content
    digest = hashlib.sha256(body).hexdigest()
    found = (client.get('/harness.sha256').text, digest in client.get('/help').text,
             client.get('/harness/anything').status_code)
    monkeypatch.setenv('CHANNEL_HARNESS_FILE', str(tmp_path / 'missing'))
    assert (found, client.get('/harness').status_code, client.get('/help').status_code) == (
        (digest + '  harness.pyz\n', True, 401), 503, 503)


@pytest.mark.parametrize('kind', ['absent', 'live', 'expired', 'error'])
def test_status_verifies_identity_and_reports_owned_state(tmp_path, monkeypatch, capsys, kind):
    state_dir = tmp_path / 'state'
    state = exchange.new_state({'agent_id': 'synthetic', 'sender': 'Synthetic'},
                               {'channel': 'work', 'conversation_id': 'task', 'cursor': '0-0'}, 10)
    if kind != 'absent':
        exchange.open_exchange(state_dir, 'synthetic-key', state)
        path = state_dir / 'synthetic/exchange.json'
        if kind == 'expired':
            exchange.write_state(path, {**state, 'deadline': time.time() - 1})
        if kind == 'error':
            path.write_text('invalid private-input')
    requests = []
    def respond(request):
        requests.append((request.url.path, request.headers['authorization']))
        return httpx.Response(200, json={'agent_id': 'synthetic', 'sender': 'Synthetic'})
    original = httpx.Client
    monkeypatch.setattr(cli.httpx, 'Client', lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs))
    cli.client_status(argparse.Namespace(profile='first', url='http://localhost', state_dir=str(state_dir), credential_file='/protected/path'), 'synthetic-key')
    result = json.loads(capsys.readouterr().out)
    assert (result['exchange'], result['identity'], requests, 'synthetic-key' in json.dumps(result)) == (
        kind, {'agent_id': 'synthetic', 'sender': 'Synthetic'}, [('/agents/me', 'Bearer synthetic-key')], False)


def test_system_python310_installed_wrapper_identity_and_exchange(artifact, tmp_path):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread
    python = shutil.which('python3.10')
    if not python:
        pytest.skip('system Python 3.10 unavailable')
    requests = []
    peer = []

    class Service(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            requests.append((self.path.split('?')[0], self.headers.get('Authorization')))
            payload = {'agent_id': 'synthetic-agent', 'sender': 'Synthetic'}
            if self.path.startswith('/channels/'):
                payload = {'messages': list(peer), 'cursor': '2-0' if peer else '0-0'}
            content = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(content)))
            self.end_headers()
            self.wfile.write(content)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Service)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        root = tmp_path / 'shared'
        install = invoke(artifact, 'install', '--target', root, '--profile', 'synthetic', '--client', 'nohook',
                         '--url', 'http://127.0.0.1:' + str(server.server_port), cwd=tmp_path, interpreter=python)
        credential = root / '.env/credentials/synthetic.key'
        credential.write_text('synthetic-credential')
        credential.chmod(0o600)
        wrapper = root / 'bin/channels'
        def command(*args):
            return subprocess.run([str(wrapper), '--profile', 'synthetic', *args], cwd='/',
                                  input='{}', text=True, capture_output=True, timeout=15)
        status = command('status')
        opened = command('open')
        waiting = command('hook', '--timeout', '.05')
        waited = json.loads((root / 'state/synthetic/last-hook.json').read_text())['state']
        peer.append({'id': '2-0', 'agent_id': 'peer', 'conversation_id': 'main', 'text': 'synthetic peer', 'reply_to': None})
        received = command('hook', '--timeout', '1')
        received_state = json.loads((root / 'state/synthetic/last-hook.json').read_text())['state']
        closed = command('close')
        absent = command('hook')
        assert (install.returncode, status.returncode, json.loads(status.stdout)['identity']['agent_id'],
                opened.returncode, json.loads(waiting.stdout)['decision'], waited,
                json.loads(received.stdout)['decision'], received_state, closed.returncode, absent.stdout,
                set(authorization for _, authorization in requests)) == (
            0, 0, 'synthetic-agent', 0, 'block', 'wait', 'block', 'peer', 0, '{}\n', {'Bearer synthetic-credential'})
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize('client,relative', [('codex', '.codex/hooks.json'), ('claude', '.claude/settings.local.json')])
def test_native_scope_rejects_conflict_and_explicitly_rebinds(artifact, tmp_path, client, relative):
    root = tmp_path / 'selected root'
    foreign = tmp_path / 'foreign/bin/channels'
    foreign.parent.mkdir(parents=True)
    foreign.write_text('#!/bin/sh\necho unrelated\n')
    hook = tmp_path / relative
    hook.parent.mkdir()
    hook.write_text(json.dumps({'hooks': {'Stop': [{'hooks': [
        {'type': 'command', 'command': str(foreign) + ' hook'}]}]}}))
    arguments = ['--target', root, '--client', client, '--project', tmp_path,
                 '--url', 'http://localhost:18765']
    first = invoke(artifact, 'install', *arguments, '--profile', 'first', cwd=tmp_path)
    before = hook.read_bytes()
    conflict = invoke(artifact, 'install', *arguments, '--profile', 'second', cwd=tmp_path)
    unchanged = hook.read_bytes() == before and not (root / 'state/second').exists()
    plan = invoke(artifact, 'plan', *arguments, '--profile', 'second', '--replace', cwd=tmp_path)
    still_unchanged = hook.read_bytes() == before
    rebind = invoke(artifact, 'install', *arguments, '--profile', 'second', '--replace', cwd=tmp_path)
    commands = [item['command'] for entry in json.loads(hook.read_text())['hooks']['Stop'] for item in entry['hooks']]
    import shlex
    assert (first.returncode, conflict.returncode, unchanged, plan.returncode, still_unchanged,
            json.loads(plan.stdout)['previous_bindings'], rebind.returncode, commands) == (
        0, 1, True, 0, True, [{'root': str(root), 'profile': 'first'}], 0,
        [str(foreign) + ' hook', shlex.join([str(root / 'bin/channels'), '--profile', 'second', 'hook'])])


@pytest.mark.parametrize('candidate', ['channels hook', 'channels --profile old hook',
                                      '/unreadable-example/bin/channels --profile old hook',
                                      'env /unreadable-example/bin/channels --profile old hook'])
@pytest.mark.parametrize('replace', [False, True])
def test_uncertain_receivers_preserved_without_product_writes(artifact, tmp_path, candidate, replace):
    root = tmp_path / 'root'
    hook = tmp_path / '.claude/settings.local.json'
    hook.parent.mkdir()
    original = json.dumps({'hooks': {'Stop': [{'hooks': [{'type': 'command', 'command': candidate}]}]}})
    hook.write_text(original)
    result = invoke(artifact, 'install', '--target', root, '--profile', 'new', '--client', 'claude',
                    '--project', tmp_path, '--url', 'http://localhost:18765',
                    *(['--replace'] if replace else []), cwd=tmp_path)
    assert (result.returncode, hook.read_text(), (root / 'config.json').exists(),
            (root / 'bin/channels').exists(), (root / 'state').exists()) == (1, original, False, False, False)


@pytest.mark.parametrize('interpreter', [sys.executable, '/usr/bin/python3.10'])
def test_canonical_alias_and_other_root_ownership(artifact, tmp_path, interpreter):
    root = tmp_path / 'root'
    project = tmp_path / 'project'
    arguments = ['--profile', 'one', '--client', 'codex', '--project', project,
                 '--url', 'http://localhost:18765']
    first = invoke(artifact, 'install', '--target', root, *arguments, cwd=tmp_path, interpreter=interpreter)
    (root / 'extra').mkdir()
    hook = project / '.codex/hooks.json'
    before = hook.read_bytes()
    alias = invoke(artifact, 'install', '--target', root / 'extra/..', *arguments, cwd=tmp_path, interpreter=interpreter)
    other = tmp_path / 'elsewhere/root'
    conflict = invoke(artifact, 'install', '--target', other, *arguments, cwd=tmp_path, interpreter=interpreter)
    assert (first.returncode, alias.returncode, conflict.returncode, hook.read_bytes(),
            (other / 'config.json').exists()) == (0, 0, 1, before, False)


@pytest.mark.parametrize('scope', ['root', 'hook'])
def test_installer_waits_for_persistent_lock(artifact, tmp_path, scope):
    import fcntl
    root = tmp_path / 'root'
    project = tmp_path / 'project'
    lock = root / '.install.lock' if scope == 'root' else project / '.claude/settings.local.json.channels.lock'
    lock.parent.mkdir(parents=True)
    with lock.open('w') as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        process = subprocess.Popen([sys.executable, str(artifact), 'install', '--target', str(root),
                                    '--profile', 'one', '--client', 'claude', '--project', str(project),
                                    '--url', 'http://localhost:18765'], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            time.sleep(.3)
            blocked = process.poll() is None and not (root / 'config.json').exists()
            fcntl.flock(held, fcntl.LOCK_UN)
            process.communicate(timeout=15)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()
    assert (blocked, process.returncode, lock.exists(), lock.read_bytes(), lock.is_symlink()) == (True, 0, True, b'', False)


@pytest.mark.parametrize('different_url', [False, True])
def test_concurrent_native_installs_keep_one_binding(artifact, tmp_path, different_url):
    import concurrent.futures
    root = tmp_path / 'root'
    project = tmp_path / 'project'
    def install(index):
        return invoke(artifact, 'install', '--target', root, '--profile', ['alpha', 'beta'][index],
                      '--client', 'claude', '--project', project,
                      '--url', 'http://localhost:' + str(18765 + (index if different_url else 0)), cwd=tmp_path)
    with concurrent.futures.ThreadPoolExecutor() as pool:
        results = list(pool.map(install, [0, 1]))
    hook = json.loads((project / '.claude/settings.local.json').read_text())
    commands = [item['command'] for entry in hook['hooks']['Stop'] for item in entry['hooks']]
    winner = next(index for index, result in enumerate(results) if result.returncode == 0)
    assert (sorted(result.returncode for result in results), len(commands),
            '--profile ' + ['alpha', 'beta'][winner] + ' hook' in commands[0],
            json.loads((root / 'config.json').read_text())['url']) == (
        [0, 1], 1, True, 'http://localhost:' + str(18765 + (winner if different_url else 0)))


@pytest.mark.parametrize('content,profile,code', [
    (None, 'test', 'config_missing'),
    ('private-input', 'test', 'config_json_invalid'),
    ('{"url":"http://localhost","private-input":true}', 'test', 'config_schema_invalid'),
    ('{"url":"http://user:private-input@localhost"}', 'test', 'config_url_invalid'),
    ('{"url":"http://[private-input"}', 'test', 'config_url_invalid'),
    ('{"url":"http://localhost:private-input"}', 'test', 'config_url_invalid'),
    ('{"url":"http://localhost"}', '../private-input', 'config_profile_invalid'),
])
@pytest.mark.parametrize('command', ['status', 'hook'])
def test_config_error_codes_are_fixed_and_private_values_never_print(artifact, tmp_path, content, profile, code, command):
    config = tmp_path / 'private-input.json'
    if content is not None:
        config.write_text(content)
    result = invoke(artifact, '--config', config, '--profile', profile, command,
                    cwd=tmp_path, interpreter='/usr/bin/python3.10', stdin='private-input')
    prefix = 'channel hook configuration failed: ' if command == 'hook' else 'channel configuration failed: '
    suffix = '; allowing stop\n' if command == 'hook' else '\n'
    records = list((tmp_path / 'state').rglob('last-hook.json')) if (tmp_path / 'state').exists() else []
    recorded = [json.loads(path.read_text()) for path in records]
    assert (result.returncode, result.stdout, result.stderr, 'private-input' in json.dumps(recorded),
            all(set(item) == {'timestamp', 'state'} and item['state'] == 'config_error' for item in recorded)) == (
        0 if command == 'hook' else 1, '{}\n' if command == 'hook' else '', prefix + code + suffix, False, True)


@pytest.mark.parametrize('command', ['status', 'hook'])
def test_nonconfig_private_failures_are_distinct(tmp_path, monkeypatch, capsys, command):
    config = tmp_path / 'config.json'
    config.write_text('{"url":"http://localhost"}')
    monkeypatch.setattr(sys, 'argv', ['channels', '--config', str(config), '--profile', 'test', command])
    def fail(*args):
        raise ValueError('private-input credential state transport')
    monkeypatch.setattr(cli, 'read_secret', fail)
    monkeypatch.setattr(exchange, 'has_open_state', lambda *args: True)
    if command == 'status':
        with pytest.raises(SystemExit, match='1'):
            cli.main()
    if command == 'hook':
        cli.main()
    captured = capsys.readouterr()
    assert ('private-input' in captured.out + captured.err, 'config_' in captured.err,
            'channel client failed:' in captured.err if command == 'status' else
            json.loads((tmp_path / 'state/test/last-hook.json').read_text())['state'] == 'error') == (False, False, True)


@pytest.mark.parametrize('scope', ['root', 'hook'])
@pytest.mark.parametrize('kind', ['symlink', 'nonempty'])
def test_invalid_lock_preserves_destination_and_refuses(artifact, tmp_path, scope, kind):
    root = tmp_path / 'root'
    project = tmp_path / 'project'
    lock = root / '.install.lock' if scope == 'root' else project / '.claude/settings.local.json.channels.lock'
    lock.parent.mkdir(parents=True)
    destination = tmp_path / 'owned-sentinel'
    destination.write_text('preserve')
    if kind == 'symlink':
        lock.symlink_to(destination)
    if kind == 'nonempty':
        lock.write_text('preserve')
    result = invoke(artifact, 'install', '--target', root, '--profile', 'one', '--client', 'claude',
                    '--project', project, '--url', 'http://localhost:18765', cwd=tmp_path)
    assert (result.returncode, (root / 'config.json').exists(), destination.read_text(),
            lock.read_text()) == (1, False, 'preserve', 'preserve')


@pytest.mark.parametrize('client', ['nohook', 'claude'])
def test_shared_root_concurrent_url_conflict(artifact, tmp_path, client):
    import concurrent.futures
    root = tmp_path / 'root'
    def install(index):
        return invoke(artifact, 'install', '--target', root, '--profile', ['alpha', 'beta'][index],
                      '--client', client, '--project', tmp_path / ('project-' + str(index)),
                      '--url', 'http://localhost:' + str(18765 + index), cwd=tmp_path)
    with concurrent.futures.ThreadPoolExecutor() as pool:
        results = list(pool.map(install, [0, 1]))
    winner = next(index for index, result in enumerate(results) if result.returncode == 0)
    assert (sorted(result.returncode for result in results),
            json.loads((root / 'config.json').read_text())['url'],
            (tmp_path / ('project-' + str(1 - winner)) / '.claude/settings.local.json').exists()) == (
        [0, 1], 'http://localhost:' + str(18765 + winner), False)


def test_conflict_preserves_every_existing_product_file(artifact, tmp_path):
    root = tmp_path / 'root'
    arguments = ['--target', root, '--client', 'claude', '--project', tmp_path,
                 '--url', 'http://localhost:18765']
    first = invoke(artifact, 'install', *arguments, '--profile', 'alpha', cwd=tmp_path)
    credential = root / '.env/credentials/alpha.key'
    credential.write_text('isolated-synthetic-value')
    exchange = root / 'state/alpha/exchange.json'
    exchange.write_text('{"synthetic":"preserve"}')
    before = {str(path.relative_to(tmp_path)): path.read_bytes() for path in tmp_path.rglob('*') if path.is_file()}
    refused = invoke(artifact, 'install', *arguments, '--profile', 'beta', cwd=tmp_path)
    after = {str(path.relative_to(tmp_path)): path.read_bytes() for path in tmp_path.rglob('*') if path.is_file()}
    assert (first.returncode, refused.returncode, after) == (0, 1, before)


def test_parallel_same_binding_and_independent_adapters(artifact, tmp_path):
    import concurrent.futures
    root = tmp_path / 'root'
    def install(client):
        return invoke(artifact, 'install', '--target', root, '--profile', 'alpha' if client == 'claude' else 'beta',
                      '--client', client, '--project', tmp_path, '--url', 'http://localhost:18765', cwd=tmp_path)
    with concurrent.futures.ThreadPoolExecutor() as pool:
        same = list(pool.map(install, ['claude', 'claude']))
        independent = list(pool.map(install, ['claude', 'codex']))
    documents = [json.loads((tmp_path / relative).read_text()) for relative in ['.claude/settings.local.json', '.codex/hooks.json']]
    commands = [[item['command'] for entry in document['hooks']['Stop'] for item in entry['hooks']] for document in documents]
    assert ([result.returncode for result in same + independent], [len(items) for items in commands],
            '--profile alpha hook' in commands[0][0], '--profile beta hook' in commands[1][0]) == ([0, 0, 0, 0], [1, 1], True, True)
