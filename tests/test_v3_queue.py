import json
import os
import signal
import subprocess
import sys
import time

import pytest

from agy_bridge import cli, config, queue, verdicts


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
    r = h.raw('verdict','--repo',str(h.repo),'--job','x','--verdict','pass','--by','reader','--evidence','checked') if target.startswith('verdict') else h.raw('status','--repo',str(h.repo))
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
    r=h.raw('verdict','--repo',str(h.repo),'--job','x','--verdict','reject','--by','r','--evidence','checked')
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
    args=['verdict','--repo',str(h.repo),'--job','x','--by','reader','--evidence','checked\x1b[31m']
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
    from agy_bridge import events
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
