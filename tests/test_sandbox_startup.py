"""Startup regressions use fake children first: never reproduce a kernel hang."""
import subprocess
import sys
import time

import pytest

from agy_bridge import grading, queue, runner, sandbox


def test_mac_profile_root_read_precedes_secret_denials(tmp_path):
    profile = sandbox.mac_profile(str(tmp_path), sys.executable)
    allowance = '(allow file-read* (literal "/"))'
    assert allowance in profile
    assert profile.index(allowance) < profile.index('(deny file-read* (subpath ')


def test_mac_profile_allows_framework_reexec(tmp_path):
    runtime = tmp_path/'Python.framework/Versions/3.12'
    stub = runtime/'bin/python3.12'
    real = runtime/'Resources/Python.app/Contents/MacOS/Python'
    for path in (stub, real):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    profile = sandbox.mac_profile(str(tmp_path/'scratch'), str(stub))
    assert f'(allow process-exec (literal "{real}"))' in profile
    assert str(real) in sandbox.runtime_paths(str(stub))


def test_mac_profile_allows_clt_framework_and_case_home_denials(tmp_path):
    prefix = tmp_path/'CommandLineTools'
    stub = prefix/'usr/bin/python3'
    version = prefix/'Library/Frameworks/Python3.framework/Versions/3.9'
    app = version/'Resources/Python.app/Contents/MacOS/Python'
    for path in (stub, app):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    (version.parent/'Current').symlink_to(version, target_is_directory=True)
    home = tmp_path/'scratch/home'
    profile = sandbox.mac_profile(str(tmp_path/'scratch'), str(stub), str(home))
    assert f'(allow process-exec (literal "{app}"))' in profile
    assert str(version) in sandbox.runtime_paths(str(stub))
    assert profile.index(f'(deny file-read* (subpath "{home}/.ssh"))') > profile.index('(allow file-read* (literal "/"))')


def test_preflight_success_uses_only_isolated_case_interpreter(tmp_path, monkeypatch):
    class Process:
        pid, returncode = 99999999, 0
        def wait(self, timeout=None):
            assert timeout in (10, 1)
            return 0
    def popen(argv, **kwargs):
        assert argv[-5:] == [sys.executable, '-I', '-S', '-c', 'print(1)']
        assert kwargs['start_new_session'] and kwargs['stdin'] == subprocess.DEVNULL
        kwargs['stdout'].write(b'1\n')
        return Process()
    monkeypatch.setattr(runner.subprocess, 'Popen', popen)
    monkeypatch.setattr(runner.os, 'killpg', lambda *args: None)
    runner.sandbox_preflight(sys.executable, str(tmp_path), str(tmp_path/'repo'))


@pytest.mark.parametrize('failure', ['timeout', 'abort', 'spawn', 'wrong-output', 'missing'])
def test_preflight_refuses_without_running_case(tmp_path, monkeypatch, failure):
    calls, waits, kills = [], [], []
    class Process:
        pid = 99999999
        returncode = -6 if failure == 'abort' else 0
        def wait(self, timeout=None):
            assert timeout is not None, 'an unbounded wait can hang forever'
            waits.append(timeout)
            if failure == 'timeout':
                raise subprocess.TimeoutExpired('probe', timeout)
            return self.returncode
    def popen(argv, **kwargs):
        calls.append(argv)
        assert kwargs['start_new_session']
        assert argv[-4:] == ['-I', '-S', '-c', 'print(1)']
        if failure == 'missing':
            raise OSError('cannot spawn')
        kwargs['stdout'].write(b'posix_spawn: Operation not permitted' if failure == 'spawn' else b'bad output')
        return Process()
    monkeypatch.setattr(runner.subprocess, 'Popen', popen)
    monkeypatch.setattr(runner.os, 'killpg', lambda *args: kills.append(args))
    with pytest.raises(sandbox.Unavailable, match='sandbox unavailable on this machine.*unsafe-no-sandbox') as error:
        runner.sandbox_preflight(sys.executable, str(tmp_path), str(tmp_path/'repo'))
    assert len(calls) == 1
    if failure == 'timeout':
        assert waits[0] == 10 and max(waits) <= 10 and len(waits) == 2
        assert 'abandon' in str(error.value)
        assert kills == [(99999999, runner.signal.SIGKILL)]


def test_unkillable_check_is_abandoned_with_bounded_wait(tmp_path, monkeypatch):
    waits = []
    class Process:
        pid, returncode = 99999999, None
        def wait(self, timeout=None):
            assert timeout is not None
            waits.append(timeout)
            raise subprocess.TimeoutExpired('check', timeout)
    monkeypatch.setattr(runner.subprocess, 'Popen', lambda *a, **k: Process())
    monkeypatch.setattr(runner.os, 'killpg', lambda *args: None)
    with pytest.raises(sandbox.Unavailable, match='abandon'):
        runner.run_scratch([sys.executable, '-c', 'pass'], str(tmp_path), str(tmp_path/'repo'), timeout=.01)
    assert len(waits) == 2 and waits[1] <= 2


@pytest.mark.parametrize('operation', ['mutant', 'diff'])
def test_unavailable_preflight_aborts_before_checks_or_patch(h, monkeypatch, operation):
    (h.repo/'impl.py').write_text('value = 1\n')
    case = dict(name='startup', files={'impl.py':'impl.py'}, solution_files=['impl.py'],
                test_cmd=[sys.executable, '-c', 'pass'])
    def unavailable(*args, **kwargs):
        raise sandbox.Unavailable('sandbox unavailable on this machine; use --unsafe-no-sandbox explicitly')
    monkeypatch.setattr(runner, 'sandbox_preflight', unavailable)
    monkeypatch.setattr(runner, 'run_scratch', lambda *a, **k: pytest.fail('code or patch ran before preflight'))
    with pytest.raises(sandbox.Unavailable, match='sandbox unavailable'):
        if operation == 'mutant':
            grading.grade(str(h.repo), case, [dict(file='impl.py', old='1', new='2', why='probe')])
        else:
            grading.apply_diff(str(h.repo), case, '--- impl.py\n+++ impl.py\n@@ -1 +1 @@\n-value = 1\n+value = 2\n')


@pytest.mark.parametrize('kind', ['patch-timeout', 'patch-abort', 'git-spawn'])
def test_patch_sandbox_failures_abort_measurement(tmp_path, monkeypatch, kind):
    class Process:
        pid, returncode = 99999999, -6 if kind == 'patch-abort' else 1
        def wait(self, timeout=None):
            assert timeout is not None
            if kind == 'patch-timeout' and timeout == .01:
                raise subprocess.TimeoutExpired('patch', timeout)
            return self.returncode
    def popen(argv, **kwargs):
        if kind == 'git-spawn':
            kwargs['stdout'].write(b'posix_spawn: Operation not permitted')
        return Process()
    monkeypatch.setattr(runner.subprocess, 'Popen', popen)
    monkeypatch.setattr(runner.os, 'killpg', lambda *args: None)
    with pytest.raises(sandbox.Unavailable, match='sandbox unavailable'):
        runner.run_scratch(['git' if kind == 'git-spawn' else 'patch', '--version'],
                           str(tmp_path), str(tmp_path/'repo'), timeout=.01, sandbox_step=True)


@pytest.mark.parametrize('line', ['NEED: missing\n', 'UNVERIFIED (no citation): NEED: missing\n', 'NEED: ' + 'x'*900 + '\n'],
                         ids=['many', 'annotated', 'candidate-bytes'])
def test_many_need_lines_have_bounded_filesystem_work(h, monkeypatch, line):
    calls = []
    def local(*args):
        calls.append(1)
        raise OSError('missing')
    monkeypatch.setattr(queue.safety, 'local', local)
    start = time.monotonic()
    assert queue.need_files(str(h.repo), line*(1024*1024//len(line)), []) == []
    assert len(calls) <= 100
    assert sum(len(line) for _ in calls) <= 64*1024
    assert time.monotonic() - start < 1
