"""The remote-agent prompt (staged files inlined between delimiters) and reading the answer back off the screen.

A remote agent runs in a cloud environment and cannot see local files, so the staged buffers travel inside the
prompt. The terminal program echoes the prompt and renders the reply as Markdown (which drops `__`, `**`, backticks
and the like), so the model is asked to put its answer in a fenced code block, which the program shows verbatim, and
to frame it with two markers that never appear literally in the prompt: the prompt names each marker as two halves. The
answer is the text between the last begin marker line and the first end marker line after it. Pure: no I/O.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import List, Optional, Sequence

from .config import UsageError
from .staging import RULE_LINE

_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_INTRO = ("You are answering a read-only task. The complete contents of %d file(s) are included below, each between "
          "a BEGIN FILE and an END FILE delimiter line that carry the tag %s. These files are your only source.\n"
          "Rules: use no tools of any kind: no shell commands, no file reads or searches, no web or URL access, no "
          "code execution and no edits. Do not read any other files, repositories, documents or uploads, including "
          "any attached to this project. Answer only from the text below.\n")
_FRAME = ("\nStart your reply with a line containing only the text formed by joining %s and %s with nothing between "
          "them. Then put your whole answer inside one fenced code block (a line of three backticks before it and "
          "one after it) so that it is shown verbatim. End your reply with a line containing only the text formed "
          "by joining %s and %s. Put nothing before the first of those lines or after the last.\n")


@dataclass(frozen=True)
class Markers:
    """The reply frame. `stem` + `begin_word` is the begin marker; it never occurs in the prompt itself."""
    stem: str
    begin_word: str = "BEGIN"
    end_word: str = "END"

    @property
    def begin(self) -> str:
        return self.stem + self.begin_word

    @property
    def end(self) -> str:
        return self.stem + self.end_word


def new_markers() -> Markers:
    return Markers("".join(secrets.choice(_ALPHABET) for _ in range(10)))


def _file_block(rel: str, data: bytes, tag: str) -> str:
    text = data.decode("utf-8")
    if text.startswith("﻿"):
        text = text[1:]
    text = text.replace("\r\n", "\n")
    if text and not text.endswith("\n"):
        text += "\n"
    name = '"%s"' % rel.replace("\\", "\\\\").replace('"', '\\"')
    return "----- BEGIN FILE %s [%s] -----\n%s----- END FILE %s [%s] -----\n" % (name, tag, text, name, tag)


def build_prompt(task: str, entries: Sequence, markers: Markers) -> str:
    """The whole prompt: rules, every staged file (Entry.rel, Entry.data) inlined, the task, then the reply frame.

    The delimiter tag is the marker stem, which a file cannot guess, so file text cannot fake a delimiter."""
    tag = markers.stem
    for entry in entries:
        if ("[%s]" % tag) in entry.data.decode("utf-8") or markers.begin in entry.data.decode("utf-8"):
            raise UsageError("a staged file happens to contain the random delimiter; retry")
    parts = [_INTRO % (len(entries), tag), "\n"]
    parts.extend(_file_block(entry.rel, entry.data, tag) + "\n" for entry in entries)
    parts.append("Task:\n%s\n\n%s" % (task.strip(), RULE_LINE))
    parts.append(_FRAME % (markers.stem, markers.begin_word, markers.stem, markers.end_word))
    prompt = "".join(parts)
    if markers.begin in prompt or markers.end in prompt:
        raise UsageError("the task text contains the random reply marker; retry")
    return prompt


def _dedent(lines: List[str]) -> List[str]:
    indents = [len(line) - len(line.lstrip(" ")) for line in lines if line.strip()]
    cut = min(indents) if indents else 0
    return [line[cut:] for line in lines]


def _trim(body: List[str]) -> List[str]:
    """Drop blank lines at both ends, and a fence line at either end if the program left one on screen."""
    while body and not body[0].strip():
        body.pop(0)
    while body and not body[-1].strip():
        body.pop()
    if body and body[0].strip().startswith("```"):
        return _trim(body[1:])
    if body and body[-1].strip() == "```":
        return _trim(body[:-1])
    return body


def extract_answer(lines: Sequence[str], markers: Markers) -> Optional[str]:
    """The reply between the last begin marker line and the first end marker line after it; None when either
    is missing. The terminal's left margin and any surrounding fence are removed."""
    start = None
    for index in range(len(lines) - 1, -1, -1):
        if lines[index].strip() == markers.begin:
            start = index
            break
    if start is None:
        return None
    for index in range(start + 1, len(lines)):
        if lines[index].strip() == markers.end:
            body = _dedent(_trim(list(lines[start + 1:index])))
            return "\n".join(line.rstrip() for line in body) + ("\n" if body else "")
    return None


def begun(lines: Sequence[str], markers: Markers) -> bool:
    """True once the begin marker is on screen (the reply has started)."""
    return any(line.strip() == markers.begin for line in lines)
