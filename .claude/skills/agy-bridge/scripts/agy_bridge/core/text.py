"""Text safety: unambiguous names and invisible, terminal-control and bidi Unicode checks."""
from __future__ import annotations

import json
import re
import shlex
import unicodedata
from typing import Dict, List, Optional, Tuple

from .refusal import Refusal


def printable(text: str) -> str:
    """Escape names unambiguously, including controls and literal backslashes."""
    return json.dumps(text, ensure_ascii=True)[1:-1]


def shell_arg(path) -> str:
    """A path for a suggested shell command, shell-quoted so that shlex.split gives the path back.

    Printable text, including non-ASCII letters such as Polish diacritics, is quoted as is. A path with a control
    character, a lone surrogate or another non-printable character is escaped like printable() first, so a
    copy-pasted command cannot carry terminal escapes (it then names an escaped spelling, not the path)."""
    text = str(path)
    if text.isprintable():
        return shlex.quote(text)
    return shlex.quote(printable(text))


# Invisible Unicode that can hide instructions or change meaning unseen by a reviewer. `is_invisible` is true for
# every format (Cf), line/paragraph separator (Zl/Zp), private use (Co) and surrogate (Cs) code point, plus the
# explicit ranges below that are not in those categories or must stay cheap to match: tag characters, variation
# selectors, zero-width and format characters, combining grapheme joiner, Hangul fillers, Khmer inherent vowels,
# musical-symbol formatting and private use. The regex covers the explicit ranges; the category check runs only on
# non-ASCII characters the regex did not match. Shared by the implement checks (check_terminal_text,
# escape_for_terminal, escape_invisible) and report.clean_answer.
INVISIBLE_CLASS = ("\U000e0000-\U000e007f\ufe00-\ufe0f\U000e0100-\U000e01ef\u200b-\u200f\u2060-\u2064\ufeff"
                   "\u180e\u061c\u00ad\u034f\u115f\u1160\u3164\uffa0\u17b4\u17b5\U0001d173-\U0001d17a"
                   "\u2028\u2029\ue000-\uf8ff\U000f0000-\U000ffffd\U00100000-\U0010fffd")
INVISIBLE = re.compile("[" + INVISIBLE_CLASS + "]")
_INVISIBLE_CATEGORIES = frozenset(("Cf", "Zl", "Zp", "Co", "Cs"))
_NON_ASCII = re.compile("[^\x00-\x7f]")


def is_invisible(ch: str) -> bool:
    """True for one invisible or format code point (see INVISIBLE_CLASS)."""
    if ch < "\x80":
        return False
    return bool(INVISIBLE.match(ch)) or unicodedata.category(ch) in _INVISIBLE_CATEGORIES


def invisible_positions(text: str) -> List[int]:
    """Indexes of invisible characters in text (fast path for ASCII)."""
    if text.isascii():
        return []
    return [m.start() for m in _NON_ASCII.finditer(text) if is_invisible(m.group())]


def strip_invisible(text: str) -> Tuple[str, int]:
    """Remove invisible characters; return (text, number removed)."""
    if text.isascii():
        return text, 0
    removed = [0]

    def drop(match):
        if is_invisible(match.group()):
            removed[0] += 1
            return ""
        return match.group()
    return _NON_ASCII.sub(drop, text), removed[0]


# C0 controls other than tab/newline, a CR not followed by LF, DEL, C1 controls and bidi overrides/isolates; with
# is_invisible above they can rewrite what a terminal or reviewer sees in a patch, so model-written files may not
# add them (a BOM is allowed at offset 0 of a file).
_TERMINAL_UNSAFE = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069"
                              + INVISIBLE_CLASS + "]|\r(?!\n)")


def _unsafe_positions(line: str) -> List[int]:
    found = {m.start() for m in _TERMINAL_UNSAFE.finditer(line)}
    found.update(invisible_positions(line))
    return sorted(found)


def _without_bom(text: str) -> str:
    return text[1:] if text.startswith("\ufeff") else text


def _lines_with_newlines(text: str) -> List[str]:
    """Split on LF keeping it, so CR before LF stays distinguishable from a lone CR."""
    return re.findall("[^\n]*\n|[^\n]+$", text)


def check_terminal_text(rel: str, data: bytes, before: Optional[bytes] = None) -> List[Refusal]:
    """Refuse text that adds terminal control or bidi characters.

    With `before`, a line holding such characters is tolerated only when the identical line already exists in
    `before` (each original line covers one copy), so an existing escape cannot be swapped for a different one.
    """
    text = _without_bom(data.decode("utf-8", "replace"))
    if not _TERMINAL_UNSAFE.search(text) and not invisible_positions(text):
        return []
    known: Dict[str, int] = {}
    if before is not None:
        for old in _lines_with_newlines(_without_bom(before.decode("utf-8", "replace"))):
            if _unsafe_positions(old):
                known[old.rstrip("\n")] = known.get(old.rstrip("\n"), 0) + 1
    first = None
    point = 0
    count = 0
    offset = 0
    for line in _lines_with_newlines(text):
        found = _unsafe_positions(line)
        key = line.rstrip("\n")
        if found and known.get(key, 0) > 0:
            known[key] -= 1
        elif found:
            count += len(found)
            if first is None:
                first = len(re.findall("\r\n|\r|\n", text[:offset + found[0]])) + 1
                point = ord(line[found[0]])
        offset += len(line)
    if first is None:
        return []
    # Name the code point, never the raw character.
    return [Refusal("R6", "terminal control, bidi or invisible Unicode character U+%04X" % point, rel, count, first)]


def _invisible_marker(ch: str) -> str:
    return "\\u{%04x}" % ord(ch)


def escape_for_terminal(data: bytes) -> bytes:
    """Show non-printable and invisible characters as escapes; keep tab, newline and CR before LF."""
    out = []
    text = data.decode("utf-8", "backslashreplace")
    for index, ch in enumerate(text):
        if is_invisible(ch):
            out.append(_invisible_marker(ch))
        elif ch in "\t\n" or ch.isprintable() or (ch == "\r" and text[index + 1:index + 2] == "\n"):
            out.append(ch)
        else:
            out.append("\\x%02x" % ord(ch) if ord(ch) < 0x100 else "\\u%04x" % ord(ch))
    return "".join(out).encode("utf-8")


def escape_invisible(data: bytes) -> bytes:
    """Replace only invisible characters (is_invisible) with \\u{XXXX} markers; every other byte is kept.

    Invalid UTF-8 bytes decode to lone surrogates U+DC80-U+DCFF here; they are written back unchanged.
    """
    text = data.decode("utf-8", "surrogateescape")
    if text.isascii():
        return data

    def mark(match):
        ch = match.group()
        return ch if "\udc80" <= ch <= "\udcff" or not is_invisible(ch) else _invisible_marker(ch)
    return _NON_ASCII.sub(mark, text).encode("utf-8", "surrogateescape")
