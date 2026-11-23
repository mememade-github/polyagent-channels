"""Read-only plan, shared-key client installation and conservative uninstall."""
import argparse
from contextlib import contextmanager, ExitStack
import fcntl
import stat
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import hashlib
import re
import zipfile
from pathlib import Path

from polyagent_channels.client_config import SESSION_RESTART, ConfigError, default_root, read_config, service_url
from polyagent_channels.exchange import RECEPTION_TIMEOUT
from polyagent_channels.identity import MAX_MEMBER_LABEL_BYTES, is_identifier

from polyagent_channels.codex_config import (CODEX_SETTING, SetupError, codex_block, codex_config,
                                             codex_config_removed, filtered_config, has_registration, is_immediate_unload,
                                             missing_root_hint, parsed_toml)
from polyagent_channels.codex_git import CodexGit, clone, filter_program

NATIVE_TRUST = "Accept the client's own folder trust prompt. Never write native trust records yourself."
TOOLS_READY = 'That session finds the channels tools by name and needs no further setup. A running client does not load new tools. '
NEXT_STEP = {'claude': (SESSION_RESTART + ', or resume this one. ' + TOOLS_READY + NATIVE_TRUST
                        + ' Send the new session one operator message before leaving it idle so Claude Code supplies the channel instructions and tool names.'),
             'codex': (SESSION_RESTART + '. Codex applies project settings when it loads a session, so a session '
                       'still loaded on its shared server can keep its old settings: close it and wait about a minute before '
                       'resuming it. ' + TOOLS_READY + NATIVE_TRUST)}
UNINSTALL_NOTICE = ('Open sessions stop listening after their current receive or notice attempt ends; '
                    'their channels tools then report the removal. Unconfirmed messages stay recorded under '
                    'unconfirmed: reinstalling and resuming that session continues them.')
NATIVE_BINDING_PATHS = {'claude': '.claude/settings.local.json', 'codex': '.codex/config.toml'}
LEGACY_CODEX_HOOKS = '.codex/hooks.json'
RECEIVER_EVENTS = ('SessionStart', 'Stop')
MCP_NAME = 'channels'
CLAUDE_TOOL_RULE = 'mcp__channels'
LEGACY_LOCK_FILES = {'root': ('.install.lock', '.lifecycle.lock'),
                     'project': ('.claude/settings.local.json.channels.lock', '.codex/hooks.json.channels.lock')}


def atomic_write(path, content, mode=None, *, prefix='.channels-', temporary_dir=None):
    """Replace a file whole; an existing file keeps its mode, a new one is private unless MODE says otherwise."""
    if path.is_symlink():
        raise SetupError('Refusing symlink destination')
    if mode is None:
        mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
    descriptor, temporary = tempfile.mkstemp(dir=temporary_dir or path.parent, prefix=prefix)
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
            raise SetupError('Ambiguous hook; inspect indicated native hook position') from None
        return None
    if not words or 'hook' not in words:
        return None
    executable = Path(words[0])
    if executable.name != 'channels':
        if any(Path(word).name == 'channels' for word in words[1:]):
            raise SetupError('Ambiguous indirect receiver; inspect indicated native hook position')
        return None
    if not executable.is_absolute():
        raise SetupError('Ambiguous hook; inspect indicated native hook position')
    try:
        executable = canonical_path(executable)
        content = executable.read_bytes()
    except (OSError, ValueError):
        raise SetupError('Ambiguous hook; inspect indicated native hook position') from None
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
        if not is_identifier(words[2]):
            raise ValueError()
        if words[1] == '--client' and words[2] not in ('claude', 'codex'):
            raise ValueError()
    except (IndexError, UnicodeError, ValueError):
        raise SetupError('Ambiguous managed hook; inspect indicated native hook position') from None
    return (str(root), words[1], words[2])


def managed_wrapper_root(wrapper):
    binding = managed_binding(shlex.join([str(wrapper), '--profile', 'ownership-check', 'hook']))
    return binding[0] if binding else None


def receiver_callback(command):
    return {'type': 'command', 'command': command, 'asyncRewake': True, 'timeout': RECEPTION_TIMEOUT}


LEGACY_CALLBACK_KIND = '--profile'
# install --replace may replace callbacks of these kinds; without it they conflict.
REPLACEABLE_WITH_FLAG = (LEGACY_CALLBACK_KIND,)


def hook_owner(hook, position, command=None):
    """The managed binding a hook's command names, or None for an unrelated hook."""
    if command and command == hook.get('command') and not Path(shlex.split(command)[0]).exists():
        return callback_binding(command)
    try:
        return managed_binding(hook.get('command', ''))
    except ValueError:
        raise SetupError(f'Ambiguous receiver at {position}; inspect manually' + hook_recovery_hint(hook)) from None


def hook_recovery_hint(hook):
    try:
        words = shlex.split(hook.get('command', ''))
    except ValueError:
        return ''
    return missing_root_hint(words[0]) if words else ''


def owned_hooks(data, event, command=None):
    """Each entry of EVENT with its hooks paired with their owners; every hook is checked before anything changes."""
    return [(entry, [(hook, hook_owner(hook, f'{event}[{entry_index}].hooks[{hook_index}]', command))
                     for hook_index, hook in enumerate(entry.get('hooks', []))])
            for entry_index, entry in enumerate(data.get('hooks', {}).get(event) or [])]


def callback_binding(command):
    words = shlex.split(command)
    return (str(Path(words[0]).parent.parent), '--client', words[2])


def is_callback(hook, command):
    return hook.get('type') == 'command' and hook.get('command') == command


def owned_by_client(owner, client):
    """A legacy callback, or one bound to this client; another client's callback is left alone."""
    return owner is not None and not (owner[1] == '--client' and owner[2] != client)


def remaining_entries(filtered):
    """Entries with their remaining hooks; an entry left without hooks goes unless it held none to begin with."""
    return [{**entry, 'hooks': hooks} for entry, hooks in filtered if hooks or not entry.get('hooks')]


def remove_hook_event(data, event, command):
    """Remove this root's callback for one event, keeping every unrelated hook."""
    if data.get('hooks', {}).get(event) is None:
        return
    binding = callback_binding(command)
    filtered = [(entry, [hook for hook, owner in hooks if not (is_callback(hook, command) and owner == binding)])
                for entry, hooks in owned_hooks(data, event)]
    data['hooks'][event] = remaining_entries(filtered)


def replacement_problem(hook, owner, command, replaceable):
    """Why an existing callback of this client may not be replaced, as a message template, or None."""
    if owner != callback_binding(command) and owner[1] not in replaceable:
        return 'Native scope conflict at {event}; --replace only replaces verified legacy callbacks'
    if not is_callback(hook, command) and owner[1] != LEGACY_CALLBACK_KIND:
        return 'Ambiguous managed callback at {event}; inspect manually'
    return None


def add_hook_event(data, event, command, replaceable):
    """Put this root's callback on one event and return the earlier callbacks it replaces.

    REPLACEABLE names the legacy callback kinds that may be replaced; any other managed callback of this client
    stops installation."""
    client = shlex.split(command)[2]
    classified = owned_hooks(data, event, command)
    ours = [(hook, owner) for _, hooks in classified for hook, owner in hooks if owned_by_client(owner, client)]
    problems = [problem for hook, owner in ours if (problem := replacement_problem(hook, owner, command, replaceable))]
    if problems:
        raise SetupError(problems[0].format(event=event))
    filtered = [(entry, [hook for hook, owner in hooks if not owned_by_client(owner, client)]) for entry, hooks in classified]
    data.setdefault('hooks', {})[event] = remaining_entries(filtered) + [{'hooks': [receiver_callback(command)]}]
    return [list(owner) for _, owner in ours]


def json_bytes(data):
    return (json.dumps(data, indent=2, ensure_ascii=False) + '\n').encode()


def settings_document(path):
    return json.loads(path.read_text()) if path.exists() else {}


def claude_created_entries(path, data):
    """Name what this install adds so that uninstall removes only that."""
    hooks = data.get('hooks', {})
    permissions = data.get('permissions', {})
    candidates = {'directory': not path.parent.exists(), 'file': not path.exists(), 'hooks': 'hooks' not in data,
                  **{'hooks.' + event: event not in hooks for event in RECEIVER_EVENTS},
                  'permissions': 'permissions' not in data, 'permissions.allow': 'allow' not in permissions,
                  'rule': CLAUDE_TOOL_RULE not in permissions.get('allow', [])}
    return [name for name, created in candidates.items() if created]


def claude_settings(path, command, replaceable):
    """Return the settings bytes with both background callbacks and the tool rule, and what they add."""
    data = settings_document(path)
    created = claude_created_entries(path, data)
    previous = []
    for event in RECEIVER_EVENTS:
        previous.extend(add_hook_event(data, event, command, replaceable))
    allowed = data.setdefault('permissions', {}).setdefault('allow', [])
    if CLAUDE_TOOL_RULE not in allowed:
        allowed.append(CLAUDE_TOOL_RULE)
    return json_bytes(data), previous, created


def claude_settings_removed(path, command, created):
    """Return the settings with this binding removed; only keys this install created disappear."""
    data = settings_document(path)
    for event in RECEIVER_EVENTS:
        remove_hook_event(data, event, command)
    if 'rule' in created and CLAUDE_TOOL_RULE in data.get('permissions', {}).get('allow', []):
        data['permissions']['allow'].remove(CLAUDE_TOOL_RULE)
    for name in ('hooks.SessionStart', 'hooks.Stop', 'hooks', 'permissions.allow', 'permissions'):
        drop_if_empty(data, name.split('.'), name in created)
    return data


def drop_if_empty(data, keys, created):
    parent = data
    for key in keys[:-1]:
        parent = parent.get(key, {})
    if created and keys[-1] in parent and not parent[keys[-1]]:
        del parent[keys[-1]]


def claude_command(project, *arguments):
    """Let the client change its own registry; this product never edits ~/.claude.json."""
    executable = shutil.which('claude')
    if executable is None:
        raise SetupError('claude command not found; run this from the Claude Code environment')
    return subprocess.run([executable, *arguments], cwd=project, stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, timeout=60)


def claude_tool_registration(project, wrapper):
    """Return 'absent', 'owned' or 'foreign' for the channels server visible in this project."""
    done = claude_command(project, 'mcp', 'get', MCP_NAME)
    if done.returncode:
        return 'absent'
    parts = (line.partition(':') for line in done.stdout.splitlines())
    fields = {name.strip(): value.strip() for name, ignored, value in parts}
    owned = (fields.get('Scope', '').startswith('Local') and fields.get('Command') == str(wrapper)
             and fields.get('Args') == '--client claude mcp')
    return 'owned' if owned else 'foreign'


def register_claude_tools(project, wrapper):
    state = claude_tool_registration(project, wrapper)
    if state == 'foreign':
        raise SetupError('Another MCP server named channels is configured for this project; inspect it with claude mcp get channels')
    if state == 'absent':
        done = claude_command(project, 'mcp', 'add', '-s', 'local', MCP_NAME, '--', str(wrapper), '--client', 'claude', 'mcp')
        if done.returncode:
            raise SetupError('claude mcp add failed; run it again from the Claude Code environment')


def unregister_claude_tools(project, wrapper):
    """Remove only this root's local registration and report what stayed."""
    if claude_tool_registration(project, wrapper) != 'owned':
        return False
    done = claude_command(project, 'mcp', 'remove', '-s', 'local', MCP_NAME)
    if done.returncode:
        raise SetupError('claude mcp remove failed; inspect it with claude mcp get channels')
    return True


def is_same_directory(path, descriptor):
    try:
        return os.path.samestat(os.stat(path), os.fstat(descriptor))
    except FileNotFoundError:
        return False


@contextmanager
def directory_lock(path, operation):
    """Lock a directory itself: no lock file exists, so removal has none to delete while it is held."""
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fcntl.flock(descriptor, operation)
        # A removal may have deleted the directory while this process waited for it.
        if not is_same_directory(path, descriptor):
            raise SetupError('Installation in use; try again')
        yield
    finally:
        os.close(descriptor)


@contextmanager
def lifecycle_lock(root, exclusive=False):
    """Short CLI commands share the root; installation changes hold it alone and never wait."""
    operation = fcntl.LOCK_EX | fcntl.LOCK_NB if exclusive else fcntl.LOCK_SH
    try:
        with directory_lock(canonical_path(root), operation):
            yield
    except BlockingIOError:
        raise SetupError('Installation in use; finish running channel commands before changing the installation') from None


def installation_name(value):
    if not value.strip() or len(value.encode('utf-8')) > MAX_MEMBER_LABEL_BYTES:
        raise argparse.ArgumentTypeError(f'name must be nonblank and <={MAX_MEMBER_LABEL_BYTES} UTF-8 bytes')
    return value


def main(arguments=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('plan', 'install', 'uninstall'))
    parser.add_argument('--url')
    parser.add_argument('--target', type=Path, default=default_root())
    parser.add_argument('--client', choices=('claude', 'codex'))
    parser.add_argument('--project', type=Path, default=Path.cwd())
    parser.add_argument('--key-file', type=Path)
    parser.add_argument('--name', type=installation_name, help='Display name for this project and client')
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


def native_binding(project, client):
    return canonical_path(project / NATIVE_BINDING_PATHS[client])


def binding_command(root, client):
    """The hook command for Claude; for Codex the command its project configuration starts."""
    return shlex.join([str(root / 'bin/channels'), '--client', client, 'hook' if client == 'claude' else 'mcp'])


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


def binding_record(root, project, client):
    return {'project': str(project), 'client': client, 'path': str(project / NATIVE_BINDING_PATHS[client]),
            'command': binding_command(root, client)}


def recorded_bindings(manifest, project, client):
    return [item for item in manifest['hooks'] if (item['project'], item['client']) == (str(project), client)]


def installed_project(root, project, client):
    """Return the deepest installed project containing PROJECT, or None when none applies."""
    try:
        projects = [Path(item['project']) for item in manifest_data(root)['hooks'] if item['client'] == client]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    containing = [path for path in projects if path == project or path in project.parents]
    return max(containing, key=lambda path: len(path.parts), default=None)


def claude_binding_mismatch(path, root):
    document = json.loads(path.read_text())
    command = binding_command(root, 'claude')
    for event in RECEIVER_EVENTS:
        entries = document.get('hooks', {}).get(event, [])
        matching = [hook for entry in entries for hook in entry.get('hooks', []) if hook.get('command') == command]
        if matching != [receiver_callback(command)]:
            return event, 'callback_mismatch'
        if any(entry.get('matcher') for entry in entries if any(hook.get('command') == command for hook in entry.get('hooks', []))):
            return event, 'scope_mismatch'
    return None


def codex_binding_mismatch(path, root):
    content = path.read_bytes().decode('utf-8')
    if not has_registration(content, root / 'bin/channels'):
        return 'mcp', 'block_mismatch'
    return None if is_immediate_unload(parsed_toml(content, path).get('thread_unload_delay_secs')) else ('mcp', 'unload_setting_mismatch')


BINDING_MISMATCH = {'claude': claude_binding_mismatch, 'codex': codex_binding_mismatch}


def hook_binding_status(root, project, client):
    """Inspect this project/client's installation record and native binding without changing either."""
    record = binding_record(root, project, client)
    path = Path(record['path'])
    result = {**record, 'status': 'hook_missing', 'reason': 'project_client_not_installed'}
    try:
        canonical_path(path)
        bindings = recorded_bindings(manifest_data(root), project, client)
        if not bindings:
            return result
        if [{key: item.get(key) for key in record} for item in bindings] != [record]:
            return {**result, 'status': 'hook_invalid', 'reason': 'manifest_binding_mismatch'}
        if not path.is_file():
            return {**result, 'reason': 'binding_file_missing'}
        mismatch = BINDING_MISMATCH[client](path, root)
        if mismatch:
            return {**result, 'status': 'hook_invalid', 'event': mismatch[0], 'reason': mismatch[1]}
        if managed_wrapper_root(root / 'bin/channels') != str(root):
            return {**result, 'status': 'hook_invalid', 'reason': 'wrapper_mismatch'}
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return {**result, 'status': 'hook_invalid', 'reason': 'binding_unreadable_or_invalid'}
    return {**result, 'status': 'installed', 'reason': None}


def execute(options):
    root = canonical_path(options.target)
    project = canonical_path(options.project)
    clients = [options.client] if options.client else ['claude', 'codex']
    # A mistyped --project must not become a new directory tree. Uninstall creates nothing, so it skips this check.
    if options.action != 'uninstall' and not project.is_dir():
        raise SetupError(f'Project directory {project} does not exist or is not a directory; pass an existing --project')
    if options.action == 'plan':
        return install(options, root, project)
    with ExitStack() as stack:
        repository = clone(project) if 'codex' in clients else None
        git_binding = CodexGit(*repository, project, root / 'bin/channels') if repository else None
        if git_binding:
            stack.enter_context(directory_lock(git_binding.common, fcntl.LOCK_EX))
            git_binding.refuse_stale_driver()
        enabled, filter_notice = (False, '')
        if git_binding and options.action == 'install':
            enabled, filter_notice = git_binding.preflight()
        if options.action == 'install':
            if git_binding:
                git_binding.cleanup_metadata_temps()
            common = git_binding.common if git_binding else None
            return install_locked(options, root, project, stack, git_binding if enabled else None, filter_notice, common)
        lock_setup_directories(stack, root, project, git_binding.common if git_binding else None)
        if not root.is_dir() or not any(root.iterdir()):
            if git_binding and git_binding.has_filter_metadata():
                raise SetupError(f'The client root {root} is missing and its local Git filter remains. '
                                 f'Reinstall for this project with the same --target {shlex.quote(str(root))}, then uninstall.')
            return report_nothing_installed(root, project)
        if git_binding:
            git_binding.cleanup_metadata_temps()
        stack.enter_context(lifecycle_lock(root, exclusive=True))
        uninstall(options, root, project, clients, git_binding)


def lock_setup_directories(stack, root, project, common):
    # Installers wait on these directories; a directory shared by both paths is locked only once.
    for directory in sorted({root.parent, project} - {common}):
        if directory.is_dir():
            stack.enter_context(directory_lock(directory, fcntl.LOCK_EX))


def install_locked(options, root, project, stack, git_binding, filter_notice, common):
    source = canonical_path(sys.argv[0])
    program = filter_program(source) if git_binding and source.suffix == '.pyz' and archive_owned(source) else None
    root.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_setup_directories(stack, root, project, common)
    if installation_matches(options, root, project) and filter_matches(git_binding, root, project, program):
        if git_binding:
            git_binding.cleanup_binding_temps()
            git_binding.refresh()
            git_binding.check_driver_config()
        print(json.dumps({'action': 'install', 'status': 'already_installed', 'root': str(root),
                          'binding': str(native_binding(project, options.client)),
                          'resume_command': native_resume_command(options.client, project),
                          'next_step': next_step(options.client, filter_notice)}))
        return
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    stack.enter_context(lifecycle_lock(root, exclusive=True))
    install(options, root, project, git_binding, program, filter_notice)


def filter_matches(git_binding, root, project, program):
    if not git_binding:
        return True
    wrapper = root / 'bin/channels'
    if not git_binding.ready(program):
        return False
    binding = native_binding(project, 'codex')
    return filtered_config(binding, wrapper, ()) == binding.read_bytes()


def next_step(client, filter_notice):
    return ' '.join(part for part in (NEXT_STEP[client], filter_notice) if part)


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
        if hook_binding_status(root, project, options.client)['status'] != 'installed':
            return False
        if options.client == 'codex':
            binding = native_binding(project, options.client)
            if codex_config(binding, wrapper)[0] != binding.read_bytes():
                return False
        if options.client == 'claude' and claude_tool_registration(project, wrapper) != 'owned':
            return False
        manifest = manifest_data(root)
        if manifest['files'] != {name: digest(body) for name, (body, _) in files.items()}:
            return False
        if options.name is not None and recorded_bindings(manifest, project, options.client)[0].get('name') != options.name:
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


def install(options, root, project, git_binding=None, program=None, filter_notice=''):
    config = canonical_path(root / 'config.json')
    wrapper = canonical_path(root / 'bin/channels')
    archive = canonical_path(root / 'harness.pyz')
    key = canonical_path(root / '.env/channel.key')
    url = service_url(options.url)
    saved_url = read_config(config) if config.exists() else None
    if saved_url is not None and saved_url != url:
        raise SetupError(f'This root uses {saved_url}. Install with --url {shlex.quote(saved_url)} '
                         'to add a project or client to the same service. Shared URL change: uninstall every project '
                         'and client using this root, then install with the new --url')
    binding = native_binding(project, options.client)
    command = binding_command(root, options.client)
    previous, binding_bytes, created = [], b'', []
    if options.client == 'claude':
        replaceable = REPLACEABLE_WITH_FLAG if options.replace else ()
        binding_bytes, previous, created = claude_settings(binding, command, replaceable)
    if options.client == 'codex':
        binding_bytes, created = codex_config(binding, wrapper)
        legacy_hooks_update(project, root)
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
    if options.client == 'claude' and claude_tool_registration(project, wrapper) == 'foreign':
        raise SetupError('Another MCP server named channels is configured for this project; inspect it with claude mcp get channels')
    result = {'action': options.action, 'root': str(root), 'binding': str(binding), 'command': command,
              'previous_bindings': previous, 'key': str(key), 'resume_command': native_resume_command(options.client, project),
              'next_step': next_step(options.client, filter_notice)}
    if options.action == 'plan':
        print(json.dumps(result))
        return
    if source.suffix != '.pyz' or not archive_owned(source):
        raise SetupError('Install requires the downloaded zipapp')
    files = {'harness.pyz': source.read_bytes(), 'config.json': (json.dumps({'url': url}) + '\n').encode(),
             'bin/channels': content}
    for directory in (root, wrapper.parent, key.parent):
        canonical_path(directory).mkdir(parents=True, exist_ok=True, mode=0o700)
    key.parent.chmod(0o700)
    if key_bytes is not None and not key.exists():
        atomic_write(key, key_bytes, 0o600)
    for name, body in files.items():
        atomic_write(root / name, body, 0o700 if name == 'bin/channels' else 0o600)
        manifest['files'][name] = digest(body)
    earlier = recorded_bindings(manifest, project, options.client)
    previously_created = earlier[0].get('created', []) if earlier else []
    record = binding_record(root, project, options.client)
    name = options.name
    if name is None and earlier:
        name = earlier[0].get('name')
    if name is not None:
        record['name'] = name
    # Ownership is recorded before the client changes, so uninstall can undo a run that fails part way. What this run
    # creates joins the record only once it exists, so a failed run never claims a settings file it did not write.
    record_ownership(root, manifest, {**record, 'created': previously_created})
    if options.client == 'codex':
        remove_legacy_codex_hook(project, root)
        if not binding.parent.exists():
            created.append('directory')
    if options.client == 'claude':
        register_claude_tools(project, wrapper)
    binding.parent.mkdir(parents=True, exist_ok=True)
    if git_binding:
        legacy_ownership = () if git_binding.has_assignment() else previously_created
        binding_bytes = filtered_config(binding, wrapper, legacy_ownership)
        git_binding.enable(program, atomic_write)
        git_binding.cleanup_binding_temps()
        atomic_write(binding, binding_bytes, prefix=git_binding.binding_prefix)
        git_binding.refresh()
    else:
        atomic_write(binding, binding_bytes)
    record_ownership(root, manifest, {**record, 'created': sorted({*previously_created, *created})})
    if git_binding:
        git_binding.check_driver_config()
    print(json.dumps(result))


def record_ownership(root, manifest, record):
    """Write the manifest with RECORD as the only binding record for its project and client."""
    others = [item for item in manifest['hooks'] if (item['project'], item['client']) != (record['project'], record['client'])]
    manifest['hooks'] = others + [record]
    atomic_write(root / 'install-manifest.json', (json.dumps(manifest, indent=2) + '\n').encode(), 0o600)


def legacy_hooks_update(project, root):
    """Return the legacy hooks document without this root's old callback, or None when it holds none.

    Install computes it before writing anything, so a file it cannot read stops it with nothing left."""
    path = project / LEGACY_CODEX_HOOKS
    try:
        return legacy_hooks_without_callback(path, root) if path.is_file() else None
    except SetupError:
        raise
    except (ValueError, OSError, AttributeError, TypeError, KeyError):
        raise SetupError(f'{path} is not a hooks file this installer can read; fix or remove it') from None


def legacy_hooks_without_callback(path, root):
    data = settings_document(path)
    before = json.dumps(data)
    remove_hook_event(data, 'Stop', shlex.join([str(root / 'bin/channels'), '--client', 'codex', 'hook']))
    if json.dumps(data) == before:
        return None
    if not data['hooks']['Stop']:
        del data['hooks']['Stop']
    if not data['hooks']:
        del data['hooks']
    return data


def remove_legacy_codex_hook(project, root):
    """Earlier releases waited in a Codex Stop hook; it would hold the turn open if it stayed."""
    data = legacy_hooks_update(project, root)
    if data is None:
        return False
    path = project / LEGACY_CODEX_HOOKS
    if data:
        atomic_write(path, json_bytes(data))
        return True
    # Nothing but that entry was in the file, so the file and what the old release left beside it go too.
    path.unlink()
    remove_legacy_lock_files(project, 'project')
    if not any(path.parent.iterdir()):
        path.parent.rmdir()
    return True


def claude_removal_plan(root, project, record, legacy):
    """Check everything before writing: return the new settings document and the external step."""
    path = project / NATIVE_BINDING_PATHS['claude']
    command = binding_command(root, 'claude')
    created = (record or {}).get('created', [])
    data = claude_settings_removed(path, command, created)
    if legacy and archive_owned(root / 'harness.pyz'):
        for entry in data.get('hooks', {}).get('Stop', []):
            entry['hooks'] = [item for item in entry['hooks'] if not legacy_callback(item, root)]
    def unregister():
        return unregister_claude_tools(project, root / 'bin/channels') if record else False
    changed = path.exists() and data != settings_document(path)
    body = json_bytes(data) if data or 'file' not in created else None
    return path, changed, body, created, unregister


def codex_removal_plan(root, project, record, legacy, git_binding=None):
    path = project / NATIVE_BINDING_PATHS['codex']
    created = (record or {}).get('created', [])
    if git_binding and git_binding.has_assignment():
        created = []
    elif git_binding and git_binding.tracked(git_binding.name):
        created = [item for item in created if item not in ('file', 'directory')]
    wrapper = root / 'bin/channels'
    content = codex_config_removed(path, wrapper, created) if path.exists() else ''
    changed = path.exists() and content.encode() != path.read_bytes()
    body = content.encode() if content or 'file' not in created else None
    def remove_legacy():
        return remove_legacy_codex_hook(project, root)
    return path, changed, body, created, remove_legacy


REMOVAL_PLANS = {'claude': claude_removal_plan, 'codex': codex_removal_plan}


def apply_removal(plan, write):
    """Write one planned removal and delete only the file and directory this install created."""
    path, changed, body, created, external = plan
    external_changed = external()
    if not changed:
        return external_changed
    if body is None:
        path.unlink()
    if body is not None:
        write(path, body)
    if 'directory' in created and not any(path.parent.iterdir()):
        path.parent.rmdir()
    return True


def uninstall(options, root, project, clients, git_binding=None):
    manifest = manifest_data(root)
    removed = []
    legacy = not (root / 'install-manifest.json').exists()
    remaining = [item for item in manifest['hooks']
                 if (item['project'], item['client']) not in {(str(project), client) for client in clients}]
    if options.purge_key and remaining:
        raise SetupError('Cannot purge shared key: another registered project or adapter still depends on this root')
    refuse_busy_session_locks(root)
    plans = []
    for client in clients:
        records = recorded_bindings(manifest, project, client)
        record = records[0] if records else None
        if client == 'codex' and git_binding:
            plan = codex_removal_plan(root, project, record, legacy, git_binding)
        else:
            plan = REMOVAL_PLANS[client](root, project, record, legacy)
        plans.append((client, plan))
    bindings_removed = []
    for client, plan in plans:
        binding = git_binding if client == 'codex' else None
        write = binding.metadata_writer(atomic_write) if binding and binding.has_assignment() else atomic_write
        changed = apply_removal(plan, write)
        if binding:
            changed = binding.disable(atomic_write) or changed
        if changed:
            bindings_removed.append({'client': client, 'path': str(plan[0])})
    manifest['hooks'] = remaining
    kept = []
    if manifest['hooks']:
        atomic_write(root / 'install-manifest.json', (json.dumps(manifest, indent=2) + '\n').encode())
    if legacy:
        kept.append(str(root) + ' (legacy ownership across projects unknown; shared files preserved)')
    if not manifest['hooks'] and not legacy:
        for name, expected in manifest['files'].items():
            if name not in ('harness.pyz', 'config.json', 'bin/channels'):
                raise SetupError('Unknown manifest file; preserving it')
            path = canonical_path(root / name)
            if path.exists() and digest(path.read_bytes()) == expected:
                path.unlink()
                removed.append(str(path))
        (root / 'install-manifest.json').unlink(missing_ok=True)
        remove_generated_state(root, removed)
        remove_legacy_lock_files(root, 'root')
    remove_legacy_lock_files(project, 'project')
    key = canonical_path(root / '.env/channel.key')
    if options.purge_key and key.exists():
        key.unlink()
        removed.append(str(key))
    if options.purge_key:
        purge_joined_topics(root, removed)
    follow_up = None
    if key.exists() or (root / 'joined-topics.json').exists():
        installer = Path(sys.argv[0]).absolute()
        follow_up = shlex.join(['rm', '-f', '--', str(key), str(root / 'joined-topics.json'), str(root / 'joined-topics.lock')])
        if installer.is_file():
            follow_up = shlex.join(['python3', str(installer), 'uninstall',
                                   '--target', str(root), '--project', str(project), '--purge-key'])
    # Pruning comes last: this process holds the root until it finishes and changes nothing afterwards.
    prune_empty_directories(root)
    unconfirmed = unconfirmed_work(root)
    print(json.dumps({'action': 'uninstall', 'bindings_removed': bindings_removed,
                      'removed': removed, 'kept': kept + remaining_paths(root), 'purge_key_command': follow_up,
                      'unconfirmed': unconfirmed,
                      'discard_unconfirmed_command': discard_command(root, unconfirmed) if not manifest['hooks'] else None,
                      'note': UNINSTALL_NOTICE}))


def purge_joined_topics(root, removed):
    paths = [root / name for name in ('joined-topics.json', 'joined-topics.lock')]
    for path in [path for path in paths if path.exists()]:
        path.unlink()
        removed.append(str(path))


def report_nothing_installed(root, project):
    print(json.dumps({'action': 'uninstall', 'bindings_removed': [], 'removed': [], 'kept': [], 'purge_key_command': None,
                      'note': 'Nothing is installed at ' + str(root) + ' for ' + str(project)}))


def remaining_paths(root):
    """List what stays without traversing unknown directories such as a manual virtual environment."""
    if not root.is_dir():
        return []
    paths = [*root.iterdir()]
    for directory in (root / '.env', root / 'credentials'):
        if directory.is_dir() and not directory.is_symlink():
            paths.extend(directory.iterdir())
    return sorted({str(path) for path in paths})


def finish_removal(root):
    """A receiver that outlived uninstall clears what it held, unless an installation or command is using the root."""
    try:
        with lifecycle_lock(root, exclusive=True):
            if (root / 'config.json').exists() or (root / 'install-manifest.json').exists():
                return
            remove_generated_state(root, [])
            prune_empty_directories(root)
    except (SetupError, OSError):
        return


def remove_legacy_lock_files(base, kind):
    """Earlier releases left empty lock files; they coordinate nothing with directory locks."""
    for name in LEGACY_LOCK_FILES[kind]:
        path = base / name
        if not path.is_symlink() and path.is_file() and not path.stat().st_size:
            path.unlink()


def prune_empty_directories(root):
    for directory in (root / 'state/clients', root / 'state', root / 'bin', root / '.env', root):
        if directory.is_dir() and not directory.is_symlink() and not any(directory.iterdir()):
            directory.rmdir()


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
    """A held receiver lock only means an open session is listening, and that receiver stops by itself."""
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


def unconfirmed_work(root):
    """List delivered messages no session has confirmed; removal keeps this record and never confirms it."""
    from polyagent_channels.exchange import read_state

    work = []
    for session in session_directories(root):
        for path in generated_topic_paths(session):
            try:
                state = read_state(path)
            except (OSError, ValueError, TypeError, KeyError):
                continue
            if state['pending']:
                work.append({'session_id': state['session_id'], 'channel': state['channel'], 'conversation_id': state['conversation_id'],
                             'message_ids': state['pending'], 'state_path': str(path)})
    return work


def is_generated_lock(path):
    """The product's locks are empty files with these exact names; any other file is left alone."""
    return (not path.is_symlink() and path.is_file() and not path.stat().st_size
            and bool(re.fullmatch(r'(exchange|receiver|connection-probe|topic-[a-f0-9]{64})\.lock', path.name)))


def is_generated_session_file(path):
    """Recognize session files by their expected names, paths and contents."""
    from polyagent_channels.exchange import read_state

    if path.is_symlink() or not path.is_file():
        return False
    if path.name == 'last-hook.json':
        return valid_diagnostic(path)
    if path.name == 'connection-probe.json':
        return valid_connection_probe(path)
    if path.suffix == '.lock':
        return is_generated_lock(path)
    try:
        return path in generated_topic_paths(path.parent) and bool(read_state(path))
    except (OSError, ValueError, TypeError, KeyError):
        return False


def discard_command(root, unconfirmed):
    """Name exact verified files, and only directories they empty, so dropping the record stays an explicit, narrow choice."""
    if not unconfirmed:
        return None
    sessions = sorted({Path(item['state_path']).parent for item in unconfirmed})
    files = [path for session in sessions for path in sorted(session.iterdir()) if is_generated_session_file(path)]
    emptied = [session for session in sessions if all(path in files for path in session.iterdir())]
    state = root / 'state'
    if all(path in emptied for path in state.iterdir()):
        emptied.append(state)
    command = shlex.join(['rm', '--', *map(str, files)])
    return command + ' && ' + shlex.join(['rmdir', '--', *map(str, emptied)]) if emptied else command


def valid_diagnostic(path):
    from polyagent_channels.exchange import validate_session_id

    if path.is_symlink() or not path.is_file():
        return False
    try:
        data = json.loads(path.read_text())
        if set(data) - {'callback_id'} != {'timestamp', 'state', 'session_id'}:
            return False
        session_id = validate_session_id(data['session_id'])
        return (digest(session_id.encode('ascii')) == path.parent.name
                and type(data['timestamp']) in (int, float)
                and data['state'] in ('absent', 'expired', 'wait', 'waiting', 'callback_observed', 'peer', 'error',
                                      'config_error', 'notify_failed', 'notified'))
    except (OSError, ValueError, TypeError, KeyError):
        return False


def remove_generated_state(root, removed):
    pointers = root / 'state/clients'
    if pointers.is_dir() and not pointers.is_symlink():
        for pointer in pointers.iterdir():
            if pointer.name.isdecimal() and pointer.is_file():
                pointer.unlink()
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
        remove_idle_locks(session)
        if not any(session.iterdir()):
            session.rmdir()


def remove_idle_locks(session):
    """Unlink only locks nobody holds; every holder re-checks its inode, so exclusion stays intact."""
    for lock in session.glob('*.lock'):
        if not is_generated_lock(lock):
            continue
        descriptor = os.open(lock, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            lock.unlink()
        except BlockingIOError:
            continue
        finally:
            os.close(descriptor)


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

    try:
        record = read_state(state)
    except (OSError, ValueError, TypeError, KeyError):
        record = None
    # Unknown state also protects the session's diagnostics.
    if state.is_symlink() or (state.exists() and (record is None or record['pending'])):
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
