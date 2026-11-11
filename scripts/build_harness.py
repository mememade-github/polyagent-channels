"""Build only the client allowlist and pinned pure-Python dependency records."""
import argparse
import importlib.metadata as metadata
import re
from pathlib import Path
import zipfile

PACKAGES = ('httpx', 'httpcore', 'h11', 'certifi', 'idna', 'anyio', 'typing-extensions',
            'websockets', 'async-timeout', 'exceptiongroup', 'tomli')
SOURCES = ('__init__.py', 'cli.py', 'exchange.py', 'client_config.py', 'identity.py', 'setup.py', 'stdio_tools.py')
ENTRY = '''import sys
if sys.argv[1:2] and sys.argv[1] in ('plan', 'install', 'uninstall'):
    from polyagent_channels.setup import main
    sys.exit(main())
from polyagent_channels.cli import main
main()
'''


def build(output, root):
    pins = dict(re.findall(r'^([\w-]+)==([^\s;]+)', (root / 'requirements.lock').read_text(), re.M))
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('__main__.py', ENTRY)
        archive.writestr('POLYAGENT-HARNESS', '1\n')
        archive.write(root / 'LICENSE', 'LICENSE')
        for name in SOURCES:
            archive.write(root / 'src/polyagent_channels' / name, 'polyagent_channels/' + name)
        for name in PACKAGES:
            distribution = metadata.distribution(name)
            if distribution.version != pins[name]:
                raise ValueError('Client distribution differs from lock: ' + name)
            files = distribution.files
            if not files:
                raise ValueError('Missing distribution records: ' + name)
            licenses = [file for file in files if 'license' in str(file).lower() or 'copying' in str(file).lower()]
            if not licenses:
                raise ValueError('Missing dependency license: ' + name)
            for file in files:
                path = str(file)
                if path.startswith(('/', '../')) or path.endswith(('.pyc', '.pyo', '.so', '.pyd')) or '/__pycache__/' in path:
                    continue
                if '.dist-info/' in path and file.name not in ('METADATA', 'WHEEL', 'RECORD', 'top_level.txt', 'entry_points.txt') and file not in licenses:
                    continue
                # Distribution records provide package resources, including certifi's CA bundle.
                source = Path(distribution.locate_file(file))
                if not source.is_file():
                    raise ValueError('Missing client resource: ' + name + '/' + path)
                archive.write(source, path)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    options = parser.parse_args()
    build(options.output, Path(__file__).resolve().parents[1])
