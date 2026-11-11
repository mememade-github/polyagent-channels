"""Synthetic bundled installer lifecycle and preservation checks."""
import concurrent.futures
import fcntl
import json
import os
from pathlib import Path
import runpy
import shlex
import subprocess
import sys
import time
import zipfile

import pytest

from polyagent_channels import exchange
from polyagent_channels.setup import (CLAUDE_TOOL_RULE, CODEX_SETTING, SetupError, codex_block, finish_removal,
                                      hook_binding_status, installed_project, lifecycle_lock, wrapper_content)

ROOT = Path(__file__).resolve().parents[1]
FAKE_CLAUDE = """#!/usr/bin/env python3
import json, os, pathlib, sys
store = pathlib.Path(os.environ['FAKE_CLAUDE_STORE']) / (str(pathlib.Path.cwd()).replace('/', '_') + '.json')
arguments = sys.argv[1:]
if arguments[:2] == ['mcp', 'get']:
    if not store.exists():
        sys.exit(1)
    entry = json.loads(store.read_text())
    print('channels:\\n  Scope: ' + entry['scope'] + '\\n  Command: ' + entry['command'] + '\\n  Args: ' + ' '.join(entry['args']))
elif arguments[:2] == ['mcp', 'add']:
    separator = arguments.index('--')
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text(json.dumps({'scope': 'Local config', 'command': arguments[separator + 1], 'args': arguments[separator + 2:]}))
elif arguments[:2] == ['mcp', 'remove']:
    store.unlink()
else:
    sys.exit(2)
"""


@pytest.fixture(scope='module')
def artifact(tmp_path_factory):
    path = tmp_path_factory.mktemp('bundle') / 'harness.pyz'
    runpy.run_path(str(ROOT / 'scripts/build_harness.py'))['build'](path, ROOT)
    return path


@pytest.fixture(autouse=True)
def synthetic_claude(tmp_path_factory, monkeypatch):
    """Tests never reach the real client registry: the first claude on PATH records to a temp directory."""
    directory = tmp_path_factory.mktemp('fake-claude')
    executable = directory / 'claude'
    executable.write_text(FAKE_CLAUDE)
    executable.chmod(0o700)
    monkeypatch.setenv('PATH', str(directory) + os.pathsep + os.environ['PATH'])
    monkeypatch.setenv('FAKE_CLAUDE_STORE', str(directory / 'store'))
    return directory / 'store'


def registered(store):
    return sorted(store.iterdir()) if store.exists() else []


def invoke(artifact, *arguments, cwd, input_text=None):
    environment = {key: value for key, value in os.environ.items() if not key.startswith(('CHANNEL_', 'PYTHON'))}
    return subprocess.run([sys.executable, str(artifact), *map(str, arguments)], cwd=cwd,
                          env=environment, input=input_text, text=True, capture_output=True, timeout=30)


def project_directory(path):
    """Installation requires the project directory to exist already."""
    path.mkdir(parents=True, exist_ok=True)
    return path


def args(tmp_path, client='claude', project=None):
    return ['--target', tmp_path / 'root', '--client', client, '--project', project or tmp_path,
            '--url', 'http://localhost:18765']


def commands(path, event='Stop'):
    return [hook for row in json.loads(path.read_text())['hooks'][event] for hook in row['hooks']]


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
    assert (first.returncode, second.returncode, hook.read_bytes(), json.loads(second.stdout)['status']) == (0, 0, before, 'already_installed')
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


@pytest.mark.parametrize('client', ['claude', 'codex'])
def test_install_binds_the_client_with_named_tools_and_no_turn_hold(artifact, tmp_path, synthetic_claude, client):
    installed = invoke(artifact, 'install', *args(tmp_path, client), cwd=tmp_path)
    assert installed.returncode == 0, installed.stderr
    wrapper = tmp_path / 'root/bin/channels'
    if client == 'claude':
        document = json.loads((tmp_path / '.claude/settings.local.json').read_text())
        expected = {'type': 'command', 'command': shlex.join([str(wrapper), '--client', 'claude', 'hook']),
                    'asyncRewake': True, 'timeout': exchange.RECEPTION_TIMEOUT}
        assert (commands(tmp_path / '.claude/settings.local.json') == commands(tmp_path / '.claude/settings.local.json', 'SessionStart') == [expected]
                and document['permissions']['allow'] == [CLAUDE_TOOL_RULE] and len(registered(synthetic_claude)) == 1
                and not (tmp_path / '.codex').exists())
    if client == 'codex':
        assert ((tmp_path / '.codex/config.toml').read_text() == CODEX_SETTING + codex_block(wrapper)
                and not (tmp_path / '.codex/hooks.json').exists() and not (tmp_path / '.claude').exists())
    assert hook_binding_status(tmp_path / 'root', tmp_path, client)['status'] == 'installed'
    assert not list(tmp_path.glob('.channels*')) and not list(tmp_path.rglob('*.channels.lock'))


@pytest.mark.parametrize('client', ['claude', 'codex'])
def test_uninstall_removes_exactly_what_install_added_to_an_empty_project(artifact, tmp_path, synthetic_claude, client):
    project = tmp_path / 'project'
    project.mkdir()
    assert invoke(artifact, 'install', *args(tmp_path, client, project), cwd=tmp_path).returncode == 0
    removed = invoke(artifact, 'uninstall', '--target', tmp_path / 'root', '--project', project, cwd=tmp_path)
    output = json.loads(removed.stdout)
    assert (removed.returncode, list(project.iterdir()), registered(synthetic_claude), (tmp_path / 'root').exists(),
            output['kept'], len(output['bindings_removed'])) == (0, [], [], False, [], 1)


def failed_claude_install(artifact, tmp_path):
    """Install for Claude into a project whose .claude directory cannot be written, then make it writable again."""
    if os.geteuid() == 0:
        pytest.skip('root writes regardless of directory modes')
    project = project_directory(tmp_path / 'project')
    (project / '.claude').mkdir(mode=0o500)
    failed = invoke(artifact, 'install', *args(tmp_path, 'claude', project), cwd=tmp_path)
    (project / '.claude').chmod(0o700)
    return project, failed


def uninstall_project(artifact, tmp_path, project):
    return invoke(artifact, 'uninstall', '--target', tmp_path / 'root', '--project', project, cwd=tmp_path)


def test_failed_install_reports_failure(artifact, tmp_path):
    assert failed_claude_install(artifact, tmp_path)[1].returncode == 1


def test_uninstall_after_failed_install_removes_the_tool_registration(artifact, tmp_path, synthetic_claude):
    project, _ = failed_claude_install(artifact, tmp_path)
    uninstall_project(artifact, tmp_path, project)
    assert registered(synthetic_claude) == []


def test_uninstall_after_failed_install_removes_the_installation(artifact, tmp_path):
    project, _ = failed_claude_install(artifact, tmp_path)
    uninstall_project(artifact, tmp_path, project)
    assert not (tmp_path / 'root').exists()


def test_uninstall_after_failed_install_keeps_settings_the_user_wrote_later(artifact, tmp_path):
    project, _ = failed_claude_install(artifact, tmp_path)
    settings = project / '.claude/settings.local.json'
    settings.write_text(json.dumps({'permissions': {'allow': [CLAUDE_TOOL_RULE]}}))
    uninstall_project(artifact, tmp_path, project)
    assert json.loads(settings.read_text())['permissions']['allow'] == [CLAUDE_TOOL_RULE]


def test_install_after_a_failed_install_succeeds(artifact, tmp_path):
    project, _ = failed_claude_install(artifact, tmp_path)
    assert invoke(artifact, 'install', *args(tmp_path, 'claude', project), cwd=tmp_path).returncode == 0


@pytest.mark.parametrize('content', ['{not json', '[]', '{"hooks": []}', '{"hooks": {"Stop": "x"}}', '{"hooks": {"Stop": [1]}}'])
def test_codex_install_with_broken_legacy_hooks_writes_nothing(artifact, tmp_path, content):
    project = project_directory(tmp_path / 'project')
    (project / '.codex').mkdir()
    (project / '.codex/hooks.json').write_text(content)
    invoke(artifact, 'install', *args(tmp_path, 'codex', project), cwd=tmp_path)
    assert list((tmp_path / 'root').rglob('*')) == []


def test_uninstall_restores_existing_settings_content_and_keeps_user_rule(artifact, tmp_path):
    settings = tmp_path / '.claude/settings.local.json'
    settings.parent.mkdir()
    original = {'permissions': {'allow': ['Read', CLAUDE_TOOL_RULE]},
                'hooks': {'SessionStart': [{'matcher': 'startup', 'hooks': [{'type': 'command', 'command': 'echo unrelated', 'timeout': 3}]}],
                          'OtherEvent': []}}
    settings.write_text(json.dumps(original))
    assert invoke(artifact, 'install', *args(tmp_path), cwd=tmp_path).returncode == 0
    assert invoke(artifact, 'uninstall', '--target', tmp_path / 'root', '--project', tmp_path, cwd=tmp_path).returncode == 0
    assert json.loads(settings.read_text()) == original


def test_uninstall_keeps_a_user_setting_added_after_install(artifact, tmp_path):
    settings = tmp_path / '.claude/settings.local.json'
    assert invoke(artifact, 'install', *args(tmp_path), cwd=tmp_path).returncode == 0
    document = json.loads(settings.read_text())
    document['custom'] = True
    document['permissions']['allow'].append('Bash(ls:*)')
    settings.write_text(json.dumps(document))
    assert invoke(artifact, 'uninstall', '--target', tmp_path / 'root', '--project', tmp_path, cwd=tmp_path).returncode == 0
    assert json.loads(settings.read_text()) == {'permissions': {'allow': ['Bash(ls:*)']}, 'custom': True}


@pytest.mark.parametrize('original', ['model = "custom"\n', 'model = "no newline"'])
def test_codex_block_is_appended_and_removed_byte_exactly(artifact, tmp_path, original):
    config = tmp_path / '.codex/config.toml'
    config.parent.mkdir()
    config.write_text(original)
    config.chmod(0o644)
    assert invoke(artifact, 'install', *args(tmp_path, 'codex'), cwd=tmp_path).returncode == 0
    installed = config.read_text()
    assert invoke(artifact, 'uninstall', '--target', tmp_path / 'root', '--project', tmp_path, '--client', 'codex', cwd=tmp_path).returncode == 0
    assert (installed.startswith(CODEX_SETTING) and installed.endswith(codex_block(tmp_path / 'root/bin/channels'))
            and config.read_text() == original and config.stat().st_mode & 0o777 == 0o644)


FOREIGN_CODEX_SERVERS = ['[mcp_servers.channels]\ncommand = "other"\n', '[mcp_servers."channels".tools.publish]\nx = 1\n',
                         "[mcp_servers.'channels']\ncommand = 'other'\n", '[ mcp_servers . channels ]\ncommand = "other"\n',
                         'mcp_servers.channels.command = "other"\n', 'mcp_servers = { channels = { command = "other" } }\n']


def refused_foreign_codex_install(artifact, tmp_path, foreign):
    """Install for Codex into a project whose configuration already names another channels server."""
    config = tmp_path / '.codex/config.toml'
    config.parent.mkdir()
    config.write_text(foreign)
    return config, invoke(artifact, 'install', *args(tmp_path, 'codex'), cwd=tmp_path)


@pytest.mark.parametrize('foreign', FOREIGN_CODEX_SERVERS)
def test_foreign_codex_server_is_refused_and_preserved(artifact, tmp_path, foreign):
    config, refused = refused_foreign_codex_install(artifact, tmp_path, foreign)
    assert (refused.returncode, 'Another Codex channels server' in refused.stderr, config.read_text()) == (1, True, foreign)


@pytest.mark.parametrize('foreign', FOREIGN_CODEX_SERVERS)
def test_uninstall_after_a_refused_install_keeps_the_foreign_server(artifact, tmp_path, foreign):
    config, _ = refused_foreign_codex_install(artifact, tmp_path, foreign)
    removed = invoke(artifact, 'uninstall', '--target', tmp_path / 'root', '--project', tmp_path, '--client', 'codex', cwd=tmp_path)
    assert (removed.returncode, config.read_text()) == (0, foreign)


def test_other_servers_in_an_inline_table_stop_the_install_before_it_breaks_them(artifact, tmp_path):
    config = tmp_path / '.codex/config.toml'
    config.parent.mkdir()
    original = 'mcp_servers = { other = { command = "x" } }\n'
    config.write_text(original)
    refused = invoke(artifact, 'install', *args(tmp_path, 'codex'), cwd=tmp_path)
    assert refused.returncode == 1 and config.read_text() == original


def test_invalid_codex_config_is_reported_and_left_alone(artifact, tmp_path):
    config = tmp_path / '.codex/config.toml'
    config.parent.mkdir()
    config.write_text('[broken\n')
    refused = invoke(artifact, 'install', *args(tmp_path, 'codex'), cwd=tmp_path)
    assert refused.returncode == 1 and 'not valid TOML' in refused.stderr and config.read_text() == '[broken\n'


def test_valid_config_with_top_level_keys_and_tables_stays_valid(artifact, tmp_path):
    import tomllib
    config = tmp_path / '.codex/config.toml'
    config.parent.mkdir()
    config.write_text('model = "custom"\n[mcp_servers.other]\ncommand = "x"\n[other]\nvalue = 1\n')
    assert invoke(artifact, 'install', *args(tmp_path, 'codex'), cwd=tmp_path).returncode == 0
    parsed = tomllib.loads(config.read_text())
    assert (parsed['thread_unload_delay_secs'] == 0 and parsed['model'] == 'custom' and parsed['other'] == {'value': 1}
            and set(parsed['mcp_servers']) == {'other', 'channels'} and 'thread_unload_delay_secs' not in parsed['other'])


@pytest.mark.parametrize('value,accepted', [(0, True), (60, False)])
def test_existing_unload_setting_is_kept_at_zero_and_refused_otherwise(artifact, tmp_path, value, accepted):
    config = tmp_path / '.codex/config.toml'
    config.parent.mkdir()
    original = f'thread_unload_delay_secs = {value}\n'
    config.write_text(original)
    installed = invoke(artifact, 'install', *args(tmp_path, 'codex'), cwd=tmp_path)
    if not accepted:
        assert installed.returncode == 1 and 'thread_unload_delay_secs' in installed.stderr and config.read_text() == original
        return
    assert installed.returncode == 0 and config.read_text().count('thread_unload_delay_secs') == 1
    assert invoke(artifact, 'uninstall', '--target', tmp_path / 'root', '--project', tmp_path, cwd=tmp_path).returncode == 0
    assert config.read_text() == original


def install_without_the_unload_setting(artifact, tmp_path):
    """An earlier Codex install whose project configuration lacks the unload setting."""
    assert invoke(artifact, 'install', *args(tmp_path, 'codex'), cwd=tmp_path).returncode == 0
    config = tmp_path / '.codex/config.toml'
    config.write_text(codex_block(tmp_path / 'root/bin/channels'))
    return config


def test_install_without_the_unload_setting_is_reported(artifact, tmp_path):
    install_without_the_unload_setting(artifact, tmp_path)
    assert hook_binding_status(tmp_path / 'root', tmp_path, 'codex')['reason'] == 'unload_setting_mismatch'


def test_older_install_without_the_unload_setting_is_upgraded(artifact, tmp_path):
    config = install_without_the_unload_setting(artifact, tmp_path)
    upgraded = invoke(artifact, 'install', *args(tmp_path, 'codex'), cwd=tmp_path)
    assert (upgraded.returncode, config.read_text().startswith(CODEX_SETTING),
            hook_binding_status(tmp_path / 'root', tmp_path, 'codex')['status']) == (0, True, 'installed')


def test_python310_client_refuses_the_same_foreign_table(artifact, tmp_path):
    python = Path('/usr/bin/python3.10')
    if not python.exists():
        pytest.skip('system Python 3.10 unavailable')
    config = tmp_path / '.codex/config.toml'
    config.parent.mkdir()
    foreign = "[mcp_servers.'channels']\ncommand = 'other'\n"
    config.write_text(foreign)
    refused = subprocess.run([str(python), '-I', str(artifact), 'install', *map(str, args(tmp_path, 'codex'))],
                             cwd=tmp_path, capture_output=True, text=True, timeout=15)
    assert refused.returncode == 1 and 'Another Codex channels server' in refused.stderr and config.read_text() == foreign


def test_foreign_claude_server_is_refused_and_preserved(artifact, tmp_path, synthetic_claude):
    synthetic_claude.mkdir(parents=True)
    entry = synthetic_claude / (str(tmp_path).replace('/', '_') + '.json')
    entry.write_text(json.dumps({'scope': 'User config', 'command': '/usr/bin/other', 'args': []}))
    refused = invoke(artifact, 'install', *args(tmp_path), cwd=tmp_path)
    assert (refused.returncode, 'Another MCP server named channels' in refused.stderr, (tmp_path / '.claude').exists(),
            json.loads(entry.read_text())['command']) == (1, True, False, '/usr/bin/other')


def test_claude_install_without_the_client_fails_visibly(artifact, tmp_path, monkeypatch):
    monkeypatch.setenv('PATH', str(tmp_path / 'empty'))
    environment = {key: value for key, value in os.environ.items() if not key.startswith(('CHANNEL_', 'PYTHON'))}
    result = subprocess.run([sys.executable, str(artifact), 'install', *map(str, args(tmp_path))], cwd=tmp_path, env=environment,
                            text=True, capture_output=True, timeout=30)
    assert result.returncode == 1 and 'claude command not found' in result.stderr


def test_codex_upgrade_removes_only_its_legacy_stop_wait(artifact, tmp_path):
    wrapper = tmp_path / 'root/bin/channels'
    hooks = tmp_path / '.codex/hooks.json'
    hooks.parent.mkdir()
    unrelated = {'type': 'command', 'command': 'echo unrelated'}
    assert invoke(artifact, 'install', *args(tmp_path, 'codex'), cwd=tmp_path).returncode == 0
    hooks.write_text(json.dumps({'hooks': {'Stop': [{'hooks': [unrelated]}, {'hooks': [
        {'type': 'command', 'command': shlex.join([str(wrapper), '--client', 'codex', 'hook']), 'timeout': 660}]}]}}))
    (tmp_path / '.codex/config.toml').unlink()
    upgraded = invoke(artifact, 'install', *args(tmp_path, 'codex'), cwd=tmp_path)
    assert upgraded.returncode == 0 and commands(hooks) == [unrelated]


def install_with_a_blocking_stop_callback(artifact, tmp_path):
    """An earlier Claude install whose settings hold the old blocking Stop callback."""
    assert invoke(artifact, 'install', *args(tmp_path), cwd=tmp_path).returncode == 0
    settings = tmp_path / '.claude/settings.local.json'
    command = json.loads(settings.read_text())['hooks']['Stop'][0]['hooks'][0]['command']
    settings.write_text(json.dumps({'hooks': {'Stop': [{'hooks': [{'type': 'command', 'command': command, 'timeout': 660}]}]}}))
    return settings


def test_blocking_stop_callback_is_reported_invalid(artifact, tmp_path):
    install_with_a_blocking_stop_callback(artifact, tmp_path)
    assert hook_binding_status(tmp_path / 'root', tmp_path, 'claude')['status'] == 'hook_invalid'


def test_claude_upgrade_replaces_the_blocking_stop_callback(artifact, tmp_path):
    settings = install_with_a_blocking_stop_callback(artifact, tmp_path)
    upgraded = invoke(artifact, 'install', *args(tmp_path), cwd=tmp_path)
    assert (upgraded.returncode, commands(settings)[0]['asyncRewake'],
            hook_binding_status(tmp_path / 'root', tmp_path, 'claude')['status']) == (0, True, 'installed')


@pytest.mark.parametrize('settings', ['permissions', 'empty_hooks', 'other_root'])
def test_uninstall_unregistered_project_preserves_settings_bytes(artifact, tmp_path, settings):
    installed = invoke(artifact, 'install', *args(tmp_path, project=project_directory(tmp_path / 'registered')), cwd=tmp_path)
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
            json.loads(result.stdout)['bindings_removed']) == (0, body, registered_before, [])


def test_multiple_projects_and_adapters(artifact, tmp_path):
    projects = [project_directory(tmp_path / 'one'), project_directory(tmp_path / 'two')]
    for project in projects:
        for client in ['claude', 'codex']:
            result = invoke(artifact, 'install', *args(tmp_path, client, project), cwd=tmp_path)
            assert result.returncode == 0, result.stderr
    root = tmp_path / 'root'
    first = invoke(artifact, 'uninstall', '--target', root, '--project', projects[0], cwd=tmp_path)
    assert (first.returncode, (root / 'bin/channels').exists(), (projects[1] / '.codex/config.toml').exists()) == (0, True, True)
    second = invoke(artifact, 'uninstall', '--target', root, '--project', projects[1], cwd=tmp_path)
    assert (second.returncode, root.exists(), list(projects[0].iterdir()), list(projects[1].iterdir())) == (0, False, [], [])


def test_uninstall_of_one_client_creates_nothing_for_the_other(artifact, tmp_path):
    project = project_directory(tmp_path / 'project')
    assert invoke(artifact, 'install', *args(tmp_path, 'claude', project), cwd=tmp_path).returncode == 0
    assert invoke(artifact, 'uninstall', '--target', tmp_path / 'root', '--project', project, cwd=tmp_path).returncode == 0
    assert list(project.iterdir()) == []


def test_shared_wrapper_does_not_imply_another_project_or_client_is_installed(artifact, tmp_path):
    root = tmp_path / 'root'
    claude = invoke(artifact, 'install', *args(tmp_path), cwd=tmp_path)
    assert claude.returncode == 0, claude.stderr
    key = root / '.env/channel.key'
    key.write_text('synthetic-retained-key')
    key.chmod(0o600)
    before = key.read_bytes(), (tmp_path / '.claude/settings.local.json').read_bytes()
    missing_client = hook_binding_status(root, tmp_path, 'codex')
    missing_project = hook_binding_status(root, tmp_path / 'another-project', 'claude')
    codex = invoke(artifact, 'install', *args(tmp_path, 'codex'), cwd=tmp_path)
    assert (missing_client['status'] == missing_project['status'] == 'hook_missing'
            and codex.returncode == 0
            and hook_binding_status(root, tmp_path, 'claude')['status'] == 'installed'
            and hook_binding_status(root, tmp_path, 'codex')['status'] == 'installed'
            and (key.read_bytes(), (tmp_path / '.claude/settings.local.json').read_bytes()) == before)


def test_installed_project_finds_the_deepest_containing_installation(artifact, tmp_path):
    outer, inner = project_directory(tmp_path / 'outer'), project_directory(tmp_path / 'outer/inner')
    for project in (outer, inner):
        assert invoke(artifact, 'install', *args(tmp_path, 'codex', project), cwd=tmp_path).returncode == 0
    root = tmp_path / 'root'
    assert (installed_project(root, inner / 'deep/work', 'codex'), installed_project(root, outer / 'other', 'codex'),
            installed_project(root, tmp_path, 'codex'), installed_project(root, inner, 'claude')) == (inner, outer, None, None)


@pytest.mark.parametrize('client', ['claude', 'codex'])
def test_install_and_reinstall_emit_current_native_resume_command(artifact, tmp_path, monkeypatch, client):
    project = project_directory(tmp_path / "project 'quoted' $(false)")
    monkeypatch.setenv('CLAUDE_CODE_SESSION_ID', 'native-claude')
    monkeypatch.setenv('CODEX_THREAD_ID', 'native-codex')
    first = invoke(artifact, 'install', *args(tmp_path, client, project), cwd=tmp_path)
    second = invoke(artifact, 'install', *args(tmp_path, client, project), cwd=tmp_path)
    assert first.returncode == second.returncode == 0, first.stderr + second.stderr
    for output in (first.stdout, second.stdout):
        directory, command = json.loads(output)['resume_command'].split(' && ')
        assert (shlex.split(directory) == ['cd', '--', str(project)]
                and shlex.split(command) == [client, '--resume' if client == 'claude' else 'resume', 'native-' + client])


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


def test_files_named_like_old_locks_are_preserved_unless_empty(artifact, tmp_path):
    assert invoke(artifact, 'install', *args(tmp_path, 'codex'), cwd=tmp_path).returncode == 0
    root = tmp_path / 'root'
    (root / '.install.lock').write_text('preserve')
    (root / '.lifecycle.lock').touch()
    (tmp_path / '.codex/hooks.json.channels.lock').touch()
    removed = invoke(artifact, 'uninstall', '--target', root, '--project', tmp_path, cwd=tmp_path)
    assert (removed.returncode, (root / '.install.lock').read_text(), (root / '.lifecycle.lock').exists(),
            (tmp_path / '.codex/hooks.json.channels.lock').exists()) == (0, 'preserve', False, False)


def test_install_leaves_no_lock_file_anywhere(artifact, tmp_path):
    project = project_directory(tmp_path / 'project')
    assert invoke(artifact, 'install', *args(tmp_path, 'codex', project), cwd=tmp_path).returncode == 0
    assert not [path for path in tmp_path.rglob('*.lock')] and not list(project.glob('.channels*'))


def test_second_exclusive_lock_is_refused_while_the_first_is_held(tmp_path):
    root = tmp_path / 'root'
    root.mkdir()
    probe = ('import sys\nfrom pathlib import Path\nfrom polyagent_channels.setup import lifecycle_lock\n'
             'with lifecycle_lock(Path(sys.argv[1]), exclusive=True):\n    print("acquired")')
    environment = {**os.environ, 'PYTHONPATH': str(ROOT / 'src')}
    with lifecycle_lock(root, exclusive=True):
        finish_removal(root)
        during = subprocess.run([sys.executable, '-c', probe, str(root)], env=environment, capture_output=True, text=True, timeout=15)
    after = subprocess.run([sys.executable, '-c', probe, str(root)], env=environment, capture_output=True, text=True, timeout=15)
    assert (during.returncode, 'Installation in use' in during.stderr, after.stdout.strip(), root.exists()) == (1, True, 'acquired', True)


def test_waiter_on_a_removed_root_fails_visibly_instead_of_working_on_a_ghost(tmp_path):
    root = tmp_path / 'root'
    root.mkdir()
    errors = []
    def wait_for_shared():
        try:
            with lifecycle_lock(root):
                pass
        except SetupError as error:
            errors.append(str(error))
    with concurrent.futures.ThreadPoolExecutor(1) as pool:
        with lifecycle_lock(root, exclusive=True):
            waiter = pool.submit(wait_for_shared)
            time.sleep(.2)
            root.rmdir()
        waiter.result(5)
    assert errors == ['Installation in use; try again']


def test_late_receiver_cleanup_leaves_a_reinstall_alone(artifact, tmp_path):
    assert invoke(artifact, 'install', *args(tmp_path, 'codex'), cwd=tmp_path).returncode == 0
    root = tmp_path / 'root'
    state = exchange.new_state('new-session', {'channel': 'general', 'conversation_id': 'main', 'cursor': '0-0'}, 60)
    exchange.open_exchange(root / 'state', 'new-session', state)
    path = exchange.owned_state(root / 'state', 'new-session', state)
    finish_removal(root)
    assert path.exists() and (root / 'harness.pyz').exists()


def test_concurrent_same_dispatcher(artifact, tmp_path):
    with concurrent.futures.ThreadPoolExecutor() as pool:
        results = list(pool.map(lambda _: invoke(artifact, 'install', *args(tmp_path), cwd=tmp_path), range(2)))
    assert ([item.returncode for item in results], len(commands(tmp_path / '.claude/settings.local.json'))) == ([0, 0], 1)


def test_legacy_replace_and_conservative_uninstall(artifact, tmp_path):
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


def test_bundle_is_client_only_and_carries_the_tool_adapter(artifact):
    with zipfile.ZipFile(artifact) as archive:
        names = archive.namelist()
        entry = archive.read('__main__.py').decode()
    assert ('uninstall' in entry, any(name.startswith(('redis/', 'starlette/', 'mcp/')) for name in names),
            any(name.endswith(('.pyc', '.so')) for name in names), 'polyagent_channels/stdio_tools.py' in names) == (True, False, False, True)


def test_uninstall_only_valid_generated_session_state(artifact, tmp_path):
    result = invoke(artifact, 'install', *args(tmp_path), cwd=tmp_path)
    root = tmp_path / 'root'
    state = exchange.new_state('synthetic-session',
                               {'channel': 'general', 'conversation_id': 'main', 'cursor': '0-0'}, 60)
    exchange.open_exchange(root / 'state', 'synthetic-session', state)
    state_path = exchange.owned_state(root / 'state', 'synthetic-session', state)
    unknown = root / 'state/manual'
    unknown.mkdir()
    (unknown / 'exchange.json').write_text('preserve')
    removed = invoke(artifact, 'uninstall', '--target', root, '--project', tmp_path, cwd=tmp_path)
    assert (result.returncode, removed.returncode, state_path.exists(), state_path.parent.exists(),
            (unknown / 'exchange.json').read_text()) == (0, 0, False, False, 'preserve')


def test_flock_serializes_installer(artifact, tmp_path):
    root = tmp_path / 'root'
    held = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
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
    finally:
        os.close(held)
    assert (blocked, process.returncode) == (True, 0)


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
    assert (installed.returncode, hook.returncode, removed.returncode, wrapper.exists()) == (0, 0, 0, False)


def test_final_uninstall_leaves_only_the_key_and_unknown_files(artifact, tmp_path):
    installed = invoke(artifact, 'install', *args(tmp_path), cwd=tmp_path)
    root = tmp_path / 'root'
    session_id = 'closed-synthetic'
    state = exchange.new_state(session_id, {'channel': 'general', 'conversation_id': 'main', 'cursor': '0-0'}, 60)
    exchange.open_exchange(root / 'state', session_id, state)
    exchange.close_exchange(root / 'state', session_id, state)
    directory = exchange.session_directory(root / 'state', session_id)
    exchange.write_state(directory / 'last-hook.json', {'timestamp': time.time(), 'state': 'waiting', 'session_id': session_id})
    exchange.write_state(directory / 'connection-probe.json', {'session_id': session_id})
    manual = root / 'manual-venv/lib'
    manual.mkdir(parents=True)
    for number in range(1400):
        (manual / str(number)).touch()
    (root / '.env').mkdir(exist_ok=True)
    key = root / '.env/channel.key'
    key.write_text('synthetic-protected-key')
    result = invoke(artifact, 'uninstall', '--target', root, '--project', tmp_path, cwd=tmp_path)
    output = json.loads(result.stdout)
    assert (installed.returncode, result.returncode, directory.exists(), (root / 'harness.pyz').exists(),
            sorted(path.name for path in root.iterdir()), str(key) in output['kept'], '--purge-key' in output['purge_key_command'],
            'synthetic-protected-key' in result.stdout) == (0, 0, False, False, ['.env', 'manual-venv'], True, True, False)


def test_purge_shared_key_refused_before_callback_changes(artifact, tmp_path):
    projects = [project_directory(tmp_path / 'one'), project_directory(tmp_path / 'two')]
    for project in projects:
        result = invoke(artifact, 'install', *args(tmp_path, project=project), cwd=tmp_path)
        assert result.returncode == 0
    root = tmp_path / 'root'
    hook = projects[0] / '.claude/settings.local.json'
    before = hook.read_bytes(), (root / 'install-manifest.json').read_bytes()
    result = invoke(artifact, 'uninstall', '--target', root, '--project', projects[0], '--purge-key', cwd=tmp_path)
    assert (result.returncode, hook.read_bytes(), (root / 'install-manifest.json').read_bytes()) == (1, *before)


def pending_session(root, session, topic='work', message='3-0'):
    state = exchange.new_state(session, {'channel': 'general', 'conversation_id': topic, 'cursor': '0-0'}, 60)
    exchange.open_exchange(root / 'state', session, {**state, 'pending': [message], 'delivered': message})
    return exchange.owned_state(root / 'state', session, state)


def test_uninstall_preserves_unconfirmed_work_and_says_where_it_is(artifact, tmp_path, synthetic_claude):
    project = project_directory(tmp_path / 'project')
    assert invoke(artifact, 'install', *args(tmp_path, project=project), cwd=tmp_path).returncode == 0
    root = tmp_path / 'root'
    (root / '.env').mkdir(exist_ok=True)
    path = pending_session(root, 'stopped-session')
    removed = invoke(artifact, 'uninstall', '--target', root, '--project', project, cwd=tmp_path)
    output = json.loads(removed.stdout)
    assert (removed.returncode, list(project.iterdir()), registered(synthetic_claude), (root / 'harness.pyz').exists(), path.exists()) == (0, [], [], False, True)
    assert output['unconfirmed'] == [{'session_id': 'stopped-session', 'channel': 'general', 'conversation_id': 'work',
                                      'message_ids': ['3-0'], 'state_path': str(path)}]
    assert 'ack' not in output['note'] and str(path) in output['discard_unconfirmed_command']


def test_discard_command_removes_the_preserved_state(artifact, tmp_path):
    project = project_directory(tmp_path / 'project')
    assert invoke(artifact, 'install', *args(tmp_path, 'codex', project), cwd=tmp_path).returncode == 0
    root = tmp_path / 'root'
    pending_session(root, 'stopped-session')
    removed = invoke(artifact, 'uninstall', '--target', root, '--project', project, cwd=tmp_path)
    done = subprocess.run(['sh', '-c', json.loads(removed.stdout)['discard_unconfirmed_command']], cwd=tmp_path, capture_output=True, text=True, timeout=15)
    assert done.returncode == 0, done.stderr
    assert not (root / 'state').exists()


def test_discard_command_removes_only_files_the_product_wrote(artifact, tmp_path):
    project = project_directory(tmp_path / 'project')
    assert invoke(artifact, 'install', *args(tmp_path, 'codex', project), cwd=tmp_path).returncode == 0
    root = tmp_path / 'root'
    path = pending_session(root, 'stopped-session')
    notes = path.parent / 'unknown-user-notes.txt'
    notes.write_text('preserve')
    lookalike = path.parent / 'topic-manual.json'
    lookalike.write_text('preserve')
    other = root / 'state/manual'
    other.mkdir()
    (other / 'kept.txt').write_text('preserve')
    removed = invoke(artifact, 'uninstall', '--target', root, '--project', project, cwd=tmp_path)
    done = subprocess.run(['sh', '-c', json.loads(removed.stdout)['discard_unconfirmed_command']], cwd=tmp_path, capture_output=True, text=True, timeout=15)
    assert done.returncode == 0, done.stderr
    assert (not path.exists() and notes.read_text() == lookalike.read_text() == 'preserve'
            and (other / 'kept.txt').read_text() == 'preserve')


def test_uninstall_removes_only_lock_files_the_product_could_have_made(artifact, tmp_path):
    project = project_directory(tmp_path / 'project')
    assert invoke(artifact, 'install', *args(tmp_path, 'codex', project), cwd=tmp_path).returncode == 0
    root = tmp_path / 'root'
    directory = exchange.session_directory(root / 'state', 'closed-session')
    directory.mkdir(parents=True)
    (directory / 'exchange.lock').touch()
    (directory / 'notes.lock').touch()
    (directory / 'receiver.lock').write_text('a user file with a product name')
    removed = invoke(artifact, 'uninstall', '--target', root, '--project', project, cwd=tmp_path)
    assert (removed.returncode, sorted(path.name for path in directory.iterdir()),
            (directory / 'receiver.lock').read_text()) == (0, ['notes.lock', 'receiver.lock'], 'a user file with a product name')


def legacy_hook_project(tmp_path, document=None):
    """A project as d71c677 left it: only its Stop hook in hooks.json, plus its empty lock file."""
    project = project_directory(tmp_path / 'project')
    wrapper = tmp_path / 'root/bin/channels'
    hooks = project / '.codex/hooks.json'
    hooks.parent.mkdir(parents=True)
    product = {'hooks': [{'type': 'command', 'command': shlex.join([str(wrapper), '--client', 'codex', 'hook']), 'timeout': 660}]}
    base = document or {}
    hooks.write_text(json.dumps({**base, 'hooks': {**base.get('hooks', {}), 'Stop': [*base.get('hooks', {}).get('Stop', []), product]}}))
    (project / '.codex/hooks.json.channels.lock').touch()
    return project, hooks


def old_wrapper(artifact, tmp_path):
    root = tmp_path / 'root'
    (root / 'bin').mkdir(parents=True)
    (root / 'bin/channels').write_bytes(wrapper_content(Path(sys.executable), root))
    (root / 'harness.pyz').write_bytes(artifact.read_bytes())


def upgrade_over_a_legacy_hook(artifact, tmp_path):
    """Install over an old release whose Codex hook file holds only the product entry."""
    project, hooks = legacy_hook_project(tmp_path)
    old_wrapper(artifact, tmp_path)
    upgraded = invoke(artifact, 'install', *args(tmp_path, 'codex', project), cwd=tmp_path)
    return project, hooks, upgraded


def test_upgrade_deletes_a_hook_file_holding_only_the_product_entry(artifact, tmp_path):
    _, hooks, upgraded = upgrade_over_a_legacy_hook(artifact, tmp_path)
    assert (upgraded.returncode, hooks.exists()) == (0, False)


def test_uninstall_after_upgrading_a_legacy_hook_leaves_an_empty_project(artifact, tmp_path):
    project, _, _ = upgrade_over_a_legacy_hook(artifact, tmp_path)
    removed = invoke(artifact, 'uninstall', '--target', tmp_path / 'root', '--project', project, cwd=tmp_path)
    assert (removed.returncode, list(project.iterdir())) == (0, [])


def test_uninstall_of_an_old_install_deletes_a_hook_file_holding_only_the_product_entry(artifact, tmp_path):
    project, hooks = legacy_hook_project(tmp_path)
    old_wrapper(artifact, tmp_path)
    removed = invoke(artifact, 'uninstall', '--target', tmp_path / 'root', '--project', project, cwd=tmp_path)
    assert (removed.returncode, hooks.exists(), list(project.iterdir())) == (0, False, [])


@pytest.mark.parametrize('document', [{'keep': True}, {'hooks': {'PreToolUse': [{'hooks': [{'type': 'command', 'command': 'echo user'}]}]}},
                                      {'hooks': {'Stop': [{'hooks': [{'type': 'command', 'command': 'echo user'}]}]}}])
def test_upgrade_keeps_a_hook_file_that_has_user_content(artifact, tmp_path, document):
    project, hooks = legacy_hook_project(tmp_path, document)
    old_wrapper(artifact, tmp_path)
    assert invoke(artifact, 'install', *args(tmp_path, 'codex', project), cwd=tmp_path).returncode == 0
    assert json.loads(hooks.read_text()) == document


def test_hook_file_the_product_never_touched_stays_even_if_empty(artifact, tmp_path):
    project = project_directory(tmp_path / 'project')
    hooks = project / '.codex/hooks.json'
    hooks.parent.mkdir(parents=True)
    hooks.write_text('{"hooks": {}}')
    assert invoke(artifact, 'install', *args(tmp_path, 'codex', project), cwd=tmp_path).returncode == 0
    assert invoke(artifact, 'uninstall', '--target', tmp_path / 'root', '--project', project, cwd=tmp_path).returncode == 0
    assert hooks.read_text() == '{"hooks": {}}'


def test_discard_command_keeps_the_state_directory_for_another_session(artifact, tmp_path):
    project = project_directory(tmp_path / 'project')
    assert invoke(artifact, 'install', *args(tmp_path, 'codex', project), cwd=tmp_path).returncode == 0
    root = tmp_path / 'root'
    path = pending_session(root, 'stopped-session')
    (root / 'state/manual').mkdir()
    (root / 'state/manual/kept.txt').write_text('preserve')
    removed = invoke(artifact, 'uninstall', '--target', root, '--project', project, cwd=tmp_path)
    done = subprocess.run(['sh', '-c', json.loads(removed.stdout)['discard_unconfirmed_command']], cwd=tmp_path, capture_output=True, text=True, timeout=15)
    assert done.returncode == 0, done.stderr
    assert not path.parent.exists() and (root / 'state/manual/kept.txt').exists()


def test_unconfirmed_work_of_another_project_does_not_block_removal(artifact, tmp_path):
    one, two = project_directory(tmp_path / 'one'), project_directory(tmp_path / 'two')
    for project in (one, two):
        assert invoke(artifact, 'install', *args(tmp_path, 'codex', project), cwd=tmp_path).returncode == 0
    root = tmp_path / 'root'
    path = pending_session(root, 'session-of-project-one')
    removed = invoke(artifact, 'uninstall', '--target', root, '--project', two, '--client', 'codex', cwd=tmp_path)
    assert removed.returncode == 0 and list(two.iterdir()) == [] and path.exists() and (one / '.codex/config.toml').exists() and (root / 'harness.pyz').exists()


def test_reinstall_keeps_preserved_work_for_the_session_that_comes_back(artifact, tmp_path):
    project = project_directory(tmp_path / 'project')
    assert invoke(artifact, 'install', *args(tmp_path, 'codex', project), cwd=tmp_path).returncode == 0
    root = tmp_path / 'root'
    path = pending_session(root, 'stopped-session')
    assert invoke(artifact, 'uninstall', '--target', root, '--project', project, cwd=tmp_path).returncode == 0
    assert invoke(artifact, 'install', *args(tmp_path, 'codex', project), cwd=tmp_path).returncode == 0
    assert exchange.read_state(path)['pending'] == ['3-0']


@pytest.mark.parametrize('value', ['false', '0.0', '"0"'])
def test_unload_setting_of_the_wrong_type_is_refused(artifact, tmp_path, value):
    config = tmp_path / '.codex/config.toml'
    config.parent.mkdir()
    original = f'thread_unload_delay_secs = {value}\n'
    config.write_text(original)
    refused = invoke(artifact, 'install', *args(tmp_path, 'codex'), cwd=tmp_path)
    assert refused.returncode == 1 and 'thread_unload_delay_secs' in refused.stderr and config.read_text() == original


def test_codex_next_step_separates_a_loaded_session_from_a_new_one(artifact, tmp_path):
    codex = json.loads(invoke(artifact, 'install', *args(tmp_path, 'codex'), cwd=tmp_path).stdout)['next_step']
    claude = json.loads(invoke(artifact, 'install', *args(tmp_path), cwd=tmp_path).stdout)['next_step']
    assert 'still loaded' in codex and 'still loaded' not in claude


def test_open_session_receiver_does_not_block_install_of_the_other_client(artifact, tmp_path):
    assert invoke(artifact, 'install', *args(tmp_path), cwd=tmp_path).returncode == 0
    directory = exchange.session_directory(tmp_path / 'root/state', 'listening')
    directory.mkdir(parents=True)
    with exchange.receiver_slot(directory) as held:
        second = invoke(artifact, 'install', *args(tmp_path, 'codex'), cwd=tmp_path)
    assert held is True and second.returncode == 0, second.stderr


def test_uninstall_proceeds_while_a_session_listens_and_the_receiver_cleans_up(artifact, tmp_path):
    assert invoke(artifact, 'install', *args(tmp_path), cwd=tmp_path).returncode == 0
    root = tmp_path / 'root'
    state = exchange.new_state('listening', {'channel': 'general', 'conversation_id': 'main', 'cursor': '0-0'}, 600)
    exchange.open_exchange(root / 'state', 'listening', state)
    directory = exchange.session_directory(root / 'state', 'listening')
    with exchange.receiver_slot(directory) as held:
        removed = invoke(artifact, 'uninstall', '--target', root, '--project', tmp_path, cwd=tmp_path)
        during = sorted(path.name for path in directory.iterdir())
    finish_removal(root)
    assert (held, removed.returncode, during, root.exists(), list(tmp_path.glob('.claude/*'))) == (True, 0, ['receiver.lock'], False, [])


def test_help_uses_request_origin_and_quotes_host_data(monkeypatch, server_module):
    from polyagent_channels import harness
    from starlette.requests import Request
    import asyncio

    monkeypatch.setattr(harness, 'artifact_bytes', lambda: b'synthetic-artifact')
    request = Request({'type': 'http', 'scheme': 'https', 'path': '/help', 'root_path': '',
                       'headers': [(b'host', b'channels.example:2345')], 'server': ('localhost', 80)})
    response = asyncio.run(server_module.help_page(request))
    malicious = 'http://host$(id)'
    assert ('SERVICE=https://channels.example:2345' in response.body.decode(),
            'SERVICE=' + shlex.quote(malicious) in harness.setup_help(malicious),
            harness.setup_help('http://host\nwhoami')) == (True, True, None)


def test_help_commands_install_and_save_a_key_in_a_custom_root(artifact, tmp_path, monkeypatch):
    from polyagent_channels import harness

    monkeypatch.setattr(harness, 'artifact_bytes', lambda: artifact.read_bytes())
    text = harness.setup_help('http://localhost:18765')
    lines = text.splitlines()
    block = '\n'.join(lines[lines.index(next(line for line in lines if line.startswith('SERVICE='))):lines.index(') || exit 1') + 1])
    served = tmp_path / 'served'
    served.mkdir()
    (served / 'harness').write_bytes(artifact.read_bytes())
    block = block.replace('curl --fail "$SERVICE/harness" -o harness.pyz', f'cp {served}/harness harness.pyz')
    project = tmp_path / 'project'
    project.mkdir()
    environment = {**{key: value for key, value in os.environ.items() if not key.startswith(('CHANNEL_', 'PYTHON', 'CODEX'))},
                   'CLAUDE_CODE_SESSION_ID': 'synthetic', 'CHANNEL_ROOT': str(tmp_path / 'custom')}
    done = subprocess.run(['bash', '-c', block + '\n"$CHANNELS" key --stdin'], input='synthetic-help-key', cwd=project, env=environment,
                          text=True, capture_output=True, timeout=30)
    assert (done.returncode, (tmp_path / 'custom/.env/channel.key').read_text().strip(), (project / '.claude/settings.local.json').exists(),
            'synthetic-help-key' in done.stdout + done.stderr) == (0, 'synthetic-help-key', True, False)


def test_busy_closed_session_lock_refuses_uninstall(artifact, tmp_path):
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


@pytest.mark.parametrize('action', ['plan', 'install'])
def test_install_refuses_a_missing_project(artifact, tmp_path, action):
    result = invoke(artifact, action, *args(tmp_path, project=tmp_path / 'does/not/exist'), cwd=tmp_path)
    assert 'does not exist or is not a directory' in result.stderr


def test_install_refuses_a_file_as_project(artifact, tmp_path):
    (tmp_path / 'notes.txt').write_text('synthetic')
    result = invoke(artifact, 'install', *args(tmp_path, project=tmp_path / 'notes.txt'), cwd=tmp_path)
    assert 'is not a directory' in result.stderr


def test_refused_install_creates_no_project_directory(artifact, tmp_path):
    invoke(artifact, 'install', *args(tmp_path, project=tmp_path / 'does/not/exist'), cwd=tmp_path)
    assert not (tmp_path / 'does').exists()


def test_running_command_reuses_installation_and_refuses_changes(artifact, tmp_path):
    key = tmp_path / 'private/key'
    key.parent.mkdir(mode=0o700)
    key.write_text('synthetic-installed-key')
    key.chmod(0o600)
    arguments = args(tmp_path) + ['--key-file', key]
    assert invoke(artifact, 'install', *arguments, cwd=tmp_path).returncode == 0
    root = tmp_path / 'root'
    hook = tmp_path / '.claude/settings.local.json'
    before = hook.read_bytes()
    with lifecycle_lock(root):
        reused = invoke(artifact, 'install', *arguments, cwd=tmp_path)
        assert reused.returncode == 0 and json.loads(reused.stdout)['status'] == 'already_installed'
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
                   (artifact, 'install', arguments + ['--project', project_directory(tmp_path / 'new-project')]),
                   (artifact, 'uninstall', ['--target', root, '--project', tmp_path])]
        for candidate, action, changed in changes:
            result = invoke(candidate, action, *changed, cwd=tmp_path)
            assert (result.returncode, hook.read_bytes(), 'Installation in use' in result.stderr) == (1, before, True)


def test_hook_after_removal_writes_nothing(artifact, tmp_path):
    assert invoke(artifact, 'install', *args(tmp_path), cwd=tmp_path).returncode == 0
    root = tmp_path / 'root'
    removed = invoke(artifact, 'uninstall', '--target', root, '--project', tmp_path, cwd=tmp_path)
    assert removed.returncode == 0
    result = invoke(artifact, '--config', root / 'config.json', 'hook', cwd=tmp_path,
                    input_text='{"session_id":"synthetic-after-removal"}')
    assert result.returncode == 0 and not root.exists()


def test_old_release_pending_topic_is_preserved_with_unknown_files(artifact, tmp_path):
    invoke(artifact, 'install', *args(tmp_path), cwd=tmp_path)
    root = tmp_path / 'root'
    topic = exchange.new_state('pending-synthetic',
                               {'channel': 'general', 'conversation_id': 'work', 'cursor': '0-0'}, 60)
    exchange.open_exchange(root / 'state', 'pending-synthetic', topic)
    path = exchange.owned_state(root / 'state', 'pending-synthetic', topic)
    document = json.loads(path.read_text())
    path.write_text(json.dumps({key: value for key, value in {**document, 'pending': '3-0', 'deadline': 1}.items() if key != 'delivered'}))
    unknown = path.parent / 'topic-manual.json'
    unknown.write_text('operator data')
    removed = invoke(artifact, 'uninstall', '--target', root, '--project', tmp_path, cwd=tmp_path)
    assert (removed.returncode == 0 and path.exists() and unknown.read_text() == 'operator data'
            and json.loads(removed.stdout)['unconfirmed'][0]['message_ids'] == ['3-0'])


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


def test_uninstall_preflights_both_clients_before_any_write(artifact, tmp_path, synthetic_claude):
    for client in ['claude', 'codex']:
        result = invoke(artifact, 'install', *args(tmp_path, client), cwd=tmp_path)
        assert result.returncode == 0, result.stderr
    claude = tmp_path / '.claude/settings.local.json'
    codex = tmp_path / '.codex/config.toml'
    document = json.loads(claude.read_text())
    document['hooks']['SessionStart'].append({'hooks': [{'command': 'channels hook'}]})
    claude.write_text(json.dumps(document))
    root = tmp_path / 'root'
    paths = [claude, codex, root / 'install-manifest.json', root / 'harness.pyz']
    before = [path.read_bytes() for path in paths]
    result = invoke(artifact, 'uninstall', '--target', root, '--project', tmp_path, cwd=tmp_path)
    assert (result.returncode, [path.read_bytes() for path in paths], 'SessionStart' in result.stderr, len(registered(synthetic_claude))) == (1, before, True, 1)


@pytest.mark.parametrize('event', ['Stop', 'SessionStart'])
def test_other_root_conflict_and_uninstall_preservation(artifact, tmp_path, event):
    other = tmp_path / 'other'
    project = project_directory(tmp_path / 'project')
    installed = invoke(artifact, 'install', '--target', other, '--client', 'claude',
                       '--project', project, '--url', 'http://localhost:18765', cwd=tmp_path)
    assert installed.returncode == 0, installed.stderr
    path = project / '.claude/settings.local.json'
    document = json.loads(path.read_text())
    document['hooks'] = {event: document['hooks'][event]}
    path.write_text(json.dumps(document))
    before = path.read_bytes()
    result = invoke(artifact, 'install', *args(tmp_path, project=project), '--replace', cwd=tmp_path)
    assert (result.returncode, path.read_bytes(), (tmp_path / 'root/bin/channels').exists(),
            event in result.stderr) == (1, before, False, True)
    removed = invoke(artifact, 'uninstall', '--target', tmp_path / 'root', '--project', project, cwd=tmp_path)
    assert (removed.returncode, path.read_bytes(), json.loads(removed.stdout)['bindings_removed']) == (0, before, [])
