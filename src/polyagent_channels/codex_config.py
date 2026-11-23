"""Exact TOML transformations shared by client setup and the Git filter."""
import hashlib
import json
from pathlib import Path
import shlex
import sys

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

CODEX_BLOCK_START = '# polyagent-channels managed block\n'
CODEX_BLOCK_END = '# end polyagent-channels managed block\n'
FILTERED_BLOCK_START = '# polyagent-channels filter ownership: '
OWNERSHIP_SHAPES = ((), ('setting',), ('newline',), ('setting', 'newline'))
CODEX_SETTING = '# polyagent-channels: end the session when its window closes\nthread_unload_delay_secs = 0\n'
LEGACY_ENV_VARS = 'env_vars = ["CODEX_HOME"]\n'
CODEX_ENV_VARS = 'env_vars = ["CODEX_HOME", "SKIP_CODEX_UPDATE"]\n'


class SetupError(ValueError):
    """A diagnostic containing only installer-authored safe text."""


def missing_root_hint(command):
    if not isinstance(command, str):
        return ''
    wrapper = Path(command)
    if not wrapper.is_absolute() or wrapper.parts[-2:] != ('bin', 'channels'):
        return ''
    return (f' Found command {shlex.quote(command)}. If this is a Polyagent Channels binding from a missing client root, '
            f'install with --target {shlex.quote(str(wrapper.parent.parent))} to restore it, then uninstall there before moving.')


def codex_block(wrapper):
    """Project MCP needs no per-hook review; the approval mode records the one consent given at installation."""
    return (CODEX_BLOCK_START + '[mcp_servers.channels]\ncommand = ' + json.dumps(str(wrapper))
            + '\nargs = ["--client", "codex", "mcp"]\n' + CODEX_ENV_VARS
            + 'default_tools_approval_mode = "approve"\n' + CODEX_BLOCK_END)


def is_immediate_unload(value):
    """Codex types this setting as an integer, so false and 0.0 do not count as 0."""
    return type(value) is int and value == 0


def parsed_toml(text, path):
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        raise SetupError('Codex configuration is not valid TOML; fix it first: ' + str(path)) from None


def codex_config(path, wrapper):
    """Return the config with the tools and the close-on-exit setting added, judged by its parsed meaning."""
    content = path.read_bytes().decode('utf-8') if path.exists() else ''
    updated, additions = add_registration(content, path, wrapper)
    created = [name for name, added in (('directory', not path.parent.exists()), ('file', not path.exists())) if added]
    return updated.encode(), created + additions


def add_registration(content, path, wrapper, *, filtered=False):
    settings = parsed_toml(content, path)
    block = codex_block(wrapper)
    match = owned_registration(content, wrapper, settings)
    registered = match is not None
    servers = settings.get('mcp_servers', {})
    if not isinstance(servers, dict) or (not registered and 'channels' in servers):
        server = servers.get('channels') if isinstance(servers, dict) else None
        hint = missing_root_hint(server.get('command')) if isinstance(server, dict) else ''
        raise SetupError('Another Codex channels server is configured in ' + str(path) + '; inspect it' + hint)
    delay = settings.get('thread_unload_delay_secs')
    if delay is not None and not is_immediate_unload(delay):
        raise SetupError('thread_unload_delay_secs in ' + str(path) + ' would keep a closed window running; set it to 0 or remove it')
    if filtered and match is not None and not is_filtered_block(match[1]):
        content = without_registration(content, match)
        registered = False
    ending = '\r\n' if '\r\n' in content and '\n' not in content.replace('\r\n', '') else '\n'
    if registered:
        _, _, ownership, ending = match
        if delay is None and 'setting' not in ownership:
            ownership = (*ownership, 'setting')
        content = replace_registration(content, match, block, ownership=ownership)
    created = [name for name, added in (('setting', delay is None),
                                        ('newline', not registered and bool(content) and not content.endswith('\n'))) if added]
    updated = (CODEX_SETTING.replace('\n', ending) if delay is None else '') + content
    if not registered:
        separator = '\n' if 'newline' in created else ''
        registered_block = filtered_block(block, created) if filtered else block
        updated += (separator + registered_block).replace('\n', ending)
    try:
        result = tomllib.loads(updated)
    except tomllib.TOMLDecodeError:
        raise SetupError(f'Cannot extend mcp_servers in {path}; define MCP servers using regular tables '
                         'such as [mcp_servers.NAME]') from None
    result_servers = result.get('mcp_servers', {})
    server = result_servers.get('channels')
    if not is_immediate_unload(result.get('thread_unload_delay_secs')) or not isinstance(server, dict) or server.get('command') != str(wrapper):
        raise SetupError('Could not add the channels server to ' + str(path) + '; inspect it')
    return updated, created


def codex_config_removed(path, wrapper, created):
    """Take out exactly what installation appended and prepended; anything edited since stays."""
    content = path.read_bytes().decode('utf-8')
    return remove_registration(content, wrapper, created)


def ownership_label(created):
    setting = 'setting' if 'setting' in created else 'no-setting'
    newline = 'separator' if 'newline' in created else 'no-separator'
    return f'{setting} {newline}'


def filtered_block(block, created):
    body = block.removeprefix(CODEX_BLOCK_START).removesuffix(CODEX_BLOCK_END)
    ownership = ownership_label(created)
    checksum = hashlib.sha256(f'{ownership}\n{body}'.encode()).hexdigest()[:16]
    return (f'{FILTERED_BLOCK_START}{ownership} {checksum}\n' + body
            + f'# end polyagent-channels filtered block: {ownership}\n')


def has_registration(content, wrapper):
    if isinstance(content, bytes):
        try:
            content = content.decode('utf-8')
        except UnicodeDecodeError:
            return False
    return owned_registration(content, wrapper) is not None


def owned_registration(content, wrapper, settings=None, created=()):
    if settings is None:
        try:
            settings = tomllib.loads(content)
        except tomllib.TOMLDecodeError:
            return None
    servers = settings.get('mcp_servers')
    if not isinstance(servers, dict) or 'channels' not in servers:
        return None
    for block in registration_blocks(wrapper):
        if servers['channels'] == tomllib.loads(block)['mcp_servers']['channels']:
            return find_registration(content, block, settings, created)
    return None


def find_registration(content, block, settings, created):
    for registered, ownership, ending in registration_candidates(block, created):
        offset = owned_offset(content, registered, settings, ending)
        if offset is not None:
            return offset, registered, ownership, ending
    return None


def owned_offset(content, registered, settings, ending):
    # Renaming just the candidate header must create a new table. A header
    # inside a multiline value stays string data, even if the real table exists.
    probe = 'polyagent_channels_ownership_probe'
    while probe in settings:
        probe += '_'
    header = '[mcp_servers.channels]' + ending
    offset = content.find(registered)
    while offset != -1:
        if starts_on_line(content, registered, ending, offset):
            position = offset + registered.index(header)
            renamed = content[:position] + f'[{probe}]' + ending + content[position + len(header):]
            try:
                if probe in tomllib.loads(renamed):
                    return offset
            except tomllib.TOMLDecodeError:
                pass
        offset = content.find(registered, offset + 1)
    return None


def starts_on_line(content, registered, ending, offset):
    return registered.startswith(ending) or offset == 0 or content[offset - 1] == '\n'


def registration_blocks(wrapper):
    block = codex_block(wrapper)
    return block, block.replace(CODEX_ENV_VARS, LEGACY_ENV_VARS)


def registration_candidates(block, created=()):
    for ending in ('\n', '\r\n'):
        for ownership in OWNERSHIP_SHAPES:
            separator = '\n' if 'newline' in ownership else ''
            registered = separator + filtered_block(block, ownership)
            yield registered.replace('\n', ending), ownership, ending
    separator = '\n' if 'newline' in created else ''
    for ending in ('\n', '\r\n'):
        yield (separator + block).replace('\n', ending), created, ending


def has_registration_candidate(content, wrapper):
    return any(has_block_candidate(content, block) for block in registration_blocks(wrapper))


def has_block_candidate(content, block):
    for registered, _, ending in registration_candidates(block):
        start = 0
        while (offset := content.find(registered, start)) != -1:
            if starts_on_line(content, registered, ending, offset):
                return True
            start = offset + 1
    return False


def is_filtered_block(registered):
    return registered.lstrip('\r\n').startswith(FILTERED_BLOCK_START)


def replace_registration(content, match, block, *, ownership=None):
    offset, registered, previous_ownership, ending = match
    if ownership is None:
        ownership = previous_ownership
    replacement = filtered_block(block, ownership) if is_filtered_block(registered) else block
    separator = ending if registered.startswith(ending) else ''
    return content[:offset] + separator + replacement.replace('\n', ending) + content[offset + len(registered):]


def remove_registration(content, wrapper, created=()):
    """Remove only a complete, exact registration for this wrapper."""
    match = owned_registration(content, wrapper, created=created)
    return without_registration(content, match) if match is not None else content


def without_registration(content, match):
    offset, registered, ownership, ending = match
    suffix = content[offset + len(registered):]
    if suffix and registered.startswith(ending):
        # The separator now also keeps the user's appended lines separate.
        suffix = ending + suffix
    content = content[:offset] + suffix
    setting = CODEX_SETTING.replace('\n', ending)
    if 'setting' in ownership and content.startswith(setting):
        return content[len(setting):]
    return content


def filter_content(mode, body, project, wrapper):
    path = project / '.codex/config.toml'
    if mode == 'clean':
        return clean_content(body, path, wrapper)
    try:
        content = body.decode('utf-8')
    except UnicodeDecodeError:
        return body
    try:
        updated, _ = add_registration(content, path, wrapper, filtered=True)
    except SetupError:
        return body
    return updated.encode('utf-8')


def clean_content(body, path, wrapper):
    try:
        content = body.decode('utf-8')
        settings = tomllib.loads(content)
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        if has_registration_candidate(body.decode('utf-8', 'surrogateescape'), wrapper):
            raise SetupError(f'Fix malformed TOML in {path} before Git can clean its channels block') from None
        return body
    match = owned_registration(content, wrapper, settings)
    return without_registration(content, match).encode('utf-8') if match is not None else body


def filter_main():
    mode, worktree, name, wrapper, filename = sys.argv[1:]
    if Path.cwd() != Path(worktree) or filename != name:
        sys.exit('Channels Git filter scope mismatch. Restore the installed project location and check the filter assignment.')
    body = sys.stdin.buffer.read()
    try:
        updated = filter_content(mode, body, Path(worktree), wrapper)
    except SetupError as error:
        sys.exit(str(error))
    sys.stdout.buffer.write(updated)


def filtered_config(path, wrapper, created):
    body = path.read_bytes()
    content = body.decode('utf-8')
    match = owned_registration(content, wrapper, created=created)
    if match is not None:
        if is_filtered_block(match[1]):
            block = codex_block(wrapper)
            if 'thread_unload_delay_secs' in parsed_toml(content, path):
                return replace_registration(content, match, block).encode('utf-8')
            _, _, ownership, ending = match
            repaired = replace_registration(content, match, block, ownership=(*ownership, 'setting'))
            return (CODEX_SETTING.replace('\n', ending) + repaired).encode('utf-8')
        content = without_registration(content, match)
    updated, _ = add_registration(content, path, wrapper, filtered=True)
    return updated.encode('utf-8')
