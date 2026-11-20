"""Install the clone-local Git driver for a tracked Codex project binding."""
from dataclasses import dataclass
from functools import partial
import hashlib
import io
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import zipfile

from polyagent_channels.codex_config import SetupError, has_registration


TRACKED_ATTRIBUTES = '.codex/.gitattributes is tracked.'
FOREIGN_FILTER = 'another filter applies to .codex/config.toml.'
TRACKED_IGNORE = '.codex/.gitignore is tracked and Git ignore rules must stay local to this worktree.'
SPLIT_FILESYSTEM = 'the Git common directory and .codex are on different filesystems.'
LOCAL_FILTER = 'A Git filter local to this clone keeps the installed lines out of Git.'
VISIBLE = 'Git can see the installed lines because '
INDEX_REGISTRATION = 'Stage and commit .codex/config.toml to remove the installed server block from Git.'
LEGACY_REWRITE = ('With the clone-local filter in place, Git clean and checkout, reinstall, and uninstall preserve '
                  'the marked unload setting and preceding newline of an exact plain legacy block because either '
                  'may be yours. Review what remains before committing the cleanup.')


def git(directory, *arguments, accepted=(0,), index_file=None):
    environment = {**os.environ, 'LC_ALL': 'C'}
    if index_file is not None:
        environment['GIT_INDEX_FILE'] = str(index_file)
        environment['GIT_OPTIONAL_LOCKS'] = '1'
    try:
        result = subprocess.run(
            ['git', '-C', str(directory), *arguments], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=environment, check=False,
        )
    except OSError as error:
        raise SetupError(f'Git {arguments[0]} could not run ({type(error).__name__})') from None
    if result.returncode not in accepted:
        # Git's diagnostics can contain arbitrary filter output and private configuration.
        raise SetupError(f'Git {arguments[0]} failed for Codex project configuration (exit {result.returncode})')
    return result


def clone(project):
    if not project.is_dir() or shutil.which('git') is None:
        return None
    result = git(project, 'rev-parse', '--show-toplevel', accepted=(0, 128))
    if result.returncode == 128:
        if b'not a git repository' in result.stderr and not any(
                (parent / '.git').exists() for parent in (project, *project.parents)):
            return None
        raise SetupError('Git could not find the working tree for Codex project configuration (rev-parse)')
    worktree = Path(os.fsdecode(result.stdout.removesuffix(b'\n')))
    common = git(project, 'rev-parse', '--path-format=absolute', '--git-common-dir').stdout
    return worktree, Path(os.fsdecode(common.removesuffix(b'\n')))


@dataclass(frozen=True)
class CodexGit:
    worktree: Path
    common: Path
    project: Path
    wrapper: Path

    @property
    def name(self):
        return (self.project / '.codex/config.toml').relative_to(self.worktree).as_posix()

    @property
    def driver(self):
        binding = os.fsencode(self.project) + b'\0' + os.fsencode(self.wrapper)
        return 'polyagent-channels-' + hashlib.sha256(binding).hexdigest()

    @property
    def program(self):
        return self.common / (self.driver + '.pyz')

    @property
    def attributes(self):
        return self.project / '.codex/.gitattributes'

    @property
    def assignment(self):
        # Own the separator so removal restores a file without a final newline.
        return f'\n/config.toml filter={self.driver}\n'.encode()

    @property
    def binding_prefix(self):
        return f'.{self.driver}-config-'

    @property
    def local_ignore(self):
        return self.attributes.with_name('.gitignore')

    @property
    def local_rule(self):
        return f'# {self.driver}\n/.gitattributes\n/.gitignore\n/{self.binding_prefix}*\n'.encode()

    def run(self, *arguments, **keywords):
        return git(self.worktree, *arguments, **keywords)

    def index(self, **options):
        entries = self.run('ls-files', '--stage', '-z', '--', literal(self.name), **options).stdout
        if not entries:
            return None
        records = entries.rstrip(b'\0').split(b'\0')
        mode, blob, stage = records[0].split(b'\t', 1)[0].decode().split()
        if len(records) != 1 or stage != '0':
            raise SetupError('Resolve the Git conflict in .codex/config.toml before installing channels')
        return mode, blob

    def tracked(self, name):
        return bool(self.run('ls-files', '-z', '--', literal(name)).stdout)

    def effective_driver(self):
        result = self.run('check-attr', '-z', '--all', '--', self.name).stdout
        fields = result.split(b'\0')
        for attribute, value in zip(fields[1::3], fields[2::3]):
            if attribute == b'filter':
                return value.decode('utf-8', 'replace')
        return None

    def refuse_stale_driver(self):
        driver = self.effective_driver()
        if driver is None or driver == self.driver or not driver.startswith('polyagent-channels-'):
            return
        body = read(self.worktree / self.name)
        if has_registration(body, self.wrapper):
            raise SetupError('The channels Git filter belongs to another project location. '
                             'For a move, move back and uninstall with the current installer at the original location '
                             'before moving again. For a copy, start with a clean checkout. Then reinstall.')

    def preflight(self):
        index = self.index()
        if not (self.worktree / self.name).is_file() and (index is not None or self.head_entry()):
            raise SetupError('Git tracks .codex/config.toml but it is missing; restore it before installing channels')
        if index is None:
            return False, ''
        if index[0] not in ('100644', '100755'):
            raise SetupError('Restore .codex/config.toml as a regular tracked file before installing channels')
        attribute_name = self.attributes.relative_to(self.worktree).as_posix()
        if self.tracked(attribute_name):
            return False, VISIBLE + TRACKED_ATTRIBUTES
        driver = self.effective_driver()
        if driver not in (None, self.driver):
            return False, VISIBLE + FOREIGN_FILTER
        if self.tracked(self.local_ignore.relative_to(self.worktree).as_posix()):
            return False, VISIBLE + TRACKED_IGNORE
        if self.common.stat().st_dev != self.attributes.parent.stat().st_dev:
            return False, VISIBLE + SPLIT_FILESYSTEM
        self.check_metadata()
        self.check_driver_config(repairable_scopes=(b'', b'system', b'global', b'local'))
        indexed = self.run('cat-file', 'blob', index[1]).stdout
        record = self.head_entry()
        committed = b''
        if record:
            blob = record.split(b'\t', 1)[0].decode().split()[2]
            committed = self.run('cat-file', 'blob', blob).stdout
        staged_or_committed = any(has_registration(body, self.wrapper) for body in (indexed, committed))
        if staged_or_committed:
            return True, ' '.join((LOCAL_FILTER, INDEX_REGISTRATION, LEGACY_REWRITE))
        return True, LOCAL_FILTER

    def head_entry(self):
        head = self.run('rev-parse', '--verify', '--quiet', 'HEAD', accepted=(0, 1))
        if head.returncode == 0:
            return self.run('ls-tree', '-z', 'HEAD', '--', literal(self.name)).stdout
        return b''

    def check_metadata(self):
        for path in (self.attributes, self.local_ignore, self.program):
            if path.is_symlink() or (path.exists() and not path.is_file()):
                raise SetupError(f'Refusing a non-regular Git filter file: {path.name}')

    def settings(self):
        prefix = ' '.join(map(filter_argument, [os.path.abspath(sys.executable), '-I', str(self.program)]))
        arguments = ' '.join(map(filter_argument, [str(self.worktree), self.name, str(self.wrapper)]))
        return {f'filter.{self.driver}.{mode}': f'{prefix} {mode} {arguments} %f'
                for mode in ('clean', 'smudge')} | {f'filter.{self.driver}.required': 'true'}

    def config_values(self, key):
        result = self.run('config', '--local', '--null', '--get-all', key, accepted=(0, 1))
        return result.stdout.decode().split('\0')[:-1]

    def effective_config(self, key):
        result = self.run('config', '--null', '--show-scope', '--get', key, accepted=(0, 1))
        if result.returncode == 1:
            return b'', b''
        scope, value, _ = result.stdout.split(b'\0')
        return scope, value

    def check_driver_config(self, *, repairable_scopes=()):
        for key, expected in self.settings().items():
            scope, value = self.effective_config(key)
            if value != expected.encode() and scope not in repairable_scopes:
                raise SetupError(f'Git configuration overrides the channels filter setting {key}; '
                                 'inspect its effective Git configuration before installing channels')
        if self.effective_config(f'filter.{self.driver}.process')[1]:
            raise SetupError('A Git process filter overrides the channels filter; inspect Git configuration')

    def ready(self, program):
        complete = (read(self.program) == program and self.has_assignment()
                    and self.local_rule in read(self.local_ignore) and self.effective_driver() == self.driver
                    and self.ignore_rules_apply()
                    and all(self.config_values(key) == [value] for key, value in self.settings().items()))
        if complete:
            self.check_driver_config()
        return complete

    def metadata_writer(self, write):
        return partial(write, temporary_dir=self.common, prefix=self.metadata_prefix)

    def has_assignment(self):
        return has_rule(self.attributes, self.assignment)

    @property
    def metadata_prefix(self):
        return f'.{self.driver}-metadata-'

    def cleanup_metadata_temps(self):
        for path in self.common.glob(self.metadata_prefix + '*'):
            if path.is_file() and not path.is_symlink():
                path.unlink(missing_ok=True)

    def enable(self, program, write):
        write = self.metadata_writer(write)
        # The assignment becomes effective only when a complete required driver exists.
        if read(self.program) != program:
            write(self.program, program)
        for key, value in self.settings().items():
            if self.config_values(key) != [value]:
                self.run('config', '--local', '--replace-all', key, value)
        self.check_driver_config()
        prepend_rule(self.local_ignore, self.local_rule, write)
        if not self.ignore_rules_apply():
            raise SetupError('Git ignore rules override the local channels rules in .codex')
        # The last matching assignment wins, including after a local !filter.
        attributes = read(self.attributes)
        if self.assignment not in attributes:
            write(self.attributes, attributes + self.assignment)
        if self.effective_driver() != self.driver:
            raise SetupError('A Git attribute overrides the channels filter for .codex/config.toml')

    def has_filter_metadata(self):
        return (self.program.exists() or self.has_assignment()
                or has_rule(self.local_ignore, self.local_rule)
                or any(self.config_values(key) for key in self.settings()))

    def disable(self, write):
        if not self.has_filter_metadata():
            return False
        self.check_metadata()
        self.cleanup_binding_temps()
        write = self.metadata_writer(write)
        remove_rule(self.attributes, self.assignment, write)
        self.refresh()
        remove_rule(self.local_ignore, self.local_rule, write)
        for key in self.settings():
            if self.config_values(key):
                self.run('config', '--local', '--unset-all', key)
        self.program.unlink(missing_ok=True)
        return True

    def cleanup_binding_temps(self):
        # A killed installer can leave an ignored temporary containing the registration.
        for path in self.attributes.parent.glob(self.binding_prefix + '*'):
            if path.is_file() and not path.is_symlink():
                path.unlink(missing_ok=True)

    def ignore_rules_apply(self):
        temp_probe = self.attributes.with_name(self.binding_prefix + 'probe')
        return (self.ignored(self.attributes) and self.ignored(temp_probe)
                and self.ignored(self.local_ignore))

    def ignored(self, path):
        name = path.relative_to(self.worktree).as_posix()
        return self.run('check-ignore', '--no-index', '--', name, accepted=(0, 1)).returncode == 0

    def refresh(self):
        if not self.run('diff-files', '--name-only', '-z', '--', literal(self.name)).stdout:
            return
        raw = self.run('rev-parse', '--path-format=absolute', '--git-path', 'index').stdout
        index = Path(os.fsdecode(raw.removesuffix(b'\n')))
        lock = index.with_name(index.name + '.lock')
        try:
            target = lock.open('xb')
        except FileExistsError:
            raise SetupError('Git index is locked; retry channels setup after Git finishes') from None
        try:
            with target, index.open('rb') as source:
                shutil.copyfileobj(source, target)
            if self.reset_cached_stat(lock):
                os.replace(lock, index)
                return
        except BaseException:
            lock.unlink(missing_ok=True)
            raise
        lock.unlink()

    def reset_cached_stat(self, lock):
        # Work on the snapshot taken under Git's real index lock. Publishing the
        # same entry invalidates stat data without ever staging working bytes.
        entry = self.index(index_file=lock)
        path = self.worktree / self.name
        if entry is None or path.is_symlink() or not path.is_file():
            return False
        mode, blob = entry
        if mode not in ('100644', '100755'):
            return False
        debug = self.run('ls-files', '--debug', '--', literal(self.name), index_file=lock).stdout
        flags = int(debug.rsplit(b'flags: ', 1)[1], 16)
        # CE_VALID and CE_EXTENDED identify persistent entry flags that
        # --cacheinfo would drop. Split-index bookkeeping is transient.
        if flags & 0xc000:
            return False
        actual = self.run('hash-object', f'--path={self.name}', '--', self.name).stdout.decode().strip()
        if actual != blob:
            return False
        self.run('update-index', '--cacheinfo', mode, blob, self.name, index_file=lock)
        # Status caches the current stat data if the working content still matches the blob.
        self.run('status', '--porcelain', '--untracked-files=no', '--ignore-submodules=all',
                 '--', literal(self.name), index_file=lock)
        return True


def filter_argument(argument):
    # Git expands %f before the shell parses the command. Quote each literal
    # percent separately so Git never sees an adjacent percent and f here.
    return "'%'".join(map(shlex.quote, argument.split('%')))


def literal(name):
    return ':(literal)' + name


def read(path):
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return b''


def has_rule(path, rule):
    return not path.is_symlink() and path.is_file() and rule in read(path)


def prepend_rule(path, rule, write):
    existing = read(path)
    if rule not in existing:
        write(path, rule + existing)


def remove_rule(path, rule, write):
    existing = read(path)
    if rule in existing:
        remaining = existing.replace(rule, b'', 1)
        if remaining:
            write(path, remaining)
        if not remaining:
            path.unlink()


def filter_program(source):
    """Copy just the transformation and its Python 3.10 TOML reader into a standalone zipapp."""
    output = io.BytesIO()
    with zipfile.ZipFile(source) as installer, zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as program:
        write_program_entry(program, '__main__.py', 'from codex_config import filter_main\nfilter_main()\n')
        write_program_entry(program, 'codex_config.py', installer.read('polyagent_channels/codex_config.py'))
        write_program_entry(program, 'LICENSE', installer.read('LICENSE'))
        for name in installer.namelist():
            if name.startswith('tomli/') or (name.startswith('tomli-') and 'license' in name.lower()):
                write_program_entry(program, name, installer.read(name))
    return output.getvalue()


def write_program_entry(program, name, body):
    # Stable archive bytes let an identical install leave the clone untouched.
    program.writestr(zipfile.ZipInfo(name), body, compress_type=zipfile.ZIP_DEFLATED)
