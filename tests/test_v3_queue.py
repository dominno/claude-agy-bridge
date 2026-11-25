import json
import os
import signal
import subprocess
import sys
import time

import pytest

from agy_bridge.app import queue_commands
from agy_bridge.app import queue, queue_store, verdicts
from agy_bridge.core import queue_job
from agy_bridge.core import config, stage


def job(**kw):
    return dict(dict(name='example', model='fake', task='review', files=['notes.md'], timeout='5s', context=False), **kw)


def enqueue(h, **kw):
    path = h.root / 'input.json'
    path.write_text(json.dumps(job(**kw)))
    return h.raw('queue', 'add', str(path), '--repo', str(h.repo))


def test_queue_real_delegate_path_and_verdict(h):
    assert enqueue(h).returncode == 0
    r = h.raw('queue', 'run', '--repo', str(h.repo))
    assert r.returncode == 0, r.stderr
    base = h.repo / '.agy-bridge'
    assert (base/'queue/done/example.job').exists()
    text = (base/'results/example.md').read_text()
    for token in ['unverified model output', 'model: fake', 'bytes: 11', 'files sent:', 'notes.md', 'VERIFIED:', 'UNVERIFIED:']:
        assert token in text
    assert set(h.record()['env']) - {'__CF_USER_TEXT_ENCODING'} == set(config.ALLOWED_ENV_NAMES)
    r = h.raw('status', '--repo', str(h.repo))
    assert 'example' in r.stdout and 'without verdict' in r.stdout
    assert h.raw('verdict', '--repo', str(h.repo), '--job', 'example', '--verdict', 'pass', '--by', 'reader', '--evidence', 'checked files').returncode == 0
    r = h.raw('verdicts', '--repo', str(h.repo))
    assert json.loads(r.stdout)['job'] == 'example'
    assert 'without verdict: none' in h.raw('status', '--repo', str(h.repo)).stdout


@pytest.mark.parametrize('fields', [dict(extra='oops'), dict(name='../x'), dict(name='a\nb'), dict(task=''), dict(files=['../x']), dict(files=['.env']), dict(files=['.git/config']), dict(files=[]), dict(timeout=0), dict(cite=1), dict(context='all'), dict(model='--bad'), dict(files='notes.md')])
def test_invalid_jobs_rejected(h, fields):
    assert enqueue(h, **fields).returncode in (1, 4)
    assert not h.record_path.exists()


@pytest.mark.parametrize('option', ['--queue-dir', '--results-dir'])
@pytest.mark.parametrize('value', ['../escape', '.git/queue', '.env/jobs', 'node_modules/jobs'])
def test_directories_confined(h, option, value):
    assert h.raw('queue', 'status', '--repo', str(h.repo), option, value).returncode in (1, 4)


def test_links_refused_and_add_never_overwrites(h):
    (h.repo/'link.md').symlink_to(h.repo/'notes.md')
    assert enqueue(h, files=['link.md']).returncode == 4
    assert enqueue(h).returncode == 0
    assert enqueue(h).returncode != 0
    (h.repo/'other').symlink_to(h.root)
    assert h.raw('queue', 'status', '--repo', str(h.repo), '--queue-dir', 'other/queue').returncode == 4


def test_config_context_is_prepend_only(h):
    (h.repo/'facts.md').write_text('public facts')
    (h.repo/'.agy-bridge.json').write_text(json.dumps(dict(context_files=['facts.md', 'src/a.py'])))
    assert enqueue(h, context='short').returncode == 0
    assert h.raw('queue','run','--repo',str(h.repo)).returncode == 0
    event = json.loads((h.repo/'.agy-bridge/events.jsonl').read_text().splitlines()[-1])
    assert [e['path'] for e in event['files']] == ['facts.md', 'notes.md']


def test_need_safety_and_limits(h):
    for i in range(5):
        (h.repo/f'{i}.txt').write_text('public')
    (h.repo/'link').symlink_to(h.repo/'0.txt')
    text = '\n'.join('NEED: '+p for p in ['../x','/etc/passwd','.git/config','.env','link','missing','0.txt','0.txt','1.txt','2.txt','3.txt'])
    assert queue.need_files(str(h.repo), text, []) == ['0.txt','1.txt','2.txt']


def test_quota_backoff_and_timeout(monkeypatch, h):
    calls, sleeps = [], []
    def attempt(root, j, files, task, allow=False):
        calls.append(files)
        return dict(exit_code=10, answer='', files=[], not_sent=[], seconds=1, total_bytes=0, cite={})
    monkeypatch.setattr(queue, 'attempt', attempt)
    monkeypatch.setattr(queue.time, 'sleep', sleeps.append)
    paths = queue.Paths(str(h.repo))
    p = paths.add(job())
    assert queue.run_job(paths, p) == 10
    assert len(calls) == 4 and sleeps == [5,10,20]
    calls.clear(); sleeps.clear()
    monkeypatch.setattr(queue, 'attempt', lambda *a: dict(attempt(*a), exit_code=12))
    p = paths.add(job(name='timeout'))
    assert queue.run_job(paths, p) == 12
    assert len(calls) == 1 and sleeps == []


def test_drop_and_failed_second_round_preserves_answer(monkeypatch, h):
    (h.repo/'extra.txt').write_text('more facts')
    replies = [
        dict(exit_code=4, answer='', not_sent=[dict(path='notes.md',rule='R5')]),
        dict(exit_code=0, answer='first answer\nNEED: extra.txt', not_sent=[]),
        dict(exit_code=12, answer='', not_sent=[])]
    calls=[]
    def attempt(root,j,files,task,allow=False):
        calls.append(list(files))
        return dict(files=[dict(path=p,bytes=3) for p in files], total_bytes=3*len(files), seconds=2.5,cite={}, **replies.pop(0))
    monkeypatch.setattr(queue,'attempt',attempt)
    paths=queue.Paths(str(h.repo)); p=paths.add(job(files=['notes.md','src/a.py'],need=True))
    assert queue.run_job(paths,p)==0
    text=(h.repo/'.agy-bridge/results/example.md').read_text()
    assert 'round 2 failed' in text and 'first answer' in text and 'FILES NOT SENT: notes.md' in text
    assert calls == [['notes.md','src/a.py'],['src/a.py'],['src/a.py','extra.txt']]


def test_lock_excludes_second_runner(h):
    paths=queue.Paths(str(h.repo))
    with paths.lock('watch'):
        assert h.raw('queue','watch','--repo',str(h.repo)).returncode == 4
        assert 'watcher: running' in h.raw('status','--repo',str(h.repo)).stdout
    assert 'watcher: stopped' in h.raw('status','--repo',str(h.repo)).stdout


@pytest.mark.parametrize('target', ['verdicts.jsonl', 'queue', 'results'])
def test_metadata_symlinks_refused(h,target):
    base=h.repo/'.agy-bridge'; base.mkdir()
    (base/target).symlink_to(h.root/'outside')
    r = h.raw('verdict','--repo',str(h.repo),'--job','x','--verdict','pass','--by','reader','--evidence','checked','--force') if target.startswith('verdict') else h.raw('status','--repo',str(h.repo))
    assert r.returncode == 4


def test_interrupt_leaves_job_and_releases_lock(h):
    assert enqueue(h).returncode == 0
    h.configure(mode='timeout')
    from conftest import BIN
    p=subprocess.Popen([sys.executable,BIN,'queue','watch','--repo',str(h.repo),'--poll','0.1'],env=h.env(),stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    try:
        deadline=time.monotonic()+10
        while not h.record_path.exists() and time.monotonic()<deadline:
            time.sleep(.05)
        p.send_signal(signal.SIGTERM)
        p.communicate(timeout=10)
        assert p.returncode == 130
        assert (h.repo/'.agy-bridge/queue/example.job').exists()
        assert 'watcher: stopped' in h.raw('status','--repo',str(h.repo)).stdout
    finally:
        if p.poll() is None: p.kill(); p.wait()


def test_status_empty_has_no_side_effect(h):
    r=h.raw('status','--repo',str(h.repo))
    assert r.returncode==0 and 'pending: 0' in r.stdout
    assert not (h.repo/'.agy-bridge').exists()


@pytest.mark.parametrize('context,expected',[(True,['facts.md','src/a.py','notes.md']),(False,['notes.md']),('short',['facts.md','notes.md'])])
def test_context_modes(h,context,expected):
    (h.repo/'facts.md').write_text('public facts')
    (h.repo/'.agy-bridge.json').write_text(json.dumps(dict(context_files=['facts.md','src/a.py'])))
    assert enqueue(h,context=context).returncode==0
    assert h.raw('queue','run','--repo',str(h.repo)).returncode==0
    row=json.loads((h.repo/'.agy-bridge/events.jsonl').read_text().splitlines()[-1])
    assert [f['path'] for f in row['files']]==expected


@pytest.mark.parametrize('fields',[{'command':['touch','x']},{'context_files':['notes.md']},{'env':{'PATH':'x'}}])
def test_job_cannot_select_execution(h,fields):
    assert enqueue(h,**fields).returncode==1


@pytest.mark.parametrize('context_files',[['../escape'],['.env'],['.git/config'],['/etc/passwd'],['missing'],[42]])
def test_config_context_paths_safe(h,context_files):
    (h.repo/'.agy-bridge.json').write_text(json.dumps(dict(context_files=context_files)))
    assert enqueue(h,context=True).returncode in (1,4)
    assert not h.record_path.exists()


def test_result_reconciliation_avoids_repeat_send(h):
    assert enqueue(h).returncode==0
    assert h.raw('queue','run','--repo',str(h.repo)).returncode==0
    base=h.repo/'.agy-bridge'
    before=(base/'events.jsonl').read_bytes()
    (base/'queue/done/example.job').rename(base/'queue/example.job')
    assert h.raw('queue','run','--repo',str(h.repo)).returncode==0
    assert (base/'events.jsonl').read_bytes()==before


def test_real_scan_drop_has_omission_evidence(h):
    # Construct an invented key shape; it must never enter staged files or a result.
    key='sk-'+'q'*32
    (h.repo/'bad.txt').write_text(key)
    assert enqueue(h,files=['bad.txt','notes.md']).returncode==0
    assert h.raw('queue','run','--repo',str(h.repo)).returncode==0
    text=(h.repo/'.agy-bridge/results/example.md').read_text()
    assert 'FILES NOT SENT: bad.txt (R5)' in text and key not in text
    assert 'bad.txt' not in h.record()['listing']


def test_job_order_and_no_double_run(h):
    assert enqueue(h,name='z-last').returncode==0
    assert enqueue(h,name='a-first').returncode==0
    r=h.raw('queue','run','--repo',str(h.repo))
    assert r.returncode==0 and r.stdout.index('a-first')<r.stdout.index('z-last')
    assert h.raw('queue','run','--repo',str(h.repo)).returncode==0
    assert len((h.repo/'.agy-bridge/events.jsonl').read_text().splitlines())==2


def test_invalid_manual_job_cannot_starve_next(h):
    assert enqueue(h).returncode==0
    q=h.repo/'.agy-bridge/queue'
    (q/'000-invalid.job').write_text('{broken')
    r=h.raw('queue','run','--repo',str(h.repo))
    assert r.returncode==1 and (q/'failed/000-invalid.job').exists() and (q/'done/example.job').exists()


@pytest.mark.parametrize('kind',['symlink','hardlink','fifo','directory'])
def test_verdict_journal_hostile_files(h,kind):
    base=h.repo/'.agy-bridge'; base.mkdir()
    target=base/'verdicts.jsonl'; outside=h.root/'outside'; outside.write_text('untouched')
    if kind=='symlink': target.symlink_to(outside)
    elif kind=='hardlink': os.link(outside,target)
    elif kind=='fifo': os.mkfifo(target)
    else: target.mkdir()
    r=h.raw('verdict','--repo',str(h.repo),'--job','x','--verdict','reject','--by','r','--evidence','checked','--force')
    assert r.returncode==4 and outside.read_text()=='untouched'


def test_status_active_job_threshold(h):
    paths=queue.Paths(str(h.repo))
    with paths.lock('watch'):
        paths.state('busy')
        assert 'busy' in queue.status(paths,0) and 'exceeds threshold' in queue.status(paths,0)


def test_lock_symlink_refused(h):
    q=h.repo/'.agy-bridge/queue'; q.mkdir(parents=True)
    outside=h.root/'outside'; outside.write_text('untouched')
    (q/'watcher.pid').symlink_to(outside)
    assert h.raw('queue','watch','--repo',str(h.repo)).returncode==4
    assert outside.read_text()=='untouched'


def test_verdict_append_only_and_escaped_controls(h):
    args=['verdict','--repo',str(h.repo),'--job','x','--by','reader','--evidence','checked\x1b[31m','--force']
    assert h.raw(*args,'--verdict','fixes').returncode==0
    path=h.repo/'.agy-bridge/verdicts.jsonl'; first=path.read_bytes()
    assert h.raw(*args,'--verdict','pass').returncode==0
    assert path.read_bytes().startswith(first) and len(path.read_text().splitlines())==2
    assert '\x1b' not in h.raw('verdicts','--repo',str(h.repo)).stdout
    assert path.stat().st_mode & 0o777==0o600


def test_two_rounds_retry_counters_bounded(h,monkeypatch):
    for n in range(4): (h.repo/f'{n}.txt').write_text('safe')
    paths=queue.Paths(str(h.repo)); calls=[]
    replies=[(4,'0.txt',''),(4,'1.txt',''),(0,None,'NEED: 3.txt'),(4,'2.txt',''),(0,None,'done')]
    def attempt(root,j,files,task,allow=False):
        rc,bad,answer=replies.pop(0); calls.append(list(files))
        return dict(exit_code=rc,answer=answer,files=[],not_sent=[dict(path=bad,rule='R5')] if bad else [],seconds=1,cite={})
    monkeypatch.setattr(queue,'attempt',attempt)
    p=paths.add(job(files=['0.txt','1.txt','2.txt','notes.md'],need=True))
    assert queue.run_job(paths,p)==0
    assert calls==[['0.txt','1.txt','2.txt','notes.md'],['1.txt','2.txt','notes.md'],['2.txt','notes.md'],['2.txt','notes.md','3.txt'],['notes.md','3.txt']]
    assert not replies


def test_key_shaped_job_name_not_echoed(h):
    name='sk-'+'q'*32
    r=enqueue(h,name=name)
    assert r.returncode==1 and name not in r.stdout+r.stderr


def test_long_answer_kept_in_queue_result(h):
    text='long answer line\n'*1000
    h.configure(answer=text)
    assert enqueue(h).returncode==0
    assert h.raw('queue','run','--repo',str(h.repo)).returncode==0
    result=(h.repo/'.agy-bridge/results/example.md').read_text()
    assert result.count('long answer line')==1000
    assert 'more characters not shown' not in result


def test_verdict_status_uses_entire_history(h):
    from agy_bridge.core import events
    fd=events.open_log(str(h.repo),name='verdicts.jsonl')
    try:
        events.append(fd,dict(job='old',verdict='pass',by='r',evidence='checked'))
        for n in range(10001):
            events.append(fd,dict(job='new',verdict='fixes',by='r',evidence='checked'))
    finally: os.close(fd)
    folder=h.repo/'.agy-bridge/results'; folder.mkdir()
    (folder/'old.md').write_text('result')
    assert 'without verdict: none' in h.raw('status','--repo',str(h.repo)).stdout


def test_verdict_cannot_create_an_unreadable_oversized_record(h):
    text='☃'*8000
    with pytest.raises(config.UsageError):
        verdicts.record(str(h.repo),'example','pass',text,text,text)
    assert not (h.repo/'.agy-bridge/verdicts.jsonl').exists()


def test_job_path_with_control_characters_is_refused_at_add(h):
    (h.repo / 'a\nb.txt').write_text('safe')
    paths = queue.Paths(str(h.repo))
    with pytest.raises(config.UsageError, match='control characters'):
        paths.add(job(files=['a\nb.txt']))
    assert not (h.repo / '.agy-bridge/queue/example.job').exists()


def test_not_sent_path_is_escaped_in_the_result_header(h, monkeypatch):
    paths = queue.Paths(str(h.repo))
    evil = 'x\nexit_code: 0\n\x1b[2Jy.txt'

    def attempt(root, j, files, task, allow=False):
        return dict(exit_code=0, answer='fine', files=[dict(path='notes.md', bytes=11)],
                    not_sent=[dict(path=evil, rule='R1')], seconds=1, cite={})
    monkeypatch.setattr(queue, 'attempt', attempt)
    rel = paths.add(job())
    assert queue.run_job(paths, rel) == 0
    text = (h.repo / '.agy-bridge/results/example.md').read_text()
    assert 'FILES NOT SENT: x\\nexit_code: 0\\n\\u001b[2Jy.txt (R1)' in text
    assert '\x1b' not in text
    assert [line for line in text.splitlines() if line.startswith('exit_code: 0')] == ['exit_code: 0', 'exit_code: 0']


def test_queue_add_dry_run_shows_context_files_and_adds_nothing(h):
    (h.repo / 'docs').mkdir()
    (h.repo / 'docs/ctx.md').write_text('context\n')
    (h.repo / '.agy-bridge.json').write_text(json.dumps({'context_files': ['docs/ctx.md']}))
    path = h.root / 'input.json'
    path.write_text(json.dumps(job(context=True, need=True)))
    r = h.raw('queue', 'add', '--dry-run', str(path), '--repo', str(h.repo))
    assert r.returncode == 0, r.stderr
    assert 'context files from .agy-bridge.json: docs/ctx.md' in r.stdout and 'NEED round: yes' in r.stdout
    manifest = r.stdout.split('agy command')[0]
    assert 'docs/ctx.md' in manifest.split('Would send')[1] and 'notes.md' in manifest.split('Would send')[1]
    assert 'No file contents or task text have been sent' in r.stdout
    assert not (h.repo / '.agy-bridge/queue').exists() and not h.record_path.exists()
    path.write_text(json.dumps(job(context=False)))
    r = h.raw('queue', 'add', '--dry-run', str(path), '--repo', str(h.repo))
    assert 'context files from .agy-bridge.json: none' in r.stdout and 'NEED round: no' in r.stdout
    assert 'docs/ctx.md' not in r.stdout.split('Would send')[1]


def test_queue_add_dry_run_applies_the_scan(h):
    (h.repo / 'cfg.py').write_text("password = 'abcdefghij0123456789'\n")
    path = h.root / 'input.json'
    path.write_text(json.dumps(job(files=['notes.md', 'cfg.py'])))
    r = h.raw('queue', 'add', '--dry-run', str(path), '--repo', str(h.repo))
    assert r.returncode == 4 and 'R5' in r.stderr + r.stdout
    assert 'abcdefghij' not in r.stdout + r.stderr
    assert not (h.repo / '.agy-bridge/queue').exists()


def test_interrupted_add_leaves_no_job_and_the_name_stays_free(h, monkeypatch):
    from agy_bridge.port import safety
    paths = queue.Paths(str(h.repo))

    def interrupted(fd, data):
        os.write(fd, data[:5])
        raise KeyboardInterrupt
    monkeypatch.setattr(safety, '_write_all', interrupted)
    with pytest.raises(KeyboardInterrupt):
        paths.add(job())
    q = h.repo / '.agy-bridge/queue'
    assert not (q / 'example.job').exists()
    assert not [n for n in os.listdir(q) if n.startswith(safety.TEMP_PREFIX)]
    monkeypatch.undo()
    rel = paths.add(job())
    assert json.loads((h.repo / rel).read_text())['name'] == 'example'
    with pytest.raises(config.UsageError, match='already exists'):
        paths.add(job())


def test_stale_queue_temp_is_swept_and_never_run(h):
    from agy_bridge.port import safety
    q = h.repo / '.agy-bridge/queue'
    q.mkdir(parents=True)
    stale = q / (safety.TEMP_PREFIX + 'ghost.job-0011223344556677')
    stale.write_text(json.dumps(job(name='ghost')))
    old = time.time() - 7200
    os.utime(stale, (old, old))
    fresh = q / (safety.TEMP_PREFIX + 'young.job-8899aabbccddeeff')
    fresh.write_text('{}')
    r = h.raw('queue', 'run', '--repo', str(h.repo))
    assert r.returncode == 0, r.stderr
    assert not stale.exists() and fresh.exists()
    assert 'ghost' not in r.stdout and not h.record_path.exists()
    assert not (h.repo / '.agy-bridge/results/ghost.md').exists()


def test_refusal_result_is_complete_or_absent(h, monkeypatch):
    from agy_bridge.port import safety
    paths = queue.Paths(str(h.repo))
    q = h.repo / '.agy-bridge/queue'
    q.mkdir(parents=True)
    (q / 'broken.job').write_text('{broken')
    real = safety._write_all

    def interrupted(fd, data):
        real(fd, data[:7])
        raise KeyboardInterrupt
    monkeypatch.setattr(safety, '_write_all', interrupted)
    with pytest.raises(KeyboardInterrupt):
        paths.failure(paths.queue + '/broken.job', 'invalid input')
    results = h.repo / '.agy-bridge/results'
    assert not results.exists() or os.listdir(results) == []
    monkeypatch.setattr(safety, '_write_all', real)
    (q / 'again.job').write_text('{broken')
    paths.failure(paths.queue + '/again.job', 'invalid input')
    assert (results / 'again.md').read_text() == (
        '# Queue refusal\n<!-- agy-bridge-refusal-v1 sent: no -->\n\ninvalid job: invalid input; archived as failed\n')


def test_unknown_field_is_named_with_the_allowed_list(h):
    path = h.root / 'input.json'
    path.write_text(json.dumps({'id': 'x'}))
    r = h.raw('queue', 'add', str(path), '--repo', str(h.repo))
    assert r.returncode == 1
    assert 'unknown job field(s): id; allowed: ' + ', '.join(queue_job.JOB_FIELDS) in r.stderr
    path.write_text('[1]')
    r = h.raw('queue', 'add', str(path), '--repo', str(h.repo))
    assert r.returncode == 1 and 'job is not a JSON object' in r.stderr


def test_missing_job_file_is_named(h):
    r = h.raw('queue', 'add', str(h.root / 'nope.json'), '--repo', str(h.repo))
    assert r.returncode == 1 and 'job file not found: ' in r.stderr and 'nope.json' in r.stderr
    assert not (h.repo / '.agy-bridge/queue').exists()


def test_bad_job_path_is_echoed(h):
    r = enqueue(h, files=['../outside.md'])
    assert r.returncode == 1 and '../outside.md' in r.stderr
    with pytest.raises(config.UsageError, match=r'traversal: \.\./\\u017c\.md'):
        queue.validate(str(h.repo), job(files=['../\u017c.md']))


def test_nonexistent_repo_is_echoed(h):
    r = h.raw('queue', 'status', '-C', str(h.root / 'missing-repo'))
    assert r.returncode == 1 and '--repo is not a directory: ' in r.stderr and 'missing-repo' in r.stderr


def test_inherited_sighup_ignore_is_kept_by_the_stop_handlers():
    from conftest import BIN
    code = ("import signal\nfrom agy_bridge.cli import signals\nsignals.install_stop_handlers()\n"
            "print(signal.getsignal(signal.SIGHUP) is signal.SIG_IGN, "
            "signal.getsignal(signal.SIGTERM) is signal.SIG_IGN)\n")
    out = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, timeout=30,
                         preexec_fn=lambda: signal.signal(signal.SIGHUP, signal.SIG_IGN),
                         env=dict(os.environ, PYTHONPATH=os.path.dirname(BIN)))
    assert out.stdout.split() == ['True', 'False'], out.stderr


def _watcher_pid(stdout):
    import re
    match = re.search(r'watcher started pid (\d+); log (.+); stop with: kill (\d+)', stdout)
    assert match and match.group(1) == match.group(3), stdout
    return int(match.group(1)), match.group(2)


def _stop(pid):
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(.05)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def test_watch_detach_survives_the_parent_holds_the_lock_and_drains_jobs(h):
    r = h.raw('queue', 'watch', '--detach', '--repo', str(h.repo), '--poll', '0.1')
    pid = None
    try:
        assert r.returncode == 0, r.stderr
        pid, log = _watcher_pid(r.stdout)
        assert log == str(h.repo / '.agy-bridge/queue/watcher.log')
        info = os.stat(log)
        assert oct(info.st_mode & 0o777) == oct(0o600)
        os.kill(pid, 0)  # the parent exited; the watcher is still alive
        status = h.raw('status', '--repo', str(h.repo)).stdout
        assert 'watcher: running (pid %d, watch)' % pid in status
        again = h.raw('queue', 'watch', '--detach', '--repo', str(h.repo))
        assert again.returncode == 4 and 'R6' in again.stderr and 'active runner' in again.stderr
        assert enqueue(h).returncode == 0
        deadline = time.monotonic() + 30
        while not (h.repo / '.agy-bridge/queue/done/example.job').exists() and time.monotonic() < deadline:
            time.sleep(.1)
        assert (h.repo / '.agy-bridge/results/example.md').exists()
        assert 'example.job: exit 0' in open(log).read()
    finally:
        if pid:
            _stop(pid)
    assert 'watcher: stopped' in h.raw('status', '--repo', str(h.repo)).stdout


def test_watch_detach_passes_custom_folders(h):
    r = h.raw('queue', 'watch', '--detach', '--repo', str(h.repo), '--queue-dir', 'jobs/q',
              '--results-dir', 'jobs/out', '--poll', '0.1')
    pid = None
    try:
        assert r.returncode == 0, r.stderr
        pid, log = _watcher_pid(r.stdout)
        assert log == str(h.repo / 'jobs/q/watcher.log')
        assert 'pid %d' % pid in h.raw('status', '--repo', str(h.repo), '--queue-dir', 'jobs/q',
                                       '--results-dir', 'jobs/out').stdout
        assert 'watcher: stopped' in h.raw('status', '--repo', str(h.repo)).stdout
    finally:
        if pid:
            _stop(pid)


def test_watch_detach_refuses_while_a_runner_holds_the_lock(h):
    paths = queue.Paths(str(h.repo))
    with paths.lock('run'):
        r = h.raw('queue', 'watch', '--detach', '--repo', str(h.repo))
    assert r.returncode == 4 and 'active runner' in r.stderr
    assert not (h.repo / '.agy-bridge/queue/watcher.log').exists()


def test_watch_detach_refuses_a_symlinked_log(h):
    paths = queue.Paths(str(h.repo))
    (h.repo / '.agy-bridge/queue').mkdir(parents=True)
    (h.repo / '.agy-bridge/queue/watcher.log').symlink_to(h.root / 'elsewhere.log')
    with pytest.raises(OSError):
        queue_commands.detach_watcher(paths, 1)
    assert not (h.root / 'elsewhere.log').exists()


def test_watch_detach_stops_the_child_when_it_misses_the_lock_deadline(h, monkeypatch, capsys):
    """A start reported as failed after the timeout leaves no watcher behind to drain the queue."""
    import re
    paths = queue.Paths(str(h.repo))
    monkeypatch.setattr(queue_commands, 'DETACH_WAIT_S', 0.0)
    assert queue_commands.detach_watcher(paths, 1) == 2
    err = capsys.readouterr().err
    assert 'did not take the queue lock' in err and 'was stopped' in err
    pid = int(re.search(r'watcher pid (\d+)', err).group(1))
    with pytest.raises(OSError):
        os.kill(pid, 0)
    assert paths.running() is None


def test_invalid_job_result_names_the_specific_reason_and_next_step(h):
    (h.repo / 'f.txt').write_text('content\n')
    assert enqueue(h, name='gone', files=['f.txt']).returncode == 0
    (h.repo / 'f.txt').unlink()
    r = h.raw('queue', 'run', '--repo', str(h.repo))
    assert r.returncode == 1
    assert 'gone.job: invalid job (file not found: f.txt); archived as failed' in r.stdout
    nxt = 'next: fix the job and re-add it under a new name: agy-bridge queue add -C %s <job.json>' % h.repo
    assert nxt in r.stdout
    text = (h.repo / '.agy-bridge/results/gone.md').read_text()
    assert 'invalid job: file not found: f.txt; archived as failed\n' + nxt in text
    assert text.splitlines()[:2] == ['# Queue refusal', '<!-- agy-bridge-refusal-v1 sent: no -->']
    assert (h.repo / '.agy-bridge/queue/failed/gone.job').exists()


def test_hand_placed_malformed_job_reports_invalid_json(h):
    q = h.repo / '.agy-bridge/queue'
    q.mkdir(parents=True)
    (q / 'broken.job').write_text('{"name": "broken", AKIAABCDEFGHIJKLMNOP')
    r = h.raw('queue', 'run', '--repo', str(h.repo))
    assert 'broken.job: invalid job (invalid JSON); archived as failed' in r.stdout
    text = (h.repo / '.agy-bridge/results/broken.md').read_text()
    assert 'invalid job: invalid JSON; archived as failed' in text and 'AKIA' not in text + r.stdout


def test_invalid_reason_is_escaped_and_redacted():
    from agy_bridge.core import stage
    assert queue.invalid_reason(config.UsageError('bad\nexit_code: 0')) == 'bad\\nexit_code: 0'
    assert queue.invalid_reason(config.UsageError('value AKIAABCDEFGHIJKLMNOP')) == 'invalid input (detail redacted)'
    refused = stage.Refused([stage.Refusal('R1', 'path is on the denylist', '.env')])
    assert queue.invalid_reason(refused) == 'Rule R1: path is on the denylist (.env)'
    assert queue.invalid_reason(config.UsageError('job name already exists; conflict')) == 'reused name conflict'


def test_queue_cancel_archives_the_pending_job_and_a_run_sends_nothing(h):
    assert enqueue(h).returncode == 0
    r = h.raw('queue', 'cancel', 'example', '--repo', str(h.repo), '--reason', 'task dropped')
    assert r.returncode == 0, r.stderr
    assert 'cancelled example; archived as failed; nothing was sent' in r.stdout
    base = h.repo / '.agy-bridge'
    assert not (base / 'queue/example.job').exists() and (base / 'queue/failed/example.job').exists()
    text = (base / 'results/example.md').read_text()
    assert text.startswith('# Queue refusal') and 'cancelled by reader: task dropped; archived as failed' in text
    assert text.splitlines()[1] == queue_job.REFUSAL_FORMAT % 'no' == '<!-- agy-bridge-refusal-v1 sent: no -->'
    r = h.raw('queue', 'run', '--repo', str(h.repo))
    assert r.returncode == 0, r.stderr
    assert not h.record_path.exists()
    assert 'failed: 1' in h.raw('status', '--repo', str(h.repo)).stdout


def test_queue_cancel_of_the_running_job_is_refused(h):
    assert enqueue(h).returncode == 0
    paths = queue.Paths(str(h.repo))
    with paths.lock('run'):
        paths.state('example')
        with pytest.raises(queue.CancelRefused, match=r'job "example" is running \(pid %d\); wait for it or stop '
                                                      r'the runner' % os.getpid()):
            queue.cancel(str(h.repo), 'example')
    assert (h.repo / '.agy-bridge/queue/example.job').exists()
    assert not (h.repo / '.agy-bridge/results/example.md').exists()


def test_queue_cancel_refuses_unknown_and_finished_jobs(h):
    r = h.raw('queue', 'cancel', 'nosuch', '--repo', str(h.repo))
    assert r.returncode == 1 and 'queue cancel: no pending job "nosuch"' in r.stderr
    assert enqueue(h).returncode == 0
    assert h.raw('queue', 'run', '--repo', str(h.repo)).returncode == 0
    r = h.raw('queue', 'cancel', 'example', '--repo', str(h.repo))
    assert r.returncode == 4 and 'job "example" already finished' in r.stderr and 'queue/done/' in r.stderr
    assert enqueue(h, name='second').returncode == 0
    assert h.raw('queue', 'cancel', 'second', '--repo', str(h.repo)).returncode == 0
    r = h.raw('queue', 'cancel', 'second', '--repo', str(h.repo))
    assert r.returncode == 4 and 'already failed or was cancelled' in r.stderr


VERDICT_PASS = ('--verdict', 'pass', '--by', 'reader', '--evidence', 'checked')


def test_verdict_pass_is_refused_on_a_cancelled_job_despite_its_refusal_result(h):
    assert enqueue(h).returncode == 0
    assert h.raw('queue', 'cancel', 'example', '--repo', str(h.repo)).returncode == 0
    assert (h.repo / '.agy-bridge/results/example.md').read_text().splitlines()[1] == queue_job.REFUSAL_FORMAT % 'no'
    r = h.raw('verdict', '--repo', str(h.repo), '--job', 'example', *VERDICT_PASS)
    assert r.returncode == 1 and 'job "example" failed or was cancelled' in r.stderr, r.stderr
    assert not (h.repo / '.agy-bridge/verdicts.jsonl').exists()
    r = h.raw('verdict', '--repo', str(h.repo), '--job', 'example', *VERDICT_PASS, '--force')
    assert r.returncode == 0, r.stderr
    r = h.raw('verdict', '--repo', str(h.repo), '--job', 'example', '--verdict', 'reject', '--by', 'r',
              '--evidence', 'e')
    assert r.returncode == 0 and 'warning' not in r.stderr


def test_verdict_pass_is_refused_on_a_job_whose_agy_run_failed(h):
    h.configure(mode='exit', exit_code=3)
    assert enqueue(h).returncode == 0
    h.raw('queue', 'run', '--repo', str(h.repo))
    base = h.repo / '.agy-bridge'
    assert (base / 'queue/failed/example.job').exists() and (base / 'results/example.md').exists()
    assert not (base / 'queue/done/example.job').exists()
    r = h.raw('verdict', '--repo', str(h.repo), '--job', 'example', *VERDICT_PASS)
    assert r.returncode == 1 and 'job "example" failed or was cancelled' in r.stderr, r.stderr
    assert not (base / 'verdicts.jsonl').exists()


def test_verdict_pass_is_refused_on_a_refusal_result_left_in_the_queue(h):
    # The archive move after a refusal can fail; the job then stays in the queue beside a refusal result.
    assert enqueue(h).returncode == 0
    base = h.repo / '.agy-bridge'
    (base / 'results').mkdir(parents=True, exist_ok=True)
    (base / 'results/example.md').write_text(queue_job.refusal_text('refused: test'))
    assert (base / 'queue/example.job').exists()
    r = h.raw('verdict', '--repo', str(h.repo), '--job', 'example', *VERDICT_PASS)
    assert r.returncode == 1 and 'job "example" failed or was cancelled' in r.stderr, r.stderr
    assert not (base / 'verdicts.jsonl').exists()
    r = h.raw('verdict', '--repo', str(h.repo), '--job', 'example', '--verdict', 'reject', '--by', 'r',
              '--evidence', 'e')
    assert r.returncode == 0 and 'warning' not in r.stderr, r.stderr


@pytest.mark.parametrize('reason', ['', 'x' * 201, 'bad\nline'])
def test_queue_cancel_reason_is_bounded_and_printable(h, reason):
    assert enqueue(h).returncode == 0
    with pytest.raises(config.UsageError, match='--reason'):
        queue.cancel(str(h.repo), 'example', reason)
    assert (h.repo / '.agy-bridge/queue/example.job').exists()


def test_refusal_after_an_agy_attempt_started_says_sent_maybe(h):
    assert enqueue(h).returncode == 0
    paths = queue.Paths(str(h.repo))
    paths.failure(paths.queue + '/example.job', 'result could not be published', sent=True)
    text = (h.repo / '.agy-bridge/results/example.md').read_text()
    assert text.splitlines()[:2] == ['# Queue refusal', '<!-- agy-bridge-refusal-v1 sent: maybe -->']


def test_queue_cancel_redacts_a_key_shaped_reason(h):
    assert enqueue(h).returncode == 0
    queue.cancel(str(h.repo), 'example', 'AKIAABCDEFGHIJKLMNOP')
    text = (h.repo / '.agy-bridge/results/example.md').read_text()
    assert 'AKIA' not in text and 'redacted key-shaped reason' in text


def test_a_job_withdrawn_after_listing_is_skipped_without_sending(h):
    paths = queue.Paths(str(h.repo))
    (h.repo / '.agy-bridge/queue').mkdir(parents=True)
    assert queue.run_job(paths, paths.queue + '/gone.job') is None


def _hold_runner_row(h, row):
    """Hold the runner lock as another runner would and write its watcher.pid row; return the open fd."""
    import fcntl
    folder = h.repo / '.agy-bridge/queue'
    folder.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(folder / 'watcher.pid'), os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    os.write(fd, json.dumps(row).encode())
    return fd


@pytest.mark.parametrize('extra,shown', [
    (dict(version='0.0.1', code_root='/old/plugin/src'), 'runs agy-bridge 0.0.1 from /old/plugin/src'),
    (dict(), 'runs agy-bridge unknown from unknown'),
])
def test_status_warns_about_a_watcher_from_another_version(h, extra, shown):
    from agy_bridge import __version__
    fd = _hold_runner_row(h, dict(pid=424242, mode='watch', job=None, started=None, **extra))
    try:
        r = h.raw('status', '--repo', str(h.repo))
        assert r.returncode == 0, r.stderr
        expected = ('WARN watcher pid 424242 %s; this is %s; stop it (kill 424242) and restart with queue watch '
                    '--detach' % (shown, __version__))
        assert expected in r.stdout
        r = h.raw('ps', '--repo', str(h.repo))
        assert expected in r.stderr
        assert enqueue(h).returncode == 0
        r = enqueue(h, name='second')
        assert r.returncode == 0 and expected in r.stderr
    finally:
        os.close(fd)


def test_status_has_no_warning_for_a_watcher_of_this_version(h):
    from agy_bridge import __version__
    fd = _hold_runner_row(h, dict(pid=424242, mode='watch', job=None, started=None, version=__version__,
                                  code_root=queue_store.CODE_ROOT))
    try:
        r = h.raw('status', '--repo', str(h.repo))
        assert 'running (pid 424242, watch)' in r.stdout and 'WARN' not in r.stdout
    finally:
        os.close(fd)


def test_runner_row_records_version_and_code_root(h):
    from agy_bridge import __version__
    paths = queue.Paths(str(h.repo))
    with paths.lock('run'):
        row = json.loads((h.repo / '.agy-bridge/queue/watcher.pid').read_text())
    assert row['version'] == __version__ and row['code_root'] == queue_store.CODE_ROOT


def test_watch_sweeps_temp_links_each_iteration_then_runs_the_job(h, monkeypatch):
    paths = queue.Paths(str(h.repo))
    folder = h.repo / '.agy-bridge/queue'
    folder.mkdir(parents=True)
    calls, sleeps = [], []

    def fake_run_job(paths_, rel):
        info = os.stat(os.path.join(paths_.root, rel), follow_symlinks=False)
        calls.append((rel, info.st_nlink))
        paths_.move(rel, True)
        return 0

    def fake_sleep(_):
        sleeps.append(1)
        if len(sleeps) == 1:
            # Added after the watcher started: an interrupted queue add left its temp link behind.
            (folder / 'late.job').write_text(json.dumps(job(name='late')))
            os.link(folder / 'late.job', folder / (queue.safety.TEMP_PREFIX + 'late.job-0123456789abcdef'))
            return
        raise KeyboardInterrupt

    monkeypatch.setattr(queue, 'run_job', fake_run_job)
    monkeypatch.setattr(queue.time, 'sleep', fake_sleep)
    with pytest.raises(KeyboardInterrupt):
        queue.run(paths, watch=True, poll=.05)
    assert calls == [(paths.queue + '/late.job', 1)]
    assert not [n for n in os.listdir(folder) if n.startswith(queue.safety.TEMP_PREFIX)]
    assert (folder / 'done/late.job').exists()


def test_watch_stops_when_its_code_root_is_gone(h, monkeypatch, capsys):
    paths = queue.Paths(str(h.repo))
    monkeypatch.setattr(queue_store, 'CODE_ROOT', str(h.root / 'removed-skill/scripts'))
    monkeypatch.setattr(queue.time, 'sleep', lambda _: pytest.fail('watch kept polling'))
    assert queue.run(paths, watch=True, poll=.05) == 0
    assert 'no longer exists' in capsys.readouterr().out


def _suggested_c_argument(text, command):
    import shlex
    line = text[text.index(command):].splitlines()[0]
    for end in (' with the same', ', then'):
        line = line.split(end, 1)[0]
    words = shlex.split(line)
    return words[words.index('-C') + 1]


def test_suggested_commands_keep_a_non_ascii_repo_path(h):
    polish = h.root / 'zażółć repo'
    (polish / '.git').mkdir(parents=True)
    r = h.raw('verdict', '--repo', str(polish), '--job', 'nope', *VERDICT_PASS)
    assert r.returncode == 1, r.stderr
    assert _suggested_c_argument(r.stderr, 'agy-bridge status -C') == str(polish)


def test_shell_arg_escapes_only_non_printable_paths():
    import shlex
    for path in ('/tmp/zażółć repo', "/tmp/it's", '/tmp/back\\slash'):
        assert shlex.split(stage.shell_arg(path)) == [path]
    escaped = stage.shell_arg('/tmp/bad\x1b[31m')
    assert '\x1b' not in escaped and shlex.split(escaped) == ['/tmp/bad\\u001b[31m']


def test_suggested_commands_quote_a_repo_path_with_a_space(h):
    spaced = h.root / 'my repo'
    (spaced / '.git').mkdir(parents=True)
    r = h.raw('verdict', '--repo', str(spaced), '--job', 'nope', *VERDICT_PASS)
    assert r.returncode == 1, r.stderr
    assert _suggested_c_argument(r.stderr, 'agy-bridge status -C') == str(spaced)
    r = h.raw('queue', 'cancel', 'nope', '--repo', str(spaced))
    assert r.returncode == 1, r.stderr
    assert _suggested_c_argument(r.stderr, 'agy-bridge status -C') == str(spaced)
    (spaced / 'notes.md').write_text('n\n')
    path = h.root / 'input.json'
    path.write_text(json.dumps(job()))
    assert h.raw('queue', 'add', str(path), '--repo', str(spaced)).returncode == 0
    r = h.raw('verdict', '--repo', str(spaced), '--job', 'example', *VERDICT_PASS)
    assert r.returncode == 1 and _suggested_c_argument(r.stderr, 'agy-bridge queue run -C') == str(spaced)


@pytest.mark.parametrize('mode, note', [('watch', 'this job will be sent now'), ('run', 'it may send this job')])
def test_queue_add_notes_a_running_watcher(h, mode, note):
    assert enqueue(h, name='first').returncode == 0
    paths = queue.Paths(str(h.repo))
    with paths.lock(mode):
        r = enqueue(h, name='second')
    assert r.returncode == 0, r.stderr
    expected = 'NOTE watcher pid %d is running; this job will be sent now' % os.getpid() if mode == 'watch' else \
        'NOTE runner pid %d (queue run) is running; it may send this job' % os.getpid()
    assert expected in r.stdout and note in expected
    r = enqueue(h, name='third')
    assert r.returncode == 0 and 'NOTE' not in r.stdout


def test_queue_status_lists_pending_names(h):
    for name in ('alpha', 'beta'):
        assert enqueue(h, name=name).returncode == 0
    r = h.raw('queue', 'status', '--repo', str(h.repo))
    assert r.returncode == 0, r.stderr
    lines = r.stdout.splitlines()
    assert lines[lines.index('pending: 2') + 1] == 'pending jobs: alpha, beta'


def test_queue_status_bounds_the_pending_list(h):
    q = h.repo / '.agy-bridge/queue'
    q.mkdir(parents=True)
    for number in range(25):
        (q / ('job%02d.job' % number)).write_text('{}')
    out = queue.status(queue.Paths(str(h.repo)))
    line = [x for x in out.splitlines() if x.startswith('pending jobs: ')][0]
    assert line.count(',') == 19 and 'job19' in line and 'job20' not in line
    assert '(+5 more; agy-bridge jobs list -C ' in line


def test_watcher_record_is_never_empty_while_the_lock_is_held(h, monkeypatch):
    paths = queue.Paths(str(h.repo))
    record = h.repo / '.agy-bridge/queue/watcher.pid'
    seen = []
    real_truncate = os.ftruncate

    def truncate(fd, length):
        seen.append(record.read_bytes())  # what a concurrent reader would see right before the cut
        assert length > 0
        return real_truncate(fd, length)
    monkeypatch.setattr(queue.os, 'ftruncate', truncate)
    with paths.lock('watch'):
        assert json.loads(record.read_text())['mode'] == 'watch'
        paths.state('a-much-longer-job-name-than-usual')
        assert json.loads(record.read_text())['job'] == 'a-much-longer-job-name-than-usual'
        paths.state()
        assert json.loads(record.read_text())['job'] is None
    monkeypatch.setattr(queue.os, 'ftruncate', real_truncate)
    assert seen and all(seen)
    row = json.loads(record.read_text())
    assert row['stopping'] is True and row['pid'] == os.getpid()
    assert paths.running() is None  # the lock is released, so the leftover row is not trusted


def test_running_retries_an_empty_read_while_the_lock_is_held(h, monkeypatch):
    paths = queue.Paths(str(h.repo))
    real_pread = os.pread
    calls = []

    def pread(fd, size, offset):
        calls.append(offset)
        return b'' if len(calls) == 1 else real_pread(fd, size, offset)
    with paths.lock('run'):
        monkeypatch.setattr(queue.os, 'pread', pread)
        watcher = paths.running()
        monkeypatch.setattr(queue.os, 'pread', real_pread)
    assert len(calls) == 2 and watcher['pid'] == os.getpid() and watcher['mode'] == 'run'
