"""Crashing checks cannot spoof sandbox availability; agy probes never reap forever."""
import json
import subprocess
import sys

import pytest

from agy_bridge import cli, grading, runner
from sandbox_worker import bounded_python


@pytest.fixture
def case(h, guarded_sandbox):
    (h.repo/'impl.py').write_text('value = 1\nunused = 2\n')
    (h.repo/'check.py').write_text("import runpy\nassert runpy.run_path('impl.py')['value'] == 1\n")
    return h, dict(name='crash', files={'impl.py': 'impl.py', 'check.py': 'check.py'},
                   solution_files=['impl.py'], test_cmd=[sys.executable, 'check.py'])


@pytest.mark.parametrize('payload', ["__import__('os').abort()",
                                      "print('posix_spawn: invented'); raise RuntimeError('failed')",
                                      "print('BRA failure blob'); raise RuntimeError('failed')"],
                         ids=['abort', 'posix-spawn', 'bra'])
@pytest.mark.parametrize('mode', ['mutant', 'diff'])
def test_real_check_failure_does_not_report_unavailable(case, payload, mode):
    h, spec = case
    if mode == 'mutant':
        rows, _, _ = grading.grade(str(h.repo), spec, [
            dict(file='impl.py', old='unused = 2', new=payload, why='crash or spoof'),
            dict(file='impl.py', old='value = 1', new='value = 0', why='next mutant'),
        ])
        assert [row['status'] for row in rows] == ['KILLED', 'KILLED'], rows
        first = rows[0]
    else:
        diff = f'--- impl.py\n+++ impl.py\n@@ -2 +2 @@\n-unused = 2\n+{payload}\n'
        first = grading.apply_diff(str(h.repo), spec, diff)
        assert first['applied'] and first['tests_pass'] is False, first
    if 'abort' in payload and sys.platform.startswith('linux'):
        # bwrap's PID 1 reports a child's signal as a shell-style exit code.
        assert first['exit_code'] == 134
    elif 'abort' in payload:
        assert first['exit_code'] < 0
        assert 'signal' in first.get('detail', first.get('test_tail', ''))


@pytest.mark.parametrize('marker', ['posix_spawn: invented', 'BRA failure blob'])
def test_real_printed_marker_does_not_fail_passing_check(case, marker):
    h, spec = case
    rows, _, _ = grading.grade(str(h.repo), spec, [
        dict(file='impl.py', old='unused = 2', new=f'print({marker!r})', why='printed text')])
    assert rows[0]['status'] == 'SURVIVES', rows


def test_real_broken_profile_still_reports_unavailable(case):
    h, spec = case
    # Establish host availability using the normal guarded preflight/skip first.
    grading.preflight(str(h.repo), spec, h.root/'healthy-probe')
    if sys.platform != 'darwin':
        pytest.skip('macOS profile regression')
    code = '''
import json, sys
from agy_bridge import grading, sandbox
root, spec = sys.argv[1], json.loads(sys.argv[2])
sandbox.mac_profile = lambda *a, **kw: '(version 1)(deny default)'
try:
    grading.grade(root, spec, [dict(file='impl.py', old='value = 1', new='value = 0', why='probe')])
except sandbox.Unavailable as exc:
    assert 'sandbox unavailable' in str(exc) and 'unsafe-no-sandbox' in str(exc)
    print('unavailable')
else:
    raise AssertionError('broken profile ran checks')
'''
    assert bounded_python(code, [str(h.repo), json.dumps(spec)]).strip() == 'unavailable'


@pytest.mark.parametrize('caller', ['version', 'models'])
@pytest.mark.parametrize('outcome', ['success', 'timeout', 'abandon', 'launch'])
def test_agy_probe_waits_are_bounded(tmp_path, monkeypatch, caller, outcome):
    waits, kills, launches = [], [], []
    class Process:
        pid, returncode = 99999999, 0
        def wait(self, timeout=None):
            assert timeout is not None, 'unbounded reap'
            waits.append(timeout)
            if outcome == 'abandon' or (outcome == 'timeout' and len(waits) == 1):
                raise subprocess.TimeoutExpired('agy', timeout)
            return 0
    def popen(argv, **kw):
        launches.append(argv)
        assert kw['env'] == {'LANG': 'C'} and kw['cwd']
        assert kw['start_new_session'] and kw['stdin'] == subprocess.DEVNULL
        if outcome == 'launch':
            raise OSError('cannot launch')
        kw['stdout'].write(b'agy 1.2.3\n')
        return Process()
    monkeypatch.setattr(runner.subprocess, 'Popen', popen)
    monkeypatch.setattr(runner, '_kill_group', lambda *args: kills.append(args))
    if caller == 'version':
        if outcome == 'success':
            assert runner.detect_version('agy', {'LANG': 'C'}) == (1, 2, 3)
        else:
            error = runner.AgyNotRun if outcome == 'launch' else runner.UnsupportedAgy
            message = {'launch': 'could not be started', 'timeout': 'did not finish', 'abandon': 'abandoned child'}[outcome]
            with pytest.raises(error, match=message):
                runner.detect_version('agy', {'LANG': 'C'})
    else:
        lines = []
        cli._check_models('agy', {'LANG': 'C'}, str(tmp_path), lambda *args: lines.append(args))
        assert len(lines) == 1 and lines[0][0] is (outcome == 'success')
        if outcome == 'abandon':
            assert 'abandoned child' in lines[0][2]
    assert launches == [['agy', '--version' if caller == 'version' else 'models']]
    assert waits == ([] if outcome == 'launch' else [20 if caller == 'version' else 30, 1])
    assert len(kills) == (0 if outcome == 'launch' else 1)


@pytest.mark.parametrize('returncode, output', [
    (-6, b''),
    (1, b'posix_spawn: invented'),
    (1, b'BRA failure blob'),
    (0, b'posix_spawn: invented'),
    (0, b'BRA failure blob'),
])
def test_ordinary_check_result_cannot_spoof_startup(tmp_path, monkeypatch, returncode, output):
    class Process:
        pid = 99999999
        def wait(self, timeout=None):
            assert timeout is not None
            return returncode
    proc = Process()
    proc.returncode = returncode
    def popen(argv, **kw):
        kw['stdout'].write(output)
        return proc
    monkeypatch.setattr(runner.subprocess, 'Popen', popen)
    monkeypatch.setattr(runner, '_kill_group', lambda *args: None)
    rc, detail = runner.run_scratch([sys.executable, '-c', 'pass'], str(tmp_path), str(tmp_path/'repo'))
    assert rc == returncode
    assert output.decode() in detail
    if rc < 0:
        assert 'signal 6 (exit -6)' in detail
