"""Limits, the allowed environment, and the repo-local rule file."""
from __future__ import annotations

import json
import os
import re
import stat
import tempfile
from typing import List, Optional

# R2: the only environment variable names agy ever receives.
ALLOWED_ENV_NAMES = ("HOME", "PATH", "LANG", "TERM", "TMPDIR")

# Exit codes (plan section 4).
EXIT_OK = 0
EXIT_USAGE = 1
EXIT_AGY_FAILED = 2
EXIT_EMPTY = 3
EXIT_REFUSED = 4
EXIT_QUOTA = 10
EXIT_AUTH = 11
EXIT_TIMEOUT = 12
EXIT_AGY_MISSING = 13
EXIT_UNSUPPORTED = 14
EXIT_SOFT_DENY = 15
EXIT_INCOMPLETE = 16
EXIT_UNVERIFIED = 17

MAX_FILE_BYTES = 200 * 1024
MAX_TOTAL_BYTES = 1024 * 1024
MAX_OUT_BYTES = 1024 * 1024
MAX_FILES = 500
MAX_TASK_CHARS = 20000
DEFAULT_TIMEOUT_S = 300
MAX_TIMEOUT_S = 3600
# A stale-folder sweep must never remove a live run: older than the longest run plus two hours of margin.
SWEEP_MIN_AGE_S = MAX_TIMEOUT_S + 2 * 3600
INLINE_ANSWER_CHARS = 8000
PREVIEW_CHARS = 2000

REPO_CONFIG_NAME = ".agy-bridge.json"
_REPO_CONFIG_MAX_BYTES = 64 * 1024


class UsageError(Exception):
    """Bad arguments or unreadable input; maps to exit code 1."""


# The test suite creates this marker file while it runs and sets AGY_BRIDGE_TEST_MODE=1 for the
# commands it starts. Without both, AGY_BRIDGE_HOME is ignored, so a poisoned environment variable
# cannot redirect the log in real use (a leftover marker alone is harmless).
TEST_MARKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_test_mode")


def bridge_home() -> str:
    override = os.environ.get("AGY_BRIDGE_HOME")
    if override and os.path.exists(TEST_MARKER) and os.environ.get("AGY_BRIDGE_TEST_MODE") == "1":
        return override
    return os.path.join(os.path.expanduser("~"), ".agy-bridge")


def safe_path(value: Optional[str], repo_root: Optional[str] = None) -> str:
    """Canonical absolute PATH entries, excluding the repository and its aliases."""
    root = os.path.realpath(repo_root) if repo_root else None
    entries = []
    for entry in (value or "").split(os.pathsep):
        if not entry or not os.path.isabs(entry):
            continue
        real = os.path.realpath(entry)
        if root and (real == root or real.startswith(root.rstrip(os.sep) + os.sep)):
            continue
        if real not in entries:
            entries.append(real)
    return os.pathsep.join(entries) if entries else "/usr/bin:/bin"


def build_agy_env(repo_root: Optional[str] = None) -> dict:
    """The exact environment handed to agy: five names, nothing inherited."""
    env = {
        "HOME": os.environ.get("HOME") or os.path.expanduser("~"),
        "PATH": safe_path(os.environ.get("PATH"), repo_root),
        "LANG": os.environ.get("LANG") or "en_US.UTF-8",
        "TERM": os.environ.get("TERM") or "xterm-256color",
        "TMPDIR": os.environ.get("TMPDIR") or tempfile.gettempdir(),
    }
    assert set(env) == set(ALLOWED_ENV_NAMES)
    return env


_DURATION = re.compile(r"^(\d+)\s*([smh]?)$")


def parse_duration(text: str) -> int:
    match = _DURATION.match(text.strip().lower())
    if not match:
        raise UsageError("timeout must look like 90, 90s, 5m or 1h")
    seconds = int(match.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600}[match.group(2)]
    if seconds < 1 or seconds > MAX_TIMEOUT_S:
        raise UsageError("timeout must be between 1 second and 1 hour")
    return seconds


def load_repo_config(repo_root: str) -> dict:
    """Read extra denylist globs from <repo>/.agy-bridge.json. It can only add rules."""
    path = os.path.join(repo_root, REPO_CONFIG_NAME)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return {}
    except OSError:
        raise UsageError("%s exists but cannot be opened safely (symlink?)" % REPO_CONFIG_NAME)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise UsageError('repository config must be a single-link regular file')
        raw = os.read(fd, _REPO_CONFIG_MAX_BYTES + 1)
    finally:
        os.close(fd)
    if len(raw) > _REPO_CONFIG_MAX_BYTES:
        raise UsageError("%s is larger than 64 KB" % REPO_CONFIG_NAME)
    try:
        data = json.loads(raw.decode("utf-8"))
    except ValueError:
        raise UsageError("%s is not valid JSON" % REPO_CONFIG_NAME)
    if not isinstance(data, dict) or set(data) - {"deny", "context_files"}:
        raise UsageError('%s may only contain deny and context_files: it can add rules, never remove them'
                         % REPO_CONFIG_NAME)
    deny = data.get("deny", [])
    if not isinstance(deny, list) or not all(isinstance(p, str) and p.strip() for p in deny):
        raise UsageError('"deny" in %s must be a list of non-empty strings' % REPO_CONFIG_NAME)
    context = data.get("context_files", [])
    if not isinstance(context, list) or not all(isinstance(p, str) and p.strip() for p in context):
        raise UsageError('context_files must be a list of nonempty paths')
    data['deny'] = [p.strip() for p in deny]
    return data


def load_extra_deny(repo_root: str) -> List[str]:
    return load_repo_config(repo_root).get('deny', [])


def validate_model(name: Optional[str]) -> Optional[str]:
    if name is None:
        return None
    if not isinstance(name, str) or not re.match(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,63}$", name):
        raise UsageError("model name has characters that are not allowed")
    return name
