import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from agy_bridge import config, edit_sandbox, implement, runner
from agy_bridge.cli import main


CONVERSATION = '1f6682c8-3ea1-4019-9d32-e7feeb746681'


@pytest.fixture
def project(tmp_path, monkeypatch):
    # Call CLI without installing its signal handlers in the pytest process.
    from agy_bridge import cli
    monkeypatch.setattr(cli, 'install_stop_handlers', lambda: None)
    repo = tmp_path / 'project'
    (repo / '.git').mkdir(parents=True)
    (repo / 'src').mkdir()
    src = repo / 'src' / 'app.py'
    src.write_text('result = 0\n')
    profile = tmp_path / 'profile'
    profile.mkdir(mode=0o700)
    jobs = tmp_path / 'jobs'
    monkeypatch.setenv('AGY_BRIDGE_HOME', str(jobs))
    fake = tmp_path / 'agy'
    fake.write_text(f'''#!{sys.executable}
import json
from pathlib import Path
import sys
import time

args = sys.argv[1:]
assert '--mode=accept-edits' in args
print(json.dumps({{'event':'init', 'conversation_id':{CONVERSATION!r}, 'init':{{}}}}), flush=True)
file = Path('src/app.py')
if '--conversation' in args:
    assert args[args.index('--conversation') + 1] == {CONVERSATION!r}
    assert file.read_text() == 'result = 1\\n'
    file.write_text('result = 2\\n')
else:
    file.write_text('result = 1\\n')
    if 'test interrupted edit' in args[-1]:
        time.sleep(30)
print(json.dumps({{'event':'result', 'result':{{'conversation_id':{CONVERSATION!r}, 'status':'SUCCESS'}}}}), flush=True)
''')
    fake.chmod(0o700)
    monkeypatch.setattr(runner, 'find_agy', lambda *_: str(fake))
    monkeypatch.setattr(edit_sandbox, 'preflight', lambda **kw: '1.5.16')
    monkeypatch.setattr(edit_sandbox, 'agent_command', lambda argv, **kw: argv)
    return repo, src, profile, jobs


def _submit(repo, profile, *args, task='fix the value'):
    return main(['implement', '--repo', str(repo), '--profile', str(profile), '--file', 'src/app.py',
                 '--foreground', *args, '--', task])


def _only_job(jobs):
    job, = (jobs / 'implement').iterdir()
    return job


def test_cli_checks_exact_candidate_and_applies_only_reviewed_hash(project, capsys, monkeypatch):
    repo, src, profile, jobs = project
    checked = []

    def check(argv, *, tree, timeout_s, should_cancel, deadline=None):
        assert argv == ['python3', '-m', 'unittest']
        assert timeout_s > 0 and not should_cancel()
        candidate = tree / 'src' / 'app.py'
        value = candidate.read_text()
        checked.append(value)
        candidate.write_text('test altered its own disposable copy\n')
        return (0, '') if value == 'result = 2\n' else (1, 'expected result = 2')

    monkeypatch.setattr(edit_sandbox, 'run_test', check)
    assert _submit(repo, profile, '--check', '["python3", "-m", "unittest"]') == 0
    job = _only_job(jobs)
    assert checked == ['result = 0\n', 'result = 1\n', 'result = 2\n']
    assert src.read_text() == 'result = 0\n'
    assert main(['implement-status', job.name]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status['phase'] == 'verified' and status['attempt'] == 2
    assert status['conversation_id'] == CONVERSATION and status['check_exit'] == 0
    assert status['last_agy_activity_at'] >= status['created_at']
    assert main(['implement-patch', job.name]) == 0
    diff = capsys.readouterr().out
    assert '-result = 0\n+result = 2\n' in diff
    assert 'disposable copy' not in diff
    assert main(['implement-apply', job.name, '--sha256', '0' * 64]) != 0
    assert src.read_text() == 'result = 0\n'
    assert main(['implement-apply', job.name, '--sha256', status['patch_sha256']]) == 0
    assert src.read_text() == 'result = 2\n'


def test_timeout_preserves_changed_files_and_resumes_conversation(project):
    repo, src, profile, jobs = project
    assert _submit(repo, profile, '--turn-timeout', '1s', task='test interrupted edit') == config.EXIT_TIMEOUT
    job = _only_job(jobs)
    state = implement._status(job)
    assert state['conversation_id'] == CONVERSATION
    assert state['phase'] == 'timed_out' and state['patch_sha256']
    assert (job / 'tree' / 'src' / 'app.py').read_text() == 'result = 1\n'
    assert main(['implement-resume', job.name, '--foreground']) == 0
    status = implement._status(job)
    assert status['phase'] == 'ready'
    assert status['patch_sha256'] != state['patch_sha256']
    assert src.read_text() == 'result = 0\n'


def test_dry_run_is_local_and_creates_no_job(project, capsys):
    repo, _, _, jobs = project
    assert main(['implement', '--dry-run', '--repo', str(repo), '--file', 'src/app.py', '--', 'fix it']) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan['manifest'][0]['path'] == 'src/app.py'
    assert not jobs.exists()


def test_cancel_command_preserves_work_in_progress(project):
    repo, src, profile, jobs = project
    with ThreadPoolExecutor(max_workers=1) as workers:
        pending = workers.submit(_submit, repo, profile, '--turn-timeout', '5s', task='test interrupted edit')
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            if (jobs / 'implement').is_dir() and list((jobs / 'implement').iterdir()):
                job = _only_job(jobs)
                file = job / 'tree' / 'src' / 'app.py'
                if file.is_file() and file.read_text() == 'result = 1\n':
                    break
            time.sleep(0.02)
        else:
            pytest.fail('AGY did not start editing in time')
        assert main(['implement-cancel', job.name]) == 0
        assert pending.result(timeout=5) == 130
    status = implement._status(job)
    assert status['phase'] == 'canceled' and status['patch_sha256']
    assert src.read_text() == 'result = 0\n'


def test_detached_worker_handoff_handles_failure(project, capsys, monkeypatch):
    repo, _, profile, jobs = project
    monkeypatch.setenv('PATH', '/nonexistent')
    assert main(['implement', '--repo', str(repo), '--profile', str(profile), '--file', 'src/app.py',
                 '--', 'fix it']) == 0
    job = _only_job(jobs)
    assert json.loads(capsys.readouterr().out)['job_id'] == job.name
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        status = implement._status(job)
        if status['phase'] not in implement.ACTIVE:
            break
        time.sleep(0.02)
    assert status['phase'] == 'failed'
    assert status['bridge_exit'] == config.EXIT_AGY_MISSING
