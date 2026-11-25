"""Version detection and guarded read-only and editing transports for agy (the model runner adapter)."""
from __future__ import annotations

import errno
import json
import math
import os
import pty
import re
import select
import shutil
import signal
import tempfile
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

from ..core import config
from ..port import process
from ..port.model_runner import (AgyMissing, AgyNotRun, EditCleanupError, EditRunResult,  # noqa: F401
                                 RunResult, UnsupportedAgy)
from .agy_stream import (_EditProtocolError, _EditStream, _edit_error, _markers, _structured_agy_error,  # noqa: F401
                         MarkerScan, valid_conversation_id)
from .edit_runtime import GUEST_PRIVATE_PATHS, MIN_AGY_VERSION, execution_deadline

PROMPT = ("Read TASK.md in the current directory and do what it says. Use only the files in this "
          "directory. You may not write files, run commands or fetch URLs. Reply with plain text.")


class _ProbeCancelled(Exception):
    pass


@dataclass(frozen=True)
class ArgvSpec:
    print_flag: str
    model_flag: Optional[str]
    fixed: Tuple[str, ...]


# Legacy delegate options are keyed by major version. Modern policy is pinned
# separately below because an execution default changed within major 1.
ARGV_SPECS: Dict[int, ArgvSpec] = {
    1: ArgvSpec(print_flag="--print", model_flag="--model", fixed=()),
}

# R3: substrings that must never appear in any argv token.
FORBIDDEN_FRAGMENTS = ("skip-permission", "yolo", "dangerous", "bypass", "auto-approve", "allow-")

# Auto became the headless default for eligible users in 1.5.7. Require a
# verified way to pin session settings before delegating to those versions.
_AUTO_DEFAULT_VERSION = (1, 5, 7)
_BLOCKED_TOOLS = ("command", "unsandboxed", "read_url", "execute_url", "mcp")


def _session_settings(edit: bool, api_key_auth: bool = False) -> str:
    denied = [action + "(*)" for action in _BLOCKED_TOOLS]
    if edit:
        denied.extend(action + "(" + path + ")" for path in GUEST_PRIVATE_PATHS
                      for action in ("read_file", "write_file"))
    else:
        denied.append("write_file(*)")
    settings = {
        "agentMode": "accept-edits" if edit else "",
        "toolPermission": "request-review",
        "allowNonWorkspaceAccess": False,
        "permissions": {"deny": denied, "ask": [], "allow": []},
    }
    if api_key_auth:
        settings["modelProvider"] = "gemini"
    return json.dumps(settings, separators=(",", ":"))


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


def run_probe(argv, env, cwd, timeout, *, should_cancel=None):
    """Short agy probe with bounded execution and reaping, without pipe drains."""
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        proc = process.spawn(argv, env=env, cwd=cwd, stdin=process.DEVNULL,
                             stdout=out, stderr=err, start_new_session=True)
        try:
            if should_cancel is None:
                proc.wait(timeout=timeout)
            else:
                end = time.monotonic() + timeout
                while proc.poll() is None:
                    if should_cancel():
                        raise _ProbeCancelled
                    remaining = end - time.monotonic()
                    if remaining <= 0:
                        raise process.TimeoutExpired(argv, timeout)
                    try:
                        proc.wait(timeout=min(remaining, 0.1))
                    except process.TimeoutExpired:
                        continue
        finally:
            process.kill_group(proc.pid, signal.SIGKILL)
            try:
                proc.wait(timeout=1)
            except process.TimeoutExpired:
                raise UnsupportedAgy('agy probe did not exit after SIGKILL; abandoned child after 1s cleanup limit') from None
        out.seek(0)
        err.seek(0)
        return process.CompletedProcess(argv, proc.returncode, out.read(), err.read())


def detect_version(exe: str, env: Dict[str, str]) -> Tuple[int, int, int]:
    """Run 'agy --version' with the same five-name environment, in an empty folder."""
    try:
        with tempfile.TemporaryDirectory(prefix="agy-bridge-ver-") as empty:
            proc = run_probe([exe, "--version"], env, empty, timeout=20)
    except OSError:
        raise AgyNotRun("'agy --version' could not be started")
    except process.TimeoutExpired:
        raise UnsupportedAgy("'agy --version' did not finish in time")
    return parse_version((proc.stdout + proc.stderr).decode("utf-8", "replace"))


def build_argv(version: Tuple[int, int, int], exe: str, model: Optional[str]) -> List[str]:
    """Build the read-only command. Unsupported versions fail closed."""
    spec = ARGV_SPECS.get(version[0])
    if spec is None:
        raise UnsupportedAgy("agy %d.%d.%d is not a version this bridge knows" % version)
    argv = [exe, spec.print_flag, PROMPT]
    if model:
        if not spec.model_flag:
            raise UnsupportedAgy("this agy version has no known model flag")
        argv += [spec.model_flag, model]
    argv += list(spec.fixed)
    if version >= MIN_AGY_VERSION:
        argv += ["--settings", _session_settings(edit=False), "--disable-slash-commands"]
    elif version >= _AUTO_DEFAULT_VERSION:
        raise UnsupportedAgy("this agy defaults to Auto; update to %s+ for pinned read-only settings" % _edit_version_label())
    for token in argv[1:]:
        if any(frag in token.lower() for frag in FORBIDDEN_FRAGMENTS):
            raise UnsupportedAgy("refusing to build a command line with a permission-widening flag")
    return argv


def _edit_version_label():
    return ".".join(map(str, MIN_AGY_VERSION))


def require_edit_version(version):
    if version[0] != MIN_AGY_VERSION[0] or version < MIN_AGY_VERSION:
        raise UnsupportedAgy("editing requires AGY %s or later in major version %d" %
                             (_edit_version_label(), MIN_AGY_VERSION[0]))


def build_edit_argv(version, exe, model, conversation_id, prompt, timeout_s, api_key_auth=False):
    require_edit_version(version)
    if conversation_id is not None and not valid_conversation_id(conversation_id):
        raise config.UsageError("invalid saved AGY conversation ID")
    model = config.validate_model(model)
    if not isinstance(prompt, str) or not prompt.strip() or "\0" in prompt:
        raise config.UsageError("editing requires a nonempty AGY prompt without NUL characters")
    # The host timer is authoritative. Give it a short lead over the provider
    # timer, which still bounds the turn if the host supervisor is delayed.
    provider_timeout = "%ds" % (math.ceil(timeout_s) + 1)
    argv = [exe, "--mode=accept-edits", "--settings", _session_settings(edit=True, api_key_auth=api_key_auth),
            "--disable-slash-commands", "--output-format", "stream-json",
            "--print-timeout", provider_timeout]
    if model:
        argv += ["--model", model]
    if conversation_id is not None:
        argv += ["--conversation", conversation_id]
    return argv + ["--print", prompt]




def _read_chunk(fd):
    try:
        return os.read(fd, 65536)
    except OSError as exc:
        if exc.errno not in (errno.EIO, errno.EBADF):
            raise
        return b""


class _PtyCapture:
    """What run_agy keeps from agy: the pty output up to the cap, the stderr size and its markers."""

    def __init__(self):
        self.out = bytearray()
        self.err_bytes = 0
        self.markers = MarkerScan()
        self.timed_out = self.truncated = False

    def drain(self, master, err_fd, deadline, max_out):
        open_fds = {master, err_fd}
        while open_fds:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.timed_out = True
                break
            ready, _, _ = select.select(sorted(open_fds), [], [], min(remaining, 0.25))
            for fd in ready:
                chunk = _read_chunk(fd)
                if not chunk:
                    open_fds.discard(fd)
                elif fd == master:
                    self.out += chunk
                    if len(self.out) > max_out:
                        del self.out[max_out:]
                        self.truncated = True
                        open_fds.clear()
                else:
                    self.err_bytes += len(chunk)
                    self.markers.feed(chunk)


def _reap_pty_run(proc, master, slave):
    """Kill the whole group, reap it within 5s and close every descriptor run_agy opened."""
    if proc is not None:
        process.kill_group(proc.pid, signal.SIGKILL)
        try:
            proc.wait(timeout=5)
        except process.TimeoutExpired:
            pass
        if proc.stderr:
            proc.stderr.close()
    for fd in (master, slave):
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass


def run_agy(argv: List[str], cwd: str, env: Dict[str, str], timeout_s: float, max_out: int, on_started=None) -> RunResult:
    """Run agy in a pty with a wall clock and an output cap; kill its whole group at the end."""
    started = time.monotonic()
    deadline = started + timeout_s
    master, slave = pty.openpty()
    proc = None
    capture = _PtyCapture()
    try:
        proc = process.spawn(argv, cwd=cwd, env=env, stdin=slave, stdout=slave, stderr=process.PIPE,
                             start_new_session=True, close_fds=True)
        if on_started is not None:
            on_started()
        os.close(slave)
        slave = -1
        capture.drain(master, proc.stderr.fileno(), deadline, max_out)
        if not capture.timed_out and not capture.truncated:
            try:
                proc.wait(timeout=max(0.1, deadline - time.monotonic()))
            except process.TimeoutExpired:
                capture.timed_out = True
        if capture.timed_out or capture.truncated:
            process.kill_group(proc.pid, signal.SIGTERM)
            time.sleep(0.2)
    finally:
        _reap_pty_run(proc, master, slave)
    code = proc.returncode if proc is not None else None
    return RunResult(code, bytes(capture.out), capture.err_bytes, capture.markers.found, capture.timed_out,
                     capture.truncated, time.monotonic() - started)


class _StoppedBeforeLaunch(Exception):
    """The editing turn was cancelled or ran out of time before agy started."""

    def __init__(self, kind):
        super().__init__(kind)
        self.kind = kind


def _before_edit_launch(conversation_id, started, error_kind):
    return EditRunResult(conversation_id, "ERROR", "", None, error_kind == "timeout", False,
                         error_kind == "cancelled", _markers(b""), time.monotonic() - started, error_kind, None)


def _check_stop(should_cancel, deadline):
    if should_cancel is not None and should_cancel():
        raise _StoppedBeforeLaunch("cancelled")
    if time.monotonic() >= deadline:
        raise _StoppedBeforeLaunch("timeout")


def _isolated_version(exe, cwd, env, wrap, should_cancel, deadline):
    """Probe 'agy --version' inside the isolation wrapper, within 20s and the turn deadline."""
    probe_argv = wrap([exe, "--version"])
    probe_budget = min(20, deadline - time.monotonic())
    if probe_budget <= 0:
        raise _StoppedBeforeLaunch("timeout")
    try:
        probe = run_probe(probe_argv, env, cwd, timeout=probe_budget, should_cancel=should_cancel)
    except _ProbeCancelled:
        raise _StoppedBeforeLaunch("cancelled") from None
    except process.TimeoutExpired:
        if probe_budget < 20 or time.monotonic() >= deadline:
            raise _StoppedBeforeLaunch("timeout") from None
        raise UnsupportedAgy("isolated 'agy --version' did not finish in time") from None
    if probe.returncode:
        raise UnsupportedAgy("isolated 'agy --version' failed")
    return parse_version((probe.stdout + probe.stderr).decode("utf-8", "replace"))


class _EditTurn:
    """The stream-json stdout and the stderr of one editing turn, read until the result, a stop or the deadline."""

    def __init__(self, stream, max_out, on_activity):
        self.stream = stream
        self.max_out = max_out
        self.on_activity = on_activity
        self.markers = MarkerScan()
        self.agy_error = None
        self.timed_out = self.truncated = self.cancelled = self.malformed = False

    def follow(self, proc, stdout, stderr, should_cancel, deadline):
        pending = {stdout: b"", stderr: b""}
        while pending or proc.poll() is None:
            if should_cancel is not None and should_cancel():
                self.cancelled = True
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.timed_out = True
                break
            ready, _, _ = select.select(list(pending), [], [], min(remaining, 0.20))
            for fd in ready:
                chunk = os.read(fd, 65536)
                if fd == stderr:
                    self.markers.feed(chunk)
                lines = (pending[fd] + chunk).split(b"\n")
                if chunk:
                    pending[fd] = lines.pop()
                else:
                    pending.pop(fd)
                if fd == stdout:
                    self._stdout_lines(lines, pending.get(fd, b""))
                else:
                    self._stderr_lines(lines, pending, fd)
            if self.truncated:
                break

    def _stdout_lines(self, lines, partial):
        for line in lines:
            if len(line) > self.max_out:
                self.truncated = True
                break
            self.stream.line(line)
        if lines and self.on_activity is not None:
            self.on_activity()
        self.truncated |= len(partial) > self.max_out

    def _stderr_lines(self, lines, pending, fd):
        for line in lines:
            if len(line) <= 65536:
                observed_error = _structured_agy_error(line)
                if observed_error is not None:
                    self.agy_error = observed_error
        if len(pending.get(fd, b"")) > 65536:
            pending[fd] = b""


def _stop_edit_process(proc):
    """SIGTERM a live turn, then SIGKILL its group; refuse to continue when it cannot be reaped."""
    try:
        if proc.poll() is None:
            process.kill_group(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=0.25)
            except process.TimeoutExpired:
                pass
        process.kill_group(proc.pid, signal.SIGKILL)
        try:
            proc.wait(timeout=1)
        except process.TimeoutExpired:
            raise EditCleanupError("isolated AGY did not exit after SIGKILL and the cleanup deadline") from None
    finally:
        proc.stdout.close()
        proc.stderr.close()


def _run_edit_turn(argv, cwd, env, turn, should_cancel, deadline, started):
    proc = process.spawn(argv, cwd=cwd, env=env, stdin=process.DEVNULL,
                         stdout=process.PIPE, stderr=process.PIPE, start_new_session=True, close_fds=True)
    stdout, stderr = proc.stdout.fileno(), proc.stderr.fileno()
    try:
        turn.follow(proc, stdout, stderr, should_cancel, deadline)
    except _EditProtocolError:
        turn.malformed = True
    finally:
        _stop_edit_process(proc)
    stream = turn.stream
    error_kind = _edit_error(stream, proc.returncode, turn.timed_out, turn.truncated, turn.cancelled,
                             turn.malformed, turn.markers.found, turn.agy_error)
    retryable = (turn.agy_error or {}).get("retryable")
    return EditRunResult(stream.conversation_id, stream.status or "ERROR", stream.response,
                         proc.returncode, error_kind == "timeout", turn.truncated,
                         error_kind == "cancelled", turn.markers.found,
                         time.monotonic() - started, error_kind,
                         retryable if type(retryable) is bool else None)


def run_edit_agy(
        exe: str, cwd: str, env: Dict[str, str], model: Optional[str],
        timeout_s: float, conversation_id: Optional[str], prompt: str,
        on_conversation: Optional[Callable[[str], None]] = None, *,
        wrap, max_out: int = config.MAX_OUT_BYTES, should_cancel=None,
        on_activity: Optional[Callable[[], None]] = None,
        version: Optional[Tuple[int, int, int]] = None, deadline=None) -> EditRunResult:
    """Run one isolated coding turn under a relative limit and an optional job deadline."""
    started = time.monotonic()
    deadline = execution_deadline(started, timeout_s, deadline)
    if not callable(wrap) or max_out < 1:
        raise config.UsageError("editing requires an isolation wrapper and a positive output limit")
    try:
        _check_stop(should_cancel, deadline)
        if version is None:
            version = _isolated_version(exe, cwd, env, wrap, should_cancel, deadline)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _StoppedBeforeLaunch("timeout")
        argv = wrap(build_edit_argv(version, exe, model, conversation_id, prompt, remaining,
                                   api_key_auth="GEMINI_API_KEY" in env))
        _check_stop(should_cancel, deadline)
    except _StoppedBeforeLaunch as stopped:
        return _before_edit_launch(conversation_id, started, stopped.kind)
    turn = _EditTurn(_EditStream(conversation_id, on_conversation), max_out, on_activity)
    return _run_edit_turn(argv, cwd, env, turn, should_cancel, deadline, started)
