"""Synthetic bundled installer lifecycle and preservation checks."""
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import runpy
import shlex
import subprocess
import sys
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope='module')
def artifact(tmp_path_factory):
    path = tmp_path_factory.mktemp('bundle') / 'harness.pyz'
    runpy.run_path(str(ROOT / 'scripts/build_harness.py'))['build'](path, ROOT)
    return path


def invoke(artifact, *arguments, cwd, input_text=None):
    environment = {key: value for key, value in os.environ.items() if not key.startswith(('CHANNEL_', 'PYTHON'))}
    return subprocess.run([sys.executable, str(artifact), *map(str, arguments)], cwd=cwd,
                          env=environment, input=input_text, text=True, capture_output=True, timeout=15)


def args(tmp_path, client='claude', project=None):
    return ['--target', tmp_path / 'root', '--client', client, '--project', project or tmp_path,
            '--url', 'http://localhost:18765']


def commands(path):
    return [hook for row in json.loads(path.read_text())['hooks']['Stop'] for hook in row['hooks']]


def test_read_only_idempotent_secure_key_and_uninstall(artifact, tmp_path):
    source = tmp_path / 'private/key'
    source.parent.mkdir(mode=0o700)
    source.write_text('synthetic-key\n')
    source.chmod(0o600)
    arguments = args(tmp_path) + ['--key-file', source]
    plan = invoke(artifact, 'plan', *arguments, cwd=tmp_path)
    assert (plan.returncode, (tmp_path / 'root').exists()) == (0, False)
    hook = tmp_path / '.claude/settings.local.json'
    hook.parent.mkdir()
    hook.write_text(json.dumps({'keep': True, 'hooks': {'Stop': [{'hooks': [{'command': 'other'}]}]}}))
    first = invoke(artifact, 'install', *arguments, cwd=tmp_path)
    before = hook.read_bytes()
    second = invoke(artifact, 'install', *arguments, cwd=tmp_path)
    assert (first.returncode, second.returncode, hook.read_bytes(), commands(hook)[-1]['timeout']) == (0, 0, before, 660)
    root = tmp_path / 'root'
    (root / 'unknown').write_text('preserve')
    (root / 'config.json').write_text('{"url":"http://changed.test"}')
    removed = invoke(artifact, 'uninstall', '--target', root, '--project', tmp_path, cwd=tmp_path)
    assert (removed.returncode, (root / 'bin/channels').exists(), (root / '.env/channel.key').read_bytes(),
            source.read_bytes(), (root / 'unknown').read_text(), commands(hook), json.loads(hook.read_text())['keep']) == (
        0, False, source.read_bytes(), b'synthetic-key\n', 'preserve', [{'command': 'other'}], True)
    purge = subprocess.run(shlex.split(json.loads(removed.stdout)['purge_key_command']),
                           cwd=tmp_path, text=True, capture_output=True, timeout=15)
    assert (purge.returncode, (root / '.env/channel.key').exists(), source.exists()) == (0, False, True)


@pytest.mark.parametrize('settings', ['permissions', 'empty_hooks', 'other_root'])
def test_uninstall_unregistered_project_preserves_settings_bytes(artifact, tmp_path, settings):
    installed = invoke(artifact, 'install', *args(tmp_path, project=tmp_path / 'registered'), cwd=tmp_path)
    assert installed.returncode == 0, installed.stderr
    registered_hook = tmp_path / 'registered/.claude/settings.local.json'
    registered_before = registered_hook.read_bytes()
    body = b'{"permissions":{"allow":["Read"]}}\n'
    if settings == 'empty_hooks':
        body = b'{ "hooks": { "Stop": [] }, "permissions": {} }\n'
    if settings == 'other_root':
        other = tmp_path / 'other'
        other.mkdir()
        installed = invoke(artifact, 'install', *args(other), cwd=tmp_path)
        assert installed.returncode == 0, installed.stderr
        body = (other / '.claude/settings.local.json').read_bytes()
    hook = tmp_path / 'unregistered/.claude/settings.local.json'
    hook.parent.mkdir(parents=True)
    hook.write_bytes(body)
    result = invoke(artifact, 'uninstall', '--target', tmp_path / 'root', '--client', 'claude',
                    '--project', hook.parent.parent, cwd=tmp_path)
    assert (result.returncode, hook.read_bytes(), registered_hook.read_bytes(),
            json.loads(result.stdout)['callbacks_removed']) == (0, body, registered_before, [])


def test_multiple_projects_and_adapters(artifact, tmp_path):
    projects = [tmp_path / 'one', tmp_path / 'two']
    for project in projects:
        for client in ['claude', 'codex']:
            result = invoke(artifact, 'install', *args(tmp_path, client, project), cwd=tmp_path)
            assert result.returncode == 0, result.stderr
    root = tmp_path / 'root'
    first = invoke(artifact, 'uninstall', '--target', root, '--project', projects[0], cwd=tmp_path)
    assert (first.returncode, (root / 'bin/channels').exists(), len(commands(projects[1] / '.codex/hooks.json'))) == (0, True, 1)
    second = invoke(artifact, 'uninstall', '--target', root, '--project', projects[1], cwd=tmp_path)
    assert (second.returncode, (root / 'bin/channels').exists()) == (0, False)


@pytest.mark.parametrize('candidate', ['channels hook', '/missing/bin/channels hook', 'env /missing/bin/channels hook'])
def test_unknown_receivers_fail_without_product_writes(artifact, tmp_path, candidate):
    hook = tmp_path / '.claude/settings.local.json'
    hook.parent.mkdir()
    hook.write_text(json.dumps({'hooks': {'Stop': [{'hooks': [{'command': candidate}]}]}}))
    before = hook.read_bytes()
    result = invoke(artifact, 'install', *args(tmp_path), '--replace', cwd=tmp_path)
    assert (result.returncode, hook.read_bytes(), (tmp_path / 'root/bin/channels').exists()) == (1, before, False)


def test_conflicting_key_never_overwrites_or_prints(artifact, tmp_path):
    source = tmp_path / 'private/key'
    source.parent.mkdir(mode=0o700)
    source.write_text('synthetic-one')
    source.chmod(0o600)
    first = invoke(artifact, 'install', *args(tmp_path), '--key-file', source, cwd=tmp_path)
    source.write_text('synthetic-two')
    result = invoke(artifact, 'install', *args(tmp_path), '--key-file', source, cwd=tmp_path)
    assert (first.returncode, result.returncode, 'synthetic-' in result.stdout + result.stderr,
            (tmp_path / 'root/.env/channel.key').read_text()) == (0, 1, False, 'synthetic-one')


@pytest.mark.parametrize('kind', ['symlink', 'nonempty'])
def test_invalid_locks_preserved(artifact, tmp_path, kind):
    root = tmp_path / 'root'
    root.mkdir()
    lock = root / '.install.lock'
    sentinel = tmp_path / 'sentinel'
    sentinel.write_text('preserve')
    if kind == 'symlink':
        lock.symlink_to(sentinel)
    if kind == 'nonempty':
        lock.write_text('preserve')
    result = invoke(artifact, 'install', *args(tmp_path), cwd=tmp_path)
    assert (result.returncode, sentinel.read_text(), lock.read_text(), (root / 'config.json').exists()) == (1, 'preserve', 'preserve', False)


def test_concurrent_same_dispatcher(artifact, tmp_path):
    with concurrent.futures.ThreadPoolExecutor() as pool:
        results = list(pool.map(lambda _: invoke(artifact, 'install', *args(tmp_path), cwd=tmp_path), range(2)))
    assert ([item.returncode for item in results], len(commands(tmp_path / '.claude/settings.local.json'))) == ([0, 0], 1)


def test_legacy_replace_and_conservative_uninstall(artifact, tmp_path):
    from polyagent_channels.setup import wrapper_content
    root = tmp_path / 'root'
    wrapper = root / 'bin/channels'
    wrapper.parent.mkdir(parents=True)
    wrapper.write_bytes(wrapper_content(Path(sys.executable), root))
    (root / 'harness.pyz').write_bytes(artifact.read_bytes())
    hook = tmp_path / '.claude/settings.local.json'
    hook.parent.mkdir()
    old = {'type': 'command', 'command': shlex.join([str(wrapper), '--profile', 'old', 'hook'])}
    hook.write_text(json.dumps({'hooks': {'Stop': [{'hooks': [old]}]}}))
    refused = invoke(artifact, 'plan', *args(tmp_path), cwd=tmp_path)
    replaced = invoke(artifact, 'install', *args(tmp_path), '--replace', cwd=tmp_path)
    assert (refused.returncode, replaced.returncode, commands(hook)[0]['command']) == (
        1, 0, shlex.join([str(wrapper), '--client', 'claude', 'hook']))
    (root / 'install-manifest.json').unlink()
    removed = invoke(artifact, 'uninstall', '--target', root, '--project', tmp_path, cwd=tmp_path)
    assert (removed.returncode, commands(hook), wrapper.exists()) == (0, [], True)


def test_bundle_is_client_only(artifact):
    with zipfile.ZipFile(artifact) as archive:
        names = archive.namelist()
        entry = archive.read('__main__.py').decode()
    assert ('uninstall' in entry, any(name.startswith(('redis/', 'starlette/', 'mcp/')) for name in names),
            any(name.endswith(('.pyc', '.so')) for name in names)) == (True, False, False)


def test_uninstall_only_valid_generated_session_state(artifact, tmp_path):
    from polyagent_channels import exchange
    result = invoke(artifact, 'install', *args(tmp_path), cwd=tmp_path)
    root = tmp_path / 'root'
    state = exchange.new_state('synthetic-session',
                               {'channel': 'general', 'conversation_id': 'main', 'cursor': '0-0'}, 60)
    state['deadline'] -= 120
    exchange.open_exchange(root / 'state', 'synthetic-session', state)
    state_path = exchange.owned_state(root / 'state', 'synthetic-session', state)
    unknown = root / 'state/manual'
    unknown.mkdir()
    (unknown / 'exchange.json').write_text('preserve')
    removed = invoke(artifact, 'uninstall', '--target', root, '--project', tmp_path, cwd=tmp_path)
    assert (result.returncode, removed.returncode, state_path.exists(), (unknown / 'exchange.json').read_text()) == (0, 0, False, 'preserve')


def test_flock_serializes_installer(artifact, tmp_path):
    import fcntl
    import time

    root = tmp_path / 'root'
    root.mkdir()
    lock = root / '.install.lock'
    with lock.open('w') as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        process = subprocess.Popen([sys.executable, str(artifact), 'install', *map(str, args(tmp_path))],
                                   cwd=tmp_path, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            time.sleep(.3)
            blocked = process.poll() is None and not (root / 'config.json').exists()
            fcntl.flock(held, fcntl.LOCK_UN)
            process.communicate(timeout=15)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()
    assert (blocked, process.returncode, lock.read_bytes()) == (True, 0, b'')


def test_alias_normalization_and_same_basename_preservation(artifact, tmp_path):
    root = tmp_path / 'root'
    foreign = tmp_path / 'foreign/bin/channels'
    foreign.parent.mkdir(parents=True)
    foreign.write_text('#!/bin/sh\necho unrelated\n')
    hook = tmp_path / '.claude/settings.local.json'
    hook.parent.mkdir()
    unrelated = {'type': 'command', 'command': shlex.join([str(foreign), 'hook'])}
    hook.write_text(json.dumps({'hooks': {'Stop': [{'hooks': [unrelated]}]}}))
    first = invoke(artifact, 'install', *args(tmp_path), cwd=tmp_path)
    (root / 'extra').mkdir()
    arguments = args(tmp_path)
    arguments[1] = root / 'extra/..'
    alias = invoke(artifact, 'install', *arguments, cwd=tmp_path)
    assert (first.returncode, alias.returncode, commands(hook)[0], len(commands(hook))) == (0, 0, unrelated, 2)


def test_python310_bundle_installed_wrapper_and_uninstall(artifact, tmp_path):
    python = Path('/usr/bin/python3.10')
    if not python.exists():
        pytest.skip('system Python 3.10 unavailable')
    installed = subprocess.run([str(python), '-I', str(artifact), 'install', *map(str, args(tmp_path))],
                               cwd=tmp_path, capture_output=True, text=True, timeout=15)
    wrapper = tmp_path / 'root/bin/channels'
    hook = subprocess.run([str(wrapper), '--client', 'claude', 'hook'], cwd='/',
                          input='{"session_id":"synthetic-session"}', capture_output=True, text=True, timeout=15)
    removed = subprocess.run([str(python), '-I', str(artifact), 'uninstall', '--target', str(tmp_path / 'root'),
                             '--project', str(tmp_path)], cwd=tmp_path, capture_output=True, text=True, timeout=15)
    assert (installed.returncode, hook.returncode, hook.stdout, removed.returncode, wrapper.exists()) == (0, 0, '{}\n', 0, False)


def test_closed_diagnostic_cleanup_and_concise_kept_output(artifact, tmp_path):
    from polyagent_channels import exchange
    from polyagent_channels.cli import record_hook
    from argparse import Namespace

    installed = invoke(artifact, 'install', *args(tmp_path), cwd=tmp_path)
    root = tmp_path / 'root'
    session_id = 'closed-synthetic'
    state = exchange.new_state(session_id, {'channel': 'general', 'conversation_id': 'main', 'cursor': '0-0'}, 60)
    exchange.open_exchange(root / 'state', session_id, state)
    exchange.close_exchange(root / 'state', session_id, state)
    record_hook(Namespace(session_id=session_id, state_dir=root / 'state'), 'peer', 'callback-1')
    directory = exchange.session_directory(root / 'state', session_id)
    inode = (directory / 'exchange.lock').stat().st_ino
    manual = root / 'manual-venv/lib'
    manual.mkdir(parents=True)
    for number in range(1400):
        (manual / str(number)).touch()
    key = root / '.env/channel.key'
    key.write_text('synthetic-protected-key')
    result = invoke(artifact, 'uninstall', '--target', root, '--project', tmp_path, cwd=tmp_path)
    output = json.loads(result.stdout)
    assert (installed.returncode, result.returncode, (directory / 'last-hook.json').exists(),
            (directory / 'exchange.lock').stat().st_ino, (root / 'harness.pyz').exists(),
            len(output['kept']) < 10, str(key) in output['kept'], '--purge-key' in output['purge_key_command'],
            len(output['callbacks_removed']), 'synthetic-protected-key' in result.stdout) == (0, 0, False, inode, False, True, True, True, 1, False)


def test_purge_shared_key_refused_before_callback_changes(artifact, tmp_path):
    projects = [tmp_path / 'one', tmp_path / 'two']
    for project in projects:
        result = invoke(artifact, 'install', *args(tmp_path, project=project), cwd=tmp_path)
        assert result.returncode == 0
    root = tmp_path / 'root'
    hook = projects[0] / '.claude/settings.local.json'
    before = hook.read_bytes(), (root / 'install-manifest.json').read_bytes()
    result = invoke(artifact, 'uninstall', '--target', root, '--project', projects[0], '--purge-key', cwd=tmp_path)
    assert (result.returncode, hook.read_bytes(), (root / 'install-manifest.json').read_bytes()) == (1, *before)


def test_live_exchange_refuses_uninstall_before_mutation(artifact, tmp_path):
    from polyagent_channels import exchange
    invoke(artifact, 'install', *args(tmp_path), cwd=tmp_path)
    root = tmp_path / 'root'
    state = exchange.new_state('live-synthetic', {'channel': 'general', 'conversation_id': 'main', 'cursor': '0-0'}, 60)
    exchange.open_exchange(root / 'state', 'live-synthetic', state)
    hook = tmp_path / '.claude/settings.local.json'
    before = hook.read_bytes()
    result = invoke(artifact, 'uninstall', '--target', root, '--project', tmp_path, cwd=tmp_path)
    assert (result.returncode, hook.read_bytes(), (root / 'harness.pyz').exists(), 'Live exchange' in result.stderr) == (1, before, True, True)


def test_help_uses_request_origin_and_quotes_host_data(monkeypatch):
    from polyagent_channels import harness
    from polyagent_channels.server import help_page
    from starlette.requests import Request
    import asyncio

    monkeypatch.setattr(harness, 'artifact_bytes', lambda: b'synthetic-artifact')
    request = Request({'type': 'http', 'scheme': 'https', 'path': '/help', 'root_path': '',
                       'headers': [(b'host', b'channels.example:2345')], 'server': ('localhost', 80)})
    response = asyncio.run(help_page(request))
    malicious = 'http://host$(id)'
    assert ('SERVICE=https://channels.example:2345' in response.body.decode(),
            'SERVICE=' + shlex.quote(malicious) in harness.setup_help(malicious),
            harness.setup_help('http://host\nwhoami')) == (True, True, None)


def test_busy_closed_session_lock_refuses_uninstall(artifact, tmp_path):
    import fcntl
    from polyagent_channels import exchange
    invoke(artifact, 'install', *args(tmp_path), cwd=tmp_path)
    root = tmp_path / 'root'
    directory = exchange.session_directory(root / 'state', 'closed-busy')
    directory.mkdir(parents=True)
    with (directory / 'exchange.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        inode = os.fstat(lock.fileno()).st_ino
        result = invoke(artifact, 'uninstall', '--target', root, '--project', tmp_path, cwd=tmp_path)
        assert (result.returncode, (directory / 'exchange.lock').stat().st_ino,
                (root / 'harness.pyz').exists()) == (1, inode, True)


def test_active_installed_hook_reuses_installation_and_refuses_changes(artifact, tmp_path):
    import fcntl
    import time

    key = tmp_path / 'private/key'
    key.parent.mkdir(mode=0o700)
    key.write_text('synthetic-installed-key')
    key.chmod(0o600)
    arguments = args(tmp_path) + ['--key-file', key]
    assert invoke(artifact, 'install', *arguments, cwd=tmp_path).returncode == 0
    root = tmp_path / 'root'
    hook = tmp_path / '.claude/settings.local.json'
    before = hook.read_bytes()
    state = root / 'state/retained'
    state.parent.mkdir()
    state.write_text('session state must remain untouched')
    preserved = {path: (path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns)
                 for path in [hook, *root.rglob('*')] if path.is_file()}
    process = subprocess.Popen([sys.executable, str(root / 'harness.pyz'), '--config',
                                str(root / 'config.json'), 'hook'], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        with (root / '.lifecycle.lock').open('r') as lock:
            deadline = time.monotonic() + 5
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(lock, fcntl.LOCK_UN)
                except BlockingIOError:
                    break
                if time.monotonic() >= deadline:
                    pytest.fail('Hook did not acquire lifecycle lock')
                time.sleep(.01)
        reused = invoke(artifact, 'install', *arguments, cwd=tmp_path)
        assert reused.returncode == 0 and json.loads(reused.stdout)['status'] == 'already_installed'
        assert process.poll() is None
        assert {path: (path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns)
                for path in preserved} == preserved
        other_archive = tmp_path / 'other.pyz'
        other_archive.write_bytes(artifact.read_bytes())
        with zipfile.ZipFile(other_archive, 'a') as archive:
            archive.writestr('synthetic-release', 'different')
        other_key = key.with_name('other-key')
        other_key.write_text('synthetic-other-key')
        other_key.chmod(0o600)
        changes = [(other_archive, 'install', arguments),
                   (artifact, 'install', arguments + ['--url', 'http://localhost:18766']),
                   (artifact, 'install', arguments + ['--key-file', other_key]),
                   (artifact, 'install', arguments + ['--project', tmp_path / 'new-project']),
                   (artifact, 'uninstall', ['--target', root, '--project', tmp_path])]
        for candidate, action, changed in changes:
            result = invoke(candidate, action, *changed, cwd=tmp_path)
            assert (result.returncode, hook.read_bytes(), 'Installation in use' in result.stderr) == (1, before, True)
        assert {path: (path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns)
                for path in preserved} == preserved
        output, errors = process.communicate('{"session_id":"synthetic-hook"}', timeout=5)
        assert (process.returncode, json.loads(output), errors) == (0, {}, '')
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)


def test_shared_lifecycle_users_and_removed_config(artifact, tmp_path):
    from polyagent_channels.setup import lifecycle_lock

    assert invoke(artifact, 'install', *args(tmp_path), cwd=tmp_path).returncode == 0
    root = tmp_path / 'root'
    lock = root / '.lifecycle.lock'
    inode = lock.stat().st_ino
    with lifecycle_lock(root):
        # A second configured CLI can finish while the first shared user remains.
        result = invoke(artifact, '--config', root / 'config.json', 'hook', cwd=tmp_path,
                        input_text='{"session_id":"synthetic-after-removal"}')
        assert 'Installation in use' not in result.stderr
        refused = invoke(artifact, 'uninstall', '--target', root, '--project', tmp_path, cwd=tmp_path)
        assert refused.returncode == 1 and 'Installation in use' in refused.stderr
    removed = invoke(artifact, 'uninstall', '--target', root, '--project', tmp_path, cwd=tmp_path)
    assert removed.returncode == 0 and str(lock) in json.loads(removed.stdout)['kept']
    remaining = sorted(str(path.relative_to(root)) for path in root.rglob('*'))
    # Start the original artifact after removal, as a process already loading it could.
    result = invoke(artifact, '--config', root / 'config.json', 'hook', cwd=tmp_path,
                        input_text='{"session_id":"synthetic-after-removal"}')
    assert sorted(str(path.relative_to(root)) for path in root.rglob('*')) == remaining
    assert lock.stat().st_ino == inode


def test_expired_pending_topic_refuses_uninstall_and_preserves_unknown(artifact, tmp_path):
    from polyagent_channels import exchange
    invoke(artifact, 'install', *args(tmp_path), cwd=tmp_path)
    root = tmp_path / 'root'
    topic = exchange.new_state('pending-synthetic',
                               {'channel': 'general', 'conversation_id': 'work', 'cursor': '0-0'}, 60)
    topic.update(deadline=1, pending='3-0')
    exchange.open_exchange(root / 'state', 'pending-synthetic', topic)
    path = exchange.owned_state(root / 'state', 'pending-synthetic', topic)
    unknown = path.parent / 'topic-manual.json'
    unknown.write_text('operator data')
    callback = tmp_path / '.claude/settings.local.json'
    before = callback.read_bytes()
    refused = invoke(artifact, 'uninstall', '--target', root, '--project', tmp_path, cwd=tmp_path)
    preserved = callback.read_bytes() == before and path.exists()
    exchange.ack_exchange(root / 'state', 'pending-synthetic', topic, '3-0')
    removed = invoke(artifact, 'uninstall', '--target', root, '--project', tmp_path, cwd=tmp_path)
    assert (refused.returncode == 1 and 'pending work' in refused.stderr and preserved
            and removed.returncode == 0 and not path.exists() and unknown.read_text() == 'operator data')


def test_installed_uninstall_emits_executable_key_only_removal(artifact, tmp_path):
    root = tmp_path / "root with 'quotes'"
    installed = invoke(artifact, 'install', '--target', root, '--client', 'claude',
                       '--project', tmp_path, '--url', 'http://localhost:18765', cwd=tmp_path)
    assert installed.returncode == 0
    key = root / '.env/channel.key'
    key.write_text('synthetic-key-never-print')
    unrelated = root / 'manual/retained'
    unrelated.parent.mkdir()
    unrelated.write_text('retain')
    result = invoke(root / 'harness.pyz', 'uninstall', '--target', root,
                    '--project', tmp_path, cwd=tmp_path)
    output = json.loads(result.stdout)
    assert result.returncode == 0 and not (root / 'harness.pyz').exists()
    purge = subprocess.run(shlex.split(output['purge_key_command']), cwd=tmp_path,
                           capture_output=True, text=True, timeout=15)
    assert (purge.returncode == 0 and not key.exists() and unrelated.read_text() == 'retain'
            and 'synthetic-key-never-print' not in result.stdout + result.stderr + purge.stdout + purge.stderr)
