"""Append-only reader verdicts; no task or answer text in the journal."""
from __future__ import annotations

import json
import os
import re

from . import config, events
from .scan import scan_bytes

NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$')


def name(value):
    if not isinstance(value, str) or not NAME.fullmatch(value):
        raise config.UsageError('job name must be 1-120 ASCII letters, digits, dots, underscores or hyphens, starting with a letter or digit')
    if scan_bytes(value.encode('utf-8')):
        raise config.UsageError('key-shaped job name refused')
    return value


def record(root, job, verdict, by, evidence, note=None):
    name(job)
    if verdict not in ('pass', 'fixes', 'reject'):
        raise config.UsageError('verdict must be pass, fixes or reject')
    for text in (by, evidence):
        if not isinstance(text, str) or not text.strip() or len(text) > 8000:
            raise config.UsageError('reader and evidence must be nonempty strings of at most 8000 characters')
    if note is not None and (not isinstance(note, str) or len(note) > 8000):
        raise config.UsageError('note must be at most 8000 characters')
    row = dict(job=job, verdict=verdict, by=by, evidence=evidence)
    if note is not None:
        row['note'] = note
    if len(json.dumps(row, ensure_ascii=True).encode('utf-8')) > events.MAX_EVENT_LINE_BYTES - 100:
        raise config.UsageError('verdict is too large for the bounded journal reader')
    fd = events.open_log(root, name='verdicts.jsonl')
    try:
        events.append(fd, row)
    finally:
        os.close(fd)


def rows(root, count=None):
    output = []
    for line in events.tail(root, count, name='verdicts.jsonl'):
        row = json.loads(line)
        if not isinstance(row, dict) or not isinstance(row.get('job'), str) or row.get('verdict') not in ('pass', 'fixes', 'reject'):
            raise events.EventError('invalid verdict record')
        output.append(row)
    return output
