"""URL-only shared configuration and explicit conventional profile paths."""
import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_ROOT = Path.home() / '.local/share/polyagent-channels'


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


class ConfigProfileError(ConfigError):
    code = 'config_profile_invalid'


def service_url(value):
    try:
        parsed = urlsplit(value)
        port = parsed.port
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/'):
            raise ConfigURLError()
    except (ValueError, TypeError):
        raise ConfigURLError() from None
    return value.rstrip('/')


def profile_paths(config, profile):
    if not isinstance(profile, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', profile):
        raise ConfigProfileError()
    root = Path(os.path.abspath(Path(config).expanduser())).parent
    return root / '.env/credentials' / (profile + '.key'), root / 'state' / profile


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
    if options.config is None and options.profile is None:
        return
    if options.profile is None:
        raise ConfigProfileError()
    options.config = os.path.abspath(Path(options.config or DEFAULT_ROOT / 'config.json').expanduser())
    credential, state = profile_paths(options.config, options.profile)
    url = read_config(options.config)
    # Explicit flags retain precedence; vendor environment never selects profile paths.
    for flag, field, value in (('--url', 'url', url), ('--credential-file', 'credential_file', str(credential)), ('--state-dir', 'state_dir', str(state))):
        if not any(item == flag or item.startswith(flag + '=') for item in arguments):
            setattr(options, field, value)
    if hasattr(options, 'channel') and options.channel is None:
        options.channel = 'general'
    if hasattr(options, 'conversation_id') and options.conversation_id is None:
        options.conversation_id = 'main'
