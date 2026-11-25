"""Verify source quotations, never the truth of model claims. No filesystem reads."""
from __future__ import annotations

from dataclasses import dataclass
from typing import List

INSTRUCTION = ('\n\nAnswer only as lines of the form '
               'FILE: <path> | QUOTE: "<verbatim text, max 25 words>" | CLAIM: <text>. '
               'Use the exact relative paths in the supplied file list. '
               'Do not include headings, blank lines, or commentary.')


def _parse_line(line):
    """Split fixed delimiters in linear time, even on hostile model output."""
    if not line.startswith('FILE: '):
        return None
    path, marker, rest = line[6:].partition(' | QUOTE: "')
    if not marker or not path or '|' in path:
        return None
    quote, marker, claim = rest.rpartition('" | CLAIM: ')
    return (path, quote, claim) if marker and claim else None


@dataclass(frozen=True)
class CheckedLine:
    text: str
    verified: bool
    reason: str


@dataclass
class Check:
    lines: List[CheckedLine]

    @property
    def verified(self):
        return sum(line.verified for line in self.lines)

    @property
    def unverified(self):
        return len(self.lines) - self.verified

    def render(self):
        heading = ('Citation check: %d VERIFIED, %d UNVERIFIED. '
                   'VERIFIED means only that the quote exists; CLAIM is not verified.'
                   % (self.verified, self.unverified))
        rows = ['%s (%s): %s' % ('VERIFIED' if ln.verified else 'UNVERIFIED', ln.reason, ln.text)
                for ln in self.lines]
        return heading + '\n' + '\n'.join(rows)


def normalise(text):
    return ' '.join(text.split())


def check(answer, entries):
    # Use the scanned, staged buffers, never files reopened after the model ran.
    sources = {entry.rel: normalise(entry.data.decode('utf-8')) for entry in entries}
    checked = []
    lines = answer.splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    for line in lines:
        match = _parse_line(line)
        reason = 'malformed citation line'
        if match:
            path, quote, _claim = match
            if path not in sources:
                reason = 'file not sent'
            elif not quote.strip():
                reason = 'empty quote'
            elif len(normalise(quote)) < 8:
                reason = 'too short'
            elif len(quote.split()) > 25:
                reason = 'quote exceeds 25 words'
            elif normalise(quote) not in sources[path]:
                reason = 'quote not found'
            else:
                reason = 'quote exists'
        checked.append(CheckedLine(line, reason == 'quote exists', reason))
    return Check(checked)
