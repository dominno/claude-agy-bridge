"""A small, pure VT100/xterm screen emulator with scrollback, for reading a full-screen terminal program's output.

A terminal program draws its text with cursor movement, erases and redraws, so stripping escape sequences from the
byte stream loses or scrambles text. Feeding the bytes here rebuilds what a terminal would show: the visible rows plus
every row that scrolled off the top (the scrollback), so a long session that scrolls is not lost. Rows that the
emulator wrapped because they reached the right margin are joined again in lines().

The emulator answers the queries that a terminal must answer (cursor position, device attributes, mode reports);
the caller writes pending_replies() back to the program. No I/O happens here.
"""
from __future__ import annotations

import codecs
import re
import unicodedata
from typing import List

_TOKEN = re.compile(
    r"\x1b\[([?>=<]?)([0-9;:]*)([ -/]*)([@-~])"     # CSI: private marker, parameters, intermediates, final byte
    r"|\x1b\]([^\x07\x1b]*)(?:\x07|\x1b\\)"         # OSC, ended by BEL or ST
    r"|\x1b[P^_X][^\x1b]*\x1b\\"                     # DCS, PM, APC, SOS strings
    r"|\x1b[()*+#%\-./ ][\x20-\x7e]"                   # charset designation and other two-byte-argument escapes
    r"|\x1b(?![\[\]P^_X])[\x30-\x7e]"                # other two-byte escapes
    r"|[\x00-\x1a\x1c-\x1f\x7f]"                     # C0 controls and DEL
    r"|[^\x00-\x1f\x7f]+")                           # printable text
# What can still grow into one of the escapes above once more bytes arrive.
_PARTIAL = re.compile(r"\x1b(?:\[[?>=<]?[0-9;:]*[ -/]*|\][^\x07\x1b]*\x1b?|[P^_X][^\x1b]*\x1b?|[()*+#%\-./ ])?\Z")
_MAX_PENDING = 4096
_ALT_MODES = (47, 1047, 1049)


class Screen:
    """rows x cols cells; feed() program output; lines() gives scrollback plus screen."""

    def __init__(self, rows: int, cols: int, history_limit: int = 50000):
        if rows < 2 or cols < 2:
            raise ValueError("a screen needs at least 2 rows and 2 columns")
        self.rows, self.cols, self.history_limit = rows, cols, history_limit
        self.history: List[str] = []
        self.history_wrapped: List[bool] = []
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._pending = ""
        self._replies: List[bytes] = []
        self._saved_main = None
        self.alternate = False
        self._clear()

    def _clear(self) -> None:
        self.grid = [self._blank() for _ in range(self.rows)]
        self.wrapped = [False] * self.rows
        self.y = self.x = 0
        self.top, self.bottom = 0, self.rows - 1
        self.saved = (0, 0)
        self._wrap_next = False

    def _blank(self) -> List[str]:
        return [" "] * self.cols

    # Input

    def feed(self, data: bytes) -> None:
        text = self._pending + self._decoder.decode(data)
        self._pending = ""
        pos, end = 0, len(text)
        while pos < end:
            match = _TOKEN.match(text, pos)
            if match is None:
                rest = text[pos:]
                if len(rest) <= _MAX_PENDING and _PARTIAL.match(rest):
                    self._pending = rest  # an incomplete escape waits for the next chunk
                    return
                pos += 1  # a malformed or endless escape: drop its ESC and go on
                continue
            self._token(match)
            pos = match.end()

    def _token(self, match) -> None:
        token = match.group(0)
        if match.group(4) is not None:
            self._csi(match.group(1), match.group(2), match.group(3), match.group(4))
        elif match.group(5) is not None:
            self._osc(match.group(5))
        elif token[0] == "\x1b":
            self._escape(token)
        elif len(token) == 1 and (token < " " or token == "\x7f"):
            self._control(token)
        else:
            self._text(token)

    # Text and controls

    def _text(self, text: str) -> None:
        for ch in text:
            width = _width(ch)
            if width == 0:
                self._combine(ch)
                continue
            if self._wrap_next or self.x + width > self.cols:
                self.wrapped[self.y] = True
                self._carriage_return()
                self._line_feed()
            row = self.grid[self.y]
            row[self.x] = ch
            if width == 2:
                row[self.x + 1] = ""
            self.x += width
            if self.x >= self.cols:
                self.x = self.cols - 1
                self._wrap_next = True

    def _combine(self, ch: str) -> None:
        x = self.x - 1 if self.x > 0 and not self._wrap_next else self.x
        row = self.grid[self.y]
        while x > 0 and row[x] == "":
            x -= 1
        row[x] += ch

    def _control(self, ch: str) -> None:
        if ch == "\r":
            self._carriage_return()
        elif ch in "\n\x0b\x0c":
            self._line_feed()
        elif ch == "\b":
            self._wrap_next = False
            self.x = max(0, self.x - 1)
        elif ch == "\t":
            self.x = min(self.cols - 1, (self.x // 8 + 1) * 8)

    def _carriage_return(self) -> None:
        self.x = 0
        self._wrap_next = False

    def _line_feed(self) -> None:
        self._wrap_next = False
        if self.y == self.bottom:
            self._scroll_up(1)
        elif self.y < self.rows - 1:
            self.y += 1

    def _reverse_index(self) -> None:
        if self.y == self.top:
            self._scroll_down(1)
        elif self.y > 0:
            self.y -= 1

    def _scroll_up(self, count: int) -> None:
        for _ in range(min(count, self.bottom - self.top + 1)):
            row, wrapped = self.grid.pop(self.top), self.wrapped.pop(self.top)
            if self.top == 0 and not self.alternate:
                self._keep(row, wrapped)
            self.grid.insert(self.bottom, self._blank())
            self.wrapped.insert(self.bottom, False)

    def _scroll_down(self, count: int) -> None:
        for _ in range(min(count, self.bottom - self.top + 1)):
            self.grid.pop(self.bottom)
            self.wrapped.pop(self.bottom)
            self.grid.insert(self.top, self._blank())
            self.wrapped.insert(self.top, False)

    def _keep(self, row: List[str], wrapped: bool) -> None:
        self.history.append("".join(row).rstrip())
        self.history_wrapped.append(wrapped)
        if len(self.history) > self.history_limit:
            del self.history[0], self.history_wrapped[0]

    # Escapes

    def _escape(self, token: str) -> None:
        kind = token[1:2]
        if kind == "7":
            self.saved = (self.y, self.x)
        elif kind == "8":
            self.y, self.x = self.saved
            self._wrap_next = False
        elif kind == "M":
            self._reverse_index()
        elif kind == "D":
            self._line_feed()
        elif kind == "E":
            self._carriage_return()
            self._line_feed()
        elif kind == "c":
            self._clear()

    def _osc(self, body: str) -> None:
        if body == "11;?":  # background colour query: answer black, like a default dark terminal
            self._replies.append(b"\x1b]11;rgb:0000/0000/0000\x1b\\")

    def _csi(self, private: str, params: str, intermediate: str, final: str) -> None:
        numbers = [int(p) if p.isdigit() else 0 for p in params.replace(":", ";").split(";")] if params else []
        if private or intermediate:
            self._private_csi(private, numbers, intermediate, final)
            return
        first = numbers[0] if numbers else 0
        count = first or 1
        handler = _CSI.get(final)
        if handler is not None:
            handler(self, numbers, first, count)

    def _private_csi(self, private: str, numbers: List[int], intermediate: str, final: str) -> None:
        if private == "?" and final in "hl" and not intermediate:
            for mode in numbers:
                if mode in _ALT_MODES:
                    self._switch_screen(final == "h", save_cursor=mode == 1049)
        elif private == "?" and intermediate == "$" and final == "p" and numbers:
            self._replies.append(b"\x1b[?%d;0$y" % numbers[0])  # mode report: not recognised
        elif private == ">" and final == "c":
            self._replies.append(b"\x1b[>0;0;0c")

    def _switch_screen(self, to_alternate: bool, save_cursor: bool) -> None:
        if to_alternate == self.alternate:
            return
        if to_alternate:
            self._saved_main = (self.grid, self.wrapped, self.y, self.x, self.top, self.bottom, self.saved)
            cursor = (self.y, self.x)
            self._clear()
            if not save_cursor:
                self.y, self.x = cursor
            self.alternate = True
        else:
            self.alternate = False
            if self._saved_main is not None:
                self.grid, self.wrapped, y, x, self.top, self.bottom, self.saved = self._saved_main
                if save_cursor:
                    self.y, self.x = y, x
            self._saved_main = None

    # Output

    def pending_replies(self) -> bytes:
        """The answers to terminal queries since the last call, to write back to the program."""
        out, self._replies = b"".join(self._replies), []
        return out

    def screen_lines(self) -> List[str]:
        """The visible rows, right-trimmed (no scrollback, no rejoining)."""
        return ["".join(row).rstrip() for row in self.grid]

    def lines(self) -> List[str]:
        """Scrollback plus the main screen, with rows the emulator wrapped joined back into one line."""
        if self.alternate and self._saved_main is not None:
            grid, wrapped = self._saved_main[0], self._saved_main[1]
        else:
            grid, wrapped = self.grid, self.wrapped
        rows = self.history + ["".join(row).rstrip() for row in grid]
        flags = self.history_wrapped + list(wrapped)
        out: List[str] = []
        joining = False
        for row, flag in zip(rows, flags):
            if joining:
                out[-1] += row
            else:
                out.append(row)
            joining = flag
        while out and not out[-1]:
            out.pop()
        return out

    def text(self) -> str:
        return "\n".join(self.lines())


def _width(ch: str) -> int:
    if unicodedata.combining(ch) or unicodedata.category(ch) in ("Mn", "Me", "Cf"):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in "WF" else 1


# CSI handlers: (screen, all parameters, first parameter (0 when absent), count (first or 1)).

def _cursor_up(s: Screen, _n, _a, count: int) -> None:
    s.y = max(s.top if s.y >= s.top else 0, s.y - count)
    s._wrap_next = False


def _cursor_down(s: Screen, _n, _a, count: int) -> None:
    s.y = min(s.bottom if s.y <= s.bottom else s.rows - 1, s.y + count)
    s._wrap_next = False


def _cursor_forward(s: Screen, _n, _a, count: int) -> None:
    s.x = min(s.cols - 1, s.x + count)
    s._wrap_next = False


def _cursor_back(s: Screen, _n, _a, count: int) -> None:
    s.x = max(0, s.x - count)
    s._wrap_next = False


def _next_line(s: Screen, _n, _a, count: int) -> None:
    s.x, s.y = 0, min(s.rows - 1, s.y + count)
    s._wrap_next = False


def _previous_line(s: Screen, _n, _a, count: int) -> None:
    s.x, s.y = 0, max(0, s.y - count)
    s._wrap_next = False


def _column(s: Screen, _n, _a, count: int) -> None:
    s.x = min(s.cols - 1, count - 1)
    s._wrap_next = False


def _row(s: Screen, _n, _a, count: int) -> None:
    s.y = min(s.rows - 1, count - 1)
    s._wrap_next = False


def _position(s: Screen, numbers, _a, _c) -> None:
    row = numbers[0] if numbers and numbers[0] else 1
    col = numbers[1] if len(numbers) > 1 and numbers[1] else 1
    s.y, s.x = min(s.rows - 1, row - 1), min(s.cols - 1, col - 1)
    s._wrap_next = False


def _erase_display(s: Screen, _n, mode: int, _c) -> None:
    if mode == 0:
        _erase_line(s, [], 0, 1)
        for r in range(s.y + 1, s.rows):
            s.grid[r], s.wrapped[r] = s._blank(), False
    elif mode == 1:
        for r in range(s.y):
            s.grid[r], s.wrapped[r] = s._blank(), False
        _erase_line(s, [], 1, 1)
    elif mode == 2:
        s.grid, s.wrapped = [s._blank() for _ in range(s.rows)], [False] * s.rows
    # mode 3 (erase scrollback) is ignored on purpose: the scrollback is the record of the session.


def _erase_line(s: Screen, _n, mode: int, _c) -> None:
    row = s.grid[s.y]
    if mode == 0:
        row[s.x:] = [" "] * (s.cols - s.x)
        s.wrapped[s.y] = False
    elif mode == 1:
        row[:s.x + 1] = [" "] * (s.x + 1)
    else:
        s.grid[s.y], s.wrapped[s.y] = s._blank(), False


def _insert_lines(s: Screen, _n, _a, count: int) -> None:
    if s.top <= s.y <= s.bottom:
        for _ in range(min(count, s.bottom - s.y + 1)):
            s.grid.pop(s.bottom)
            s.wrapped.pop(s.bottom)
            s.grid.insert(s.y, s._blank())
            s.wrapped.insert(s.y, False)


def _delete_lines(s: Screen, _n, _a, count: int) -> None:
    if s.top <= s.y <= s.bottom:
        for _ in range(min(count, s.bottom - s.y + 1)):
            s.grid.pop(s.y)
            s.wrapped.pop(s.y)
            s.grid.insert(s.bottom, s._blank())
            s.wrapped.insert(s.bottom, False)


def _delete_chars(s: Screen, _n, _a, count: int) -> None:
    row = s.grid[s.y]
    del row[s.x:s.x + count]
    row.extend([" "] * (s.cols - len(row)))


def _insert_chars(s: Screen, _n, _a, count: int) -> None:
    row = s.grid[s.y]
    row[s.x:s.x] = [" "] * count
    del row[s.cols:]


def _erase_chars(s: Screen, _n, _a, count: int) -> None:
    s.grid[s.y][s.x:s.x + count] = [" "] * min(count, s.cols - s.x)


def _scroll_region(s: Screen, numbers, _a, _c) -> None:
    top = (numbers[0] - 1) if numbers and numbers[0] else 0
    bottom = (numbers[1] - 1) if len(numbers) > 1 and numbers[1] else s.rows - 1
    if 0 <= top < bottom < s.rows:
        s.top, s.bottom = top, bottom
        s.y = s.x = 0
        s._wrap_next = False


def _save(s: Screen, _n, _a, _c) -> None:
    s.saved = (s.y, s.x)


def _restore(s: Screen, _n, _a, _c) -> None:
    s.y, s.x = s.saved
    s._wrap_next = False


def _report(s: Screen, _n, mode: int, _c) -> None:
    if mode == 6:
        s._replies.append(b"\x1b[%d;%dR" % (s.y + 1, s.x + 1))
    elif mode == 5:
        s._replies.append(b"\x1b[0n")


def _attributes(s: Screen, _n, mode: int, _c) -> None:
    if mode == 0:
        s._replies.append(b"\x1b[?62;22c")


_CSI = {
    "A": _cursor_up, "B": _cursor_down, "C": _cursor_forward, "D": _cursor_back, "a": _cursor_forward,
    "e": _cursor_down, "E": _next_line, "F": _previous_line, "G": _column, "`": _column, "d": _row,
    "H": _position, "f": _position, "J": _erase_display, "K": _erase_line, "L": _insert_lines,
    "M": _delete_lines, "P": _delete_chars, "@": _insert_chars, "X": _erase_chars,
    "S": lambda s, _n, _a, count: s._scroll_up(count), "T": lambda s, _n, _a, count: s._scroll_down(count),
    "r": _scroll_region, "s": _save, "u": _restore, "n": _report, "c": _attributes,
}


def render(data: bytes, rows: int = 50, cols: int = 200) -> str:
    """Convenience for tests and diagnostics: the text a fresh screen shows after data."""
    screen = Screen(rows, cols)
    screen.feed(data)
    return screen.text()

