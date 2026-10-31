"""Run agy: version detection, the one argv builder, and a guarded pty run (R2, R3, R4)."""
from __future__ import annotations

import errno
import os
import pty
import re
import select
import shutil
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from . import config, sandbox
from typing import Dict, List, Optional, Tuple

PROMPT = ("Read TASK.md in the current directory and do what it says. Use only the files in this "
          "directory. You may not write files, run commands or fetch URLs. Reply with plain text.")


class AgyMissing(Exception):
    pass


class UnsupportedAgy(Exception):
    pass


class AgyNotRun(UnsupportedAgy):
    """agy was found but could not be started, so no program ran."""


@dataclass(frozen=True)
class ArgvSpec:
    print_flag: str
    model_flag: Optional[str]
    fixed: Tuple[str, ...]


# EVERY entry is a guess until someone runs the real CLI. See docs/COMPAT.md ("unverified").
# Keyed by agy major version. Nothing here may grant write, shell or network permission.
ARGV_SPECS: Dict[int, ArgvSpec] = {
    1: ArgvSpec(print_flag="--print", model_flag="--model", fixed=()),
}

# R3: substrings that must never appear in any argv token.
FORBIDDEN_FRAGMENTS = ("skip-permission", "yolo", "dangerous", "bypass", "auto-approve", "allow-")


def parse_version(text: str) -> Tuple[int, int, int]:
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", text)
    if not match:
        raise UnsupportedAgy("could not read a version number from 'agy --version'")
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def find_agy(keep_out_of: Optional[str] = None) -> str:
    """Look agy up on the cleaned PATH; refuse one that lives inside the repository."""
    exe = shutil.which("agy", path=config.safe_path(os.environ.get("PATH"), keep_out_of))
    if not exe:
        raise AgyMissing("agy was not found on PATH" +
                         (" outside the repository; entries inside the repository are excluded" if keep_out_of else ""))
    exe = os.path.abspath(exe)
    if keep_out_of:
        root = os.path.realpath(keep_out_of)
        real = os.path.realpath(exe)
        if real == root or real.startswith(root.rstrip(os.sep) + os.sep):
            raise AgyMissing("agy resolves inside the repository; refused")
    return exe


def run_probe(argv, env, cwd, timeout):
    """Short agy probe with bounded execution and reaping, without pipe drains."""
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        proc = subprocess.Popen(argv, env=env, cwd=cwd, stdin=subprocess.DEVNULL,
                                stdout=out, stderr=err, start_new_session=True)
        try:
            proc.wait(timeout=timeout)
        finally:
            _kill_group(proc.pid, signal.SIGKILL)
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                raise UnsupportedAgy('agy probe did not exit after SIGKILL; abandoned child after 1s cleanup limit') from None
        out.seek(0)
        err.seek(0)
        return subprocess.CompletedProcess(argv, proc.returncode, out.read(), err.read())


def detect_version(exe: str, env: Dict[str, str]) -> Tuple[int, int, int]:
    """Run 'agy --version' with the same five-name environment, in an empty folder."""
    try:
        with tempfile.TemporaryDirectory(prefix="agy-bridge-ver-") as empty:
            proc = run_probe([exe, "--version"], env, empty, timeout=20)
    except OSError:
        raise AgyNotRun("'agy --version' could not be started")
    except subprocess.TimeoutExpired:
        raise UnsupportedAgy("'agy --version' did not finish in time")
    return parse_version((proc.stdout + proc.stderr).decode("utf-8", "replace"))


def build_argv(version: Tuple[int, int, int], exe: str, model: Optional[str]) -> List[str]:
    """The only place an agy command line is made. Unknown versions fail closed."""
    spec = ARGV_SPECS.get(version[0])
    if spec is None:
        raise UnsupportedAgy("agy %d.%d.%d is not a version this bridge knows" % version)
    argv = [exe, spec.print_flag, PROMPT]
    if model:
        if not spec.model_flag:
            raise UnsupportedAgy("this agy version has no known model flag")
        argv += [spec.model_flag, model]
    argv += list(spec.fixed)
    for token in argv[1:]:
        if any(frag in token.lower() for frag in FORBIDDEN_FRAGMENTS):
            raise UnsupportedAgy("refusing to build a command line with a permission-widening flag")
    return argv


@dataclass
class RunResult:
    returncode: Optional[int]
    out: bytes
    err_bytes: int
    err_markers: Dict[str, bool]
    timed_out: bool
    truncated: bool
    seconds: float


_MARKERS = {
    "quota": ("quota", "rate limit", "rate-limit", "resource_exhausted", "429"),
    "auth": ("unauthenticated", "not logged in", "login required", "please log in", "sign in", "401"),
    "soft_deny": ("auto-denied", "permission denied by"),
}


def _markers(err: bytes) -> Dict[str, bool]:
    low = err.decode("utf-8", "replace").lower()
    return {name: any(w in low for w in words) for name, words in _MARKERS.items()}


def _kill_group(pid: int, sig: int) -> None:
    try:
        os.killpg(pid, sig)  # the child is its own session leader, so pgid == pid
    except (ProcessLookupError, PermissionError):
        pass


def run_agy(argv: List[str], cwd: str, env: Dict[str, str], timeout_s: float, max_out: int, on_started=None) -> RunResult:
    """Run agy in a pty with a wall clock and an output cap; kill its whole group at the end."""
    started = time.monotonic()
    deadline = started + timeout_s
    master, slave = pty.openpty()
    proc = None
    out = bytearray()
    err_bytes = 0
    err_tail = b""
    err_markers = _markers(b"")
    marker_overlap = max(len(word) for words in _MARKERS.values() for word in words) - 1
    timed_out = truncated = False
    try:
        proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=slave, stdout=slave, stderr=subprocess.PIPE,
                                start_new_session=True, close_fds=True)
        if on_started is not None:
            on_started()
        os.close(slave)
        slave = -1
        err_fd = proc.stderr.fileno()
        open_fds = {master, err_fd}
        while open_fds:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            ready, _, _ = select.select(sorted(open_fds), [], [], min(remaining, 0.25))
            for fd in ready:
                try:
                    chunk = os.read(fd, 65536)
                except OSError as exc:
                    if exc.errno not in (errno.EIO, errno.EBADF):
                        raise
                    chunk = b""
                if not chunk:
                    open_fds.discard(fd)
                elif fd == master:
                    out += chunk
                    if len(out) > max_out:
                        del out[max_out:]
                        truncated = True
                        open_fds.clear()
                else:
                    err_bytes += len(chunk)
                    window = err_tail + chunk
                    for name, hit in _markers(window).items():
                        err_markers[name] |= hit
                    err_tail = window[-marker_overlap:]
        if not timed_out and not truncated:
            try:
                proc.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                timed_out = True
        if timed_out or truncated:
            _kill_group(proc.pid, signal.SIGTERM)
            time.sleep(0.2)
    finally:
        if proc is not None:
            _kill_group(proc.pid, signal.SIGKILL)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            if proc.stderr:
                proc.stderr.close()
        for fd in (master, slave):
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
    code = proc.returncode if proc is not None else None
    return RunResult(code, bytes(out), err_bytes, err_markers, timed_out, truncated,
                     time.monotonic() - started)


def sandbox_preflight(interpreter, tree, repo, extra_env=None):
    """Only trusted, isolated Python startup; no case imports or site hooks."""
    try:
        rc, detail = run_scratch([interpreter, '-I', '-S', '-c', 'print(1)'], tree, repo,
                                 extra_env, timeout=10, sandbox_step=True)
    except sandbox.Unavailable:
        raise
    except OSError as exc:
        raise sandbox.unavailable('preflight could not launch: %s' % exc) from None
    if rc or detail.strip() != '1':
        raise sandbox.unavailable('preflight failed (exit %s): %s' % (rc, detail[-500:]))


def run_scratch(argv, tree, repo, extra_env=None, timeout=300, output_markers=None, unsafe_no_sandbox=False,
                sandbox_step=False):
    """Explicit local checks only: argv, clean environment, isolated process group.

    OS confinement is mandatory unless the caller explicitly opts out. Only
    grading commands call it; delegate never does. macOS forbids process-fork,
    Linux isolates PIDs; unsafe mode cannot contain setsid/double-fork children.
    Output is spooled outside the repo rather than held unbounded in memory.
    """
    env = {k: os.environ[k] for k in ('LANG', 'SYSTEMROOT') if k in os.environ}
    env.update(PATH=config.safe_path(os.environ.get('PATH'), repo),
               HOME=os.path.join(tree, '.grader-home'), PYTHONDONTWRITEBYTECODE='1')
    os.makedirs(env['HOME'], exist_ok=True)
    env.update(extra_env or {})
    env.update(TMPDIR=os.path.join(tree, '.grader-tmp'), TMP=os.path.join(tree, '.grader-tmp'), TEMP=os.path.join(tree, '.grader-tmp'))
    os.makedirs(env['TMPDIR'], exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='agy-jail-') as jail, tempfile.TemporaryFile() as output:
        argv = sandbox.wrap(argv, tree, env, unsafe_no_sandbox, jail=jail)
        try:
            proc = subprocess.Popen(argv, cwd=tree, env=env, stdin=subprocess.DEVNULL,
                                    stdout=output, stderr=output, start_new_session=True)
        except OSError as exc:
            if not unsafe_no_sandbox:
                raise sandbox.unavailable('sandboxed command could not launch: %s' % exc) from None
            raise
        timed_out = False
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
        finally:
            _kill_group(proc.pid, signal.SIGKILL)
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                message = 'process %s did not exit after SIGKILL; abandoned child after 1s cleanup limit' % proc.pid
                if not unsafe_no_sandbox:
                    raise sandbox.unavailable(message) from None
                raise config.UsageError(message) from None
        if output_markers is not None:
            output.seek(0)
            overlap = b''
            while True:
                chunk = output.read(65536)
                if not chunk:
                    break
                window = (overlap + chunk).lower()
                for marker in output_markers:
                    output_markers[marker] |= marker.encode('ascii') in window
                overlap = window[-32:]
        output.seek(0, os.SEEK_END)
        size = output.tell()
        output.seek(max(0, size-4000))
        detail = output.read().decode('utf-8', 'replace')
        if not unsafe_no_sandbox and sandbox_step and (timed_out or
                (not timed_out and proc.returncode is not None and proc.returncode < 0) or
                'BRA failure blob' in detail or 'posix_spawn:' in detail or
                'sandbox-exec: sandbox_apply:' in detail or
                'sandbox-exec: execvp(' in detail or
                (proc.returncode and argv[0].endswith(('/bwrap', '/unshare')) and
                 detail.startswith(('bwrap:', 'unshare:', 'mount:', 'chroot:')))):
            raise sandbox.unavailable('sandboxed command %s (exit %s): %s' %
                                      ('timed out' if timed_out else 'could not complete', proc.returncode, detail[-500:]))
    if not timed_out and proc.returncode is not None and proc.returncode < 0:
        detail = 'check terminated by signal %d (exit %d)\n%s' % (-proc.returncode, proc.returncode, detail)
    return (124, 'timeout after %ds' % timeout) if timed_out else (proc.returncode, detail)
