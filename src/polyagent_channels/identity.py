"""Shared identifier formats, asserted session routing identifiers and display senders.

The service and the client installer both use these formats, so each rule is written once here; callers keep their
own error messages, which belong to their interfaces."""
import re

IDENTIFIER_PATTERN = '[A-Za-z0-9_-]{1,128}'
CURSOR_PATTERN = '[0-9]{1,20}-[0-9]{1,20}'
SESSION_ID_PATTERN = '[A-Za-z0-9._:-]{1,256}'
CURSOR_PART_MAX = 2**64 - 1


def is_identifier(value):
    """A channel or conversation name."""
    return isinstance(value, str) and re.fullmatch(IDENTIFIER_PATTERN, value) is not None


def is_cursor_form(value):
    """A stream ID written as milliseconds-sequence."""
    return isinstance(value, str) and re.fullmatch(CURSOR_PATTERN, value) is not None


def within_cursor_range(value):
    """Both parts of a stream ID written in cursor form fit an unsigned 64-bit number."""
    return all(int(part) <= CURSOR_PART_MAX for part in value.split('-'))


def is_session_id(value):
    return isinstance(value, str) and re.fullmatch(SESSION_ID_PATTERN, value) is not None


def validate_session_id(session_id):
    if not is_session_id(session_id):
        raise ValueError('session_id must use 1..256 ASCII letters, digits, . _ : or -')


def validate_sender(sender):
    if not isinstance(sender, str) or not sender.strip() or len(sender.encode('utf-8')) > 256:
        raise ValueError('sender must be nonempty and <=256 UTF-8 bytes')
