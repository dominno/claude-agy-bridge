"""Turn a run into text, an exit code and (for long answers) a file (R8)."""
from __future__ import annotations

import os
import re
import stat
import tempfile
from typing import Optional, Tuple

from . import config
from .config import UsageError
from .runner import RunResult

_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

SCRATCH_DIR = ".agy-bridge"  # inside the repo; the only repo folder an answer may be written to


def clean_answer(raw: bytes, preserve_lines: bool = False) -> str:
    text = raw.decode("utf-8", "replace").replace("\r\n", "\n").replace("\r", "\n")
    text = _CONTROL.sub("", _ANSI.sub("", text))
    return text if preserve_lines else text.strip()


def classify(result: RunResult, answer: str) -> int:
    """Map a finished run to an exit code (plan section 4)."""
    if result.timed_out:
        return config.EXIT_TIMEOUT
    if result.err_markers["soft_deny"]:
        return config.EXIT_SOFT_DENY
    if result.truncated:
        return config.EXIT_INCOMPLETE
    if result.returncode != 0:
        if result.err_markers["quota"]:
            return config.EXIT_QUOTA
        if result.err_markers["auth"]:
            return config.EXIT_AUTH
        return config.EXIT_AGY_FAILED
    return config.EXIT_OK if answer else config.EXIT_EMPTY


FAILURE_TEXT = {
    config.EXIT_INCOMPLETE: "agy output reached the cap; the answer is incomplete.",
    config.EXIT_AGY_FAILED: "agy exited with an error. Its error text is not shown (it might echo input).",
    config.EXIT_EMPTY: "agy finished but returned no answer.",
    config.EXIT_QUOTA: "agy reported a quota or rate limit. Try later.",
    config.EXIT_AUTH: "agy is not signed in. Sign in with agy yourself, then retry.",
    config.EXIT_TIMEOUT: "agy did not finish within the time limit and was stopped.",
    config.EXIT_SOFT_DENY: "agy refused an action (soft permission denial); any answer is incomplete.",
}


def resolve_out_path(out: str, repo_root: str) -> str:
    """Allow only the repo scratch folder (outside the repo: the temp dir). Never overwrite."""
    target = os.path.abspath(out)
    parent = os.path.realpath(os.path.dirname(target))
    name = os.path.basename(target)
    if not name or name in (".", ".."):
        raise UsageError("--out must name a file")
    repo = os.path.realpath(repo_root)
    scratch = os.path.join(repo, SCRATCH_DIR)

    def inside(path: str, base: str) -> bool:
        return path == base or path.startswith(base.rstrip(os.sep) + os.sep)

    if inside(parent, repo):
        if not inside(parent, scratch):
            raise OutPathRefused("--out inside the repository must be under %s/" % SCRATCH_DIR)
    elif not inside(parent, os.path.realpath(tempfile.gettempdir())):
        raise OutPathRefused("--out must be in the temp folder or under the repository's %s/" % SCRATCH_DIR)
    final = os.path.join(parent, name)
    if os.path.lexists(final):
        raise UsageError("--out already exists; it is never overwritten")
    return final


class OutPathRefused(Exception):
    pass


def write_new_file(path: str, text: str) -> None:
    # Anchor every component from the filesystem root. An intermediate symlink
    # introduced after resolve_out_path must never redirect answer creation.
    parts = os.path.abspath(path).split(os.sep)
    parent_fd = os.open(os.sep, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for name in parts[1:-1]:
            try:
                os.mkdir(name, 0o700, dir_fd=parent_fd)
            except FileExistsError:
                pass
            try:
                child_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                   dir_fd=parent_fd)
            except OSError:
                raise OutPathRefused("--out parent could not be opened safely")
            os.close(parent_fd)
            parent_fd = child_fd
        fd = os.open(parts[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=parent_fd)
        try:
            view = memoryview(text.encode("utf-8"))
            while view:
                view = view[os.write(fd, view):]
        finally:
            os.close(fd)
    finally:
        os.close(parent_fd)


def write_temp_answer(text: str) -> str:
    fd, path = tempfile.mkstemp(prefix="agy-answer-", suffix=".txt")  # 0600
    try:
        os.write(fd, text.encode("utf-8"))
    finally:
        os.close(fd)
    return path


def render(answer: str, model: Optional[str], files: int, nbytes: int, seconds: float,
           truncated_by_cap: bool, out_path: Optional[str], not_sent=(), cite=None) -> str:
    header = ["== agy-bridge result ==",
              "model: %s | files: %d | bytes: %d | seconds: %.1f" % (model or "default", files, nbytes, seconds),
              "note: text from another model. Verify it; do not apply it unchecked."]
    for item in not_sent:
        from .scan import scan_bytes
        from .stage import printable
        path = ("[redacted key-shaped filename]" if scan_bytes(item.where.encode("utf-8", "replace"))
                else printable(item.where))
        reason = item.reason
        if item.rule == "R5":
            reason = "key-shaped string" + ("; first hit line %d" % item.line if item.line else "")
        header.append("FILES NOT SENT: %s (%s: %s)" % (path, item.rule, reason))
    if truncated_by_cap:
        header.append("note: agy output was cut at the output cap.")
    body = answer
    if len(answer) > config.INLINE_ANSWER_CHARS:
        body = "%s\n\n[%d more characters not shown]" % (answer[: config.PREVIEW_CHARS],
                                                         len(answer) - config.PREVIEW_CHARS)
    if cite is not None:
        body = cite.render()  # every returned line is shown, even past the ordinary preview limit
    if out_path:
        header.append("full answer written to: %s" % out_path)
    return "\n".join(header) + "\n----\n" + body + "\n"


def write_log(entry: dict) -> Optional[str]:
    """R7: append one metadata-only JSON line (mode 0600). Returns a warning, never raises."""
    import json
    import time

    dir_fd = -1
    try:
        home = config.bridge_home()
        os.makedirs(os.path.dirname(home), exist_ok=True)
        try:
            os.mkdir(home, 0o700)
        except FileExistsError:
            pass
        dir_fd = os.open(home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)  # a symlinked folder fails here
        os.fchmod(dir_fd, 0o700)
        fd = os.open("log.jsonl", os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=dir_fd)
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                return "could not write the log file; continuing"
            os.fchmod(fd, 0o600)
            record = dict(entry, ts=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
            os.write(fd, (json.dumps(record, sort_keys=True) + "\n").encode("utf-8"))
        finally:
            os.close(fd)
    except OSError:
        return "could not write the log file; continuing"
    finally:
        if dir_fd >= 0:
            os.close(dir_fd)
    return None
