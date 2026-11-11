"""Peer waits, per-session topic state and the wake decision for background receivers."""
import asyncio
import contextlib
import hashlib
import json
import math
import os
import re
import sys
import stat
import tempfile
import fcntl
import time
from pathlib import Path

if sys.version_info >= (3, 11):
    timeout_context = asyncio.timeout
else:
    from async_timeout import timeout as timeout_context

RECEPTION_TIMEOUT = 604800
POLL_TIMEOUT = 60
WATCH_POLL = 30
MAX_SESSION_TOPICS = 16
STATE_MAX_BYTES = 16384
STATE_FIELDS = {'session_id', 'channel', 'conversation_id', 'cursor', 'notified', 'delivered', 'deadline', 'errors', 'pending'}
LARGEST_SEQUENCE = 2**64 - 1


class ExchangeAlreadyOpen(FileExistsError, ValueError):
    """The selected topic is already live for this session."""


class ExchangeMismatch(ValueError):
    """The requested exchange does not match the authenticated local owner/target."""


IDENTIFIER_PATTERN = '[A-Za-z0-9_-]{1,128}'
CURSOR_PATTERN = '[0-9]{1,20}-[0-9]{1,20}'


def validate_identifier(value):
    if not isinstance(value, str) or not re.fullmatch(IDENTIFIER_PATTERN, value):
        raise ValueError('Invalid exchange identifier')


def validate_cursor(value):
    if not isinstance(value, str) or not re.fullmatch(CURSOR_PATTERN, value):
        raise ValueError('Invalid exchange cursor')
    if any(int(part) > 2**64 - 1 for part in value.split('-')):
        raise ValueError('Invalid exchange cursor')


def seconds(value):
    duration = float(value)
    if not math.isfinite(duration) or duration < 0:
        raise ValueError('Timeout must be finite and nonnegative')
    return duration


def positive_seconds(value):
    duration = seconds(value)
    if duration < 1 or duration > RECEPTION_TIMEOUT:
        raise ValueError('Timeout must be between 1 and ' + str(RECEPTION_TIMEOUT) + ' seconds')
    return duration


def validate_session_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9._:-]{1,256}', value):
        raise ValueError('session_invalid')
    return value


async def wait_for_peer(client, target, timeout=0):
    """Carry scan cursors, but return a peer row's ID so later rows stay unread."""
    cursor = target['cursor']
    timer = timeout_context(seconds(timeout) or None)
    try:
        async with timer:
            validate_session_id(target['session_id'])
            while True:
                page = await read_page(client, target, cursor, poll_seconds(timer))
                message = first_peer(page['messages'], target['session_id'])
                if message is not None:
                    return {**message, 'cursor': message['id']}
                cursor = page['cursor']
                # Also honor cancellation when successive pages are immediately ready.
                await asyncio.sleep(0)
    except (TimeoutError, asyncio.TimeoutError):
        if not timer_expired(timer):
            raise
    return {'type': 'timeout', 'cursor': cursor}


def timer_expired(timer):
    if sys.version_info >= (3, 11):
        return timer.expired()
    return timer.expired


def timer_deadline(timer):
    if sys.version_info >= (3, 11):
        return timer.when()
    return timer.deadline


def poll_seconds(timer):
    deadline = timer_deadline(timer)
    if deadline is None:
        return POLL_TIMEOUT
    remaining = deadline - asyncio.get_running_loop().time()
    return min(POLL_TIMEOUT, max(1, math.ceil(remaining)))


async def read_page(client, target, cursor, timeout, limit=100):
    response = await client.get('channels/' + target['channel'] + '/messages', params={
        'conversation_id': target['conversation_id'], 'cursor': cursor, 'timeout': timeout, 'limit': limit,
    })
    response.raise_for_status()
    page = response.json()
    validate_cursor(page['cursor'])
    if not isinstance(page['messages'], list):
        raise ValueError('Invalid message page')
    return page


def first_peer(messages, session_id):
    for message in messages:
        validate_cursor(message['id'])
        if message.get('session_id') != session_id and (message.get('session_id') or message.get('agent_id')):
            return message
    return None


def new_state(session_id, target, timeout):
    state = {**target, 'session_id': session_id, 'notified': target['cursor'], 'delivered': target['cursor'],
             'deadline': time.time() + positive_seconds(timeout), 'errors': 0, 'pending': []}
    validate_state(state)
    return state


def validate_state(state):
    if not isinstance(state, dict) or set(state) != STATE_FIELDS:
        raise ValueError('Invalid exchange state')
    validate_session_id(state['session_id'])
    for field in ('channel', 'conversation_id'):
        validate_identifier(state[field])
    validate_cursor(state['cursor'])
    validate_cursor(state['notified'])
    validate_cursor(state['delivered'])
    if not isinstance(state['pending'], list):
        raise ValueError('Invalid pending list')
    for message_id in state['pending']:
        validate_cursor(message_id)
    if not math.isfinite(state['deadline']) or state['deadline'] <= 0:
        raise ValueError('Invalid deadline')
    if type(state['errors']) is not int or state['errors'] < 0:
        raise ValueError('Invalid exchange error count')


def session_directory(directory, session_id):
    validate_session_id(session_id)
    return Path(directory) / hashlib.sha256(session_id.encode('ascii')).hexdigest()


def client_pointer(directory, pid):
    return Path(directory) / 'clients' / str(pid) if str(pid).isdecimal() else None


def topic_name(target):
    for field in ('channel', 'conversation_id'):
        validate_identifier(target[field])
    return 'topic-' + hashlib.sha256((target['channel'] + '/' + target['conversation_id']).encode('ascii')).hexdigest() + '.json'


def list_states(directory, session_id):
    folder = session_directory(directory, session_id)
    if Path(directory).is_symlink() or folder.is_symlink():
        raise ExchangeMismatch()
    paths = [path for path in sorted(folder.glob('topic-*.json'))
             if re.fullmatch(r'topic-[a-f0-9]{64}\.json', path.name)] + list(folder.glob('exchange.json'))
    for path in paths:
        if read_state(path)['session_id'] != session_id:
            raise ExchangeMismatch()
    return paths


def owned_state(directory, session_id, target=None):
    paths = list_states(directory, session_id)
    if target is None:
        if len(paths) > 1:
            raise ValueError('session_topic_required')
        return next(iter(paths), None)
    return next((path for path in paths if all(read_state(path)[field] == target[field]
                 for field in ('channel', 'conversation_id'))), None)


def upgrade_state(state):
    """Read earlier state files in place. The single-delivery format left nothing unread before its pending ID;
    the batch format that has notified kept only the newest ID, so its whole range after the cursor stays undelivered."""
    pending = state.get('pending')
    batch = 'notified' in state
    state['pending'] = [pending] if isinstance(pending, str) else [] if pending is None else pending
    state.setdefault('notified', state.get('cursor'))
    state.setdefault('delivered', state['pending'][-1] if state['pending'] and not batch else state.get('cursor'))


def read_state(path):
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError('Exchange state must be a local regular file')
    details = path.stat()
    if not stat.S_ISREG(details.st_mode) or details.st_size > STATE_MAX_BYTES:
        raise ValueError('Invalid exchange state file')
    state = json.loads(path.read_text(encoding='utf-8'))
    upgrade_state(state)
    validate_state(state)
    if path.name != 'exchange.json' and path.name != topic_name(state):
        raise ExchangeMismatch()
    if hashlib.sha256(state['session_id'].encode('ascii')).hexdigest() != path.parent.name:
        raise ExchangeMismatch()
    return state


def acquire_lock(path, operation):
    """Lock PATH, retrying when a finishing uninstall unlinked the inode this process waited on."""
    while True:
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            details = os.fstat(descriptor)
            if not stat.S_ISREG(details.st_mode) or details.st_size or details.st_nlink > 1:
                raise ValueError('Invalid lock destination')
            fcntl.flock(descriptor, operation)
            if os.path.exists(path) and os.path.samestat(os.stat(path), details):
                return descriptor
        except BaseException:
            os.close(descriptor)
            raise
        os.close(descriptor)


@contextlib.contextmanager
def locked_state(path):
    descriptor = acquire_lock(path.with_suffix('.lock'), fcntl.LOCK_EX)
    try:
        yield
    finally:
        os.close(descriptor)


@contextlib.contextmanager
def receiver_slot(directory):
    """Yield whether this process became the session's one receiver; the kernel frees it at exit."""
    try:
        descriptor = acquire_lock(directory / 'receiver.lock', fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        yield False
        return
    try:
        yield True
    finally:
        os.close(descriptor)


def write_state(path, state):
    text = json.dumps(state) + '\n'
    # A larger file would make read_state refuse the topic; refusing the write keeps the saved state usable.
    if len(text.encode('utf-8')) > STATE_MAX_BYTES:
        raise ValueError('session_state_full')
    descriptor, temporary = tempfile.mkstemp(prefix='.exchange-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
            output.write(text)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def write_state_text(path, text):
    descriptor, temporary = tempfile.mkstemp(prefix='.pointer-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='ascii') as output:
            output.write(text)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def make_room(directory, session_id):
    """At the topic limit, drop the oldest topic that is neither listening nor holding unconfirmed work.

    Such a state only remembers a position, as publish leaves one; live topics and pending work always stay."""
    states = [(read_state(path), path) for path in list_states(directory, session_id)]
    idle = sorted((state['deadline'], str(path)) for state, path in states
                  if state['deadline'] <= time.time() and not state['pending'])
    if len(states) < MAX_SESSION_TOPICS:
        return
    if not idle:
        raise ValueError('session_topic_limit')
    drop_idle(Path(idle[0][1]))


def require_idle(state):
    if state['deadline'] > time.time() or state['pending']:
        raise ValueError('session_topic_limit')


def drop_idle(path):
    """Check again under the topic's own lock, so a receive recording work there at that moment keeps it."""
    with (contextlib.nullcontext() if path.name == 'exchange.json' else locked_state(path)):
        require_idle(read_state(path))
        path.unlink()


def open_exchange(directory, session_id, state, reuse=False):
    validate_state(state)
    if state['session_id'] != session_id:
        raise ExchangeMismatch()
    path = session_directory(directory, session_id) / topic_name(state)
    if Path(directory).is_symlink() or path.parent.is_symlink():
        raise ValueError('Exchange directory must not be a symlink')
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with locked_state(path.parent / 'exchange.json'):
        existing_path = owned_state(directory, session_id, state)
        if existing_path is None:
            make_room(directory, session_id)
        if existing_path is not None:
            path = existing_path
        with (contextlib.nullcontext() if path.name == 'exchange.json' else locked_state(path)):
            if path.exists() or path.is_symlink():
                existing = read_state(path)
                if existing['deadline'] > time.time() and not reuse:
                    raise ExchangeAlreadyOpen('session_topic_already_live')
                state = {**state, 'cursor': existing['cursor'], 'notified': existing['notified'], 'delivered': existing['delivered'],
                         'pending': existing['pending'], 'deadline': max(state['deadline'], existing['deadline'])}
            write_state(path, state)
    return state


def close_exchange(directory, session_id, target):
    path = owned_state(directory, session_id, target)
    if path is None:
        raise ExchangeMismatch()
    with locked_state(path):
        state = read_state(path)
        if any(state[field] != target[field] for field in ('channel', 'conversation_id')):
            raise ExchangeMismatch()
        if state['pending']:
            raise ValueError('session_pending_ack_required')
        path.unlink()


def movable(state):
    """A listening topic is not reopened, and moving the position would hide unconfirmed work."""
    if state['deadline'] > time.time():
        raise ExchangeAlreadyOpen('session_topic_already_live')
    if state['pending']:
        raise ValueError('session_pending_ack_required')
    return state


def move_position(path, cursor):
    """An explicit open cursor replaces the position saved earlier, for example by publish."""
    with locked_state(path):
        state = movable(read_state(path))
        write_state(path, {**state, 'cursor': cursor, 'notified': cursor, 'delivered': cursor})


def remember_position(directory, session_id, target, cursor):
    """Keep where a send-only session stood so a later connect cannot miss the reply; opens no reception."""
    if owned_state(directory, session_id, target) is None:
        expired = {**new_state(session_id, {**target, 'cursor': cursor}, 1), 'deadline': time.time()}
        open_exchange(directory, session_id, expired, reuse=True)


def cursor_order(cursor):
    return tuple(map(int, cursor.split('-')))


def later_cursor(first, second):
    return max(first, second, key=cursor_order)


def with_pending(state, pending, delivered):
    """The confirmed position moves only when nothing delivered is left, and it never moves back."""
    cursor = later_cursor(state['cursor'], delivered) if not pending else state['cursor']
    return {**state, 'pending': pending, 'delivered': delivered, 'cursor': cursor,
            'notified': later_cursor(state['notified'], delivered)}


def existing_state(path):
    """A topic replaced while a read was in flight has no file left to record into."""
    if not path.exists():
        raise ValueError('session_topic_replaced')
    return read_state(path)


def settle_read(path, page_cursor, shown_ids):
    """Record a default read. IDs at or before the delivered position were handled while this read was in flight."""
    with locked_state(path):
        state = existing_state(path)
        fresh = [message_id for message_id in shown_ids if cursor_order(message_id) > cursor_order(state['delivered'])]
        pending = sorted({*state['pending'], *fresh}, key=cursor_order)
        updated = with_pending(state, pending, later_cursor(state['delivered'], page_cursor))
        write_state(path, updated)
        return updated


def confirm_processed(path, message_id):
    """Confirm one delivered message. Others stay pending; confirming one again changes nothing."""
    with locked_state(path):
        state = read_state(path)
        if message_id in state['pending']:
            updated = with_pending(state, [other for other in state['pending'] if other != message_id], state['delivered'])
            write_state(path, updated)
            return updated
        if cursor_order(message_id) > cursor_order(state['delivered']):
            raise ValueError('session_pending_message_mismatch')
        return state


def ack_exchange(directory, session_id, target, message_id):
    validate_cursor(message_id)
    path = owned_state(directory, session_id, target)
    if path is None:
        raise ExchangeMismatch()
    return confirm_processed(path, message_id)


def confirm_answered(directory, session_id, target, reply_to):
    """A reply finishes the one message it answers."""
    path = owned_state(directory, session_id, target)
    if path is not None and reply_to and reply_to in read_state(path)['pending']:
        confirm_processed(path, reply_to)


def redeliver_unconfirmed(directory, session_id):
    """A session that came back gets one new notice for what it had not confirmed before it stopped."""
    for path in list_states(directory, session_id):
        with locked_state(path):
            state = read_state(path)
            if state['notified'] != state['cursor']:
                write_state(path, {**state, 'notified': state['cursor']})


def mark_notified(path, cursor):
    """Record that CURSOR needs no further notice for this topic; a closed topic has nothing to record."""
    with locked_state(path):
        if path.exists():
            state = read_state(path)
            write_state(path, {**state, 'notified': later_cursor(state['notified'], cursor)})


def preceding_cursor(message_id):
    milliseconds, sequence = cursor_order(message_id)
    if sequence:
        return f'{milliseconds}-{sequence - 1}'
    return f'{max(milliseconds - 1, 0)}-{LARGEST_SEQUENCE if milliseconds else 0}'


async def retained_message(client, target, message_id):
    """Return the retained row with this ID, or None when retention or capacity removed it."""
    rows = (await read_page(client, target, preceding_cursor(message_id), 0, limit=1))['messages']
    return rows[0] if rows and rows[0].get('id') == message_id else None


async def reply_target(client, target, message):
    """Return the retained message this one answers, or None for a new or evicted target."""
    reply_to = message.get('reply_to')
    return await retained_message(client, target, reply_to) if reply_to else None


async def addressed_to(client, target, message, session_id):
    """A reply wakes only the author of its target; unknown or evicted targets still wake."""
    answered = await reply_target(client, target, message)
    return answered is None or answered.get('session_id') == session_id


async def next_notice(client, path, session_id, duration):
    """Long-poll one topic. Peer work is returned unrecorded so a failed notice stays due; a quiet page is recorded."""
    state = read_state(path)
    page = await read_page(client, state, state['notified'], duration)
    try:
        current = read_state(path)
    except FileNotFoundError:
        return None
    for message in page['messages']:
        validate_cursor(message['id'])
        unseen = cursor_order(message['id']) > cursor_order(current['notified'])
        if unseen and first_peer([message], session_id) and await addressed_to(client, state, message, session_id):
            return {'path': path, 'channel': state['channel'], 'conversation_id': state['conversation_id'], 'through': page['cursor']}
    mark_notified(path, page['cursor'])
    return None


async def watch_topics(client, paths, session_id, duration=WATCH_POLL):
    """Wait on every open topic at once so a quiet topic never delays another; return all topics now due."""
    tasks = {asyncio.create_task(next_notice(client, path, session_id, duration)) for path in paths}
    all_tasks = set(tasks)
    try:
        while tasks:
            done, tasks = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            notices = [notice for notice in [task.result() for task in done] if notice]
            if notices:
                return notices
        return []
    finally:
        for task in all_tasks:
            task.cancel()
        await asyncio.gather(*all_tasks, return_exceptions=True)
