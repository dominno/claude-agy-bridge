"""Version detection and guarded read-only and editing transports for agy."""
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
import subprocess
import tempfile
import time
from dataclasses import dataclass
from . import config, sandbox
from .edit_runtime import GUEST_PRIVATE_PATHS, MIN_AGY_VERSION, execution_deadline
from typing import Callable, Dict, List, Optional, Tuple

PROMPT = ("Read TASK.md in the current directory and do what it says. Use only the files in this "
          "directory. You may not write files, run commands or fetch URLs. Reply with plain text.")


class AgyMissing(Exception):
    pass


class UnsupportedAgy(Exception):
    pass


class AgyNotRun(UnsupportedAgy):
    """agy was found but could not be started, so no program ran."""


class EditCleanupError(config.UsageError):
    """The isolated editing process could not be confirmed stopped."""


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
_CONVERSATION_ID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")


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
        proc = subprocess.Popen(argv, env=env, cwd=cwd, stdin=subprocess.DEVNULL,
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
                        raise subprocess.TimeoutExpired(argv, timeout)
                    try:
                        proc.wait(timeout=min(remaining, 0.1))
                    except subprocess.TimeoutExpired:
                        continue
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


def valid_conversation_id(value) -> bool:
    return isinstance(value, str) and _CONVERSATION_ID.fullmatch(value) is not None


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
    "provider_timeout": ("print timeout",),
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


_EDIT_STATUSES = {"SUCCESS", "ERROR", "CANCELED", "INTERRUPTED", "INVALID", "WAITING", "RUNNING"}


@dataclass(frozen=True)
class EditRunResult:
    conversation_id: Optional[str]
    status: str
    response: str
    returncode: Optional[int]
    timed_out: bool
    truncated: bool
    cancelled: bool
    err_markers: Dict[str, bool]
    seconds: float
    error_kind: Optional[str]
    retryable: Optional[bool]

    @property
    def exit_code(self) -> int:
        return {
            None: config.EXIT_OK,
            "cancelled": 130,
            "timeout": config.EXIT_TIMEOUT,
            "permission": config.EXIT_SOFT_DENY,
            "output_limit": config.EXIT_INCOMPLETE,
            "quota": config.EXIT_QUOTA,
            "auth": config.EXIT_AUTH,
        }.get(self.error_kind, config.EXIT_AGY_FAILED)


class _EditProtocolError(Exception):
    pass


class _EditStream:
    def __init__(self, conversation_id, on_conversation):
        self.conversation_id = conversation_id
        self.on_conversation = on_conversation
        self.initialized = False
        self.status = None
        self.response = ""
        self.permission_denied = False
        self.provider_timeout = False

    def _conversation(self, conversation_id):
        if not valid_conversation_id(conversation_id):
            raise _EditProtocolError
        if self.conversation_id is not None and conversation_id != self.conversation_id:
            raise _EditProtocolError
        self.conversation_id = conversation_id
        if self.on_conversation is not None:
            self.on_conversation(conversation_id)
            self.on_conversation = None

    def line(self, line):
        if not line.strip():
            return
        try:
            body = json.loads(line)
        except (ValueError, UnicodeError, RecursionError):
            raise _EditProtocolError from None
        if not isinstance(body, dict) or not isinstance(body.get("event"), str) or self.status is not None:
            raise _EditProtocolError
        event = body["event"]
        if event == "init":
            init = body.get("init")
            if self.initialized or not isinstance(init, dict):
                raise _EditProtocolError
            conversation_id = body.get("conversation_id", init.get("conversation_id"))
            if conversation_id is not None:
                self._conversation(conversation_id)
            self.initialized = True
        elif event == "result":
            result = body.get("result")
            if not isinstance(result, dict):
                raise _EditProtocolError
            status = result.get("status")
            response = result.get("response", "")
            if not isinstance(status, str) or status not in _EDIT_STATUSES or not isinstance(response, str):
                raise _EditProtocolError
            if result.get("conversation_id") or status == "SUCCESS":
                self._conversation(result.get("conversation_id"))
            if status == "SUCCESS" and not self.initialized:
                raise _EditProtocolError
            self.status = status
            self.response = response
            self.permission_denied |= bool(result.get("denied_actions"))
            self.provider_timeout = result.get("finished_reason") == "print_timeout"
        elif event == "step_update":
            # The provider may report a denied tool without a failing process.
            # The raw step may contain source files, so keep only a marker.
            step = body.get("step_update")
            info = step.get("tool_info") if isinstance(step, dict) else None
            error = info.get("error") if isinstance(info, dict) else None
            if isinstance(error, dict):
                kind = error.get("type")
                message = error.get("message")
                self.permission_denied |= kind == "PERMISSION_DENIED" or (
                    isinstance(message, str) and _markers(message.encode("utf-8", "replace"))["soft_deny"])


def _structured_agy_error(line):
    prefix = b"AGY_ERROR:"
    line = line.lstrip()
    if not line.startswith(prefix):
        return None
    try:
        error = json.loads(line[len(prefix):])
    except (ValueError, UnicodeError, RecursionError):
        return {}
    return error if isinstance(error, dict) else {}


def _edit_error(stream, returncode, timed_out, truncated, cancelled, malformed, err_markers, agy_error):
    if cancelled:
        return "cancelled"
    if timed_out or stream.provider_timeout or err_markers["provider_timeout"]:
        return "timeout"
    if stream.status in ("CANCELED", "INTERRUPTED"):
        return "cancelled"
    if truncated:
        return "output_limit"
    error = agy_error or {}
    status = error.get("status") or error.get("canonical_status")
    code = error.get("error_code")
    if status == "RESOURCE_EXHAUSTED" or code == 429:
        return "quota"
    if status == "UNAUTHENTICATED" or code == 401:
        return "auth"
    if status == "DEADLINE_EXCEEDED" or code == 408:
        return "timeout"
    if (stream.permission_denied or err_markers["soft_deny"] or
            stream.status == "WAITING" or status == "PERMISSION_DENIED"):
        return "permission"
    if returncode != 0 or agy_error is not None:
        if err_markers["quota"]:
            return "quota"
        if err_markers["auth"]:
            return "auth"
        return "protocol" if malformed else "agy_failed"
    if malformed or stream.status is None:
        return "protocol"
    return None if stream.status == "SUCCESS" else "agy_failed"


def _before_edit_launch(conversation_id, started, error_kind):
    return EditRunResult(conversation_id, "ERROR", "", None, error_kind == "timeout", False,
                         error_kind == "cancelled", _markers(b""), time.monotonic() - started, error_kind, None)


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
    if should_cancel is not None and should_cancel():
        return _before_edit_launch(conversation_id, started, "cancelled")
    if time.monotonic() >= deadline:
        return _before_edit_launch(conversation_id, started, "timeout")
    if version is None:
        probe_argv = wrap([exe, "--version"])
        probe_budget = min(20, deadline - time.monotonic())
        if probe_budget <= 0:
            return _before_edit_launch(conversation_id, started, "timeout")
        try:
            probe = run_probe(probe_argv, env, cwd, timeout=probe_budget, should_cancel=should_cancel)
        except _ProbeCancelled:
            return _before_edit_launch(conversation_id, started, "cancelled")
        except subprocess.TimeoutExpired:
            if probe_budget < 20 or time.monotonic() >= deadline:
                return _before_edit_launch(conversation_id, started, "timeout")
            raise UnsupportedAgy("isolated 'agy --version' did not finish in time") from None
        if probe.returncode:
            raise UnsupportedAgy("isolated 'agy --version' failed")
        version = parse_version((probe.stdout + probe.stderr).decode("utf-8", "replace"))
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return _before_edit_launch(conversation_id, started, "timeout")
    argv = wrap(build_edit_argv(version, exe, model, conversation_id, prompt, remaining,
                               api_key_auth="GEMINI_API_KEY" in env))
    if should_cancel is not None and should_cancel():
        return _before_edit_launch(conversation_id, started, "cancelled")
    if time.monotonic() >= deadline:
        return _before_edit_launch(conversation_id, started, "timeout")
    stream = _EditStream(conversation_id, on_conversation)
    err_markers = _markers(b"")
    agy_error = None
    timed_out = truncated = cancelled = malformed = False
    proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True, close_fds=True)
    stdout, stderr = proc.stdout.fileno(), proc.stderr.fileno()
    pending = {stdout: b"", stderr: b""}
    err_tail = b""
    marker_overlap = max(len(word) for words in _MARKERS.values() for word in words) - 1
    try:
        while pending or proc.poll() is None:
            if should_cancel is not None and should_cancel():
                cancelled = True
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            ready, _, _ = select.select(list(pending), [], [], min(remaining, 0.20))
            for fd in ready:
                chunk = os.read(fd, 65536)
                if fd == stderr:
                    window = err_tail + chunk
                    for name, hit in _markers(window).items():
                        err_markers[name] |= hit
                    err_tail = window[-marker_overlap:]
                lines = (pending[fd] + chunk).split(b"\n")
                if chunk:
                    pending[fd] = lines.pop()
                else:
                    pending.pop(fd)
                if fd == stdout:
                    for line in lines:
                        if len(line) > max_out:
                            truncated = True
                            break
                        stream.line(line)
                    if lines and on_activity is not None:
                        on_activity()
                    truncated |= len(pending.get(fd, b"")) > max_out
                else:
                    for line in lines:
                        if len(line) <= 65536:
                            observed_error = _structured_agy_error(line)
                            if observed_error is not None:
                                agy_error = observed_error
                    if len(pending.get(fd, b"")) > 65536:
                        pending[fd] = b""
            if truncated:
                break
    except _EditProtocolError:
        malformed = True
    finally:
        try:
            if proc.poll() is None:
                _kill_group(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(timeout=0.25)
                except subprocess.TimeoutExpired:
                    pass
            _kill_group(proc.pid, signal.SIGKILL)
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                raise EditCleanupError("isolated AGY did not exit after SIGKILL and the cleanup deadline") from None
        finally:
            proc.stdout.close()
            proc.stderr.close()
    error_kind = _edit_error(stream, proc.returncode, timed_out, truncated, cancelled,
                             malformed, err_markers, agy_error)
    retryable = (agy_error or {}).get("retryable")
    return EditRunResult(stream.conversation_id, stream.status or "ERROR", stream.response,
                         proc.returncode, error_kind == "timeout", truncated,
                         error_kind == "cancelled", err_markers,
                         time.monotonic() - started, error_kind,
                         retryable if type(retryable) is bool else None)


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
