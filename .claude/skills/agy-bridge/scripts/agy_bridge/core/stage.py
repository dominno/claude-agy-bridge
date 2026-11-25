"""Select, check and stage inputs (plan section 4, steps 1-4 and 8).

prepare() reads every file once into memory, checks and scans that buffer, and
keeps it. materialize() later writes those same buffers. Nothing is copied by path.
"""
from __future__ import annotations

import fnmatch
import hashlib
import os
import stat
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from . import config
from .config import UsageError
from .denylist import is_denied
from .scan import EXAMPLE_FRAGMENTS, first_hit_line, scan_bytes
# The staging API other layers use. Each name lives in the module that owns it; patch it there.
from .refusal import Refusal, Refused  # noqa: F401
from .rootcheck import check_root, resolve_repo  # noqa: F401
from .staging import RULE_LINE, TASK_NAME, Staging, materialize, sweep_old_staging, task_file_text  # noqa: F401
from .text import (check_terminal_text, escape_for_terminal, escape_invisible, printable,  # noqa: F401
                   shell_arg)

_WALK_LIMIT = 100000
_DIRECTORY_LIMIT = 4096
_DEPTH_LIMIT = 64
_PREPARATION_SECONDS = 10


@dataclass
class Entry:
    rel: str
    data: bytes
    sha256: str
    executable: bool = False


@dataclass
class Prepared:
    entries: List[Entry] = field(default_factory=list)
    task: str = ""
    skipped_by_denylist: int = 0
    not_sent: List[Refusal] = field(default_factory=list)
    requested: List[str] = field(default_factory=list)

    @property
    def total_bytes(self) -> int:
        return sum(len(e.data) for e in self.entries)

    @property
    def file_count(self) -> int:
        return len(self.entries)


def describe(refusal: Refusal) -> str:
    where = ("[redacted key-shaped filename]" if scan_bytes(refusal.where.encode("utf-8", "replace"))
             else printable(refusal.where))
    if refusal.rule == "R5":
        return "Rule R5: %s contains %d key-shaped string(s) [%s]" % (where, refusal.count, refusal.reason)
    return "Rule %s: %s (%s)" % (refusal.rule, printable(refusal.reason), where)


def explain(exc: BaseException, operation: str) -> List[str]:
    """Plain lines naming why a command was refused: each rule, or the failing operation, path and reason.

    Paths go through printable() and key-shaped names are redacted, as in describe().
    """
    if isinstance(exc, Refused):
        return (["%s refused by a rule (%d problem(s)):" % (operation, len(exc.refusals))]
                + ["  - " + describe(item) for item in exc.refusals])
    if isinstance(exc, OSError):
        where = ""
        for name in (exc.filename, exc.filename2):
            if isinstance(name, bytes):
                name = os.fsdecode(name)
            if isinstance(name, str) and name:
                where += " " + ("[redacted key-shaped path]" if scan_bytes(name.encode("utf-8", "replace"))
                                else printable(name))
        reason = exc.strerror or type(exc).__name__
        return ["%s failed%s: %s" % (operation, (" at" + where) if where else "", printable(reason))]
    detail = str(exc)
    if not detail or scan_bytes(detail.encode("utf-8", "replace")):
        detail = type(exc).__name__
    return ["%s refused: %s" % (operation, printable(detail))]




def _relative(raw: str, roots: Sequence[str]) -> Tuple[Optional[str], Optional[Refusal]]:
    """Turn a user path into a clean relative path, or a refusal."""
    if "\0" in raw or not raw.strip():
        raise UsageError("empty or invalid path")
    if os.path.isabs(raw):
        norm = os.path.normpath(raw)
        for root in roots:
            if norm == root or norm.startswith(root.rstrip(os.sep) + os.sep):
                raw = os.path.relpath(norm, root)
                break
        else:
            return None, Refusal("R6", "path is outside the repository", raw)
    parts = [p for p in raw.split(os.sep) if p not in ("", ".")]
    if ".." in parts:
        return None, Refusal("R6", "path uses '..'", raw)
    if not parts:
        raise UsageError("path points at the repository root, not a file")
    return "/".join(parts), None


# ---------- step 2: select ----------

def _glob_match(pattern: List[str], parts: List[str]) -> bool:
    if not pattern:
        return not parts
    if pattern[0] == "**":
        return any(_glob_match(pattern[1:], parts[i:]) for i in range(len(parts) + 1))
    return bool(parts) and fnmatch.fnmatchcase(parts[0], pattern[0]) and _glob_match(pattern[1:], parts[1:])


def _check_deadline(deadline: float) -> None:
    if time.monotonic() > deadline:
        raise UsageError("input preparation exceeded its time limit; use narrower inputs")


def _prefix_possible(pattern: List[str], prefix: List[str]) -> bool:
    """Whether a pattern can match a file below this directory."""
    states = {0}
    for part in prefix:
        for i in range(len(pattern)):
            if i in states and pattern[i] == "**":
                states.add(i + 1)
        following = set()
        for i in states:
            if i < len(pattern):
                if pattern[i] == "**":
                    following.add(i)
                elif fnmatch.fnmatchcase(part, pattern[i]):
                    following.add(i + 1)
        states = following
    return any(i < len(pattern) for i in states)


def _expand_glob(root, pattern: str, extra: Sequence[str], deadline=None, not_sent=None) -> Tuple[List[str], int, List[Refusal]]:
    if os.path.isabs(pattern) or ".." in pattern.split("/") or "\0" in pattern:
        return [], 0, [Refusal("R6", "glob is absolute or uses '..'", pattern)]
    if len(pattern) > 200 or pattern.count("**") > 3:
        raise UsageError("glob pattern is too long or too complex")
    if deadline is None:
        deadline = time.monotonic() + _PREPARATION_SECONDS
    segs = [part for part in pattern.split("/") if part not in ("", ".")]
    found: List[str] = []
    skipped = seen = directories = 0

    def walk(dir_fd, prefix):
        nonlocal skipped, seen, directories
        _check_deadline(deadline)
        directories += 1
        if directories > _DIRECTORY_LIMIT or len(prefix) > _DEPTH_LIMIT:
            raise UsageError("glob walked too many directories or too deeply; use a narrower pattern")
        # Stream entries: never sort an unbounded directory listing.
        with os.scandir(dir_fd) as entries:
            for entry in entries:
                _check_deadline(deadline)
                seen += 1
                if seen > _WALK_LIMIT:
                    raise UsageError("glob walked too many entries; use a narrower pattern")
                parts = prefix + [entry.name]
                rel = "/".join(parts)
                if entry.is_dir(follow_symlinks=False):
                    if not _prefix_possible(segs, parts):
                        continue
                    if is_denied(rel, extra):
                        if not_sent is not None:
                            not_sent.append(Refusal("R1", "denylisted directory pruned; descendants not inspected", rel))
                        continue
                    child_fd = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                       dir_fd=dir_fd)
                    try:
                        walk(child_fd, parts)
                    finally:
                        os.close(child_fd)
                elif _glob_match(segs, parts):
                    if is_denied(rel, extra):
                        skipped += 1
                        if not_sent is not None:
                            not_sent.append(Refusal("R1", "path is on the denylist", rel))
                    else:
                        found.append(rel)
                        if len(found) > config.MAX_FILES:
                            raise UsageError("more than %d files selected" % config.MAX_FILES)
    fd = (os.open(".", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
          if isinstance(root, int) else os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW))
    try:
        walk(fd, [])
    except OSError:
        raise Refused([Refusal("R6", "glob directory could not be opened safely", "selected inputs")])
    finally:
        os.close(fd)
    return sorted(found), skipped, []


# ---------- step 3: read once, safely ----------

def _read_regular(root: str, rel: str, limit: int) -> bytes:
    return read_regular_file(root, rel, limit, verify_stable=False)[0]


def read_regular_file(root, rel: str, limit: int, *, verify_stable: bool = True) -> Tuple[bytes, int]:
    """Open component by component without following symlinks; read at most limit+1 bytes."""
    parts = rel.split("/")
    fds: List[int] = []
    try:
        dir_fd = os.dup(root) if isinstance(root, int) else os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        fds.append(dir_fd)
        for name in parts[:-1]:
            st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
            if stat.S_ISLNK(st.st_mode):
                raise Refused([Refusal("R6", "symlink in path", rel)])
            if not stat.S_ISDIR(st.st_mode):
                raise Refused([Refusal("R6", "not a regular file", rel)])
            dir_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dir_fd)
            fds.append(dir_fd)
        st = os.stat(parts[-1], dir_fd=dir_fd, follow_symlinks=False)
        if stat.S_ISLNK(st.st_mode):
            raise Refused([Refusal("R6", "symlink", rel)])
        if not stat.S_ISREG(st.st_mode):
            raise Refused([Refusal("R6", "not a regular file (folder, pipe or device)", rel)])
        before = st
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
        fds.append(fd)
        st = os.fstat(fd)  # re-check on the opened object
        if not stat.S_ISREG(st.st_mode):
            raise Refused([Refusal("R6", "not a regular file (folder, pipe or device)", rel)])
        if verify_stable and not os.path.samestat(before, st):
            raise Refused([Refusal("R6", "file changed while being opened", rel)])
        if st.st_nlink > 1:
            raise Refused([Refusal("R6", "file has several hard links", rel)])
        if st.st_size > limit:
            raise Refused([Refusal("R6", "file is over the size limit", rel)])
        chunks, total = [], 0
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise Refused([Refusal("R6", "file is over the size limit", rel)])
            chunks.append(chunk)
        if verify_stable:
            after = os.fstat(fd)
            if (st.st_size, st.st_mtime_ns, st.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise Refused([Refusal("R6", "file changed while being read", rel)])
        return b"".join(chunks), st.st_mode
    except FileNotFoundError:
        raise UsageError("file not found: %s" % printable(rel))
    except OSError:
        raise Refused([Refusal("R6", "could not be opened safely (symlink or permission)", rel)])
    finally:
        for fd in fds:
            os.close(fd)


def _is_binary(data: bytes) -> bool:
    if b"\0" in data:
        return True
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return True
    return False


def _check_text(rel: str, data: bytes, allow_fragments=()) -> List[Refusal]:
    if _is_binary(data):
        return [Refusal("R6", "binary file", rel)]
    hits = scan_bytes(data, allow_fragments) if allow_fragments else scan_bytes(data)
    if hits:
        return [Refusal("R5", "key-shaped string", rel, sum(hits.values()), first_hit_line(data, allow_fragments))]
    return []



def load_allow_fragments(root_fd, roots, path, extra_deny):
    if path is None:
        return ()
    rel, bad = _relative(path, roots)
    if bad:
        raise Refused([bad])
    if is_denied(rel, extra_deny):
        raise Refused([Refusal("R1", "allow-fragment file is on the denylist", rel)])
    try:
        name_hits = scan_bytes(rel.encode("utf-8"))
    except UnicodeEncodeError:
        raise UsageError("allow-fragment filename is not valid UTF-8")
    if name_hits:
        raise Refused([Refusal("R5", "key-shaped filename", "the allow-fragment filename", sum(name_hits.values()))])
    data = _read_regular(root_fd, rel, 16 * 1024)
    try:
        lines = data.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        raise UsageError("allow-fragment file must be UTF-8")
    if not lines:
        raise UsageError("allow-fragment file must contain at least one supported example")
    for line in lines:
        if len(line) < 12 or line not in EXAMPLE_FRAGMENTS:
            raise UsageError("allow-fragment file contains an unsupported example (minimum 12 characters)")
    return tuple(lines)


# ---------- prepare ----------

def prepare(repo: str, files: Sequence[str], globs: Sequence[str], task: str,
            max_file_bytes: int = config.MAX_FILE_BYTES, max_bytes: int = config.MAX_TOTAL_BYTES,
            extra_deny: Sequence[str] = (), allow_non_git: bool = False,
            allow_fragment_file: Optional[str] = None, audit: Optional[Prepared] = None,
            *, include_modes: bool = False) -> Tuple[str, Prepared]:
    deadline = time.monotonic() + _PREPARATION_SECONDS
    root = resolve_repo(repo)
    try:
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        raise Refused([Refusal("R6", "repository folder could not be opened safely", "the --repo folder")])
    try:
        identity = os.fstat(root_fd)
        check_root(root, allow_non_git)
        current = os.stat(root, follow_symlinks=False)
        if (identity.st_dev, identity.st_ino) != (current.st_dev, current.st_ino):
            raise Refused([Refusal("R6", "repository folder changed during validation", "the --repo folder")])
        return _prepare_pinned(root, root_fd, repo, files, globs, task, max_file_bytes,
                               max_bytes, extra_deny, deadline, allow_fragment_file, audit, include_modes)
    except OSError:
        raise Refused([Refusal("R6", "repository folder changed during validation", "the --repo folder")])
    finally:
        os.close(root_fd)



def _prepare_pinned(root, root_fd, repo, files, globs, task, max_file_bytes,
                    max_bytes, extra_deny, deadline, allow_fragment_file=None, audit=None, include_modes=False):
    try:
        task.encode("utf-8")
    except UnicodeEncodeError:
        raise UsageError("the task text is not valid UTF-8")
    roots = [root, os.path.abspath(repo)]
    refusals: List[Refusal] = []
    prepared = audit if audit is not None else Prepared()
    prepared.task = task
    allowed = load_allow_fragments(root_fd, roots, allow_fragment_file, extra_deny)
    _check_task(task, refusals)
    ordered = _select(root_fd, roots, files, globs, extra_deny, deadline, prepared, refusals)
    _read_selected(root_fd, ordered, max_file_bytes, max_bytes, extra_deny, deadline, allowed, include_modes,
                   prepared, refusals)
    if refusals:
        prepared.not_sent.extend(r for r in refusals if r.where in prepared.requested)
        raise Refused(refusals)
    if prepared.file_count == 0:
        raise UsageError("no input files matched; name files with --file or --glob")
    generated = task_file_text(task, [entry.rel for entry in prepared.entries]).encode("utf-8")
    hits = scan_bytes(generated)
    if hits:
        raise Refused([Refusal("R5", ", ".join(sorted(hits)), "the generated task", sum(hits.values()))])
    _check_deadline(deadline)
    return root, prepared


def _check_task(task: str, refusals: List[Refusal]) -> None:
    if len(task) > config.MAX_TASK_CHARS:
        raise UsageError("task text is longer than %d characters" % config.MAX_TASK_CHARS)
    task_hits = scan_bytes(task.encode("utf-8"))
    if task_hits:
        refusals.append(Refusal("R5", ", ".join(sorted(task_hits)), "the task text", sum(task_hits.values())))


def _select(root_fd, roots, files, globs, extra_deny, deadline, prepared: Prepared,
            refusals: List[Refusal]) -> List[str]:
    """Step 2: the --file paths, then each --glob, in order; returns the selected relative paths."""
    ordered: List[str] = []
    folded: Dict[str, str] = {}
    for raw in files:
        rel, bad = _relative(raw, roots)
        if bad:
            prepared.requested.append(raw)
            refusals.append(bad)
        else:
            _add(rel, prepared, refusals, ordered, folded, deadline)
    for pattern in globs:
        found, skipped, bad = _expand_glob(root_fd, pattern, extra_deny, deadline, prepared.not_sent)
        prepared.skipped_by_denylist += skipped
        refusals.extend(bad)
        prepared.not_sent.extend(bad)
        if not found and not skipped and not bad:
            prepared.not_sent.append(Refusal("R6", "glob matched no sendable files", pattern))
        for rel in found:
            _add(rel, prepared, refusals, ordered, folded, deadline)
    if len(ordered) > config.MAX_FILES:
        raise UsageError("more than %d files selected" % config.MAX_FILES)
    return ordered


def _add(rel: str, prepared: Prepared, refusals: List[Refusal], ordered: List[str], folded: Dict[str, str],
         deadline: float) -> None:
    if rel not in prepared.requested:
        prepared.requested.append(rel)
    _check_deadline(deadline)
    try:
        raw_name = rel.encode("utf-8")
    except UnicodeEncodeError:
        refusals.append(Refusal("R6", "path is not valid UTF-8", rel))
        return
    if any(not ch.isprintable() for ch in rel):
        refusals.append(Refusal("R6", "path contains control characters", rel))
        return
    hits = scan_bytes(raw_name)
    if hits:
        prepared.not_sent.append(Refusal("R5", "key-shaped filename", rel, sum(hits.values()), 1))
        refusals.append(Refusal("R5", ", ".join(sorted(hits)), "a selected filename", sum(hits.values())))
        return
    key = rel.casefold()
    if key in folded and folded[key] != rel:
        refusals.append(Refusal("R6", "two selected names differ only in letter case", rel))
    elif rel not in ordered:
        folded[key] = rel
        ordered.append(rel)
        if len(ordered) > config.MAX_FILES:
            raise UsageError("more than %d files selected" % config.MAX_FILES)


def _read_selected(root_fd, ordered, max_file_bytes, max_bytes, extra_deny, deadline, allowed, include_modes,
                   prepared: Prepared, refusals: List[Refusal]) -> None:
    """Step 3: read each selected file once, then check and scan that buffer."""
    total = 0
    for rel in ordered:
        _check_deadline(deadline)
        if rel.casefold() == TASK_NAME.casefold():
            refusals.append(Refusal("R6", "name is reserved by the bridge", rel))
            continue
        if is_denied(rel, extra_deny):
            refusals.append(Refusal("R1", "path is on the denylist", rel))
            continue
        try:
            if include_modes:
                data, mode = read_regular_file(root_fd, rel, max_file_bytes)
            else:
                data, mode = _read_regular(root_fd, rel, max_file_bytes), 0
        except Refused as exc:
            refusals.extend(exc.refusals)
            continue
        except UsageError:
            prepared.not_sent.extend(r for r in refusals if r.where in prepared.requested)
            prepared.not_sent.append(Refusal("R6", "file not found", rel))
            raise
        total += len(data)
        if total > max_bytes:  # stop before scanning more data than the limit allows
            refusals.append(Refusal("R6", "total input is over the size limit", rel))
            break
        bad = _check_text(rel, data, allowed)
        refusals.extend(bad)
        if not bad:
            prepared.entries.append(Entry(rel, data, hashlib.sha256(data).hexdigest(), bool(mode & stat.S_IXUSR)))
