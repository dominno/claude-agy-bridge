"""Verify source quotations, never the truth of model claims. No filesystem reads."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List

INSTRUCTION = ('\n\nAnswer only as lines of the form '
               'FILE: <path> | QUOTE: "<verbatim text, max 25 words>" | CLAIM: <text>. '
               'Use the exact relative paths in the supplied file list. '
               'Do not include headings, blank lines, or commentary.')


# Answer-format directives a task can prescribe; any of them means the task chose its own line format.
_DIRECTIVE = re.compile(r'(?<![A-Za-z])(QUOTE|AT|FILE):')

FORMAT_WARNING = ('WARN task text prescribes an answer format (%s); citation checking recognises only '
                  '`FILE: <path> | QUOTE: "<verbatim text>" | CLAIM: <text>` and '
                  '`QUOTE: <verbatim> AT: <path>:<line>` lines; every other line is UNVERIFIED '
                  '(malformed citation line). Leave the line format to the bridge or use one of these forms.')


def format_directives(task):
    """The distinct QUOTE:/AT:/FILE: directives in a task, in order of first use (empty: none)."""
    found = []
    for match in _DIRECTIVE.finditer(task or ''):
        token = match.group(1) + ':'
        if token not in found:
            found.append(token)
    return found


def format_warning(task):
    """The one-line warning for a task that prescribes QUOTE:/AT:/FILE: lines, or None."""
    found = format_directives(task)
    return FORMAT_WARNING % ', '.join(found) if found else None


def _parse_quote_at(line):
    """`QUOTE: <verbatim> AT: <path>[:<line>]` -> (path, quote, line number or None), linear time."""
    quote, marker, where = line[7:].rpartition(' AT: ')
    where = where.strip()
    if not marker or not where or '|' in where:
        return None
    path, colon, number = where.rpartition(':')
    if colon and number.isdigit() and path:
        where, number = path, int(number)
    else:
        number = None
    quote = quote.strip()
    if len(quote) >= 2 and quote[0] == quote[-1] and quote[0] in '"`':
        quote = quote[1:-1]
    return where, quote, number


def _parse_line(line):
    """Split fixed delimiters in linear time, even on hostile model output."""
    if line.startswith('QUOTE: '):
        return _parse_quote_at(line)
    if not line.startswith('FILE: '):
        return None
    path, marker, rest = line[6:].partition(' | QUOTE: "')
    if not marker or not path or '|' in path:
        return None
    quote, marker, claim = rest.rpartition('" | CLAIM: ')
    return (path, quote, None) if marker and claim else None


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


def _line_note(lines, quote, number):
    """An advisory note when a QUOTE/AT line names a line number the quote is not on; never unverifies."""
    if number is None:
        return ''
    if 1 <= number <= len(lines) and normalise(quote) in normalise(lines[number - 1]):
        return ''
    return '; not on line %d' % number


def check(answer, entries):
    # Use the scanned, staged buffers, never files reopened after the model ran.
    texts = {entry.rel: entry.data.decode('utf-8') for entry in entries}
    sources = {rel: normalise(text) for rel, text in texts.items()}
    split = {}
    checked = []
    lines = answer.splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    for line in lines:
        match = _parse_line(line)
        reason = 'malformed citation line'
        if match:
            path, quote, number = match
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
                if number is not None and path not in split:
                    split[path] = texts[path].splitlines()
                reason = 'quote exists' + _line_note(split.get(path), quote, number)
        checked.append(CheckedLine(line, reason.startswith('quote exists'), reason))
    return Check(checked)
