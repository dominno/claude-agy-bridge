"""R1/R6 for the --repo folder itself: system folders, home folders and the .git marker."""
from __future__ import annotations

import os
import re
import stat
from typing import List, Sequence

from .config import UsageError
from .denylist import is_denied
from .refusal import Refusal, Refused
from .text import printable


def resolve_repo(path: str) -> str:
    root = os.path.realpath(path)
    if not os.path.isdir(root):
        raise UsageError("--repo is not a directory: " + printable(path))
    return root



# Folders that are never an acceptable --repo, even with --allow-non-git-root. A "tree" entry also
# refuses everything inside it; an "exact" entry refuses only the folder itself (its children are fine).
SYSTEM_TREES = ("/etc", "/private", "/var", "/System", "/Library", "/root", "/proc", "/sys", "/boot", "/dev",
                "/bin", "/sbin", "/usr")
# Places inside those trees where people legitimately keep scratch projects (children only).
SYSTEM_ALLOWED_BELOW = ("/private/tmp", "/private/var/tmp", "/private/var/folders", "/tmp", "/var/tmp",
                        "/var/folders")
SYSTEM_EXACT = ("/var", "/private/var", "/Volumes", "/Users", "/home", "/opt", "/tmp", "/private/tmp", "/mnt", "/media")
# A home folder at one of these never lifts the system-tree refusal for its descendants (HOME=/usr or HOME=/etc).
# /root is not listed: uid 0 in a container keeps projects under /root.
HOMES_NOT_TRUSTED = ("/", "/etc", "/private", "/private/etc", "/var", "/private/var", "/System", "/Library", "/proc",
                     "/sys", "/boot", "/dev", "/bin", "/sbin", "/usr")


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


def database_home() -> List[str]:
    """The home folder from the password database only, canonical; [] when there is none.

    Only this home may lift a refusal: $HOME and ~ are set by the caller, so they may add refusals but never
    remove one."""
    try:
        import pwd
        home = pwd.getpwuid(os.getuid()).pw_dir
    except (ImportError, KeyError, OSError):
        return []
    return [canonical_path(home)] if home and os.path.isabs(home) else []


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


def _below_home(chain: List[str], homes: Sequence[str]) -> bool:
    """True when the folder is a strict descendant of a home folder (such as /root/proj for uid 0) and not inside
    a hidden top-level child of it. A home at a system root (HOMES_NOT_TRUSTED) never counts."""
    for home in homes:
        if any(_same(home, system) for system in HOMES_NOT_TRUSTED):
            continue
        for child, parent in zip(chain, chain[1:]):
            if _same(parent, home):
                return not os.path.basename(child).startswith(".")
    return False


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
    remedy = "; move the checkout under $HOME or a temporary folder"
    exact = next((system for system in SYSTEM_EXACT if _same(canon, system)), None)
    if exact:
        raise Refused([Refusal("R6", "the repository folder is system folder %s itself%s" % (exact, remedy), where)])
    trees = [system for up in chain for system in SYSTEM_TREES if _same(up, system)]
    in_scratch = any(_same(up, scratch) for scratch in SYSTEM_ALLOWED_BELOW for up in chain[1:])
    # Only the password-database home may lift this refusal; an env-only HOME (HOME=/usr/share) never does.
    if trees and not in_scratch and not _below_home(chain, database_home()):
        raise Refused([Refusal("R6", "the repository folder is inside system folder %s%s" % (trees[0], remedy),
                               where)])
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
