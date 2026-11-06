"""URL-only shared configuration and conventional shared-key paths."""
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_ROOT = Path(os.environ.get('CHANNEL_ROOT') or Path.home() / '.local/share/polyagent-channels').expanduser().absolute()


class ConfigError(ValueError):
    code = 'config_invalid'


class ConfigMissing(ConfigError):
    code = 'config_missing'


class ConfigUnreadable(ConfigError):
    code = 'config_unreadable'


class ConfigSchemaError(ConfigError):
    code = 'config_schema_invalid'


class ConfigJSONError(ConfigError):
    code = 'config_json_invalid'


class ConfigURLError(ConfigError):
    code = 'config_url_invalid'


def service_url(value):
    try:
        parsed = urlsplit(value)
        port = parsed.port
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/'):
            raise ConfigURLError()
    except (ValueError, TypeError):
        raise ConfigURLError() from None
    return value.rstrip('/')


def read_config(path):
    try:
        content = Path(path).read_text(encoding='utf-8')
    except FileNotFoundError:
        raise ConfigMissing() from None
    except OSError:
        raise ConfigUnreadable() from None
    except UnicodeError:
        raise ConfigJSONError() from None
    try:
        data = json.loads(content)
    except ValueError:
        raise ConfigJSONError() from None
    if not isinstance(data, dict) or set(data) != {'url'} or not isinstance(data['url'], str):
        raise ConfigSchemaError()
    return service_url(data['url'])


def apply_config(options, arguments):
    root = Path(options.config).expanduser().absolute().parent if options.config else DEFAULT_ROOT
    if options.config:
        url = read_config(options.config)
        if not any(item == '--url' or item.startswith('--url=') for item in arguments):
            options.url = url
    options.url = service_url(options.url)
    if options.key_file is None:
        options.key_file = str(root / '.env/channel.key')
    if options.state_dir is None:
        options.state_dir = str(root / 'state')
