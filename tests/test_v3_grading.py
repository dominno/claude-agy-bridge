import json
import os
import sys

import pytest

from agy_bridge import config, grading, stats


@pytest.fixture
def case(h, guarded_sandbox):
    (h.repo/'impl.py').write_text('value = 1\nunused = 2\n')
    (h.repo/'check.py').write_text("import runpy\nassert runpy.run_path('impl.py')['value'] == 1\n")
    c=dict(name='sample',job='sample',files={'impl.py':'impl.py','check.py':'check.py'},solution_files=['impl.py'],test_cmd=['python','check.py'],env={},expect='pass')
    return h,c


def mutant(old='unused = 2',new='unused = 3',file='impl.py'):
    return dict(file=file,old=old,new=new,why='missing assertion')


def test_mutants_outcomes_and_fresh_trees(case):
    h,c=case
    rows,table,records=grading.grade(str(h.repo),c,[mutant(),mutant('value = 1','value = 0'),mutant('absent','x'),mutant('unused = 2','unused  =  2'),mutant(' = ','=')])
    assert [r['status'] for r in rows]==['SURVIVES','KILLED','NOT-APPLICABLE','TRIVIAL','NOT-APPLICABLE']
    assert 'SURVIVES' in table.read_text() and len(records.read_text().splitlines())==5
    assert (h.repo/'impl.py').read_text()=='value = 1\nunused = 2\n'
    assert not str(table).startswith(str(h.repo))


@pytest.mark.parametrize('file',['../escape','/tmp/escape','check.py','.git/config','a/../../x','a\\..\\x'])
def test_mutant_target_restricted(case,file):
    h,c=case
    assert grading.grade(str(h.repo),c,[mutant(file=file)])[0][0]['status']=='NOT-APPLICABLE'


def test_baseline_must_pass(case):
    h,c=case; (h.repo/'impl.py').write_text('value = 0\n')
    with pytest.raises(ValueError,match='reference'):
        grading.grade(str(h.repo),c,[mutant()])


@pytest.mark.parametrize('text',['no fence','```mutants\nnot json\n```','```mutants\n{}\n```','```mutants\n\n```'])
def test_mutants_parser_strict(text):
    with pytest.raises(ValueError): grading.extract_mutants(text)


def test_annotated_parser():
    lines=['```mutants',json.dumps(mutant()),'```']
    assert grading.extract_mutants('\n'.join('UNVERIFIED (malformed citation line): '+s for s in lines))==[mutant()]


@pytest.mark.parametrize('old,new,trivial',[('x = 1','x  =  1',True),('x = 1','x = 1 # hi',True),('x = 1','x = 2',False),('x = 1','x = (1)',False),('x = 1','x = 0x1',False),('x = 1','x = 1\n"hello"',False)])
def test_trivial_python(old,new,trivial):
    assert grading.trivial_change(old,new)==trivial


def test_apply_diff_and_cli_record(case):
    h,c=case
    diff='--- impl.py\n+++ impl.py\n@@ -1,2 +1,2 @@\n value = 1\n-unused = 2\n+unused = 3\n'
    rec=grading.apply_diff(str(h.repo),c,diff)
    assert rec['applied'] and rec['tests_pass'] and rec['how']=='patch -p0'
    assert (h.repo/'impl.py').read_text().endswith('unused = 2\n')
    path=h.root/'case.json'; path.write_text(json.dumps(c))
    result=h.root/'result.md'; result.write_text('```mutants\n'+json.dumps(mutant())+'\n```')
    r=h.raw('grade-mutants',str(result),'--case',str(path),'--repo',str(h.repo),'--record','--by','reader')
    assert r.returncode==0,r.stderr
    assert json.loads((h.repo/'.agy-bridge/verdicts.jsonl').read_text())['verdict']=='pass'


@pytest.mark.parametrize('path',['../escape','/tmp/escape','.git/config','a/../../escape'])
def test_diff_traversal_refused(case,path):
    h,c=case
    diff=f'--- {path}\n+++ {path}\n@@ -1 +1 @@\n-a\n+b\n'
    with pytest.raises(ValueError): grading.apply_diff(str(h.repo),c,diff)


def test_case_no_shell_and_scratch_outside_repo(case,monkeypatch):
    h,c=case
    c['test_cmd']='echo yes'
    with pytest.raises(ValueError): grading.validate_case(str(h.repo),c)
    c['test_cmd']=['python','check.py']
    monkeypatch.setenv('AGY_BRIDGE_SCRATCH',str(h.repo/'scratch'))
    with pytest.raises(ValueError): grading.grade(str(h.repo),c,[mutant()])


def test_stats_fit_fallback_target():
    points=[(100*i,10+2*i) for i in range(1,7)]
    s=stats.summarize(points,180)
    assert s['max_bytes']==8500 and s['slope']==pytest.approx(.02)
    s=stats.summarize(points[:3],180)
    assert s['max_bytes'] is None and s['median']==14 and s['p90']==16


def test_stats_ignores_model_prose(h):
    base=h.repo/'.agy-bridge/results'; base.mkdir(parents=True)
    (base/'a.md').write_text('model: fake\nbytes: 100\nseconds: 20\n## Round 1\nmodel: other\nbytes: 999\nseconds: 999\n')
    assert stats.read_headers(str(h.repo),str(base))==[dict(name='a',model='fake',bytes=100,seconds=20.0)]


def test_case_env_clean_and_placeholders(case,monkeypatch):
    h,c=case
    monkeypatch.setenv('FAKE_PROVIDER_SECRET','must-not-inherit')
    (h.repo/'check.py').write_text("import os\nassert 'FAKE_PROVIDER_SECRET' not in os.environ\nassert os.environ['BASE'].endswith('/repo')\nassert os.environ['WORK'] == os.getcwd()\nassert os.environ['HOME'].startswith(os.getcwd())\n")
    c['env']={'BASE':'{repo}','WORK':'{scratch}'}
    rows,_,_=grading.grade(str(h.repo),c,[mutant()])
    assert rows[0]['status']=='SURVIVES'


def test_test_timeout_kills_process_group(case,monkeypatch,tmp_path):
    from agy_bridge import runner
    h,c=case; calls=[]
    class Process:
        pid=999999
        returncode=-9
        def wait(self,timeout=None):
            assert timeout is not None
            if timeout == 300:
                raise runner.subprocess.TimeoutExpired('check',timeout)
            return -9
    def popen(argv,**kw):
        assert kw['start_new_session'] and 'shell' not in kw
        assert kw['cwd']==str(tmp_path)
        return Process()
    monkeypatch.setattr(runner.subprocess,'Popen',popen)
    monkeypatch.setattr(runner.os,'killpg',lambda *args:calls.append(args))
    monkeypatch.setattr(runner,'sandbox_preflight',lambda *args: None)
    rc,detail=grading.run_tests(str(h.repo),c,tmp_path)
    assert rc==124 and detail=='timeout after 300s'
    assert calls==[(999999,runner.signal.SIGKILL)]


@pytest.mark.parametrize('path',['.env','sub/.ssh/config','.git/config','link'])
def test_case_sources_restricted(case,path):
    from agy_bridge import stage
    h,c=case
    if path=='link': (h.repo/'link').symlink_to(h.repo/'impl.py')
    c['files']['impl.py']=path
    with pytest.raises((ValueError,config.UsageError,stage.Refused)):
        grading.grade(str(h.repo),c,[mutant()])


def test_diff_second_strategy_and_failing_check(case):
    h,c=case
    diff='--- a/impl.py\n+++ b/impl.py\n@@ -1,2 +1,2 @@\n-value = 1\n+value = 0\n unused = 2\n'
    r=grading.apply_diff(str(h.repo),c,diff)
    assert r['applied'] and r['how']=='patch -p1' and not r['tests_pass']
    assert not r['fuzz']


def test_diff_fuzz_offset_and_no_diff(case):
    h,c=case
    diff='--- impl.py\n+++ impl.py\n@@ -2,2 +2,2 @@\n value = 1\n-unused = 2\n+unused = 3\n'
    r=grading.apply_diff(str(h.repo),c,diff)
    assert r['applied'] and r['fuzz']
    assert not grading.apply_diff(str(h.repo),c,None)['diff_found']


def test_diff_git_fallback_fresh_strategies(case,monkeypatch):
    h,c=case; calls=[]
    real=grading.runner.run_scratch
    def run(argv,tree,repo,env=None,timeout=300,**kwargs):
        if argv[0] in ('patch','git'):
            assert (grading.Path(tree)/'impl.py').read_text().endswith('unused = 2\n')
            calls.append(argv)
            if argv[0]=='patch':
                (grading.Path(tree)/'impl.py').write_text('broken')
                return 1,'failed patch'
            assert env['GIT_CONFIG_GLOBAL']==os.devnull
            return 0,''
        return real(argv,tree,repo,env,timeout,**kwargs)
    monkeypatch.setattr(grading.runner,'run_scratch',run)
    r=grading.apply_diff(str(h.repo),c,'--- impl.py\n+++ impl.py\n@@ -1 +1 @@\n-a\n+b\n')
    assert r['how']=='git apply -p0' and r['tests_pass'] and len(calls)==3


def test_diff_fences_and_table_cli(case):
    h,c=case
    assert grading.extract_diff('```diff\nfirst\n```\n```diff\nsecond\n```')=='first\n'
    assert grading.extract_diff('```\n--- a\n+++ b\n```')=='--- a\n+++ b\n'
    path=h.root/'records.jsonl'; path.write_text(json.dumps(dict(case='x',applied=True,tests_pass=True,how='patch -p0'))+'\n')
    r=h.raw('apply-diff','table',str(path),'--repo',str(h.repo))
    assert r.returncode==0 and '1 pass tests' in r.stdout


def test_stats_events_and_headers_kept_separate(h):
    from agy_bridge import events
    fd=events.open_log(str(h.repo))
    try:
        events.append(fd,dict(model='fake',files=[{'path':'notes.md'}],total_bytes=11,seconds=2,dry_run=False))
        events.append(fd,dict(model='dry',files=[],total_bytes=0,seconds=99,dry_run=True))
    finally: os.close(fd)
    folder=h.repo/'.agy-bridge/results'; folder.mkdir()
    (folder/'a.md').write_text('model: fake\nbytes: 11\nseconds: 3\n## Round 1\n')
    text=stats.render(str(h.repo),str(folder))
    assert '| fake | events | 1 |' in text and '| fake | results | 1 |' in text
    assert '| dry |' not in text


@pytest.mark.parametrize('metadata',['Index: ../escape\n','new file mode 120000\n','rename from impl.py\n','GIT binary patch\n'])
def test_diff_hostile_extended_headers(case,metadata):
    h,c=case
    with pytest.raises(ValueError):
        grading.apply_diff(str(h.repo),c,metadata+'--- impl.py\n+++ impl.py\n@@ -1 +1 @@\n-a\n+b\n')


def test_real_git_fallback_is_outside_source_repository(case,monkeypatch):
    h,c=case
    real=grading.runner.run_scratch
    def run(argv,tree,repo,env=None,timeout=300,**kwargs):
        if argv[0]=='patch': return 1,'force git strategy'
        return real(argv,tree,repo,env,timeout,**kwargs)
    monkeypatch.setattr(grading.runner,'run_scratch',run)
    diff='--- a/impl.py\n+++ b/impl.py\n@@ -1,2 +1,2 @@\n value = 1\n-unused = 2\n+unused = 3\n'
    record=grading.apply_diff(str(h.repo),c,diff)
    assert record['how']=='git apply -p1' and record['tests_pass']
    assert not (h.repo/'.git/config').exists()


def test_diff_reports_offsets_earlier_than_log_tail(case):
    h,c=case
    c['files']={f'file{i}.py':'impl.py' for i in range(70)}
    c['solution_files']=[]
    c['test_cmd']=['python','-c','pass']
    patches=[]
    for i in range(70):
        start=5 if i==0 else 1
        patches.append(f'--- file{i}.py\n+++ file{i}.py\n@@ -{start},2 +{start},2 @@\n value = 1\n-unused = 2\n+unused = 3\n')
    r=grading.apply_diff(str(h.repo),c,''.join(patches))
    assert r['applied'] and r['tests_pass'] and r['fuzz']


@pytest.mark.parametrize('extra',['1c\nmalicious\n.\n','!touch escaped\n','diff -e impl.py\n','Prereq: anything\n'])
def test_non_unified_patch_commands_refused(case,extra):
    h,c=case
    diff='--- impl.py\n+++ impl.py\n@@ -1,2 +1,2 @@\n value = 1\n-unused = 2\n+unused = 3\n'+extra
    with pytest.raises(ValueError,match='unified'):
        grading.apply_diff(str(h.repo),c,diff)
