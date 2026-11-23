"""Render the marked operator-document regions from runtime definitions and Compose."""
import argparse
import ast
from contextlib import redirect_stdout
import hashlib
from io import StringIO
import inspect
import os
from pathlib import Path
import re
import sys
import stat
from tempfile import TemporaryDirectory
import tomllib
from unittest.mock import patch

from polyagent_channels import auth, cli, client_config, codex_git, exchange, harness, identity, membership, persistence, server, setup, stdio_tools


SOURCE = Path(__file__).resolve()
ROOT = SOURCE.parent.parent
FIELD = re.compile(r'\{\{([A-Za-z_][A-Za-z_0-9.]*)\}\}')


def rendered_documents():
    values = definitions()
    template = ROOT / 'scripts/REFERENCE.md.in'
    reference = FIELD.sub(lambda match: str(values[match[1]]), template.read_text(encoding='utf-8'))
    return {
        ROOT / 'REFERENCE.md': replace_region('REFERENCE.md', 'reference', reference),
        ROOT / 'UPGRADING.md': replace_region('UPGRADING.md', 'name', values['help.name_required']),
    }


def replace_region(filename, name, content):
    path = ROOT / filename
    document = path.read_text(encoding='utf-8')
    start = '<!-- generated: ' + name + ' -->\n'
    end = '<!-- end generated: ' + name + ' -->'
    before, remainder = document.split(start)
    _, after = remainder.split(end)
    return before + start + content.rstrip() + '\n' + end + after


def definitions():
    modules = (auth, cli, client_config, codex_git, exchange, identity, membership, persistence, setup, stdio_tools)
    values = {module.__name__.rsplit('.', 1)[1] + '.' + name: value
              for module in modules for name, value in vars(module).items() if name.isupper()}
    values.update(compose_defaults())
    values.update(connection_help(values['compose.CHANNEL_PORT']))
    values.update(command_defaults())
    values.update(key_permissions())
    manifest = ROOT / 'pyproject.toml'
    project = tomllib.loads(manifest.read_text(encoding='utf-8'))['project']
    values['project.python_requirement'] = project['requires-python']
    values['project.python_version'] = project['requires-python'].removeprefix('>=')
    values['setup.claude_next'] = setup.NEXT_STEP['claude']
    values['setup.codex_next'] = setup.NEXT_STEP['codex']
    values['setup.unload_delay'] = tomllib.loads(setup.CODEX_SETTING)['thread_unload_delay_secs']
    values['storage.validation'] = storage_validation()
    values['read.scan_limit'] = format(cli.PAGE_SIZE * cli.MAX_SCANNED_PAGES, ',')
    values['state.kib'] = exchange.STATE_MAX_BYTES // 1024
    values['tools.names'] = ', '.join('`' + tool['name'] + '`' for tool in stdio_tools.TOOLS)
    values['tools.instructions'] = stdio_tools.INSTRUCTIONS.removeprefix('Polyagent Channels:\n')
    publish = next(tool for tool in stdio_tools.TOOLS if tool['name'] == 'publish')
    properties = publish['inputSchema']['properties']
    values['tools.recipients'] = properties['to']['description']
    values['tools.reply_to'] = properties['reply_to']['description']
    ack = next(tool for tool in stdio_tools.TOOLS if tool['name'] == 'ack')
    values['tools.ack_caveat'] = ack['description'].removeprefix(stdio_tools.ACK_GUIDANCE).strip()
    values['receive.recovery'] = recovery_action()
    values['cli.diagnostics'] = diagnostics()
    values['storage.version'] = re.search(r"if version and version ~= '([^']+)'", persistence.LIFECYCLE)[1]
    values['subscribe.timeout'] = subscription_timeout()
    values['protocol'] = protocol_reference()
    return values


def compose_defaults():
    path = ROOT / '.devcontainer/compose.channels.yml'
    content = path.read_text(encoding='utf-8')
    defaults = dict(re.findall(r'\$\{(\w+):?-([^}]*)\}', content))
    defaults['COMPOSE_PROJECT_NAME'] = re.search(r'^name: (\S+)$', content, re.M)[1]
    defaults['container_port'] = re.search(r'\$\{CHANNEL_PORT[^}]+\}:(\d+)', content)[1]
    defaults['redis_image'] = re.search(r'image: (redis:\S+)', content)[1]
    defaults['appendfsync'] = re.search(r'"--appendfsync", "([^"]+)"', content)[1]
    return {'compose.' + name: value for name, value in defaults.items()}


def between(content, start, end):
    beginning = content.index(start) + len(start)
    ending = content.index(end, beginning)
    return content[beginning:ending].strip()


def connection_help(port):
    # A documentation render needs no built artifact and must never read a deployed one.
    with patch.object(harness, 'artifact_bytes', return_value=b''):
        content = harness.setup_help('http://service-host:' + port)
    checksum = hashlib.sha256(b'').hexdigest()
    check = "  printf '%s  harness.pyz\\n' '" + checksum + "' | sha256sum --check - || exit 1"
    content = content.replace(check, '  curl --fail "$SERVICE/harness.sha256" -o harness.sha256 || exit 1\n'
                              '  sha256sum --check harness.sha256 || exit 1')
    content = content.replace('rm -f "$SETUP_DIR/harness.pyz"',
                              'rm -f "$SETUP_DIR/harness.pyz" "$SETUP_DIR/harness.sha256"')
    if checksum in content:
        raise ValueError('First-connection checksum command changed; update its documentation rendering')
    return connection_sections(content)


def connection_sections(content):
    name = between(content, 'Set NAME first:', '\nSERVICE=')
    return {
        'help.requirements': between(content, 'Polyagent Channels: first connection\n', 'Read /help'),
        'help.install': between(content, '1. ', 'Set NAME first:'),
        'help.name': 'Set NAME first: ' + name,
        'help.name_required': '`/help` step 1 now requires a display name for each project and client.\n'
                              'Set NAME first: ' + name.split('It must be', 1)[0].strip()
                              + '\nSee [Display name](REFERENCE.md#display-name) before reinstalling.',
        'help.commands': 'SERVICE=' + between(content, '\nSERVICE=', '\nStop on failure.'),
        'help.failure': 'Stop on failure. ' + between(content, 'Stop on failure. ', '\n"$CHANNELS"'),
        'help.key': '"$CHANNELS" ' + between(content, '\n"$CHANNELS"', '\n2. '),
        'help.next': between(content, '\n2. ', '\n3. '),
        'help.starts': between(content, 'instructions.\n', '\nTo remove,'),
        'help.remove': between(content, 'To remove, use:\n', '\nTo update'),
        'help.update': update_procedure(content),
    }


def update_procedure(content):
    update = 'To update ' + content.split('\nTo update', 1)[1].strip()
    update = update.replace('line\nabove', '[removal command](#uninstall)')
    return update.replace('steps 1 and 2', '[installation and restart steps](#client-installation)')


def command_defaults():
    with patch.dict(os.environ, {}, clear=True), patch.object(Path, 'cwd', return_value=Path('<current directory>')), patch.object(sys, 'argv', ['channels']):
        return command_values()


def command_values():
    parser = cli.command_parser()
    values = {'cli.options': parser.format_help().split('\noptions:\n', 1)[1].rstrip()}
    values['cli.key_help'] = command_help(parser, 'key')
    for name in ('connect', 'receive', 'wait', 'listen'):
        options = parser.parse_args([name])
        values['cli.' + name + '_timeout'] = options.timeout
        values['cli.' + name + '_cursor'] = getattr(options, 'cursor', None)
    return values


def command_help(parser, name):
    output = StringIO()
    with redirect_stdout(output):
        try:
            parser.parse_args([name, '--help'])
        except SystemExit as error:
            if error.code != 0:
                raise
    return output.getvalue().strip()


def key_permissions():
    with TemporaryDirectory() as directory:
        return created_key_permissions(Path(directory) / 'private' / 'key')


def created_key_permissions(path):
    with patch.dict(os.environ, CHANNEL_KEY_FILE=str(path)):
        auth.load_key()
    directory = path.parent
    return {'key.file_mode': format(stat.S_IMODE(path.stat().st_mode), '04o'),
            'key.directory_mode': format(stat.S_IMODE(directory.stat().st_mode), '04o')}


def subscription_timeout():
    source = inspect.getsource(server.subscribe)
    calls = [node for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Call)]
    receive = next(call for call in calls if isinstance(call.func, ast.Name) and call.func.id == 'receive')
    return ast.literal_eval(receive.args[2])


def recovery_action():
    source = inspect.getsource(cli.report_page)
    literals = [node.value for node in ast.walk(ast.parse(source))
                if isinstance(node, ast.Constant) and isinstance(node.value, str)]
    recovery = next(value for value in literals if 'were removed from retained history' in value)
    return recovery.split('. ', 1)[1]


def protocol_reference():
    content = server.HELP.replace(' ' + client_config.TRANSPORT_TRUST, '')
    content = content.replace(stdio_tools.PEER_TRUST + '\n', '')
    content = content.replace(' ' + stdio_tools.ANNOUNCEMENT_NOTICE, '')
    return content.strip()


def storage_validation():
    with patch.dict(os.environ, CHANNEL_MAX_STORAGE_BYTES='invalid'):
        try:
            persistence.max_storage_bytes()
        except ValueError as error:
            return str(error)
    raise ValueError('Storage validation unexpectedly accepted invalid input')


def diagnostics():
    options = argparse.Namespace(url='&lt;service-url&gt;')
    result = {'install_command': None, 'resume_command': None}
    missing = cli.missing_binding_action(options, result, {'status': 'hook_missing'})
    invalid = cli.missing_binding_action(options, result, {'status': 'hook_invalid'})
    rows = [('hook_missing', 'Nothing is installed for this project and client', missing),
            ('hook_invalid', 'The native binding differs from its record or cannot be read', invalid),
            ('not_open', 'No topic is listening', not_open_action())]
    rows.extend(callback_diagnostics())
    header = '| `reception` | Meaning | Action |\n|-------------|---------|--------|\n'
    return header + '\n'.join('| `' + name + '` | ' + meaning + ' | ' + action + ' |'
                             for name, meaning, action in rows)


def not_open_action():
    with TemporaryDirectory() as directory, patch.object(cli, 'binding_diagnostics', return_value={}):
        options = argparse.Namespace(session_id='documentation', state_dir=directory)
        result = cli.reception_status(options)
    return result['next_action']


def callback_diagnostics():
    cases = [(None, None, 'No receiver has reported for this listening session'),
             ({'state': 'notified'}, cli.ACTIVE_LEASE_SECONDS,
              f'A notice was handed to this session within {cli.ACTIVE_LEASE_SECONDS} seconds'),
             ({'state': 'waiting'}, cli.OBSERVATION_FRESH_SECONDS,
              f'A receiver reported waiting within {cli.OBSERVATION_FRESH_SECONDS} seconds'),
             ({'state': 'waiting'}, cli.OBSERVATION_FRESH_SECONDS + 1,
              f'The report is older than {cli.OBSERVATION_FRESH_SECONDS} seconds '
              f'({cli.ACTIVE_LEASE_SECONDS} for notified), or its timestamp is invalid'),
             ({'state': 'notify_failed'}, 0, 'A recent report other than waiting, such as error or notify_failed')]
    return [callback_diagnostic(case) for case in cases]


def callback_diagnostic(case):
    callback, age, meaning = case
    result = cli.callback_reception({}, callback, age)
    return result['reception'], meaning, result['next_action']


if __name__ == '__main__':
    for path, content in rendered_documents().items():
        path.write_text(content, encoding='utf-8')
