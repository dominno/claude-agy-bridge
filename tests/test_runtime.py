import os
import subprocess
import time

import pytest

from agy_bridge.adapters import agy, edit_sandbox
from agy_bridge.core import config
from agy_bridge.port import process as process_seam


class UnreapableProcess:
    pid = 1000

    def __init__(self, *, stdout, stderr=None):
        self.stdout = stdout
        self.stderr = stderr
        self.waits = []

    def poll(self):
        return None

    def wait(self, timeout=None):
        assert timeout is not None, 'cleanup must not perform an unbounded wait'
        self.waits.append(timeout)
        raise subprocess.TimeoutExpired('test process', timeout)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.wait()


def pipe_reader():
    read, write = os.pipe()
    os.close(write)
    return os.fdopen(read, 'rb')


def test_sandbox_cleanup_closes_stream_without_calling_popen_exit(tmp_path, monkeypatch):
    process = UnreapableProcess(stdout=pipe_reader())
    cancelled = iter((False, True))
    monkeypatch.setattr(process_seam.subprocess, 'Popen', lambda *_args, **_kw: process)
    monkeypatch.setattr(edit_sandbox.os, 'killpg', lambda *_args: None)
    with pytest.raises(edit_sandbox.CleanupError, match='cleanup deadline'):
        edit_sandbox._run(['unused'], tmp_path, {}, 5, lambda: next(cancelled))
    assert process.waits == [2]
    assert process.stdout.closed


def test_transport_cleanup_reports_unreaped_child(tmp_path, monkeypatch):
    process = UnreapableProcess(stdout=pipe_reader(), stderr=pipe_reader())
    cancelled = iter((False, False, True))
    monkeypatch.setattr(process_seam.subprocess, 'Popen', lambda *_args, **_kw: process)
    monkeypatch.setattr(process_seam, 'kill_group', lambda *_args: None)
    with pytest.raises(agy.EditCleanupError, match='cleanup deadline'):
        agy.run_edit_agy('agy', str(tmp_path), {}, None, 5, None, 'edit',
                            wrap=lambda argv: argv, version=(1, 5, 16), should_cancel=lambda: next(cancelled))
    assert process.waits == [0.25, 1]
    assert process.stdout.closed and process.stderr.closed


def test_preflight_uses_original_absolute_deadline(tmp_path, monkeypatch):
    def fake_probe(argv, _tree, _env, budget, _cancel, end):
        assert argv == ['agy', '--version']
        assert budget == edit_sandbox.PREFLIGHT_TIMEOUT_S
        assert end == deadline
        return 124, 'synthetic timeout'

    monkeypatch.setattr(edit_sandbox, 'agent_command', lambda argv, **_kw: argv)
    monkeypatch.setattr(edit_sandbox, '_run', fake_probe)
    deadline = time.monotonic() + 5
    with pytest.raises(edit_sandbox.DeadlineExceeded, match='during the AGY'):
        edit_sandbox.preflight(tree=tmp_path, profile=tmp_path, exe='agy', deadline=deadline)


def test_expired_job_deadline_prevents_agent_setup(tmp_path, monkeypatch):
    def unexpected(*_args, **_kw):
        pytest.fail('expired budget must not start an AGY process')

    deadline = time.monotonic() - 1
    monkeypatch.setattr(edit_sandbox, 'agent_command', unexpected)
    with pytest.raises(edit_sandbox.DeadlineExceeded, match='before the AGY'):
        edit_sandbox.preflight(tree=tmp_path, profile=tmp_path, exe='agy', deadline=deadline)
    answer = agy.run_edit_agy('agy', str(tmp_path), {}, None, 20, None, 'edit', wrap=unexpected, deadline=deadline)
    assert answer.exit_code == config.EXIT_TIMEOUT and answer.returncode is None


def test_edit_requires_wrapper_and_caps_fallback_version_probe(tmp_path, monkeypatch):
    wrapped = []

    def probe(argv, _env, _cwd, timeout, *, should_cancel):
        assert argv == ['agy', '--version'] and 0 < timeout <= 2
        assert should_cancel is None
        raise subprocess.TimeoutExpired(argv, timeout)

    with pytest.raises(config.UsageError, match='isolation wrapper'):
        agy.run_edit_agy('agy', str(tmp_path), {}, None, 5, None, 'edit', wrap=None)
    monkeypatch.setattr(agy, 'run_probe', probe)
    result = agy.run_edit_agy('agy', str(tmp_path), {}, None, 15, None, 'edit',
                                 wrap=lambda argv: wrapped.append(argv) or argv, deadline=time.monotonic() + 2)
    assert result.exit_code == config.EXIT_TIMEOUT
    assert wrapped == [['agy', '--version']]
