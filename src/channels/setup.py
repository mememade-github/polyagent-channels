"""Read-only plan and explicit local installation; no credential acquisition."""
import argparse
from contextlib import contextmanager, ExitStack
import fcntl
import stat
import json
import os
import shlex
import sys
import tempfile
from pathlib import Path

from channels.client_config import ConfigError, DEFAULT_ROOT, profile_paths, read_config, service_url


class SetupError(ValueError):
    """A diagnostic containing only installer-authored safe text."""


def atomic_write(path, content, mode=0o600):
    if path.is_symlink():
        raise SetupError('Refusing symlink destination')
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix='.channels-')
    try:
        with os.fdopen(descriptor, 'wb') as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def canonical_path(path):
    path = Path(path).expanduser().absolute()
    if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
        raise SetupError('Refusing symlink path')
    return Path(os.path.abspath(path))


def wrapper_content(interpreter, root):
    return ('#!/bin/sh\n# polyagent-channels managed wrapper\nexec ' +
            shlex.join([str(interpreter), str(root / 'harness.pyz'), '--config',
                        str(root / 'config.json')]) + ' "$@"\n').encode()


def managed_binding(command):
    """Return a verified binding, unrelated None, or fail on uncertain receivers."""
    try:
        words = shlex.split(command)
    except ValueError:
        if 'channels' in command and 'hook' in command:
            raise SetupError('Ambiguous hook; inspect indicated Stop position') from None
        return None
    if not words or 'hook' not in words:
        return None
    executable = Path(words[0])
    if executable.name != 'channels':
        if any(Path(word).name == 'channels' for word in words[1:]):
            raise SetupError('Ambiguous indirect receiver; inspect indicated Stop position')
        return None
    if not executable.is_absolute():
        raise SetupError('Ambiguous hook; inspect indicated Stop position')
    try:
        executable = canonical_path(executable)
        content = executable.read_bytes()
    except (OSError, ValueError):
        raise SetupError('Ambiguous hook; inspect indicated Stop position') from None
    header = b'#!/bin/sh\n# polyagent-channels managed wrapper\n'
    if not content.startswith(header):
        return None
    try:
        execution = shlex.split(content.decode().splitlines()[2])
        root = executable.parent.parent
        if not Path(execution[1]).is_absolute():
            raise ValueError()
        expected = wrapper_content(Path(execution[1]), root)
        if executable != root / 'bin/channels' or content != expected:
            raise ValueError()
        if len(words) != 4 or words[1] != '--profile' or words[3] != 'hook':
            raise ValueError()
        profile_paths(root / 'config.json', words[2])
    except (IndexError, UnicodeError, ValueError):
        raise SetupError('Ambiguous managed hook; inspect indicated Stop position') from None
    return (str(root), words[2])


def managed_wrapper_root(wrapper):
    binding = managed_binding(shlex.join([str(wrapper), '--profile', 'ownership-check', 'hook']))
    return binding[0] if binding else None


def hook_document(path, command, replace=False):
    data = json.loads(path.read_text()) if path.exists() else {}
    stops = data.setdefault('hooks', {}).setdefault('Stop', [])
    selected = shlex.split(command)
    binding = (str(Path(selected[0]).parent.parent), selected[2])
    retained = []
    previous = []
    for entry_index, entry in enumerate(stops):
        hooks = []
        for hook_index, hook in enumerate(entry.get('hooks', [])):
            try:
                owned = managed_binding(hook.get('command', ''))
            except ValueError:
                raise SetupError(f'Ambiguous receiver at Stop[{entry_index}].hooks[{hook_index}]; inspect/remove it manually before install') from None
            if owned is None:
                hooks.append(hook)
                continue
            previous.append({'root': owned[0], 'profile': owned[1]})
            if owned != binding and not replace:
                raise SetupError(f'Native scope conflict at Stop[{entry_index}].hooks[{hook_index}]; use plan --replace to review a managed rebind')
        if hooks or not entry.get('hooks'):
            retained.append({**entry, 'hooks': hooks})
    retained.append({'hooks': [{'type': 'command', 'command': command, 'timeout': 60}]})
    data['hooks']['Stop'] = retained
    return (json.dumps(data, indent=2) + '\n').encode(), previous


@contextmanager
def installation_lock(path):
    canonical_path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode) or details.st_size or details.st_nlink != 1:
            raise SetupError('Invalid installation lock destination')
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def main(arguments=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('plan', 'install'))
    parser.add_argument('--url', required=True)
    parser.add_argument('--profile', required=True)
    parser.add_argument('--target', type=Path, default=DEFAULT_ROOT)
    parser.add_argument('--client', choices=('claude', 'codex', 'nohook'), required=True)
    parser.add_argument('--project', type=Path, default=Path.cwd())
    parser.add_argument('--replace', action='store_true', help='Explicitly rebind confirmed managed Stop receivers')
    options = parser.parse_args(arguments)
    try:
        execute(options)
    except ConfigError as error:
        print('channel setup failed: ' + error.code, file=sys.stderr)
        return 1
    except SetupError as error:
        print('channel setup failed: ' + str(error), file=sys.stderr)
        return 1
    except Exception as error:
        print('channel setup failed: ' + type(error).__name__ + ' (check URL, paths and existing settings)', file=sys.stderr)
        return 1
    return 0


def native_hook(options):
    if options.client == 'nohook':
        return None
    relative = '.claude/settings.local.json' if options.client == 'claude' else '.codex/hooks.json'
    return canonical_path(canonical_path(options.project) / relative)


def execute(options):
    root = canonical_path(options.target)
    hook = native_hook(options)
    if options.action == 'plan':
        return prepare(options)
    locks = {root / '.install.lock'}
    if hook:
        locks.add(hook.with_name(hook.name + '.channels.lock'))
    # Every installer uses the same total ordering, including across shared roots.
    with ExitStack() as stack:
        for lock in sorted(locks):
            stack.enter_context(installation_lock(lock))
        prepare(options)


def prepare(options):
    root = canonical_path(options.target)
    config = root / 'config.json'
    url = service_url(options.url)
    credential, state = profile_paths(config, options.profile)
    wrapper = root / 'bin/channels'
    archive = root / 'harness.pyz'
    interpreter = Path(os.path.abspath(sys.executable))
    command = shlex.join([str(wrapper), '--profile', options.profile, 'hook'])
    hook = native_hook(options)
    for destination in (config, wrapper, archive, credential, state):
        canonical_path(destination)
    if config.exists() and read_config(config) != url:
        raise SetupError('Shared URL change requires ordinary manual configuration')
    hook_bytes, previous = hook_document(hook, command, options.replace) if hook else (None, [])
    wrapper_bytes = wrapper_content(interpreter, root)
    if wrapper.exists() and wrapper.read_bytes() != wrapper_bytes and managed_wrapper_root(wrapper) is None:
        raise FileExistsError('Unrelated wrapper')
    source = canonical_path(sys.argv[0])
    if options.action == 'install' and (source.suffix != '.pyz' or not source.is_file()):
        raise SetupError('Install requires the downloaded zipapp')
    if archive.exists() and archive != source:
        import zipfile
        with zipfile.ZipFile(archive) as existing:
            if existing.read('POLYAGENT-HARNESS') != b'1\n':
                raise FileExistsError('Unrelated archive')
    print(json.dumps({'action': options.action, 'profile': options.profile, 'url': url,
                      'interpreter': str(interpreter), 'archive': str(archive), 'wrapper': str(wrapper), 'config': str(config),
                      'credential': str(credential), 'state': str(state), 'hook': str(hook) if hook else None,
                      'previous_bindings': previous, 'new_binding': {'root': str(root), 'profile': options.profile},
                      'actions': ['copy archive', 'write URL-only config and absolute wrapper', 'merge selected Stop hook'],
                      'connection': 'Only the chosen service; plan/install make no network requests',
                      'credential_setup': 'Operator must provision this protected file or use register with an administrator key; no credentials copied',
                      'native_activation': 'Review native /hooks trust and enable project hooks where required; registration does not prove activation'}))
    if options.action == 'plan':
        return
    for directory in (root, wrapper.parent, credential.parent, state):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    credential.parent.chmod(0o700)
    atomic_write(archive, source.read_bytes())
    atomic_write(config, (json.dumps({'url': url}) + '\n').encode())
    atomic_write(wrapper, wrapper_bytes, 0o700)
    if hook:
        hook.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(hook, hook_bytes)
