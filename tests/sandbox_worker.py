"""Test-only outer process boundary for real sandbox runs (never uses pipes)."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile


def bounded_python(code, args=(), timeout=25):
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]/'src'))
    with tempfile.TemporaryFile() as output:
        proc = subprocess.Popen([sys.executable, '-c', code, *args], env=env,
                                stdout=output, stderr=output, start_new_session=True)
        try:
            proc.wait(timeout=timeout)
        finally:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                raise AssertionError('sandbox test worker did not exit; abandoned child') from None
        output.seek(0)
        result = output.read().decode('utf-8', 'replace')
        assert proc.returncode == 0, result
        return result


def scratch_call(argv, tree, repo, extra_env=None, **kwargs):
    markers = kwargs.get('output_markers')
    payload = json.dumps([argv, tree, repo, extra_env, kwargs])
    code = '''
import json, sys
from agy_bridge import runner, sandbox
argv, tree, repo, env, kw = json.loads(sys.argv[1])
try:
    result = runner.run_scratch(argv, tree, repo, env, **kw)
    print(json.dumps(dict(result=result, markers=kw.get('output_markers'))))
except sandbox.Unavailable as exc:
    print(json.dumps(dict(unavailable=str(exc))))
'''
    # The outer guard is deliberately shorter than the production 300s limit.
    response = json.loads(bounded_python(code, [payload]))
    if markers is not None:
        markers.update(response.get('markers') or {})
    return response


if __name__ == '__main__':
    raise SystemExit('test helper; not a standalone sandbox bypass')
