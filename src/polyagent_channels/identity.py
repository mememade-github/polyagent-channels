"""Validate asserted session routing identifiers and display senders."""
import re


def validate_session_id(session_id):
    if not isinstance(session_id, str) or not re.fullmatch(r'[A-Za-z0-9._:-]{1,256}', session_id):
        raise ValueError('session_id must use 1..256 ASCII letters, digits, . _ : or -')


def validate_sender(sender):
    if not isinstance(sender, str) or not sender.strip() or len(sender.encode('utf-8')) > 256:
        raise ValueError('sender must be nonempty and <=256 UTF-8 bytes')
