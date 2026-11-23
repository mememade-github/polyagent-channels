"""Exercise the bundled Codex Git integration in disposable repositories."""
from concurrent.futures import ThreadPoolExecutor
from importlib.util import find_spec
import io
import json
import os
import shlex
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
from types import SimpleNamespace
import zipfile

import httpx
import pytest

from polyagent_channels.cli import client_status
from polyagent_channels.codex_config import CODEX_ENV_VARS, CODEX_SETTING, codex_block, filtered_block
from polyagent_channels.codex_git import CodexGit
from polyagent_channels.setup import hook_binding_status
from test_harness import artifact


CONFIG = '.codex/config.toml'
ATTRIBUTES = '.codex/.gitattributes'
BASE = b'model = "base"\n'
LOCAL = 'Git filter local to this clone'
BLOCK = b'# polyagent-channels filter ownership: '
INLINE_SERVERS = (b'mcp_servers = {}\n', b'mcp_servers = { other = { command = "other" } }\n')
GIT = shutil.which('git')


def released_codex_block(wrapper, environment='env_vars = ["CODEX_HOME"]\n'):
    return codex_block(wrapper).replace(CODEX_ENV_VARS, environment)


@pytest.fixture(autouse=True)
def isolated_git(monkeypatch, tmp_path):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('GIT_CONFIG_NOSYSTEM', '1')
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', os.devnull)
    for field, value in (('NAME', 'Synthetic user'), ('EMAIL', 'synthetic@example.invalid')):
        monkeypatch.setenv('GIT_AUTHOR_' + field, value)
        monkeypatch.setenv('GIT_COMMITTER_' + field, value)


def git(project, *arguments, check=True):
    return subprocess.run([GIT, '-C', project, *arguments], capture_output=True, check=check, timeout=30)


def configured(project, content=BASE):
    path = project / CONFIG
    path.parent.mkdir(parents=True)
    path.write_bytes(content)
    return path


def repository(project, content=BASE):
    project.mkdir()
    git(project, 'init', '-q', '--template=')
    configured(project, content)
    commit(project, 'base')
    return project


def commit(project, message):
    git(project, 'add', '-A')
    git(project, 'commit', '-qm', message)


def setup(artifact, project, root, action='install', injection=None, check=True):
    options = [action, '--client', 'codex', '--project', str(project), '--target', str(root)]
    if action == 'install':
        options += ['--url', 'http://localhost:18765']
    command = [str(artifact)]
    if injection:
        command = ['-c', INJECTION, str(artifact), injection]
    environment = {key: value for key, value in os.environ.items() if not key.startswith(('CHANNEL_', 'PYTHON'))}
    result = subprocess.run([sys.executable, *command, *options], cwd=project, env=environment,
                            capture_output=True, text=True, timeout=30)
    if check and result.returncode:
        pytest.fail(result.stderr)
    return result


def next_step(result):
    return json.loads(result.stdout)['next_step']


def clean_object(project):
    return git(project, 'hash-object', f'--path={CONFIG}', '--', CONFIG).stdout


def index_object(project):
    return git(project, 'rev-parse', ':' + CONFIG).stdout


def registered(project, root):
    content = (project / CONFIG).read_bytes()
    document = tomllib.loads(content.decode())
    command = document.get('mcp_servers', {}).get('channels', {}).get('command')
    return (command, content.count(b'[mcp_servers.channels]')) == (str(root / 'bin/channels'), 1)


@pytest.mark.parametrize('tracked', [False, True])
@pytest.mark.parametrize('environment', ['env_vars = ["CODEX_HOME"]\n',
                                        'env_vars = ["CODEX_HOME", "SKIP_CODEX_UPDATE"]\n'])
def test_reinstall_updates_owned_old_codex_block_with_current_archive(artifact, tmp_path, tracked, environment):
    project = tmp_path / 'project'
    base = BASE.replace(b'\n', b'\r\n')
    if tracked:
        repository(project, base)
    if not tracked:
        configured(project, base)
    root = tmp_path / 'client'
    path = project / CONFIG
    setup(artifact, project, root)
    current = path.read_bytes()
    wrapper = root / 'bin/channels'
    block = codex_block(wrapper)
    legacy = released_codex_block(wrapper, environment)
    if tracked:
        block = filtered_block(block, ('setting',))
        legacy = filtered_block(legacy, ('setting',))
    path.write_bytes(current.replace(block.replace('\n', '\r\n').encode(), legacy.replace('\n', '\r\n').encode()))
    assert legacy.replace('\n', '\r\n').encode() in path.read_bytes()
    if tracked:
        assert clean_object(project) == index_object(project)
    status = hook_binding_status(root, project, 'codex')['status']
    setup(root / 'harness.pyz', project, root)
    updated = path.read_bytes()
    server = tomllib.loads(updated.decode())['mcp_servers']['channels']
    setup(root / 'harness.pyz', project, root, 'uninstall')
    assert (status, updated, server.get('env_vars'), path.read_bytes()) == (
        'installed', current, ['CODEX_HOME', 'SKIP_CODEX_UPDATE', 'CHANNEL_KEY_FILE', 'CHANNEL_STATE_DIR'], base)


def test_reinstall_repairs_mixed_endings_with_released_plain_block(artifact, tmp_path):
    project = tmp_path / 'project'
    project.mkdir()
    git(project, 'init', '-q', '--template=')
    root = tmp_path / 'client'
    wrapper = root / 'bin/channels'
    base = BASE.replace(b'\n', b'\r\n')
    path = configured(project, base)
    setup(artifact, project, root)
    prefix = CODEX_SETTING.replace('\n', '\r\n').encode()
    block = codex_block(wrapper).replace('\n', '\r\n').encode()
    legacy = released_codex_block(wrapper).replace('\n', '\r\n').encode()
    installed = path.read_bytes()
    assert installed == prefix + base + block
    comment = b'# user appended this line\n'
    path.write_bytes(installed.removeprefix(prefix).replace(block, legacy) + comment)

    setup(root / 'harness.pyz', project, root)
    repaired = path.read_bytes()
    setup(root / 'harness.pyz', project, root, 'uninstall')
    assert (repaired, path.read_bytes()) == (installed + comment, base + comment)


def test_smudge_repairs_mixed_endings_and_owns_setting_with_released_filtered_block(artifact, tmp_path):
    base = BASE.replace(b'\n', b'\r\n')
    user_setting = b'thread_unload_delay_secs = 0\r\n'
    project = repository(tmp_path / 'project', user_setting + base)
    root = tmp_path / 'client'
    wrapper = root / 'bin/channels'
    setup(artifact, project, root)
    path = project / CONFIG
    block = filtered_block(codex_block(wrapper), ()).replace('\n', '\r\n').encode()
    legacy = filtered_block(released_codex_block(wrapper), ()).replace('\n', '\r\n').encode()
    installed = path.read_bytes()
    assert installed == user_setting + base + block
    comment = b'# user appended this line\n'
    path.write_bytes(installed.removeprefix(user_setting).replace(block, legacy) + comment)
    blob = git(project, 'hash-object', '-w', '--no-filters', '--', CONFIG).stdout.decode().strip()

    repaired = git(project, 'cat-file', '--filters', f'--path={CONFIG}', blob).stdout
    path.write_bytes(repaired)
    git(project, 'add', '--', CONFIG)
    staged = git(project, 'show', ':' + CONFIG).stdout
    setup(root / 'harness.pyz', project, root, 'uninstall')
    prefix = CODEX_SETTING.replace('\n', '\r\n').encode()
    owned = filtered_block(codex_block(wrapper), ('setting',)).replace('\n', '\r\n').encode()
    assert (repaired, staged, path.read_bytes()) == (prefix + base + owned + comment, base + comment, base + comment)


@pytest.mark.parametrize('body', [
    BASE, BASE.rstrip(b'\n'), b'', b'"thread_unload_delay_secs" = 0x0\n', CODEX_SETTING.encode(),
    b'model = "mixed"\r\n# comment\n',
])
def test_filtered_install_and_uninstall_round_trip(artifact, tmp_path, body):
    project = repository(tmp_path / 'project', body)
    root = tmp_path / 'client'
    installed = setup(artifact, project, root)
    observed = (LOCAL in next_step(installed), registered(project, root), clean_object(project), index_object(project),
                git(project, 'status', '--porcelain').stdout, git(project, 'diff').stdout)
    git(project, 'add', '-A')
    staged = git(project, 'diff', '--cached').stdout
    setup(artifact, project, root, 'uninstall')
    assert (observed, staged, (project / CONFIG).read_bytes(), git(project, 'status', '--porcelain').stdout) == (
        (True, True, observed[3], observed[3], b'', b''), b'', body, b'')


def test_line_ending_conversion(artifact, tmp_path):
    project = repository(tmp_path / 'project')
    git(project, 'config', 'core.autocrlf', 'true')
    (project / CONFIG).unlink()
    git(project, 'restore', '--', CONFIG)
    setup(artifact, project, tmp_path / 'client')
    assert (b'\r\n' in (project / CONFIG).read_bytes(), clean_object(project)) == (True, index_object(project))


@pytest.mark.parametrize('arguments,has_registration', [((), False), (('--worktree-attributes',), True)])
def test_archive_attribute_source(artifact, tmp_path, arguments, has_registration):
    project = repository(tmp_path / 'project')
    setup(artifact, project, tmp_path / 'client')
    with tarfile.open(fileobj=io.BytesIO(git(project, 'archive', *arguments, 'HEAD').stdout)) as archive:
        content = archive.extractfile(CONFIG).read()
    expected = (project / CONFIG).read_bytes() if has_registration else BASE
    assert content == expected


def test_user_edit_can_be_committed_without_the_registration(artifact, tmp_path):
    project = repository(tmp_path / 'project')
    root = tmp_path / 'client'
    setup(artifact, project, root)
    path = project / CONFIG
    path.write_bytes(path.read_bytes().replace(BASE, b'model = "user edit"\n'))
    commit(project, 'user edit')
    assert git(project, 'show', 'HEAD:' + CONFIG).stdout == b'model = "user edit"\n'


def test_uninstall_keeps_unstaged_changes(artifact, tmp_path):
    project = repository(tmp_path / 'project')
    root = tmp_path / 'client'
    setup(artifact, project, root)
    path = project / CONFIG
    edited = b'model = "user edit"\n'
    path.write_bytes(path.read_bytes().replace(BASE, edited))
    setup(artifact, project, root, 'uninstall')
    assert (path.read_bytes(), git(project, 'diff', '--cached').stdout) == (edited, b'')


@pytest.mark.parametrize('edit', ['block', 'root', 'setting', 'ownership', 'ownership-shape'])
@pytest.mark.parametrize('legacy', [False, True])
def test_edits_to_managed_text_stay_visible(artifact, tmp_path, edit, legacy):
    project = repository(tmp_path / 'project')
    root = tmp_path / 'client'
    setup(artifact, project, root)
    path = project / CONFIG
    content = path.read_bytes()
    if legacy:
        wrapper = root / 'bin/channels'
        content = CODEX_SETTING.encode() + BASE + filtered_block(released_codex_block(wrapper), ('setting',)).encode()
    changes = {
        'block': (b'"approve"', b'"prompt"'),
        'ownership': (BLOCK, b'# ownership comment edited: '),
        'ownership-shape': (BLOCK + b'setting no-separator', BLOCK + b'no-setting no-separator'),
        'root': (os.fsencode(root), os.fsencode(tmp_path / 'different')),
        'setting': (CODEX_SETTING.encode(), b'thread_unload_delay_secs = 0\n'),
    }
    original, replacement = changes[edit]
    path.write_bytes(content.replace(original, replacement))
    changed = clean_object(project) != index_object(project)
    setup(artifact, project, root, 'uninstall')
    expected = replacement + BASE if edit == 'setting' else content.replace(original, replacement)
    assert (changed, path.read_bytes()) == (True, expected)


@pytest.mark.parametrize('body,original,replacement', [
    (CODEX_SETTING.encode(), b'no-setting', b'setting'),
    (BASE, b'setting', b'no-setting'),
])
def test_edits_to_both_ownership_labels_preserve_exact_bytes(artifact, tmp_path, body, original, replacement):
    project = repository(tmp_path / 'project', body)
    root = tmp_path / 'client'
    setup(artifact, project, root)
    path = project / CONFIG
    content = path.read_bytes()
    label = b': ' + original + b' no-separator'
    assert content.count(label) == 2
    edited = content.replace(label, b': ' + replacement + b' no-separator')
    path.write_bytes(edited)
    git(project, 'add', '--', CONFIG)
    assert git(project, 'show', ':' + CONFIG).stdout == edited
    setup(artifact, project, root, 'uninstall')
    assert path.read_bytes() == edited


@pytest.mark.parametrize('replacement', [
    b'model = "next"\n', b'model = "next"', b'', b"'thread_unload_delay_secs'=+0\n",
])
def test_switch_regenerates_ownership_for_new_config_shape(artifact, tmp_path, replacement):
    project = repository(tmp_path / 'project')
    root = tmp_path / 'client'
    initial = git(project, 'rev-parse', 'HEAD').stdout.strip()
    (project / CONFIG).write_bytes(replacement)
    commit(project, 'next')
    latest = git(project, 'rev-parse', 'HEAD').stdout.strip()
    git(project, 'switch', '--detach', initial)
    setup(artifact, project, root)
    git(project, 'switch', '--detach', latest)
    state = (registered(project, root), clean_object(project), index_object(project))
    setup(artifact, project, root, 'uninstall')
    assert (state, (project / CONFIG).read_bytes()) == ((True, state[2], state[2]), replacement)


def notes_with_registration(root, delimiter, *, legacy=False):
    block = codex_block(root / 'bin/channels')
    if not legacy:
        block = filtered_block(block, ())
    return f'thread_unload_delay_secs = 0\nnotes = {delimiter}\n{block}{delimiter}\n'.encode()


@pytest.mark.parametrize('delimiter', ["'" * 3, '"' * 3])
@pytest.mark.parametrize('legacy', [False, True])
def test_install_distinguishes_notes_from_registration(artifact, tmp_path, delimiter, legacy):
    root = tmp_path / 'client'
    body = notes_with_registration(root, delimiter, legacy=legacy)
    project = repository(tmp_path / 'project', body)
    setup(artifact, project, root)
    document = tomllib.loads((project / CONFIG).read_text())
    status = hook_binding_status(root, project, 'codex')['status']
    git(project, 'add', '-A')
    staged = git(project, 'show', ':' + CONFIG).stdout
    setup(artifact, project, root, 'uninstall')
    assert (document['mcp_servers']['channels']['command'], status, staged, (project / CONFIG).read_bytes()) == (
        str(root / 'bin/channels'), 'installed', body, body)


@pytest.mark.parametrize('delimiter', ["'" * 3, '"' * 3])
def test_rewrite_and_removal_preserve_registration_text_in_notes(artifact, tmp_path, delimiter):
    root = tmp_path / 'client'
    body = notes_with_registration(root, delimiter)
    project = repository(tmp_path / 'project')
    (project / CONFIG).write_bytes(body)
    commit(project, 'notes')
    git(project, 'switch', '--detach', 'HEAD~1')
    setup(artifact, project, root)
    git(project, 'switch', '--detach', '@{-1}')
    path = project / CONFIG
    rewritten = tomllib.loads(path.read_text())
    path.write_bytes(body + b'# user edit\n')
    status = hook_binding_status(root, project, 'codex')['status']
    git(project, 'add', '--', CONFIG)
    staged = git(project, 'show', ':' + CONFIG).stdout
    setup(artifact, project, root, 'uninstall')
    assert (rewritten['mcp_servers']['channels']['command'], status, staged, path.read_bytes()) == (
        str(root / 'bin/channels'), 'hook_invalid', body + b'# user edit\n', body + b'# user edit\n')


def test_notes_do_not_confer_ownership_on_an_unmarked_server(artifact, tmp_path):
    root = tmp_path / 'client'
    body = notes_with_registration(root, "'" * 3)
    project = repository(tmp_path / 'project', body)
    server = codex_block(root / 'bin/channels').splitlines(keepends=True)[1:-1]
    (project / CONFIG).write_bytes(body + ''.join(server).encode())
    result = setup(artifact, project, root, check=False)
    assert (result.returncode, 'Another Codex channels server' in result.stderr) == (1, True)


@pytest.mark.parametrize('replacement', [
    b'[broken\n', b'thread_unload_delay_secs = 8\n', b'[mcp_servers.channels]\ncommand = "foreign"\n',
    *INLINE_SERVERS,
])
def test_smudge_leaves_configs_the_installer_cannot_register(artifact, tmp_path, replacement, monkeypatch, capsys):
    project = repository(tmp_path / 'project')
    root = tmp_path / 'client'
    initial = git(project, 'rev-parse', 'HEAD').stdout.strip()
    (project / CONFIG).write_bytes(replacement)
    commit(project, 'cannot register')
    latest = git(project, 'rev-parse', 'HEAD').stdout.strip()
    git(project, 'switch', '--detach', initial)
    setup(artifact, project, root)
    git(project, 'switch', '--detach', latest)
    service = httpx.MockTransport(lambda request: httpx.Response(200, json={'status': 'ready'}))
    client = httpx.Client(transport=service)
    monkeypatch.setattr(httpx, 'Client', lambda **kwargs: client)
    client_status(SimpleNamespace(client='codex', config=root / 'config.json', project=project,
                                  url='http://localhost:18765', session_id=None, key_file=None), 'synthetic')
    result = json.loads(capsys.readouterr().out)
    assert ((project / CONFIG).read_bytes(), result['reception'], result['hook_binding']['reason']) == (
        replacement, 'hook_invalid', 'block_mismatch')


@pytest.mark.parametrize('body', INLINE_SERVERS)
def test_install_refuses_valid_inline_servers_with_actionable_diagnostic(artifact, tmp_path, body):
    project = repository(tmp_path / 'project', body)
    root = tmp_path / 'client'
    result = setup(artifact, project, root, check=False)
    assert (result.returncode, 'using regular tables' in result.stderr, (project / CONFIG).read_bytes(),
            (root / 'install-manifest.json').exists(), (project / ATTRIBUTES).exists()) == (1, True, body, False, False)


def test_required_filter_refuses_wrong_filename(artifact, tmp_path):
    project = repository(tmp_path / 'project')
    root = tmp_path / 'client'
    setup(artifact, project, root)
    attributes = project / ATTRIBUTES
    attributes.write_bytes(attributes.read_bytes() + attributes.read_bytes().replace(b'/config.toml', b'/other.toml'))
    other = project / '.codex/other.toml'
    other.write_bytes((project / CONFIG).read_bytes())
    result = git(project, 'add', '--', '.codex/other.toml', check=False)
    assert (result.returncode != 0, b'filter scope mismatch' in result.stderr, b'[mcp_servers.channels]' in result.stderr,
            git(project, 'diff', '--cached', '--name-only').stdout) == (True, True, False, b'')


@pytest.mark.parametrize('missing', ['program', 'interpreter'])
def test_required_filter_fails_closed(artifact, tmp_path, missing):
    project = repository(tmp_path / 'project')
    original = index_object(project)
    setup(artifact, project, tmp_path / 'client')
    attribute = (project / ATTRIBUTES).read_text().split('filter=')[1].strip()
    if missing == 'program':
        (project / '.git' / (attribute + '.pyz')).unlink()
    if missing == 'interpreter':
        git(project, 'config', 'filter.' + attribute + '.clean', str(tmp_path / 'missing-python') + ' %f')
    path = project / CONFIG
    path.write_bytes(path.read_bytes() + b'# force Git to read the working file\n')
    added = git(project, 'add', '--', CONFIG, check=False)
    assert (added.returncode != 0, index_object(project)) == (True, original)


@pytest.mark.parametrize('shape', ['filtered', 'plain', 'multiline'])
def test_malformed_config_with_own_candidate_cannot_be_staged(artifact, tmp_path, shape):
    project = repository(tmp_path / 'project')
    root = tmp_path / 'client'
    setup(artifact, project, root)
    path = project / CONFIG
    body = path.read_bytes()
    if shape == 'plain':
        body = CODEX_SETTING.encode() + BASE + codex_block(root / 'bin/channels').encode()
    if shape == 'multiline':
        body = b"notes = '''\n" + body
    body += b'\nunrelated broken TOML\n'
    path.write_bytes(body)
    diff = git(project, 'diff', '--', CONFIG, check=False)
    added = git(project, 'add', '--', CONFIG, check=False)
    assert (diff.returncode != 0, diff.stdout, added.returncode != 0, added.stdout,
            b'Fix malformed TOML' in added.stderr, git(project, 'show', ':' + CONFIG).stdout,
            path.read_bytes()) == (True, b'', True, b'', True, BASE, body)


@pytest.mark.parametrize('shape', ['no-block', 'foreign', 'edited'])
def test_malformed_config_without_own_candidate_stages_exact_bytes(artifact, tmp_path, shape):
    project = repository(tmp_path / 'project')
    root = tmp_path / 'client'
    setup(artifact, project, root)
    path = project / CONFIG
    body = BASE
    if shape == 'foreign':
        body += codex_block(tmp_path / 'other/bin/channels').encode()
    if shape == 'edited':
        body = path.read_bytes().replace(b' = "approve"', b' = "prompt"')
    body += b'\nunrelated broken TOML\n'
    path.write_bytes(body)
    git(project, 'add', '--', CONFIG)
    assert git(project, 'show', ':' + CONFIG).stdout == body


def test_unchanged_add_can_reuse_cached_stat_with_missing_required_program(artifact, tmp_path):
    project = repository(tmp_path / 'project')
    setup(artifact, project, tmp_path / 'client')
    original = index_object(project)
    path = project / CONFIG
    os.utime(path, (1, 1))
    git(project, 'add', '--', CONFIG)
    next((project / '.git').glob('polyagent-channels-*.pyz')).unlink()
    added = git(project, 'add', '--', CONFIG, check=False)
    assert (added.returncode, index_object(project)) == (0, original)


def test_filter_archive_retains_product_license(artifact, tmp_path):
    project = repository(tmp_path / 'project')
    setup(artifact, project, tmp_path / 'client')
    path = next((project / '.git').glob('polyagent-channels-*.pyz'))
    with zipfile.ZipFile(artifact) as installer, zipfile.ZipFile(path) as program:
        assert (program.read('LICENSE'), program.getinfo('LICENSE').date_time) == (
            installer.read('LICENSE'), (1980, 1, 1, 0, 0, 0))


@pytest.mark.parametrize('kind', ['nested', 'linked'])
def test_project_lifecycle_isolated(artifact, tmp_path, kind):
    project = repository(tmp_path / 'project')
    sibling = tmp_path / 'other'
    if kind == 'nested':
        sibling = project / 'nested folder'
        configured(sibling)
        commit(project, 'nested configuration')
    if kind == 'linked':
        git(project, 'worktree', 'add', '--detach', sibling)
    first_root = tmp_path / 'first'
    second_root = tmp_path / 'second'
    original_config = git(project, 'config', '--local', '--list').stdout.splitlines()
    setup(artifact, project, first_root)
    before = ((sibling / CONFIG).read_bytes(), git(sibling, 'status', '--porcelain').stdout)
    setup(artifact, sibling, second_root)
    both = (registered(project, first_root), registered(sibling, second_root))
    setup(artifact, project, first_root, 'uninstall')
    after_first = ((project / CONFIG).read_bytes(), registered(sibling, second_root),
                   git(sibling, 'status', '--porcelain').stdout)
    setup(artifact, sibling, second_root, 'uninstall')
    assert (before, both, after_first, (sibling / CONFIG).read_bytes(),
            git(project, 'config', '--local', '--list').stdout.splitlines(),
            list((project / '.git').glob('polyagent-channels-*'))) == (
        (BASE, b''), (True, True), (BASE, True, b''), BASE, original_config, [])


def test_moved_worktree_filter_refuses_add_and_allows_recovery(artifact, tmp_path):
    project = repository(tmp_path / 'project')
    linked, moved, root = (tmp_path / name for name in ('linked', 'moved', 'client'))
    other_root = tmp_path / 'other-client'
    git(project, 'worktree', 'add', '--detach', linked)
    setup(artifact, linked, root)
    setup(artifact, project, other_root)
    git(project, 'worktree', 'move', linked, moved)
    # Force Git to invoke the filter even when index stat data survived the move.
    config = moved / CONFIG
    config.write_bytes(config.read_bytes() + b'# user edit\n')
    result = git(moved, 'add', '--', CONFIG, check=False)
    assert (result.returncode != 0, b'filter scope mismatch' in result.stderr, b'[mcp_servers.channels]' in result.stderr,
            git(moved, 'show', ':' + CONFIG).stdout, git(moved, 'diff', '--cached', '--name-only').stdout) == (
        True, True, False, BASE, b'')
    git(project, 'worktree', 'move', moved, linked)
    setup(artifact, linked, root, 'uninstall')
    git(project, 'worktree', 'move', linked, moved)
    setup(artifact, moved, root)
    assert (registered(moved, root), registered(project, other_root), git(project, 'status', '--porcelain').stdout) == (
        True, True, b'')
    git(moved, 'add', '--', CONFIG)
    assert git(moved, 'show', ':' + CONFIG).stdout == BASE + b'# user edit\n'


@pytest.mark.parametrize('operation', ['install', 'uninstall'])
@pytest.mark.parametrize('relocation', ['move', 'copy'])
def test_stale_driver_refuses_matching_wrapper_only(artifact, tmp_path, operation, relocation):
    project = repository(tmp_path / 'project')
    linked, changed, root = (tmp_path / name for name in ('linked', 'changed', 'client'))
    git(project, 'worktree', 'add', '--detach', linked)
    setup(artifact, linked, root)
    if relocation == 'move':
        git(project, 'worktree', 'move', linked, changed)
    else:
        git(project, 'worktree', 'add', '--detach', changed)
        shutil.copytree(linked / '.codex', changed / '.codex', dirs_exist_ok=True)
    paths = [changed / CONFIG, changed / ATTRIBUTES, changed / '.codex/.gitignore',
             root / 'install-manifest.json', project / '.git/config', *(project / '.git').glob('polyagent-channels-*.pyz')]
    before = [path.read_bytes() for path in paths]
    wrong_target = setup(artifact, changed, tmp_path / 'wrong-client', 'uninstall')
    result = setup(artifact, changed, root, operation, check=False)
    assert (json.loads(wrong_target.stdout)['bindings_removed'], result.returncode,
            'at the original location' in result.stderr, [path.read_bytes() for path in paths]) == (
        [], 1, True, before)


def test_linked_worktree_path_ending_in_newline(artifact, tmp_path):
    project = repository(tmp_path / 'project\n')
    linked = tmp_path / 'linked\n'
    root = tmp_path / 'client'
    git(project, 'worktree', 'add', '--detach', linked)
    setup(artifact, linked, root)
    assert (registered(linked, root), git(linked, 'status', '--porcelain').stdout) == (True, b'')
    git(linked, 'add', '-A')
    assert git(linked, 'show', ':' + CONFIG).stdout == BASE
    setup(artifact, linked, root, 'uninstall')
    assert ((linked / CONFIG).read_bytes(), git(linked, 'status', '--porcelain').stdout) == (BASE, b'')


def test_two_installers_share_git_metadata_without_losing_either_rule(artifact, tmp_path):
    project = repository(tmp_path / 'project')
    other = tmp_path / 'linked'
    git(project, 'worktree', 'add', '--detach', other)
    roots = (tmp_path / 'first', tmp_path / 'second')
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = [pool.submit(setup, artifact, path, root) for path, root in zip((project, other), roots)]
        for job in jobs:
            job.result()
    assert [(registered(path, root), git(path, 'status', '--porcelain').stdout)
            for path, root in zip((project, other), roots)] == [(True, b''), (True, b'')]


def test_submodule_uses_its_own_git_directory(artifact, tmp_path):
    upstream = repository(tmp_path / 'source')
    outer = repository(tmp_path / 'outer')
    git(outer, '-c', 'protocol.file.allow=always', 'submodule', 'add', upstream, 'inner')
    commit(outer, 'add submodule')
    project = outer / 'inner'
    root = tmp_path / 'client'
    setup(artifact, project, root)
    assert ((project / '.git').is_file(), registered(project, root), git(outer, 'status', '--porcelain').stdout) == (
        True, True, b'')


@pytest.mark.parametrize('mode', ['deleted', 'staged-deletion', 'sparse', 'blob-missing'])
def test_missing_tracked_file_stops_before_creating_client_root(artifact, tmp_path, mode):
    project = repository(tmp_path / 'project')
    if mode == 'sparse':
        git(project, 'sparse-checkout', 'set', '--no-cone', '/unrelated')
    elif mode == 'staged-deletion':
        git(project, 'rm', '--', CONFIG)
    else:
        (project / CONFIG).unlink()
    if mode == 'blob-missing':
        blob = index_object(project).decode().strip()
        (project / '.git/objects' / blob[:2] / blob[2:]).unlink()
    root = tmp_path / 'root-parent/client'
    before = git(project, 'status', '--porcelain').stdout
    result = setup(artifact, project, root, check=False)
    assert (result.returncode, 'restore it' in result.stderr, root.parent.exists(), (project / ATTRIBUTES).exists(),
            (project / CONFIG).exists(), git(project, 'status', '--porcelain').stdout) == (1, True, False, False, False, before)


def test_git_rm_cached_with_working_config_uses_plain_install(artifact, tmp_path):
    project = repository(tmp_path / 'project')
    git(project, 'rm', '--cached', '--', CONFIG)
    root = tmp_path / 'client'
    result = setup(artifact, project, root)
    assert (LOCAL in next_step(result), registered(project, root), (project / ATTRIBUTES).exists()) == (False, True, False)


@pytest.mark.parametrize('mode,reason', [('foreign', 'another filter'), ('tracked', 'is tracked')])
def test_incompatible_git_attributes_report_visible_lines(artifact, tmp_path, mode, reason):
    project = repository(tmp_path / 'project')
    if mode == 'foreign':
        (project / ATTRIBUTES).write_text('/config.toml filter=another\n')
    if mode == 'tracked':
        (project / ATTRIBUTES).touch()
        commit(project, 'tracked attributes')
    result = setup(artifact, project, tmp_path / 'client')
    assert (reason in next_step(result), LOCAL in next_step(result), b'[mcp_servers.channels]' in
            git(project, 'diff', '--', CONFIG).stdout) == (True, False, True)


def test_filter_named_unspecified_uses_plain_install(artifact, tmp_path):
    project = repository(tmp_path / 'project')
    attributes = project / ATTRIBUTES
    attributes.write_bytes(b'/config.toml text filter=unspecified\n')
    git(project, 'config', 'filter.unspecified.clean', 'cat')
    git(project, 'config', 'filter.unspecified.smudge', 'cat')
    git(project, 'config', 'filter.unspecified.required', 'true')
    exclude = project / '.git/info/exclude'
    exclude.parent.mkdir(exist_ok=True)
    exclude.write_bytes(b'*.foreign\n')
    foreign = [attributes, project / '.git/config', exclude]
    before = [path.read_bytes() for path in foreign]
    root = tmp_path / 'client'
    result = setup(artifact, project, root)
    assert 'another filter' in next_step(result) and LOCAL not in next_step(result)
    assert [path.read_bytes() for path in foreign] == before
    assert b'[mcp_servers.channels]' in git(project, 'diff', '--', CONFIG).stdout
    setup(artifact, project, root, 'uninstall')
    assert [path.read_bytes() for path in foreign] == before
    assert (project / CONFIG).read_bytes() == BASE


@pytest.mark.parametrize('assignment, filtered', [('!filter', True), ('-filter', False)])
def test_filter_attribute_reset_and_unset(artifact, tmp_path, assignment, filtered):
    project = repository(tmp_path / 'project')
    (project / '.gitattributes').write_text(f'{CONFIG} filter=another\n{CONFIG} {assignment}\n')
    result = setup(artifact, project, tmp_path / 'client')
    assert (LOCAL in next_step(result), clean_object(project) == index_object(project)) == (filtered, filtered)


@pytest.mark.parametrize('ending', [b'', b'\n'], ids=['no-final-newline', 'final-newline'])
def test_local_filter_reset_round_trip(artifact, tmp_path, ending):
    project = repository(tmp_path / 'project')
    root = tmp_path / 'client'
    attributes = project / ATTRIBUTES
    original = b'/config.toml filter=another\n/config.toml !filter' + ending
    attributes.write_bytes(original)

    result = setup(artifact, project, root)
    installed = attributes.read_bytes()
    repeated = setup(artifact, project, root)
    while_installed = (LOCAL in next_step(result), registered(project, root),
                       clean_object(project) == index_object(project), git(project, 'status', '--porcelain').stdout,
                       json.loads(repeated.stdout)['status'], attributes.read_bytes())
    setup(artifact, project, root, 'uninstall')
    setup(artifact, project, root, 'uninstall')

    assert (while_installed, attributes.read_bytes(), (project / CONFIG).read_bytes()) == (
        (True, True, True, b'', 'already_installed', installed), original, BASE)


@pytest.fixture(params=['client-only', 'all-arguments'])
def percent_binding(tmp_path, monkeypatch, request):
    suffix = " space 'quote' %f %%f %%% %s %"
    root = tmp_path / ('client' + suffix)
    if request.param == 'client-only':
        return repository(tmp_path / 'project'), root
    interpreter = tmp_path / ('python' + suffix)
    interpreter.symlink_to(sys.executable)
    monkeypatch.setattr(sys, 'executable', str(interpreter))
    worktree = repository(tmp_path / ('worktree' + suffix))
    project = worktree / ('nested' + suffix)
    configured(project)
    commit(worktree, 'track nested configuration')
    return project, root


def test_clean_keeps_literal_percents_in_filter_arguments(artifact, percent_binding):
    project, root = percent_binding
    result = setup(artifact, project, root)
    git(project, 'add', '--', CONFIG)
    assert (LOCAL in next_step(result), registered(project, root),
            git(project, 'show', ':./' + CONFIG).stdout) == (True, True, BASE)


def test_smudge_keeps_literal_percents_in_filter_arguments(artifact, percent_binding):
    project, root = percent_binding
    setup(artifact, project, root)
    (project / CONFIG).unlink()
    git(project, 'restore', '--', CONFIG)
    assert registered(project, root)


@pytest.fixture
def second_filesystem(tmp_path):
    for directory in (Path('/dev/shm'), Path('/tmp'), Path('/var/tmp')):
        if directory.is_dir() and directory.stat().st_dev != tmp_path.stat().st_dev and os.access(directory, os.W_OK):
            with tempfile.TemporaryDirectory(prefix='chk-channels-', dir=directory) as alternate:
                yield Path(alternate)
            return
    pytest.skip('No writable second filesystem for a linked worktree')


def test_split_filesystem_linked_worktree_uses_plain_install(artifact, tmp_path, second_filesystem):
    peer = repository(tmp_path / 'peer')
    project = second_filesystem / 'linked'
    git(peer, 'worktree', 'add', '-qb', 'linked', str(project))
    peer_root = tmp_path / 'peer-client'
    setup(artifact, peer, peer_root)
    common = peer / '.git'
    exclude = common / 'info/exclude'
    exclude.parent.mkdir(exist_ok=True)
    exclude.write_text('/peer-notes\n')
    metadata = ('config', 'info/exclude', *sorted(path.name for path in common.glob('polyagent-channels-*.pyz')))
    before = {name: (common / name).read_bytes() for name in metadata}

    root = tmp_path / 'linked-client'
    result = setup(artifact, project, root)
    installed = (next_step(result), registered(project, root), (project / ATTRIBUTES).exists(),
                 (project / '.codex/.gitignore').exists(), git(project, 'diff', '--', CONFIG).stdout,
                 {name: (common / name).read_bytes() for name in metadata},
                 tuple(sorted(path.name for path in common.glob('polyagent-channels-*.pyz'))))
    setup(artifact, project, root, 'uninstall')

    assert (
        'Git can see the installed lines because the Git common directory and .codex are on different filesystems.'
        in installed[0], *installed[1:4], b'[mcp_servers.channels]' in installed[4],
        *installed[5:],
        (project / CONFIG).read_bytes(), git(project, 'status', '--porcelain').stdout,
        {name: (common / name).read_bytes() for name in metadata},
        tuple(sorted(path.name for path in common.glob('polyagent-channels-*.pyz'))),
        registered(peer, peer_root), git(peer, 'status', '--porcelain').stdout,
    ) == (True, True, False, False, True, before, metadata[2:], BASE, b'', before, metadata[2:], True, b'')


@pytest.mark.parametrize('state', ['staged', 'committed'])
def test_released_registration_already_in_git_gets_cleanup_notice(artifact, tmp_path, state):
    project = repository(tmp_path / 'project')
    root = tmp_path / 'client'
    path = project / CONFIG
    legacy = released_codex_block(root / 'bin/channels')
    path.write_text(CODEX_SETTING + BASE.decode() + legacy)
    git(project, 'add', '--', CONFIG)
    if state == 'committed':
        git(project, 'commit', '-qm', 'released registration')
    result = setup(artifact, project, root)
    git(project, 'add', '--', CONFIG)
    if state == 'committed':
        git(project, 'commit', '-qm', 'remove released registration from Git')
    assert ('Stage and commit' in next_step(result), 'either may be yours' in next_step(result),
            git(project, 'show', ':' + CONFIG).stdout) == (True, True, CODEX_SETTING.encode() + BASE)


@pytest.mark.parametrize('body', [CODEX_SETTING.encode() + BASE, BASE.rstrip(b'\n')])
def test_legacy_git_restore_preserves_ambiguous_setting_and_separator(artifact, tmp_path, body):
    project = tmp_path / 'project'
    project.mkdir()
    git(project, 'init', '-q', '--template=')
    path = configured(project, body)
    root = tmp_path / 'client'
    setup(artifact, project, root)
    legacy = path.read_bytes()
    commit(project, 'legacy registration')
    setup(artifact, project, root)
    git(project, 'restore', '--source=HEAD', '--worktree', '--', CONFIG)
    git(project, 'add', '--', CONFIG)
    expected = legacy.removesuffix(codex_block(root / 'bin/channels').encode())
    assert (BLOCK in path.read_bytes(), registered(project, root),
            git(project, 'show', ':' + CONFIG).stdout) == (True, True, expected)


@pytest.mark.parametrize('route', ['checkout', 'reinstall', 'uninstall'])
def test_active_filter_preserves_plain_legacy_bytes_despite_stale_manifest(artifact, tmp_path, route):
    project = repository(tmp_path / 'project', BASE.rstrip(b'\n'))
    root = tmp_path / 'client'
    initial = git(project, 'rev-parse', 'HEAD').stdout.strip()
    path = project / CONFIG
    user_content = CODEX_SETTING.encode() + b'model = "legacy"\n'
    legacy = user_content + released_codex_block(root / 'bin/channels').encode()
    path.write_bytes(legacy)
    commit(project, 'released registration')
    released = git(project, 'rev-parse', 'HEAD').stdout.strip()
    git(project, 'switch', '--detach', initial)
    setup(artifact, project, root)
    manifest = json.loads((root / 'install-manifest.json').read_bytes())
    assert manifest['hooks'][0]['created'] == ['newline', 'setting']
    git(project, 'switch', '--detach', released)
    if route != 'checkout':
        # An overlay can put a plain block back without Git invoking smudge.
        path.write_bytes(legacy)
    if route == 'reinstall':
        setup(artifact, project, root)
    if route == 'uninstall':
        setup(artifact, project, root, 'uninstall')
    expected = user_content if route == 'uninstall' else user_content + filtered_block(
        codex_block(root / 'bin/channels'), ()).encode()
    working = path.read_bytes()
    git(project, 'add', '--', CONFIG)
    staged = git(project, 'show', ':' + CONFIG).stdout
    if route != 'uninstall':
        setup(artifact, project, root, 'uninstall')
    assert (working, staged, path.read_bytes()) == (expected, user_content, user_content)


@pytest.mark.parametrize('body', [CODEX_SETTING.encode() + BASE, BASE.rstrip(b'\n'), b''])
def test_legacy_migration_and_uninstall_use_manifest_ownership(artifact, tmp_path, body):
    project = tmp_path / 'project'
    project.mkdir()
    git(project, 'init', '-q', '--template=')
    path = configured(project, body)
    root = tmp_path / 'client'
    setup(artifact, project, root)
    commit(project, 'legacy registration')
    setup(artifact, project, root)
    migrated = path.read_bytes()
    git(project, 'add', '--', CONFIG)
    staged = git(project, 'show', ':' + CONFIG).stdout
    setup(artifact, project, root, 'uninstall')
    assert (BLOCK in migrated, staged, path.read_bytes()) == (True, body, body)


@pytest.mark.parametrize('mode', ['untracked', 'outside', 'no-git'])
def test_projects_without_tracked_configuration_use_plain_install(artifact, tmp_path, monkeypatch, mode):
    project = tmp_path / 'project'
    if mode == 'untracked':
        git(tmp_path, 'init', '-q', '--template=')
    project.mkdir()
    if mode == 'no-git':
        git(project, 'init', '-q', '--template=')
        configured(project)
        commit(project, 'base')
        monkeypatch.setenv('PATH', str(tmp_path))
    root = tmp_path / 'client'
    result = setup(artifact, project, root)
    assert ('filter' in next_step(result), (project / ATTRIBUTES).exists(), registered(project, root)) == (False, False, True)
    setup(artifact, project, root, 'uninstall')
    assert (project / CONFIG).exists() == (mode == 'no-git')
    if mode == 'no-git':
        assert (project / CONFIG).read_bytes() == BASE


def test_git_command_error_stops_before_writes(artifact, tmp_path, monkeypatch):
    project = repository(tmp_path / 'project')
    binaries = tmp_path / 'bin'
    binaries.mkdir()
    executable = binaries / 'git'
    executable.write_text(f'#!/bin/sh\ncase "$3" in check-attr) exit 77;; esac\nexec "{GIT}" "$@"\n')
    executable.chmod(0o700)
    monkeypatch.setenv('PATH', str(binaries))
    root = tmp_path / 'new-parent/client'
    result = setup(artifact, project, root, check=False)
    assert (result.returncode, 'check-attr' in result.stderr, root.parent.exists(), (project / CONFIG).read_bytes()) == (
        1, True, False, BASE)


def test_uninstall_after_losing_the_client_root(artifact, tmp_path):
    project = repository(tmp_path / 'project', BASE.rstrip(b'\n'))
    root = tmp_path / 'client'
    setup(artifact, project, root)
    root.rename(tmp_path / 'lost-client')
    path = project / CONFIG
    binding = CodexGit(project, project / '.git', project, root / 'bin/channels')
    tracked_files = (path, binding.attributes, binding.local_ignore, binding.program, binding.common / 'config')
    before = [file.read_bytes() for file in tracked_files]
    result = setup(artifact, project, root, 'uninstall', check=False)
    assert result.returncode == 1
    assert f'same --target {root}, then uninstall' in result.stderr
    assert [file.read_bytes() for file in tracked_files] == before
    path.unlink()
    git(project, 'restore', '--', CONFIG)
    assert registered(project, root)
    setup(artifact, project, root)
    setup(artifact, project, root, 'uninstall')
    repeated = setup(artifact, project, root, 'uninstall')
    assert 'Nothing is installed' in json.loads(repeated.stdout)['note']
    assert not root.exists()
    assert not binding.has_filter_metadata()
    path.unlink()
    git(project, 'restore', '--', CONFIG)
    assert (path.read_bytes(), git(project, 'status', '--porcelain').stdout) == (BASE.rstrip(b'\n'), b'')


def test_missing_client_root_with_another_roots_filter_is_noop(artifact, tmp_path):
    project = repository(tmp_path / 'project')
    root = tmp_path / 'client'
    setup(artifact, project, root)
    result = setup(artifact, project, tmp_path / 'missing', 'uninstall')
    assert 'Nothing is installed' in json.loads(result.stdout)['note']
    assert registered(project, root)
    assert clean_object(project) == index_object(project)


def test_removing_ignored_attributes_makes_identical_install_repair_filter(artifact, tmp_path):
    project = repository(tmp_path / 'project')
    root = tmp_path / 'client'
    setup(artifact, project, root)
    git(project, 'clean', '-fd')
    before = (project / ATTRIBUTES).exists()
    git(project, 'clean', '-fdx')
    absent = not (project / ATTRIBUTES).exists()
    result = setup(artifact, project, root)
    assert (before, absent, LOCAL in next_step(result), git(project, 'status', '--porcelain').stdout) == (
        True, True, True, b'')


@pytest.mark.parametrize('scope,setting,override', [
    ('worktree', 'clean', 'cat'),
    ('command', 'clean', 'cat'),
    ('command', 'smudge', 'cat'),
    ('worktree', 'required', 'false'),
    ('worktree', 'process', 'cat'),
])
@pytest.mark.parametrize('installed', [False, True])
def test_higher_priority_filter_override_refuses_install(artifact, tmp_path, monkeypatch, scope, setting, override, installed):
    project = repository(tmp_path / 'project')
    root = tmp_path / 'new-parent/client'
    common = project / '.git'
    binding = CodexGit(project, common, project, root / 'bin/channels')
    if installed:
        setup(artifact, project, root)
    key = f'filter.{binding.driver}.{setting}'
    git(project, 'config', 'extensions.worktreeConfig', 'true')
    git(project, 'config', '--worktree', 'user.name', 'Worktree user')
    if scope == 'worktree':
        git(project, 'config', '--worktree', key, override)
    if scope == 'command':
        monkeypatch.setenv('GIT_CONFIG_COUNT', '1')
        monkeypatch.setenv('GIT_CONFIG_KEY_0', key)
        monkeypatch.setenv('GIT_CONFIG_VALUE_0', override)
    paths = [project / CONFIG, common / 'config', common / 'config.worktree']
    before = [path.read_bytes() for path in paths]
    index = index_object(project)
    result = setup(artifact, project, root, check=False)
    assert (result.returncode, 'overrides the channels filter' in result.stderr, result.stdout,
            [path.read_bytes() for path in paths], (project / ATTRIBUTES).exists(),
            root.parent.exists(), index_object(project)) == (1, True, '', before, installed, installed, index)


def test_reinstall_repairs_local_driver_and_preserves_concurrent_config_write(artifact, tmp_path):
    project = repository(tmp_path / 'project')
    root = tmp_path / 'client'
    setup(artifact, project, root)
    binding = CodexGit(project, project / '.git', project, root / 'bin/channels')
    key = f'filter.{binding.driver}.clean'
    git(project, 'config', '--local', key, 'cat')
    result = setup(artifact, project, root, injection='config-peer')
    repeated = setup(artifact, project, root)
    git(project, 'add', '-A')
    assert (LOCAL in next_step(result), json.loads(repeated.stdout)['status'],
            git(project, 'config', '--local', 'user.name').stdout, git(project, 'diff', '--cached').stdout) == (
        True, 'already_installed', b'Concurrent user\n', b'')


@pytest.mark.parametrize('user_setting', [b'', b'thread_unload_delay_secs = 0 # user setting\n'],
                         ids=['installer-added', 'user-owned'])
def test_reinstall_repairs_deleted_unload_setting(artifact, tmp_path, user_setting):
    project = repository(tmp_path / 'project', user_setting + BASE)
    root = tmp_path / 'client'
    path = project / CONFIG
    setup(artifact, project, root)
    prefix = user_setting or CODEX_SETTING.encode()
    installed = path.read_bytes()
    assert installed.startswith(prefix)
    path.write_bytes(installed[len(prefix):])
    damaged = hook_binding_status(root, project, 'codex')
    assert (damaged['status'], damaged['reason']) == ('hook_invalid', 'unload_setting_mismatch')

    setup(artifact, project, root)
    status = hook_binding_status(root, project, 'codex')
    git(project, 'add', '--', CONFIG)
    staged = git(project, 'show', ':' + CONFIG).stdout
    setup(artifact, project, root, 'uninstall')
    assert (status['status'], status['reason'], staged, path.read_bytes()) == ('installed', None, BASE, BASE)


def test_effective_config_is_rechecked_after_local_driver_write(artifact, tmp_path):
    project = repository(tmp_path / 'project')
    root = tmp_path / 'client'
    binding = CodexGit(project, project / '.git', project, root / 'bin/channels')
    git(project, 'config', 'extensions.worktreeConfig', 'true')
    result = setup(artifact, project, root, injection='config-override-after-preflight', check=False)
    key = f'filter.{binding.driver}.clean'
    assert (result.returncode, result.stdout, 'overrides the channels filter' in result.stderr,
            git(project, 'config', '--worktree', key).stdout, (project / ATTRIBUTES).exists(),
            (project / CONFIG).read_bytes()) == (1, '', True, b'cat\n', False, BASE)


@pytest.mark.parametrize('metadata,notice', [
    (ATTRIBUTES, '.codex/.gitattributes is tracked'),
    ('.codex/.gitignore', '.codex/.gitignore is tracked'),
])
def test_switch_with_tracked_metadata_refuses_then_uninstall_allows_switch(artifact, tmp_path, metadata, notice):
    project = repository(tmp_path / 'project')
    original = git(project, 'branch', '--show-current').stdout.decode().strip()
    git(project, 'switch', '-c', 'metadata')
    (project / metadata).write_bytes(b'# tracked metadata\n')
    updated = b'model = "destination"\n'
    (project / CONFIG).write_bytes(updated)
    commit(project, 'track metadata and change config')
    git(project, 'switch', original)
    root = tmp_path / 'client'
    setup(artifact, project, root)
    paths = [project / name for name in (CONFIG, ATTRIBUTES, '.codex/.gitignore')]
    before = [path.read_bytes() for path in paths]
    refused = git(project, 'switch', '--no-overwrite-ignore', 'metadata', check=False)
    assert (refused.returncode, metadata.encode() in refused.stderr,
            git(project, 'branch', '--show-current').stdout.decode().strip(),
            [path.read_bytes() for path in paths]) == (1, True, original, before)
    setup(artifact, project, root, 'uninstall')
    git(project, 'switch', '--no-overwrite-ignore', 'metadata')
    result = setup(artifact, project, root)
    assert (notice in next_step(result), registered(project, root), (project / metadata).read_bytes(),
            git(project, 'show', ':' + CONFIG).stdout, git(project, 'diff', '--cached').stdout) == (
        True, True, b'# tracked metadata\n', updated, b'')


def test_existing_git_attributes_config_and_exclude_survive_lifecycle(artifact, tmp_path):
    project = repository(tmp_path / 'project')
    root = tmp_path / 'client'
    attributes = project / ATTRIBUTES
    exclude = project / '.git/info/exclude'
    attributes.write_bytes(b'*.other -text')
    exclude.parent.mkdir(exist_ok=True)
    exclude.write_bytes(b'/local-only')
    git(project, 'config', 'filter.user-driver.clean', 'cat')
    setup(artifact, project, root)
    setup(artifact, project, root, 'uninstall')
    assert (attributes.read_bytes(), exclude.read_bytes(),
            git(project, 'config', 'filter.user-driver.clean').stdout) == (
        b'*.other -text', b'/local-only', b'cat\n')


def test_git_restore_recreates_the_registration(artifact, tmp_path):
    project = repository(tmp_path / 'project')
    root = tmp_path / 'client'
    setup(artifact, project, root)
    (project / CONFIG).unlink()
    git(project, 'restore', '--', CONFIG)
    assert registered(project, root)


def test_git_checkout_another_versions_config_by_path(artifact, tmp_path):
    project = repository(tmp_path / 'project')
    root = tmp_path / 'client'
    git(project, 'branch', 'old')
    (project / CONFIG).write_bytes(b'model = "new"\n')
    commit(project, 'change base config')
    setup(artifact, project, root)
    git(project, 'checkout', 'old', '--', CONFIG)
    assert (registered(project, root), git(project, 'show', ':' + CONFIG).stdout) == (True, BASE)


def test_git_merge_recreates_the_registration(artifact, tmp_path):
    project = repository(tmp_path / 'project')
    root = tmp_path / 'client'
    git(project, 'branch', 'old')
    (project / CONFIG).write_bytes(b'model = "new"\n')
    commit(project, 'change base config')
    git(project, 'branch', 'new')
    git(project, 'switch', 'old')
    (project / 'own-file').touch()
    commit(project, 'diverge')
    setup(artifact, project, root)
    git(project, 'merge', '--no-edit', 'new')
    assert (registered(project, root), clean_object(project)) == (True, index_object(project))


def test_git_rebase_recreates_the_registration(artifact, tmp_path):
    project = repository(tmp_path / 'project')
    root = tmp_path / 'client'
    git(project, 'branch', 'old')
    (project / CONFIG).write_bytes(b'model = "new"\n')
    commit(project, 'change base config')
    git(project, 'branch', 'new')
    git(project, 'switch', 'old')
    (project / 'own-file').touch()
    commit(project, 'diverge')
    setup(artifact, project, root)
    git(project, 'rebase', 'new')
    assert (registered(project, root), clean_object(project)) == (True, index_object(project))


def test_git_stash_pop_preserves_the_registration_and_user_changes(artifact, tmp_path):
    project = repository(tmp_path / 'project')
    root = tmp_path / 'client'
    setup(artifact, project, root)
    path = project / CONFIG
    path.write_bytes(path.read_bytes().replace(BASE, b'model = "stashed"\n'))
    git(project, 'stash', 'push', '--', CONFIG)
    stashed = registered(project, root)
    git(project, 'stash', 'pop')
    assert (stashed, registered(project, root), b'model = "stashed"\n' in path.read_bytes()) == (True, True, True)


INJECTION = r"""
import os
from pathlib import Path
import shutil
import sys

archive, point, *arguments = sys.argv[1:]
sys.path.insert(0, archive)
sys.argv = [archive, *arguments]
from polyagent_channels import setup

def interrupted():
    raise setup.SetupError('injected write failure')

write = setup.atomic_write
def checked_write(path, content, mode=None, **options):
    if point == 'program' and path.name.startswith('polyagent-channels-') and path.suffix == '.pyz':
        interrupted()
    if point == 'binding' and path.name == 'config.toml':
        interrupted()
    if point == path.name:
        interrupted()
    return write(path, content, mode, **options)
setup.atomic_write = checked_write

replace = os.replace
def checked_replace(source, destination):
    if point == 'killed-binding' and Path(destination).name == 'config.toml':
        os._exit(79)
    if point == 'killed-' + Path(destination).name:
        os._exit(79)
    if point == 'killed-program' and Path(destination).name.startswith('polyagent-channels-'):
        os._exit(79)
    if point == 'index-publish' and Path(destination).name == 'index':
        interrupted()
    return replace(source, destination)
os.replace = checked_replace

open_path = Path.open
def checked_open(path, *args, **kwargs):
    if point == 'index-lock' and path.name == 'index.lock' and args == ('xb',):
        interrupted()
    return open_path(path, *args, **kwargs)
Path.open = checked_open

copy = shutil.copyfileobj
def checked_copy(source, target, *args, **kwargs):
    if point == 'index-snapshot' and Path(target.name).name == 'index.lock':
        target.write(source.read(7))
        interrupted()
    return copy(source, target, *args, **kwargs)
shutil.copyfileobj = checked_copy

run = setup.CodexGit.run
def checked_run(self, *command, **options):
    if command[0] == 'update-index' and point == 'refresh':
        interrupted()
    if command[0] == 'status' and point == 'index-stat':
        run(self, *command, **options)
        interrupted()
    if options.get('index_file') is not None and command[0] in ('update-index', 'status') and point == 'concurrent-' + command[0]:
        path = self.worktree / self.name
        path.write_bytes(path.read_bytes() + b'# concurrent working edit\n')
        writer = run(self, 'add', '--', self.name, accepted=(0, 128))
        if writer.returncode != 128 or b'index.lock' not in writer.stderr:
            raise setup.SetupError('concurrent Git writer was not excluded by index lock')
    if command[-2:] == ('--git-path', 'index') and point == 'staged-before-lock':
        path = self.worktree / self.name
        path.write_bytes(path.read_bytes().replace(b'model = "base"', b'model = "staged peer"'))
        run(self, 'add', '--', self.name)
    if command[0] == 'config' and command[2] in ('--replace-all', '--unset-all'):
        if point == 'config-peer':
            run(self, 'config', '--local', 'user.name', 'Concurrent user')
        if point == 'config-override-after-preflight' and command[3].endswith('.clean'):
            run(self, 'config', '--worktree', command[3], 'cat')
        if point == command[3].rsplit('.', 1)[1]:
            interrupted()
    return run(self, *command, **options)
setup.CodexGit.run = checked_run

unlink = Path.unlink
def checked_unlink(path, *args, **kwargs):
    if point == 'killed-unlink-' + path.name:
        os._exit(79)
    if point == 'unlink-program' and path.name.startswith('polyagent-channels-') and path.suffix == '.pyz':
        interrupted()
    if point == 'unlink-' + path.name:
        interrupted()
    if point == 'unlink-metadata-temp' and '-metadata-' in path.name and path.name.startswith('.polyagent-channels-'):
        interrupted()
    if point == 'unlink-binding-temp' and '-config-' in path.name and path.name.startswith('.polyagent-channels-'):
        interrupted()
    return unlink(path, *args, **kwargs)
Path.unlink = checked_unlink

sys.exit(setup.main())
"""


@pytest.mark.parametrize('point', [
    'program', 'clean', 'smudge', 'required', '.gitignore', '.gitattributes', 'binding', 'refresh',
])
@pytest.mark.parametrize('recover', ['retry', 'uninstall'])
def test_interrupted_install_never_exposes_registration(artifact, tmp_path, point, recover):
    project = repository(tmp_path / 'project')
    root = tmp_path / 'client'
    expected = index_object(project)
    failed = setup(artifact, project, root, injection=point, check=False)
    hidden = (clean_object(project), index_object(project))
    if recover == 'retry':
        setup(artifact, project, root)
        assert registered(project, root)
    setup(artifact, project, root, 'uninstall')
    assert (failed.returncode, 'injected write failure' in failed.stderr, hidden, (project / CONFIG).read_bytes(),
            git(project, 'status', '--porcelain').stdout, list((project / '.git').glob('polyagent-channels-*'))) == (
        1, True, (expected, expected), BASE, b'', [])


@pytest.mark.parametrize('point', [
    'binding', '.gitattributes', 'refresh', '.gitignore', 'clean', 'smudge', 'required', 'unlink-program',
])
def test_interrupted_uninstall_can_be_retried(artifact, tmp_path, point):
    project = repository(tmp_path / 'project')
    root = tmp_path / 'client'
    (project / ATTRIBUTES).write_text('*.other -text\n')
    exclude = project / '.git/info/exclude'
    exclude.parent.mkdir(exist_ok=True)
    exclude.write_text('/.codex/.gitattributes\n')
    (project / '.codex/.gitignore').write_text('*.other\n')
    setup(artifact, project, root)
    with exclude.open('a') as lines:
        lines.write('/.codex/.gitignore\n')
    expected = index_object(project)
    failed = setup(artifact, project, root, 'uninstall', injection=point, check=False)
    hidden = (clean_object(project), index_object(project))
    setup(artifact, project, root, 'uninstall')
    assert (failed.returncode, 'injected write failure' in failed.stderr, hidden, (project / CONFIG).read_bytes(),
            git(project, 'status', '--porcelain').stdout, list((project / '.git').glob('polyagent-channels-*'))) == (
        1, True, (expected, expected), BASE, b'', [])


def test_identical_install_skips_filter_writes(artifact, tmp_path, monkeypatch):
    monkeypatch.setenv('GIT_OPTIONAL_LOCKS', '0')
    project = repository(tmp_path / 'project')
    root = tmp_path / 'client'
    setup(artifact, project, root)
    paths = [project / path for path in (CONFIG, ATTRIBUTES, '.codex/.gitignore', '.git/config', '.git/index')]
    paths.extend((project / '.git').glob('polyagent-channels-*.pyz'))
    times = [path.stat().st_mtime_ns for path in paths]
    result = setup(artifact, project, root)
    assert (json.loads(result.stdout)['status'], [path.stat().st_mtime_ns for path in paths]) == (
        'already_installed', times)


@pytest.mark.parametrize('project_name', ['product', 'product\n'], ids=['ordinary', 'trailing-newline'])
def test_base_import_applies_checkout_filter_to_new_config(artifact, tmp_path, project_name):
    upstream = repository(tmp_path / 'upstream')
    project = repository(tmp_path / project_name)
    root = tmp_path / 'client'
    new_base = b' model = "new base"'
    (upstream / CONFIG).write_bytes(new_base)
    commit(upstream, 'new base')
    setup(artifact, project, root)
    script = Path(__file__).resolve().parents[1] / 'scripts/update_devcontainer_base.py'
    result = subprocess.run([sys.executable, script, upstream, 'HEAD'], cwd=project, capture_output=True,
                            check=True, timeout=30)
    assert (result.returncode, registered(project, root), clean_object(project)) == (
        0, True, git(upstream, 'rev-parse', 'HEAD:' + CONFIG).stdout)


@pytest.mark.parametrize('tracked_ignore', [False, True])
def test_linked_worktree_user_attributes_remain_untracked_and_visible(artifact, tmp_path, tracked_ignore):
    project = repository(tmp_path / 'project')
    if tracked_ignore:
        (project / '.codex/.gitignore').write_text('*.other\n')
        commit(project, 'tracked ignore')
    other = tmp_path / 'linked'
    git(project, 'worktree', 'add', '--detach', other)
    (other / ATTRIBUTES).write_text('*.other -text\n')
    before = git(other, 'status', '--porcelain').stdout
    setup(artifact, project, tmp_path / 'client')
    assert (before, git(other, 'status', '--porcelain').stdout) == (b'?? .codex/.gitattributes\n', before)


def test_tracked_ignore_fallback_preserves_user_git_metadata(artifact, tmp_path):
    project = repository(tmp_path / 'project')
    tracked = project / '.codex/.gitignore'
    tracked.write_text('*.other\n')
    commit(project, 'tracked ignore')
    root = tmp_path / 'client'
    exclude = project / '.git/info/exclude'
    exclude.parent.mkdir(exist_ok=True)
    exclude.write_text('/keep-local\n')
    paths = (tracked, exclude, project / '.git/config')
    before = tuple(path.read_bytes() for path in paths)
    result = setup(artifact, project, root)
    visible = b'[mcp_servers.channels]' in git(project, 'diff', '--', CONFIG).stdout
    while_installed = tuple(path.read_bytes() for path in paths)
    setup(artifact, project, root, 'uninstall')
    assert ('.codex/.gitignore is tracked' in next_step(result), LOCAL in next_step(result), visible,
            while_installed, tuple(path.read_bytes() for path in paths), (project / ATTRIBUTES).exists()) == (
        True, False, True, before, before, False)


@pytest.mark.parametrize('point', ['index-lock', 'index-snapshot', 'index-stat', 'index-publish'])
def test_index_refresh_write_failure_can_be_retried(artifact, tmp_path, point):
    project = repository(tmp_path / 'project')
    root = tmp_path / 'client'
    index = project / '.git/index'
    original = index.read_bytes()
    failed = setup(artifact, project, root, injection=point, check=False)
    after_failure = (index.read_bytes(), clean_object(project), index_object(project))
    setup(artifact, project, root)
    assert (failed.returncode, 'injected write failure' in failed.stderr, after_failure,
            list(index.parent.glob('index.lock*')), git(project, 'status', '--porcelain').stdout) == (
        1, True, (original, after_failure[2], after_failure[2]), [], b'')


@pytest.mark.parametrize('split_index', [False, True])
@pytest.mark.parametrize('command', ['update-index', 'status'])
def test_refresh_leaves_concurrent_working_edit_unstaged(artifact, tmp_path, split_index, command):
    project = repository(tmp_path / 'project')
    if split_index:
        git(project, 'update-index', '--split-index')
    original = index_object(project)
    setup(artifact, project, tmp_path / 'client', injection='concurrent-' + command)
    assert (index_object(project), git(project, 'diff', '--', CONFIG).stdout.splitlines()[-1:]) == (
        original, [b'+# concurrent working edit'])


def test_refresh_keeps_index_change_committed_before_lock(artifact, tmp_path):
    project = repository(tmp_path / 'project')
    setup(artifact, project, tmp_path / 'client', injection='staged-before-lock')
    assert git(project, 'show', ':' + CONFIG).stdout == b'model = "staged peer"\n'


@pytest.mark.parametrize('flag', ['assume-unchanged', 'skip-worktree'])
def test_refresh_preserves_git_index_flags(artifact, tmp_path, flag):
    project = repository(tmp_path / 'project')
    git(project, 'update-index', '--' + flag, CONFIG)
    before = git(project, 'ls-files', '-v', CONFIG).stdout
    setup(artifact, project, tmp_path / 'client')
    setup(artifact, project, tmp_path / 'client', 'uninstall')
    assert git(project, 'ls-files', '-v', CONFIG).stdout == before


def test_refresh_preserves_intent_to_add(artifact, tmp_path):
    project = repository(tmp_path / 'project')
    pending = project / 'nested'
    configured(pending, b'')
    name = 'nested/' + CONFIG
    git(project, 'add', '--intent-to-add', name)
    before = git(project, 'ls-files', '--debug', name).stdout
    setup(artifact, pending, tmp_path / 'client')
    assert git(project, 'ls-files', '--debug', name).stdout == before


def test_refresh_preserves_index_executable_mode(artifact, tmp_path):
    project = repository(tmp_path / 'project')
    (project / CONFIG).chmod(0o755)
    commit(project, 'executable')
    before = git(project, 'ls-files', '--stage', CONFIG).stdout
    setup(artifact, project, tmp_path / 'client')
    assert git(project, 'ls-files', '--stage', CONFIG).stdout == before


@pytest.mark.parametrize('point', ['unlink-.gitattributes', 'unlink-.gitignore'])
@pytest.mark.parametrize('abrupt', [False, True])
def test_uninstall_retries_removing_metadata_files(artifact, tmp_path, point, abrupt):
    project = repository(tmp_path / 'project')
    root = tmp_path / 'client'
    setup(artifact, project, root)
    expected = index_object(project)
    failed = setup(artifact, project, root, 'uninstall', injection='killed-' + point if abrupt else point, check=False)
    hidden = clean_object(project) == expected
    setup(artifact, project, root, 'uninstall')
    assert (failed.returncode, 'injected write failure' in failed.stderr, hidden,
            git(project, 'status', '--porcelain').stdout) == (79 if abrupt else 1, not abrupt, True, b'')


@pytest.mark.parametrize('project_name', ['project', 'project\n'], ids=['ordinary', 'trailing-newline'])
def test_base_lock_hash_sees_real_edit_with_filter_installed(artifact, tmp_path, project_name):
    if find_spec('test_devcontainer_base') is None:
        pytest.skip('test_devcontainer_base is excluded from the source distribution')
    from test_devcontainer_base import working_blob, working_git

    project = repository(tmp_path / project_name)
    root = tmp_path / 'client'
    setup(artifact, project, root)
    command = working_git(project, tmp_path)
    expected = index_object(project).decode().strip()
    before = working_blob(command, project, CONFIG)
    path = project / CONFIG
    path.write_bytes(path.read_bytes().replace(BASE, b'model = "changed"\n'))
    after = working_blob(command, project, CONFIG)
    assert (before == expected, after == expected) == (True, False)


def test_sha256_clone_passes_pinned_base_lock_with_channel_installed(artifact, tmp_path):
    if find_spec('test_devcontainer_base') is None:
        pytest.skip('test_devcontainer_base is excluded from the source distribution')
    from test_devcontainer_base import working_blob, working_git

    source = Path(__file__).resolve().parents[1]
    lock = json.loads((source / 'devcontainer-base.lock.json').read_bytes())['blobs']
    upstream = tmp_path / 'upstream'
    upstream.mkdir()
    git(upstream, 'init', '-q', '--template=', '--object-format=sha256')
    for name in lock:
        path = upstream / name
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / name, path)
    commit(upstream, 'pinned base')
    project = tmp_path / 'project'
    git(tmp_path, 'clone', '-q', upstream, project)
    assert git(project, 'rev-parse', '--show-object-format').stdout == b'sha256\n'
    root = tmp_path / 'client'
    setup(artifact, project, root)
    assert registered(project, root)
    command = working_git(project, tmp_path)
    assert {name: working_blob(command, project, name) for name in lock} == lock
    path = project / CONFIG
    path.write_bytes(path.read_bytes() + b'\n# genuine change\n')
    assert working_blob(command, project, CONFIG) != lock[CONFIG]


def test_codex_lost_root_refusal_explains_restore_then_move(artifact, tmp_path):
    project = repository(tmp_path / 'project')
    old = tmp_path / 'old home/client-alt'
    new = tmp_path / 'new-client'
    setup(artifact, project, old)
    path = project / CONFIG
    before = path.read_bytes()
    indexed = index_object(project)
    assignment = git(project, 'check-attr', 'filter', '--', CONFIG).stdout
    old.parent.rename(tmp_path / 'saved home')
    refused = setup(artifact, project, new, check=False)
    assert (refused.returncode, shlex.quote(str(old / 'bin/channels')) in refused.stderr,
            f'install with --target {shlex.quote(str(old))} to restore it, then uninstall there before moving' in refused.stderr,
            'If this is a Polyagent Channels binding from a missing client root' in refused.stderr,
            path.read_bytes(), index_object(project), git(project, 'check-attr', 'filter', '--', CONFIG).stdout) == (
        1, True, True, True, before, indexed, assignment)
    removed = setup(artifact, project, new, 'uninstall')
    assert (json.loads(removed.stdout)['kept'], path.read_bytes()) == ([], before)
    setup(artifact, project, old)
    setup(artifact, project, old, 'uninstall')
    assert (path.read_bytes(), git(project, 'check-attr', 'filter', '--', CONFIG).stdout) == (
        BASE, f'{CONFIG}: filter: unspecified\n'.encode())
    setup(artifact, project, new)
    assert registered(project, new)


def test_uninstall_with_different_client_root_keeps_the_active_filter(artifact, tmp_path):
    project = repository(tmp_path / 'project')
    other = repository(tmp_path / 'other')
    root = tmp_path / 'client'
    wrong_root = tmp_path / 'different'
    setup(artifact, project, root)
    setup(artifact, other, wrong_root)
    setup(artifact, project, wrong_root, 'uninstall')
    assert (registered(project, root), git(project, 'status', '--porcelain').stdout,
            registered(other, wrong_root)) == (True, b'', True)



@pytest.mark.parametrize('recover', ['retry', 'uninstall'])
def test_abrupt_exit_before_binding_replace_leaves_no_stageable_temp(artifact, tmp_path, recover):
    project = repository(tmp_path / 'project')
    root = tmp_path / 'client'
    failed = setup(artifact, project, root, injection='killed-binding', check=False)
    temporary = list((project / '.codex').glob('.polyagent-channels-*-config-*'))
    git(project, 'add', '-A')
    staged = git(project, 'diff', '--cached').stdout
    if recover == 'retry':
        setup(artifact, project, root)
        assert registered(project, root)
    setup(artifact, project, root, 'uninstall')
    assert (failed.returncode, len(temporary), staged, git(project, 'status', '--porcelain').stdout,
            list((project / '.codex').glob('.polyagent-channels-*-config-*'))) == (79, 1, b'', b'', [])


def test_abrupt_uninstall_binding_replace_leaves_only_owned_private_temp(artifact, tmp_path):
    primary = repository(tmp_path / 'project')
    project = tmp_path / 'linked'
    git(primary, 'worktree', 'add', '--detach', project)
    root = tmp_path / 'client'
    other_root = tmp_path / 'other-client'
    setup(artifact, primary, other_root)
    setup(artifact, project, root)
    private = primary / '.git'
    foreign = private / '.polyagent-channels-other-metadata-keep'
    foreign.write_bytes(b'keep')
    failed = setup(artifact, project, root, 'uninstall', injection='killed-binding', check=False)
    assert failed.returncode == 79
    git(project, 'add', '-A')
    assert git(project, 'diff', '--cached').stdout == b''
    assert len(set(private.glob('.polyagent-channels-*-metadata-*')) - {foreign}) == 1
    unrelated = project / '.codex/.channels-keep'
    unrelated.write_bytes(b'user data')
    path = project / CONFIG
    edited = BASE + b'# user edit after interruption\n'
    path.write_bytes(path.read_bytes().replace(BASE, edited))
    setup(artifact, project, root, 'uninstall')
    assert path.read_bytes() == edited
    assert git(project, 'status', '--porcelain', '--', CONFIG).stdout == b' M .codex/config.toml\n'
    assert unrelated.read_bytes() == b'user data'
    assert set(private.glob('.polyagent-channels-*-metadata-*')) == {foreign}
    assert foreign.read_bytes() == b'keep'
    assert registered(primary, other_root)
    assert git(primary, 'status', '--porcelain').stdout == b''



@pytest.mark.parametrize('point', ['program', '.gitignore', '.gitattributes'])
@pytest.mark.parametrize('recover', ['retry', 'uninstall'])
def test_abrupt_metadata_install_leaves_only_private_temp(artifact, tmp_path, point, recover):
    primary = repository(tmp_path / 'project')
    project = tmp_path / 'linked'
    git(primary, 'worktree', 'add', '--detach', project)
    root = tmp_path / 'client'
    private = primary / '.git'
    foreign = private / '.polyagent-channels-other-metadata-keep'
    foreign.write_bytes(b'other binding')
    failed = setup(artifact, project, root, injection='killed-' + point, check=False)
    temporary = set(private.glob('.polyagent-channels-*-metadata-*')) - {foreign}
    git(project, 'add', '-A')
    staged = git(project, 'diff', '--cached').stdout
    if recover == 'retry':
        setup(artifact, project, root)
        assert registered(project, root)
    setup(artifact, project, root, 'uninstall')
    assert (failed.returncode, len(temporary), staged, git(project, 'status', '--porcelain').stdout,
            git(primary, 'status', '--porcelain').stdout,
            list(private.glob('.polyagent-channels-*-metadata-*')), foreign.read_bytes()) == (
        79, 1, b'', b'', b'', [foreign], b'other binding')


@pytest.mark.parametrize('point', ['.gitignore', '.gitattributes'])
def test_abrupt_metadata_uninstall_preserves_user_rules_on_retry(artifact, tmp_path, point):
    project = repository(tmp_path / 'project')
    root = tmp_path / 'client'
    attrs, ignore = project / ATTRIBUTES, project / '.codex/.gitignore'
    attrs.write_bytes(b'*.other -text\n')
    ignore.write_bytes(b'*.other\n')
    setup(artifact, project, root)
    exclude = project / '.git/info/exclude'
    exclude.parent.mkdir(exist_ok=True)
    exclude.write_text('/.codex/.gitattributes\n/.codex/.gitignore\n')
    failed = setup(artifact, project, root, 'uninstall', injection='killed-' + point, check=False)
    temporary = list((project / '.git').glob('.polyagent-channels-*-metadata-*'))
    git(project, 'add', '-A')
    staged = git(project, 'diff', '--cached').stdout
    setup(artifact, project, root, 'uninstall')
    assert (failed.returncode, len(temporary), staged, git(project, 'status', '--porcelain').stdout,
            attrs.read_bytes(), ignore.read_bytes(), list((project / '.git').glob('.polyagent-channels-*-metadata-*'))) == (
        79, 1, b'', b'', b'*.other -text\n', b'*.other\n', [])


@pytest.mark.parametrize('action', ['install', 'uninstall'])
@pytest.mark.parametrize('origin', ['install', 'uninstall'])
def test_failed_abandoned_metadata_cleanup_can_be_retried(artifact, tmp_path, action, origin):
    project = repository(tmp_path / 'project')
    root = tmp_path / 'client'
    if origin == 'uninstall':
        setup(artifact, project, root)
    point = 'killed-binding' if origin == 'uninstall' else 'killed-.gitignore'
    killed = setup(artifact, project, root, origin, injection=point, check=False)
    assert killed.returncode == 79
    failed = setup(artifact, project, root, action, injection='unlink-metadata-temp', check=False)
    git(project, 'add', '-A')
    staged = git(project, 'diff', '--cached').stdout
    setup(artifact, project, root, action)
    setup(artifact, project, root, 'uninstall')
    assert (failed.returncode, 'injected write failure' in failed.stderr, staged,
            git(project, 'status', '--porcelain').stdout) == (1, True, b'', b'')


def test_failed_abandoned_binding_cleanup_can_be_retried(artifact, tmp_path):
    project = repository(tmp_path / 'project')
    root = tmp_path / 'client'
    setup(artifact, project, root, injection='killed-binding', check=False)
    failed = setup(artifact, project, root, 'uninstall', injection='unlink-binding-temp', check=False)
    git(project, 'add', '-A')
    staged = git(project, 'diff', '--cached').stdout
    setup(artifact, project, root, 'uninstall')
    assert (failed.returncode, staged, git(project, 'status', '--porcelain').stdout) == (1, b'', b'')


def test_plain_uninstall_keeps_the_fallback_projects_tracked_attributes_symlink(artifact, tmp_path):
    project = repository(tmp_path / 'project')
    root = tmp_path / 'client'
    (project / ATTRIBUTES).symlink_to('unrelated')
    commit(project, 'track attributes symlink')
    setup(artifact, project, root)
    setup(artifact, project, root, 'uninstall')
    assert ((project / ATTRIBUTES).is_symlink(), git(project, 'status', '--porcelain').stdout) == (True, b'')
