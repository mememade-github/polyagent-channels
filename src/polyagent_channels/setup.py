"""Read-only plan, shared-key client installation and conservative uninstall."""
import argparse
from contextlib import contextmanager, ExitStack
import fcntl
import stat
import json
import os
import shlex
import sys
import tempfile
import hashlib
import re
import zipfile
import time
from pathlib import Path

from polyagent_channels.client_config import ConfigError, DEFAULT_ROOT, read_config, service_url

NATIVE_HOOK_PATHS = {'claude': '.claude/settings.local.json', 'codex': '.codex/hooks.json'}


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
        if len(words) != 4 or words[1] not in ('--profile', '--client') or words[3] != 'hook':
            raise ValueError()
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', words[2]):
            raise ValueError()
        if words[1] == '--client' and words[2] not in ('claude', 'codex'):
            raise ValueError()
    except (IndexError, UnicodeError, ValueError):
        raise SetupError('Ambiguous managed hook; inspect indicated Stop position') from None
    return (str(root), words[1], words[2])


def managed_wrapper_root(wrapper):
    binding = managed_binding(shlex.join([str(wrapper), '--profile', 'ownership-check', 'hook']))
    return binding[0] if binding else None


def hook_document(path, command, replace=False, remove=False):
    data = json.loads(path.read_text()) if path.exists() else {}
    stops = data.setdefault('hooks', {}).setdefault('Stop', [])
    selected = shlex.split(command)
    binding = (str(Path(selected[0]).parent.parent), '--client', selected[2])
    retained = []
    previous = []
    for entry_index, entry in enumerate(stops):
        hooks = []
        for hook_index, hook in enumerate(entry.get('hooks', [])):
            try:
                owned = managed_binding(hook.get('command', ''))
            except ValueError:
                raise SetupError(f'Ambiguous receiver at Stop[{entry_index}].hooks[{hook_index}]; inspect manually') from None
            exact = hook.get('type') == 'command' and hook.get('command') == command
            if remove:
                if exact and owned == binding:
                    continue
                hooks.append(hook)
                continue
            if owned is None or (owned[1] == '--client' and owned[2] != selected[2]):
                hooks.append(hook)
                continue
            if owned != binding and not (replace and owned[1] == '--profile'):
                raise SetupError('Native scope conflict; --replace only replaces verified legacy callbacks')
            previous.append(list(owned))
        if hooks or not entry.get('hooks'):
            retained.append({**entry, 'hooks': hooks})
    if not remove:
        retained.append({'hooks': [{'type': 'command', 'command': command, 'timeout': 660}]})
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


@contextmanager
def lifecycle_lock(root, exclusive=False):
    """Keep one inode shared by CLI users and exclusive installation changes."""
    path = canonical_path(root / '.lifecycle.lock')
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode) or details.st_size or details.st_nlink != 1:
            raise SetupError('Invalid lifecycle lock destination')
        operation = fcntl.LOCK_SH
        if exclusive:
            operation = fcntl.LOCK_EX | fcntl.LOCK_NB
        try:
            fcntl.flock(descriptor, operation)
        except BlockingIOError:
            raise SetupError('Installation in use; stop CLI operations and hooks before changing installation') from None
        yield
    finally:
        os.close(descriptor)


def main(arguments=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('plan', 'install', 'uninstall'))
    parser.add_argument('--url')
    parser.add_argument('--target', type=Path, default=DEFAULT_ROOT)
    parser.add_argument('--client', choices=('claude', 'codex'))
    parser.add_argument('--project', type=Path, default=Path.cwd())
    parser.add_argument('--key-file', type=Path)
    parser.add_argument('--purge-key', action='store_true')
    parser.add_argument('--replace', action='store_true')
    options = parser.parse_args(arguments)
    if options.action != 'uninstall' and (not options.url or not options.client):
        parser.error('plan/install require --url and --client')
    try:
        execute(options)
    except ConfigError as error:
        print('channel setup failed: ' + error.code, file=sys.stderr)
        return 1
    except SetupError as error:
        print('channel setup failed: ' + str(error), file=sys.stderr)
        return 1
    except Exception as error:
        print('channel setup failed: ' + type(error).__name__ + ' (check paths and settings)', file=sys.stderr)
        return 1
    return 0


def native_hook(project, client):
    return canonical_path(project / NATIVE_HOOK_PATHS[client])


def native_resume_command(client, project, session_id=None):
    from polyagent_channels.exchange import validate_session_id
    name = {'claude': 'CLAUDE_CODE_SESSION_ID', 'codex': 'CODEX_THREAD_ID'}.get(client)
    current = os.getenv(name) if name else None
    if not current or (session_id is not None and session_id != current):
        return None
    try:
        validate_session_id(current)
    except ValueError:
        return None
    arguments = [client, '--resume' if client == 'claude' else 'resume', current]
    return shlex.join(['cd', '--', str(project)]) + ' && ' + shlex.join(arguments)


def hook_binding_status(root, project, client):
    """Inspect this project/client's registered Stop binding without changing it."""
    hook = project / NATIVE_HOOK_PATHS[client]
    command = shlex.join([str(root / 'bin/channels'), '--client', client, 'hook'])
    record = {'project': str(project), 'client': client, 'path': str(hook), 'command': command}
    result = {**record, 'status': 'hook_missing', 'reason': 'project_client_not_installed'}
    try:
        canonical_path(hook)
        manifest = manifest_data(root)
        bindings = [item for item in manifest['hooks']
                    if (item['project'], item['client']) == (str(project), client)]
        if not bindings:
            return result
        if bindings != [record]:
            return {**result, 'status': 'hook_invalid', 'reason': 'manifest_binding_mismatch'}
        if not hook.is_file():
            return {**result, 'reason': 'hook_file_missing'}
        document = json.loads(hook.read_text())
        entries = document.get('hooks', {}).get('Stop', [])
        hooks = [item for entry in entries for item in entry.get('hooks', [])]
        matching = [item for item in hooks if item.get('command') == command]
        if matching != [{'type': 'command', 'command': command, 'timeout': 660}]:
            return {**result, 'status': 'hook_invalid', 'reason': 'stop_command_mismatch'}
        if any(entry.get('matcher') for entry in entries
               if any(item.get('command') == command for item in entry.get('hooks', []))):
            return {**result, 'status': 'hook_invalid', 'reason': 'stop_scope_mismatch'}
        if managed_binding(command) != (str(root), '--client', client):
            return {**result, 'status': 'hook_invalid', 'reason': 'wrapper_mismatch'}
        hook_document(hook, command)
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return {**result, 'status': 'hook_invalid', 'reason': 'binding_unreadable_or_invalid'}
    return {**result, 'status': 'installed', 'reason': None}


def execute(options):
    root = canonical_path(options.target)
    project = canonical_path(options.project)
    clients = [options.client] if options.client else ['claude', 'codex']
    hooks = [native_hook(project, client) for client in clients]
    if options.action == 'plan':
        return install(options, root, project)
    locks = {root / '.install.lock'}
    locks.update(hook.with_name(hook.name + '.channels.lock') for hook in hooks)
    with ExitStack() as stack:
        for lock in sorted(locks):
            stack.enter_context(installation_lock(lock))
        if options.action == 'install' and installation_matches(options, root, project):
            print(json.dumps({'action': 'install', 'status': 'already_installed', 'root': str(root),
                              'hook': str(native_hook(project, options.client)),
                              'resume_command': native_resume_command(options.client, project)}))
            return
        stack.enter_context(lifecycle_lock(root, exclusive=True))
        if options.action == 'uninstall':
            return uninstall(options, root, project, clients)
        install(options, root, project)


def manifest_data(root):
    path = canonical_path(root / 'install-manifest.json')
    if not path.exists():
        return {'version': 1, 'files': {}, 'hooks': []}
    data = json.loads(path.read_text())
    if data.get('version') != 1 or not isinstance(data.get('files'), dict) or not isinstance(data.get('hooks'), list):
        raise SetupError('Invalid ownership manifest; preserving installation')
    return data


def digest(content):
    return hashlib.sha256(content).hexdigest()


def archive_owned(path):
    try:
        with zipfile.ZipFile(path) as archive:
            return archive.read('POLYAGENT-HARNESS') == b'1\n'
    except (OSError, KeyError, zipfile.BadZipFile):
        return False


def protected_key(path):
    path = canonical_path(path)
    details = path.stat()
    if not stat.S_ISREG(details.st_mode) or details.st_mode & 0o077 or path.parent.stat().st_mode & 0o077:
        raise SetupError('Key file and parent must be private (0600/0700)')
    content = path.read_bytes()
    if not content.strip():
        raise SetupError('Key file is empty')
    return content


def installation_matches(options, root, project):
    """Reuse an unchanged installation without interrupting shared CLI users."""
    try:
        source = canonical_path(sys.argv[0])
        if source.suffix != '.pyz' or not archive_owned(source):
            return False
        wrapper = root / 'bin/channels'
        files = {'harness.pyz': (source.read_bytes(), 0o600),
                 'config.json': ((json.dumps({'url': service_url(options.url)}) + '\n').encode(), 0o600),
                 'bin/channels': (wrapper_content(Path(os.path.abspath(sys.executable)), root), 0o700)}
        for name, (body, mode) in files.items():
            path = canonical_path(root / name)
            if not path.is_file() or path.read_bytes() != body or stat.S_IMODE(path.stat().st_mode) != mode:
                return False
        hook = native_hook(project, options.client)
        command = shlex.join([str(wrapper), '--client', options.client, 'hook'])
        expected_hook, _ = hook_document(hook, command, options.replace)
        if not hook.is_file() or hook.read_bytes() != expected_hook:
            return False
        manifest = manifest_data(root)
        record = {'project': str(project), 'client': options.client, 'path': str(hook), 'command': command}
        bindings = [item for item in manifest['hooks']
                    if (item['project'], item['client']) == (str(project), options.client)]
        if bindings != [record] or manifest['files'] != {name: digest(body) for name, (body, _) in files.items()}:
            return False
        key = canonical_path(root / '.env/channel.key')
        if stat.S_IMODE(key.parent.stat().st_mode) != 0o700:
            return False
        if options.key_file and (not key.is_file() or protected_key(key) != protected_key(options.key_file)):
            return False
        if key.exists():
            protected_key(key)
        return True
    except (OSError, ValueError, TypeError, KeyError):
        return False


def install(options, root, project):
    config = canonical_path(root / 'config.json')
    wrapper = canonical_path(root / 'bin/channels')
    archive = canonical_path(root / 'harness.pyz')
    key = canonical_path(root / '.env/channel.key')
    url = service_url(options.url)
    if config.exists() and read_config(config) != url:
        raise SetupError('Shared URL change requires manual configuration')
    command = shlex.join([str(wrapper), '--client', options.client, 'hook'])
    hook = native_hook(project, options.client)
    hook_bytes, previous = hook_document(hook, command, options.replace)
    content = wrapper_content(Path(os.path.abspath(sys.executable)), root)
    if wrapper.exists() and wrapper.read_bytes() != content and managed_wrapper_root(wrapper) is None:
        raise SetupError('Unrelated wrapper; preserving it')
    source = canonical_path(sys.argv[0])
    if archive.exists() and not archive_owned(archive):
        raise SetupError('Unrelated archive; preserving it')
    manifest = manifest_data(root)
    key_bytes = protected_key(options.key_file) if options.key_file else None
    if key.exists() and key_bytes is not None and protected_key(key).strip() != key_bytes.strip():
        raise SetupError('Existing shared key differs; preserving it')
    print(json.dumps({'action': options.action, 'root': str(root), 'hook': str(hook),
                      'command': command, 'previous_bindings': previous,
                      'key': str(key), 'resume_command': native_resume_command(options.client, project),
                      'native_activation': 'Review and trust the exact new native hook; trust is user-controlled'}))
    if options.action == 'plan':
        return
    if source.suffix != '.pyz' or not archive_owned(source):
        raise SetupError('Install requires the downloaded zipapp')
    files = {'harness.pyz': source.read_bytes(), 'config.json': (json.dumps({'url': url}) + '\n').encode(),
             'bin/channels': content}
    for directory in (root, wrapper.parent, key.parent):
        canonical_path(directory).mkdir(parents=True, exist_ok=True, mode=0o700)
    key.parent.chmod(0o700)
    if key_bytes is not None and not key.exists():
        atomic_write(key, key_bytes)
    for name, body in files.items():
        atomic_write(root / name, body, 0o700 if name == 'bin/channels' else 0o600)
        manifest['files'][name] = digest(body)
    hook.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(hook, hook_bytes)
    record = {'project': str(project), 'client': options.client, 'path': str(hook), 'command': command}
    manifest['hooks'] = [item for item in manifest['hooks'] if (item['project'], item['client']) != (str(project), options.client)] + [record]
    atomic_write(root / 'install-manifest.json', (json.dumps(manifest, indent=2) + '\n').encode())


def uninstall(options, root, project, clients):
    manifest = manifest_data(root)
    kept = []
    removed = []
    legacy = not (root / 'install-manifest.json').exists()
    remaining = [item for item in manifest['hooks']
                 if (item['project'], item['client']) not in {(str(project), client) for client in clients}]
    if options.purge_key and remaining:
        raise SetupError('Cannot purge shared key: another registered project or adapter still depends on this root')
    refuse_live_exchanges(root)
    refuse_busy_session_locks(root)
    callbacks_removed = []
    for client in clients:
        hook = native_hook(project, client)
        command = shlex.join([str(root / 'bin/channels'), '--client', client, 'hook'])
        if hook.exists():
            body, _ = hook_document(hook, command, remove=True)
            if legacy and archive_owned(root / 'harness.pyz'):
                document = json.loads(body)
                for entry in document['hooks']['Stop']:
                    entry['hooks'] = [item for item in entry['hooks'] if not legacy_callback(item, root)]
                body = (json.dumps(document, indent=2) + '\n').encode()
            before = json.loads(hook.read_text())
            after = json.loads(body)
            old_commands = [item.get('command') for entry in before.get('hooks', {}).get('Stop', []) for item in entry.get('hooks', [])]
            new_commands = [item.get('command') for entry in after['hooks']['Stop'] for item in entry.get('hooks', [])]
            callbacks_removed.extend({'path': str(hook), 'command': command} for command in old_commands if command not in new_commands)
            if old_commands != new_commands:
                atomic_write(hook, body)
        manifest['hooks'] = [item for item in manifest['hooks'] if (item['project'], item['client']) != (str(project), client)]
    if manifest['hooks']:
        atomic_write(root / 'install-manifest.json', (json.dumps(manifest, indent=2) + '\n').encode())
        kept.append(str(root))
    elif legacy:
        kept.append(str(root) + ' (legacy ownership across projects unknown; shared files preserved)')
    else:
        for name, expected in manifest['files'].items():
            if name not in ('harness.pyz', 'config.json', 'bin/channels'):
                raise SetupError('Unknown manifest file; preserving it')
            path = canonical_path(root / name)
            if path.exists() and digest(path.read_bytes()) == expected:
                path.unlink()
                removed.append(str(path))
        remove_generated_state(root, removed)
        (root / 'install-manifest.json').unlink(missing_ok=True)
    key = canonical_path(root / '.env/channel.key')
    if options.purge_key and key.exists():
        key.unlink()
        removed.append(str(key))
    kept.extend(str(path) for path in root.iterdir())
    # Report credential locations without traversing unknown directories (including manual venvs).
    for directory in (root / ".env", root / "credentials"):
        if directory.is_dir() and not directory.is_symlink():
            kept.extend(str(path) for path in directory.iterdir())
    if key.exists():
        kept.append(str(key))
    follow_up = None
    if key.exists():
        installer = Path(sys.argv[0]).absolute()
        follow_up = shlex.join(['rm', '--', str(key)])
        if installer.is_file():
            follow_up = shlex.join(['python3', str(installer), 'uninstall',
                                   '--target', str(root), '--project', str(project), '--purge-key'])
    print(json.dumps({'action': 'uninstall', 'callbacks_removed': callbacks_removed,
                      'removed': removed, 'kept': sorted(set(kept)), 'purge_key_command': follow_up}))


def legacy_callback(hook, root):
    binding = managed_binding(hook.get('command', ''))
    return hook.get('type') == 'command' and binding is not None and binding[:2] == (str(root), '--profile')


def session_directories(root):
    directory = root / 'state'
    if directory.is_symlink() or not directory.is_dir():
        return []
    return [session for session in directory.iterdir()
            if not session.is_symlink() and session.is_dir() and re.fullmatch(r'[a-f0-9]{64}', session.name)]


def generated_topic_paths(session):
    return [session / 'exchange.json', *sorted(session.glob('topic-*.json'))]


def refuse_busy_session_locks(root):
    for session in session_directories(root):
        for lock in [session / 'exchange.lock', session / 'connection-probe.lock', *session.glob('topic-*.lock')]:
            if lock.is_symlink() or not lock.is_file():
                continue
            descriptor = os.open(lock, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise SetupError('Session lock in use; stop hooks before uninstall') from None
            finally:
                os.close(descriptor)


def refuse_live_exchanges(root):
    from polyagent_channels.exchange import read_state

    for session in session_directories(root):
        for path in generated_topic_paths(session):
            try:
                state = read_state(path)
            except (OSError, ValueError, TypeError, KeyError):
                continue
            if state['deadline'] > time.time() or state['pending'] is not None:
                raise SetupError('Live exchange or pending work present; ack and close exchanges and stop active hooks before uninstall')


def valid_diagnostic(path):
    from polyagent_channels.exchange import validate_session_id

    if path.is_symlink() or not path.is_file():
        return False
    try:
        data = json.loads(path.read_text())
        if set(data) != {'timestamp', 'state', 'session_id', 'callback_id'}:
            return False
        session_id = validate_session_id(data['session_id'])
        if data['callback_id'] is not None:
            validate_session_id(data['callback_id'])
        return (digest(session_id.encode('ascii')) == path.parent.name
                and type(data['timestamp']) in (int, float)
                and data['state'] in ('absent', 'expired', 'wait', 'waiting', 'callback_observed', 'peer', 'error', 'config_error'))
    except (OSError, ValueError, TypeError, KeyError):
        return False


def remove_generated_state(root, removed):
    for session in session_directories(root):
        safe = True
        for state in generated_topic_paths(session):
            safe = remove_generated_topic(state, removed) and safe
        diagnostic = session / 'last-hook.json'
        if safe and valid_diagnostic(diagnostic):
            diagnostic.unlink()
            removed.append(str(diagnostic))
        probe = session / 'connection-probe.json'
        if safe and valid_connection_probe(probe):
            probe.unlink()
            removed.append(str(probe))
        if not any(session.iterdir()):
            session.rmdir()


def valid_connection_probe(path):
    from polyagent_channels.exchange import validate_session_id

    if path.is_symlink() or not path.is_file():
        return False
    try:
        data = json.loads(path.read_text())
        return (set(data) == {'session_id'}
                and digest(validate_session_id(data['session_id']).encode('ascii')) == path.parent.name)
    except (OSError, ValueError, TypeError, KeyError):
        return False


def remove_generated_topic(state, removed):
    from polyagent_channels.exchange import read_state

    session = state.parent
    try:
        record = read_state(state)
    except (OSError, ValueError, TypeError, KeyError):
        record = None
    # Unknown or live state also protects the session's diagnostics.
    if state.is_symlink() or (state.exists() and (record is None or (record['deadline'] > time.time() or record['pending'] is not None))):
        return False
    lock = state.with_suffix('.lock')
    descriptor = None
    try:
        if lock.exists() or lock.is_symlink():
            if lock.is_symlink():
                return False
            descriptor = os.open(lock, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
            details = os.fstat(descriptor)
            if not stat.S_ISREG(details.st_mode) or details.st_size or details.st_nlink != 1:
                return False
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
        if record is not None and read_state(state) == record:
            state.unlink()
            removed.append(str(state))
    finally:
        if descriptor is not None:
            os.close(descriptor)

    return True
