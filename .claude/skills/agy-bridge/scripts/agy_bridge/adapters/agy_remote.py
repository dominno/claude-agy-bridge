"""Drive one remote-agent turn through agy's interactive terminal UI (the only mode agy offers for remote agents).

agy 1.5.17 refuses --remote-agent with --print, stream-json and ACP, keeps no local copy of a remote conversation and
writes no answer to its log, so the screen is the only output. This module reads the program's terminal output into
a VT screen emulator, answers terminal queries, accepts the folder-trust prompt (the folder is a fresh empty one the
bridge made), pastes the prompt with bracketed paste once the input line is ready, presses Enter, and reads the reply
between the markers once the status line is idle again. It starts no program itself: adapters/agy.py spawns agy on a
pseudo-terminal through the process seam and stops it afterwards.
"""
from __future__ import annotations

import errno
import os
import select
import time
from typing import List, Optional, Tuple

from ..core import remote_prompt
from ..core.vt_screen import Screen

ROWS, COLS = 50, 1000  # wide, so citation lines are never wrapped by the program
TRUST = "trust the contents of this project"
BUSY = "esc to cancel"
IDLE = "? for shortcuts"
READY_LIMIT_S = 90.0      # launch until the input line is ready
SUBMIT_LIMIT_S = 45.0     # Enter until the program shows it is working
SETTLE_S = 1.0            # idle and complete for this long: done
QUIET_BEFORE_PASTE_S = 2.0  # the ready screen must stay unchanged this long (late warnings such as an unknown project)
GIVE_UP_IDLE_S = 5.0      # idle this long after working without a complete reply: stop
PASTE_START, PASTE_END = b"\x1b[200~", b"\x1b[201~"
# Screen texts that end a turn early, checked outside the echoed prompt and before the reply begins.
FAILURES: Tuple[Tuple[str, str], ...] = (
    ("auth", "not signed in"), ("auth", "select login method"), ("auth", "accounts.google.com"),
    ("auth", "not logged in"), ("auth", "please sign in"),
    ("cloud_disabled", "cloud conversations are not enabled"),
    ("model_unavailable", "available for remote conversations"),
    ("remote_unavailable", "remote agents are not available"), ("remote_unavailable", "remote agent is not available"),
    ("remote_unavailable", "not available for your account"),
    ("project_mismatch", "project not found"), ("project_mismatch", "default remote project"),
    ("environment_limit", "running environments"), ("send_failed", "failed to send message"),
)
# Checked only when a turn ends without a reply, because these words can appear in ordinary text.
LATE_FAILURES: Tuple[Tuple[str, str], ...] = (
    ("quota", "quota"), ("quota", "rate limit"), ("quota", "resource_exhausted"), ("quota", "resource exhausted"),
)


TOOL_MARK = "\u25cf "  # the program prefixes a tool call it ran with a filled circle, e.g. "● Ran (uname -a)"


def tool_lines(lines: List[str]) -> int:
    return sum(1 for line in lines if line.lstrip().startswith(TOOL_MARK))


def classify(lines: List[str], patterns=FAILURES) -> Optional[str]:
    """The first failure pattern found in lines (the caller passes only the program's own lines)."""
    for line in lines:
        low = line.lower()
        for kind, needle in patterns:
            if needle in low:
                return kind
    return None


class Turn:
    """The state of one turn: the screen, the bytes still to send and the phase reached."""

    def __init__(self, prompt: str, markers, max_out: int, clock=time.monotonic, on_paste=None):
        self.prompt, self.markers, self.max_out, self.clock = prompt, markers, max_out, clock
        self.on_paste = on_paste
        self.screen = Screen(ROWS, COLS)
        self.outbox = bytearray()
        self.phase = "start"
        self.trusted = self.busy_seen = False
        self.started = clock()
        self.phase_at = self.started
        self.idle_since: Optional[float] = None
        self.last_output = self.started
        self.answer: Optional[str] = None

    def received(self, data: bytes) -> None:
        self.screen.feed(data)
        self.last_output = self.clock()
        self.outbox += self.screen.pending_replies()

    def _visible(self) -> str:
        return "\n".join(self.screen.screen_lines())

    def tools_used(self) -> int:
        return tool_lines(self._status_lines(self.screen.lines()))

    def _status_lines(self, lines: List[str]) -> List[str]:
        """The program's own lines: after the echoed prompt (whose last lines name the marker stem) and before the
        reply, so file contents, the task and the answer never count as a failure message."""
        start = 0
        for index in range(len(lines) - 1, -1, -1):
            text = lines[index].strip()
            if self.markers.stem in text and text not in (self.markers.begin, self.markers.end):
                start = index + 1
                break
        tail = lines[start:]
        for index, line in enumerate(tail):
            if line.strip() == self.markers.begin:
                return tail[:index]
        return tail

    def step(self) -> Optional[str]:
        """Advance after new output or a tick; returns 'done' or a failure kind when the turn is over."""
        now = self.clock()
        lines = self.screen.lines()
        failure = classify(self._status_lines(lines))
        if failure:
            return failure
        visible = self._visible()
        if self.phase == "start":
            return self._start(visible, now)
        if self.phase == "pasting":
            if not self.outbox and now - self.phase_at >= 0.5:
                self.outbox += b"\r"
                self._enter("submitted", now)
            return None
        return self._follow(lines, visible, now)

    def _enter(self, phase: str, now: float) -> None:
        self.phase, self.phase_at = phase, now

    def _start(self, visible: str, now: float) -> Optional[str]:
        if TRUST in visible:
            if not self.trusted:
                self.outbox += b"\r"  # "Yes, I trust this folder": the bridge's own empty folder
                self.trusted = True
        elif IDLE in visible and BUSY not in visible and now - self.last_output >= QUIET_BEFORE_PASTE_S:
            if self.on_paste is not None:
                self.on_paste()  # from here on the prompt (files included) counts as sent
            self.outbox += PASTE_START + self.prompt.encode("utf-8") + PASTE_END
            self._enter("pasting", now)
            return None
        return "not_ready" if now - self.started > READY_LIMIT_S else None

    def _follow(self, lines: List[str], visible: str, now: float) -> Optional[str]:
        busy = BUSY in visible
        begun = remote_prompt.begun(lines, self.markers)
        self.busy_seen = self.busy_seen or busy or begun
        if not self.busy_seen:
            return "not_submitted" if now - self.phase_at > SUBMIT_LIMIT_S else None
        idle = not busy and IDLE in visible
        if not idle:
            self.idle_since = None
            return None
        self.idle_since = self.idle_since if self.idle_since is not None else now
        self.answer = remote_prompt.extract_answer(lines, self.markers)
        if self.answer is not None and len(self.answer.encode("utf-8")) > self.max_out:
            return "output_limit"
        if self.answer is not None and now - self.idle_since >= SETTLE_S:
            return "done"
        if now - self.idle_since >= GIVE_UP_IDLE_S:
            return self.ended_without_reply(lines)
        return None

    def ended_without_reply(self, lines: List[str]) -> str:
        """Why the turn ended with no complete reply: a late failure text, a reply cut short, or none at all."""
        if self.answer is None:
            self.answer = remote_prompt.extract_answer(lines, self.markers)
        if self.answer is not None:
            return "output_limit" if len(self.answer.encode("utf-8")) > self.max_out else "done"
        late = classify(self._status_lines(lines), LATE_FAILURES)
        if late:
            return late
        return "no_end_marker" if remote_prompt.begun(lines, self.markers) else "no_answer"


def _read(master: int) -> Optional[bytes]:
    """The next chunk; b"" once the terminal is closed, None on a spurious wakeup."""
    try:
        return os.read(master, 65536)
    except BlockingIOError:
        return None
    except OSError as exc:
        if exc.errno in (errno.EIO, errno.EBADF):
            return b""
        raise


def _write(master: int, turn: Turn) -> None:
    try:
        sent = os.write(master, bytes(turn.outbox[:4096]))
    except BlockingIOError:
        return
    del turn.outbox[:sent]


def drive(master: int, proc, turn: Turn, deadline: float) -> str:
    """Run the turn until it is done, fails, the program exits or the deadline passes; returns the outcome."""
    os.set_blocking(master, False)
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return "timeout"
        writers = [master] if turn.outbox else []
        readable, writable, _ = select.select([master], writers, [], min(remaining, 0.1))
        if readable:
            data = _read(master)
            if data == b"":
                return _exited(turn)
            if data:
                turn.received(data)
        if writable and turn.outbox:
            _write(master, turn)
        outcome = turn.step()
        if outcome is not None:
            return outcome
        if proc.poll() is not None and not readable:
            return _exited(turn)


def _exited(turn: Turn) -> str:
    outcome = turn.ended_without_reply(turn.screen.lines())
    return "exited" if outcome == "no_answer" else outcome
