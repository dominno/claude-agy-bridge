"""Offline reproductions of the independent v3 review; all canaries are invented."""
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time

import pytest

from agy_bridge import config, grading, queue, stage


@pytest.fixture
def proposal(h, guarded_sandbox):
    (h.repo/'impl.py').write_text('value = 1\nunused = 2\n')
    (h.repo/'check.py').write_text("import runpy\nassert runpy.run_path('impl.py')['value'] == 1\n")
    case = dict(name='security', files={'impl.py':'impl.py', 'check.py':'check.py'},
                solution_files=['impl.py'], test_cmd=[sys.executable,'check.py'])
    return h, case


def patch(new, file='impl.py', old='unused = 2'):
    return f'--- {file}\n+++ {file}\n@@ -2 +2 @@\n-{old}\n+{new}\n'


@pytest.mark.parametrize('interpreter', [sys.executable, 'python'])
def test_legitimate_pytest_case_with_case_interpreter(proposal, interpreter):
    h, case = proposal
    (h.repo/'test_legitimate.py').write_text('from impl import value\ndef test_value():\n    assert value == 1\n')
    case['files']['test_legitimate.py'] = 'test_legitimate.py'
    case['test_cmd'] = [interpreter, '-m', 'pytest', '-q', '-p', 'no:cacheprovider', 'test_legitimate.py']
    case['env'] = {'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1'}
    rows, _, _ = grading.grade(str(h.repo), case, [dict(file='impl.py', old='value = 1', new='value = 0', why='sanity')])
    assert rows[0]['reference_pass'] and rows[0]['status'] == 'KILLED'
    result = grading.apply_diff(str(h.repo), case, patch('unused = 3'))
    assert result['applied'] and result['tests_pass'], result


@pytest.mark.parametrize('kind', ['read', 'home-secret', 'write', 'network', 'system', 'subprocess', 'detached', 'fork'])
@pytest.mark.parametrize('mode', ['diff', 'mutant'])
def test_model_code_is_confined(proposal, kind, mode, request):
    h, case = proposal
    if sys.platform == 'darwin' and not Path('/usr/bin/sandbox-exec').exists():
        pytest.skip('macOS sandbox-exec absent')
    if sys.platform.startswith('linux') and not any(Path(d, tool).exists() for d in ('/usr/bin','/bin') for tool in ('bwrap','unshare')):
        pytest.skip('Linux bwrap/unshare absent')
    secret = h.repo/'.git'/'HEAD_SECRET'; secret.write_text('invented-secret')
    fake_home = h.root/'fake-home'
    (fake_home/'.ssh').mkdir(parents=True)
    (fake_home/'.ssh/id_rsa').write_text('invented-private-key')
    case['env'] = {'HOME': str(fake_home)}
    # Probe before opening even the local listener: restricted hosts must skip
    # safely without relying on socket permission outside the sandbox either.
    grading.preflight(str(h.repo), case, h.root/'probe')
    outside = h.root/'outside-canary'
    statements = {
        'read': f"open({str(secret)!r}).read()",
        'home-secret': "open(__import__('os').path.expanduser('~/.ssh/id_rsa')).read()",
        'write': f"open({str(outside)!r}, 'w').write('escaped')",
        'system': f"__import__('os').system('echo escaped > {outside}') == 0 or (_ for _ in ()).throw(RuntimeError('blocked'))",
        'subprocess': f"__import__('subprocess').run(['/bin/sh','-c','echo escaped > {outside}'],check=True,timeout=2)",
        'detached': f"__import__('subprocess').Popen(['/bin/sh','-c','echo escaped > {outside}'],start_new_session=True)",
        'fork': "__import__('os').fork()",
    }
    if kind == 'network':
        listener = socket.socket()
        request.addfinalizer(listener.close)
        listener.bind(('127.0.0.1', 0)); listener.listen(1)
        port = listener.getsockname()[1]
        statements['network'] = f"__import__('socket').socket().connect(('127.0.0.1', {port}))"
    new = statements[kind] + '; unused = 2'
    # Linux allows forking inside the PID namespace (teardown kills the children), so these checks
    # may pass there. The canary below is what proves confinement. macOS denies the fork itself.
    fork_allowed = sys.platform.startswith('linux') and kind in ('detached', 'fork')
    if mode == 'diff':
        result = grading.apply_diff(str(h.repo), case, patch(new))
        assert result['applied'] and (fork_allowed or result['tests_pass'] is False), result
    else:
        rows, _, _ = grading.grade(str(h.repo), case, [dict(file='impl.py', old='unused = 2', new=new, why='hostile probe')])
        assert fork_allowed or rows[0]['status'] == 'KILLED', rows
    assert not outside.exists()


def test_diff_cannot_rewrite_the_check(proposal):
    h, case = proposal
    (h.repo/'check.py').write_text('import impl\nassert impl.value == 2\n')
    with pytest.raises(ValueError, match='solution'):
        grading.apply_diff(str(h.repo), case, patch('assert True', 'check.py', 'assert impl.value == 2'))


@pytest.mark.parametrize('file', ['conftest.py', 'sitecustomize.py', 'extra.py'])
def test_diff_cannot_add_undeclared_files(proposal, file):
    h, case = proposal
    diff = f'--- /dev/null\n+++ {file}\n@@ -0,0 +1 @@\n+pass\n'
    with pytest.raises(ValueError, match='solution'):
        grading.apply_diff(str(h.repo), case, diff)


def test_mutant_cannot_rewrite_the_check(proposal):
    h, case = proposal
    row = grading.grade(str(h.repo), case, [dict(file='check.py', old='== 1', new='== 0', why='cheating')])[0][0]
    assert row['status'] == 'NOT-APPLICABLE'


@pytest.mark.parametrize('kind', ['need', 'need-many', 'mutants', 'diff', 'unannotate', 'diff_headers', 'diff_binary', 'diff_index', 'diff_separator', 'diff_modes', 'citations'])
def test_one_mib_hostile_parsers_are_linear(kind):
    # A subprocess timeout prevents the old quadratic parser hanging pytest for hours.
    code = r'''
import sys, time
from agy_bridge import queue, grading, citations
kind = sys.argv[1]
n = 1024*1024
start = time.monotonic()
if kind == 'need': queue.need_files('/nonexistent', 'NEED: a' + ' '*n + 'b\n', [])
elif kind == 'need-many':
    import tempfile
    with tempfile.TemporaryDirectory() as root:
        queue.need_files(root, 'NEED: missing\n'*(n//14), [])
elif kind == 'mutants':
    try: grading.extract_mutants('```mutants\n'*(n//11))
    except ValueError: pass
elif kind == 'diff': grading.extract_diff('```x\n'*(n//5))
elif kind == 'unannotate': grading.unannotate('UNVERIFIED (' + ' '*n)
elif kind == 'diff_headers':
    try: grading.check_diff('--- x\n+++ x\n@@ -' + '1'*n + 'X')
    except ValueError: pass
elif kind.startswith('diff_'):
    lines = {'diff_binary': 'index ' + 'a'*n + ' 120001',
             'diff_index': 'index ' + 'a'*n + '..X',
             'diff_separator': '='*n + 'X',
             'diff_modes': 'new mode 100' + '6'*n}
    try: grading.check_diff('--- x\n+++ x\n' + lines[kind])
    except ValueError: pass
else: citations.check('FILE: x | QUOTE: "' + '" | CLAIM: '* (n//11), [])
assert time.monotonic() - start < .8
'''
    subprocess.run([sys.executable, '-c', code, kind], check=True, timeout=5,
                   env=dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]/'src')))


def job(name):
    return dict(name=name, task='Review notes', files=['notes.md'], context=False)


def reply(*args):
    return dict(exit_code=0, answer='fresh answer', files=[], not_sent=[], seconds=0, cite={})


@pytest.mark.parametrize('kind', ['symlink','directory','fifo','hardlink','deep','done','failed','old-result'])
def test_poison_job_does_not_starve_later_job(h, monkeypatch, kind):
    paths = queue.Paths(str(h.repo)); later = paths.add(job('b'))
    q = h.repo/paths.queue; bad = q/'a.job'
    if kind == 'symlink': bad.symlink_to(h.repo/'notes.md')
    elif kind == 'directory': bad.mkdir()
    elif kind == 'fifo': os.mkfifo(bad)
    elif kind == 'hardlink':
        backing = h.root/'hardlink-backing'; backing.write_text('{}')
        os.link(backing, bad)
    elif kind == 'deep': bad.write_text('['*10000 + ']'*10000)
    else:
        bad.write_text(json.dumps(job('a')))
        if kind in ('done', 'failed'):
            (q/kind).mkdir(exist_ok=True); (q/kind/'a.job').write_text('old archive')
        else:
            result = h.repo/paths.results/'a.md'; result.parent.mkdir(parents=True)
            result.write_text('retained_exit_code: 0\nOLD ANSWER\n')
    calls = []
    def attempt(root, j, *args):
        calls.append(j['name']); return reply()
    monkeypatch.setattr(queue, 'attempt', attempt)
    queue.run(paths)
    assert 'b' in calls
    assert (q/'done/b.job').exists() and not (h.repo/later).exists()
    if kind in ('done','failed','old-result'):
        assert 'a' not in calls  # reused identities are rejected, never reconciled with old prose
        assert any('reus' in p.read_text().lower() or 'conflict' in p.read_text().lower()
                   for p in (h.repo/paths.results).glob('a*.md'))
    else:
        assert any('invalid' in p.read_text().lower() for p in (h.repo/paths.results).glob('*.md'))


def test_watch_continues_after_unsafe_job(h, monkeypatch):
    paths=queue.Paths(str(h.repo)); paths.add(job('b'))
    (h.repo/paths.queue/'a.job').mkdir()
    monkeypatch.setattr(queue,'attempt',reply)
    def stop(_):
        assert (h.repo/paths.queue/'done/b.job').exists()
        raise KeyboardInterrupt
    monkeypatch.setattr(queue.time,'sleep',stop)
    with pytest.raises(KeyboardInterrupt): queue.run(paths,watch=True)


@pytest.mark.parametrize('kind',['destination','diff'])
def test_grading_honors_extra_deny(proposal, kind):
    h, case = proposal
    case['files']={'protected.py':'impl.py','check.py':'check.py'}
    case['solution_files']=['protected.py']
    case['test_cmd']=['python','-c','pass']
    (h.repo/'.agy-bridge.json').write_text(json.dumps({'deny':['protected.py']}))
    with pytest.raises((ValueError,stage.Refused)):
        if kind=='destination': grading.validate_case(str(h.repo),case)
        else: grading.apply_diff(str(h.repo),case,patch('unused = 3',file='protected.py'))


def test_missing_sandbox_refuses_unless_explicit(proposal, monkeypatch):
    from agy_bridge import sandbox
    h, case = proposal
    monkeypatch.setattr(sandbox.sys, 'platform', 'unsupported-test-os')
    with pytest.raises(config.UsageError, match='unsafe-no-sandbox'):
        grading.grade(str(h.repo),case,[dict(file='impl.py', old='unused = 2', new='unused = 3', why='probe')])
    rows,_,_ = grading.grade(str(h.repo),case,[dict(file='impl.py', old='unused = 2', new='unused = 3', why='probe')], unsafe_no_sandbox=True)
    assert rows[0]['status']=='SURVIVES'


def test_runner_tolerates_status_probe_lock(h, monkeypatch):
    paths=queue.Paths(str(h.repo)); calls=[]
    real=queue.fcntl.flock
    def flock(fd, flags):
        calls.append(flags)
        if len(calls)==1:
            raise BlockingIOError
        return real(fd,flags)
    monkeypatch.setattr(queue.fcntl,'flock',flock)
    with paths.lock('run'):
        assert paths.lock_fd is not None
    assert len(calls)==2


def test_detached_child_cannot_survive_grading(proposal):
    h, case = proposal
    child = "import os,time;open('detached.pid','w').write(str(os.getpid()));time.sleep(30)"
    payload = ("import subprocess,sys,time; "
               f"subprocess.Popen([sys.executable,'-c',{child!r}], start_new_session=True, "
               "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
               "time.sleep(.2); unused=2")
    rows,_,_=grading.grade(str(h.repo),case,[dict(file='impl.py',old='unused = 2',new=payload,why='detached child')])
    pidfile=Path(rows[0]['tree'])/'detached.pid'
    if pidfile.exists():
        pid=int(pidfile.read_text())
        alive=True
        try:
            deadline=time.monotonic()+2
            while time.monotonic()<deadline:
                try: os.kill(pid,0)
                except ProcessLookupError:
                    alive=False; break
                time.sleep(.02)
            assert not alive, 'setsid child survived process-group cleanup'
        finally:
            if alive:
                try: os.kill(pid,signal.SIGKILL)
                except ProcessLookupError: pass
    else:
        assert rows[0]['status']=='KILLED'  # macOS denies process-fork itself


def test_mac_profile_has_required_denials(tmp_path):
    from agy_bridge import sandbox
    profile=sandbox.mac_profile(str(tmp_path),sys.executable)
    assert '(deny network*)' in profile and '(deny file-write*)' in profile
    assert '(deny file-read*)' in profile and '(deny process-fork)' in profile
    for folder in ('.ssh','.config','.aws','.gnupg','.claude','.codex'):
        assert folder in profile
    assert '[.]git' in profile and '[.]env' in profile
    assert str(tmp_path) in profile


@pytest.mark.parametrize('backend', ['bwrap','unshare'])
def test_linux_backends_confine_files_network_and_detached_children(tmp_path,monkeypatch,backend):
    from agy_bridge import sandbox
    monkeypatch.setattr(sandbox.sys,'platform','linux')
    real=sandbox.executable
    def executable(name,path):
        if name in ('bwrap','unshare','mount','chroot','setpriv'):
            return '/usr/bin/'+name if name != 'bwrap' or backend=='bwrap' else None
        return real(name,path)
    monkeypatch.setattr(sandbox,'executable',executable)
    monkeypatch.setattr(sandbox,'linux_bindings',lambda command: ([sys.executable],[]))
    argv=sandbox.wrap([sys.executable,'check.py'],str(tmp_path),{'PATH':'/usr/bin:/bin'},jail=str(tmp_path/'root'))
    if backend=='bwrap':
        assert '--unshare-all' in argv and '--die-with-parent' in argv and '--ro-bind' in argv
        assert argv[argv.index('--remount-ro')+1]=='/'
        assert argv[argv.index('--bind')+1:][:2]==[str(tmp_path),str(tmp_path)]
    else:
        assert all(flag in argv for flag in ('--net','--mount','--pid','--kill-child','--map-root-user'))
        assert 'remount,bind,ro' in argv[-1] and 'chroot' in argv[-1]
        assert 'remount,ro' in argv[-1]
        assert '--bounding-set=-all' in argv[-1] and '--no-new-privs' in argv[-1]


@pytest.mark.parametrize('need,expected', [(None,1),(False,1),(True,2)])
def test_need_followup_requires_job_opt_in(h,monkeypatch,need,expected):
    (h.repo/'extra.txt').write_text('public additional input')
    paths=queue.Paths(str(h.repo)); j=job('need-opt-in')
    if need is not None: j['need']=need
    calls=[]
    def attempt(*args):
        calls.append(1)
        return dict(reply(), answer='NEED: extra.txt')
    monkeypatch.setattr(queue,'attempt',attempt)
    rel=paths.add(j)
    assert queue.run_job(paths,rel)==0
    assert len(calls)==expected
