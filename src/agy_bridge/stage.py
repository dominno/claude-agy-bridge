"""Select, check and stage inputs (plan section 4, steps 1-4 and 8).

prepare() reads every file once into memory, checks and scans that buffer, and
keeps it. materialize() later writes those same buffers. Nothing is copied by path.
"""
from __future__ import annotations

import contextlib
import fnmatch
import hashlib
import json
import os
import re
import shutil
import signal
import stat
import tempfile
import time
from dataclasses import dataclass, field
from typing import Dict, Iterator, List, Optional, Sequence, Tuple

from . import config, runner
from .config import UsageError
from .denylist import is_denied
from .scan import EXAMPLE_FRAGMENTS, first_hit_line, scan_bytes

TASK_NAME = "TASK.md"
RULE_LINE = "Read-only task: you may not write files, run commands or fetch URLs.\n"
_WALK_LIMIT = 100000
_DIRECTORY_LIMIT = 4096
_DEPTH_LIMIT = 64
_PREPARATION_SECONDS = 10


@dataclass(frozen=True)
class Refusal:
    rule: str  # R1, R5 or R6
    reason: str  # short plain-words reason; never file content
    where: str  # a relative path or a label such as "the task text"
    count: int = 1
    line: int = 0


class Refused(Exception):
    def __init__(self, refusals: Sequence[Refusal]):
        super().__init__("refused")
        self.refusals = list(refusals)


@dataclass
class Entry:
    rel: str
    data: bytes
    sha256: str


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


@dataclass
class Staging:
    path: str
    manifest: List[Tuple[str, int, str]]


def printable(text: str) -> str:
    """Escape names unambiguously, including controls and literal backslashes."""
    return json.dumps(text, ensure_ascii=True)[1:-1]


def describe(refusal: Refusal) -> str:
    where = ("[redacted key-shaped filename]" if scan_bytes(refusal.where.encode("utf-8", "replace"))
             else printable(refusal.where))
    if refusal.rule == "R5":
        return "Rule R5: %s contains %d key-shaped string(s) [%s]" % (where, refusal.count, refusal.reason)
    return "Rule %s: %s (%s)" % (refusal.rule, printable(refusal.reason), where)


# ---------- step 1: resolve ----------

def resolve_repo(path: str) -> str:
    root = os.path.realpath(path)
    if not os.path.isdir(root):
        raise UsageError("--repo is not a directory")
    return root


# Folders that are never an acceptable --repo, even with --allow-non-git-root. A "tree" entry also
# refuses everything inside it; an "exact" entry refuses only the folder itself (its children are fine).
SYSTEM_TREES = ("/etc", "/private", "/var", "/System", "/Library", "/root", "/proc", "/sys", "/boot", "/dev",
                "/bin", "/sbin", "/usr")
# Places inside those trees where people legitimately keep scratch projects (children only).
SYSTEM_ALLOWED_BELOW = ("/private/tmp", "/private/var/tmp", "/private/var/folders", "/tmp", "/var/tmp",
                        "/var/folders")
SYSTEM_EXACT = ("/var", "/private/var", "/Volumes", "/Users", "/home", "/opt", "/tmp", "/private/tmp", "/mnt", "/media")


def canonical_path(path: str) -> str:
    """Absolute path with symlinks resolved and, on macOS, the on-disk letter case (F_GETPATH)."""
    real = os.path.realpath(path)
    try:
        import fcntl
        getpath = getattr(fcntl, "F_GETPATH", None)
        if getpath is not None:
            fd = os.open(real, os.O_RDONLY)
            try:
                raw = fcntl.fcntl(fd, getpath, bytes(1024))
            finally:
                os.close(fd)
            canon = raw.split(b"\0", 1)[0].decode("utf-8", "surrogateescape")
            if canon:
                return canon
    except (OSError, ImportError):
        pass
    return real


def real_homes() -> List[str]:
    """Every home folder we know of: $HOME, ~, and the one in the password database (which an
    attacker-set $HOME cannot change)."""
    found = [os.path.expanduser("~")]
    if os.environ.get("HOME"):
        found.append(os.environ["HOME"])
    try:
        import pwd
        found.append(pwd.getpwuid(os.getuid()).pw_dir)
    except (ImportError, KeyError, OSError):
        pass
    out: List[str] = []
    for item in found:
        if item and os.path.isabs(item):
            canon = canonical_path(item)
            if canon not in out:
                out.append(canon)
    return out


def _same(a: str, b: str) -> bool:
    """Same device and inode, so letter case, links and '..' tricks cannot hide a folder."""
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def _chain(path: str) -> List[str]:
    """path, its parent, ... up to the filesystem root."""
    out = [path]
    while os.path.dirname(out[-1]) != out[-1]:
        out.append(os.path.dirname(out[-1]))
    return out


def _has_git_directory(root: str) -> bool:
    """A real .git directory, or a 'gitdir:' file naming a real directory inside the repo.

    A symlink, or a file pointing anywhere else, does not count.
    """
    path = os.path.join(root, ".git")
    try:
        st = os.lstat(path)
    except OSError:
        return False
    if stat.S_ISDIR(st.st_mode):
        return True
    if not (stat.S_ISREG(st.st_mode) and st.st_size < 4096):
        return False
    try:
        with open(path, "rb") as fh:
            text = fh.read(4096).decode("utf-8", "replace")
    except OSError:
        return False
    match = re.match(r"gitdir:\s*(.+?)\s*$", text.strip())
    if not match:
        return False
    target = os.path.realpath(os.path.join(root, match.group(1)))
    return os.path.isdir(target) and target.startswith(os.path.realpath(root).rstrip(os.sep) + os.sep)


def check_root(root: str, allow_non_git: bool = False) -> None:
    """R1/R6 for the --repo folder itself. No git program is run; '.git' is only a path marker. Raises Refused before anything is read.

    Folders are compared by device and inode, never as strings, and the path is reduced to its
    on-disk case before the denylist looks at it.
    """
    where = "the --repo folder"
    canon = canonical_path(root)
    chain = _chain(canon)
    if _same(canon, os.sep):
        raise Refused([Refusal("R6", "the repository folder is the filesystem root", where)])
    homes = real_homes()
    for home in homes:
        if _same(canon, home) or any(_same(canon, up) for up in _chain(home)):
            raise Refused([Refusal("R6", "the repository folder is your home folder or above it", where)])
    in_system_tree = any(_same(up, system) for system in SYSTEM_TREES for up in chain)
    in_scratch = any(_same(up, scratch) for scratch in SYSTEM_ALLOWED_BELOW for up in chain[1:])
    if (in_system_tree and not in_scratch) or any(_same(canon, system) for system in SYSTEM_EXACT):
        raise Refused([Refusal("R6", "the repository folder is a system folder", where)])
    if is_denied(canon):
        raise Refused([Refusal("R1", "the repository folder is, or is inside, a denylisted folder", where)])
    for home in homes:
        for child, parent in zip(chain, chain[1:]):
            first = os.path.basename(child)
            if _same(parent, home) and (first.startswith(".") or first.casefold() == "library"):
                raise Refused([Refusal("R1", "the repository folder is inside a hidden folder or ~/Library of "
                                             "your home folder (such as .config)", where)])
    if not allow_non_git and not _has_git_directory(canon):
        raise Refused([Refusal("R6", "the repository folder has no .git entry (a path check; --allow-non-git-root "
                                     "overrides it, but never for the cases above)", where)])


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
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
        fds.append(fd)
        st = os.fstat(fd)  # re-check on the opened object
        if not stat.S_ISREG(st.st_mode):
            raise Refused([Refusal("R6", "not a regular file (folder, pipe or device)", rel)])
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
        return b"".join(chunks)
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
            allow_fragment_file: Optional[str] = None, audit: Optional[Prepared] = None) -> Tuple[str, Prepared]:
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
                               max_bytes, extra_deny, deadline, allow_fragment_file, audit)
    except OSError:
        raise Refused([Refusal("R6", "repository folder changed during validation", "the --repo folder")])
    finally:
        os.close(root_fd)


def _prepare_pinned(root, root_fd, repo, files, globs, task, max_file_bytes,
                    max_bytes, extra_deny, deadline, allow_fragment_file=None, audit=None):
    try:
        task.encode("utf-8")
    except UnicodeEncodeError:
        raise UsageError("the task text is not valid UTF-8")
    roots = [root, os.path.abspath(repo)]
    refusals: List[Refusal] = []
    prepared = audit if audit is not None else Prepared()
    prepared.task = task
    allowed = load_allow_fragments(root_fd, roots, allow_fragment_file, extra_deny)

    if len(task) > config.MAX_TASK_CHARS:
        raise UsageError("task text is longer than %d characters" % config.MAX_TASK_CHARS)
    task_hits = scan_bytes(task.encode("utf-8"))
    if task_hits:
        refusals.append(Refusal("R5", ", ".join(sorted(task_hits)), "the task text", sum(task_hits.values())))

    ordered: List[str] = []
    folded: Dict[str, str] = {}

    def add(rel: str) -> None:
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

    for raw in files:
        rel, bad = _relative(raw, roots)
        if bad:
            prepared.requested.append(raw)
            refusals.append(bad)
        else:
            add(rel)
    for pattern in globs:
        found, skipped, bad = _expand_glob(root_fd, pattern, extra_deny, deadline, prepared.not_sent)
        prepared.skipped_by_denylist += skipped
        refusals.extend(bad)
        prepared.not_sent.extend(bad)
        if not found and not skipped and not bad:
            prepared.not_sent.append(Refusal("R6", "glob matched no sendable files", pattern))
        for rel in found:
            add(rel)
    if len(ordered) > config.MAX_FILES:
        raise UsageError("more than %d files selected" % config.MAX_FILES)

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
            data = _read_regular(root_fd, rel, max_file_bytes)
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
            prepared.entries.append(Entry(rel, data, hashlib.sha256(data).hexdigest()))
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


# ---------- step 4 and 8: stage and clean up ----------

def _write_new(base: str, rel: str, data: bytes) -> None:
    folder = base
    parts = rel.split("/")
    for name in parts[:-1]:
        folder = os.path.join(folder, name)
        try:
            os.mkdir(folder, 0o700)
        except FileExistsError:
            pass
    fd = os.open(os.path.join(folder, parts[-1]), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)  # O_EXCL alone refuses an existing symlink
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
    finally:
        os.close(fd)


def task_file_text(task: str, files: Optional[List[str]] = None) -> str:
    """The task, the read-only rule and, when given, the exact relative paths of the staged files.

    The list stops the model from hunting around the folder (measured on agy 1.4.3: with no list it tried paths such as
    `report1.md` and then paths outside the staging folder, which headless mode denied)."""
    listing = ""
    if files:
        listing = "Files in this folder (read exactly these paths, nothing else):\n%s\n\n" % "\n".join("- %s" % json.dumps(f, ensure_ascii=True) for f in files)
    return "# Task\n\n%s\n\n%s%s" % (task.strip(), listing, RULE_LINE)


def _chmod_no_follow(path: str) -> None:
    """chmod 0700 a real file or folder; never touch what a symlink points at."""
    if not stat.S_ISLNK(os.lstat(path).st_mode):
        os.chmod(path, 0o700)


def _cleanup(path: str) -> None:
    def fix(func, p, _exc):
        with contextlib.suppress(OSError):
            if p != path:  # never touch the folder that holds the one we are removing (TMPDIR)
                _chmod_no_follow(os.path.dirname(p))
            _chmod_no_follow(p)
            func(p)
    # A second Ctrl-C must not interrupt the removal; it is delivered afterwards.
    blocked = {signal.SIGINT, signal.SIGTERM, signal.SIGHUP}
    old = signal.pthread_sigmask(signal.SIG_BLOCK, blocked)
    try:
        shutil.rmtree(path, onerror=fix)
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, old)


# Exactly what tempfile.mkdtemp makes: our prefix plus 8 random characters.
_OWN_STAGING_NAME = re.compile(r"^agy-bridge-(?:ver-|doctor-)?[a-z0-9_]{8}$")


def sweep_old_staging(max_age_s: int = config.SWEEP_MIN_AGE_S) -> int:
    """Remove our own stale staging folders left by a crash or a SIGKILL. Returns how many.

    Only real folders owned by this user, with mode 0700 and exactly the name mkdtemp makes.
    """
    base = tempfile.gettempdir()
    removed = 0
    try:
        names = os.listdir(base)
    except OSError:
        return 0
    for name in names:
        if not _OWN_STAGING_NAME.match(name):
            continue
        full = os.path.join(base, name)
        try:
            st = os.lstat(full)
            if (stat.S_ISDIR(st.st_mode) and st.st_uid == os.getuid() and stat.S_IMODE(st.st_mode) == 0o700
                    and time.time() - st.st_mtime > max_age_s):
                _cleanup(full)
                removed += 1
        except OSError:
            continue
    return removed


@contextlib.contextmanager
def materialize(prepared: Prepared) -> Iterator[Staging]:
    """Create the 0700 staging folder, write the buffers, remove everything on exit."""
    path = tempfile.mkdtemp(prefix="agy-bridge-")
    try:
        manifest: List[Tuple[str, int, str]] = []
        for entry in prepared.entries:
            _write_new(path, entry.rel, entry.data)
            manifest.append((entry.rel, len(entry.data), entry.sha256))
        _write_new(path, TASK_NAME, task_file_text(prepared.task, [rel for rel, _size, _sha in manifest]).encode("utf-8"))
        yield Staging(path, manifest)
    finally:
        _cleanup(path)
