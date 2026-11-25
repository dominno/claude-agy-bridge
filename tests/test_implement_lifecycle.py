import argparse
import hashlib
import json
import subprocess
from types import SimpleNamespace

import pytest

from agy_bridge import config, edit_sandbox, implement, jobstore, runner, workspace


@pytest.fixture
def reviewed_job(tmp_path, monkeypatch):
    repo = tmp_path / 'source'
    repo.mkdir()
    source = repo / 'a.txt'
    source.write_text('previous value\n', encoding='utf-8')

    def git(*args):
        subprocess.run(['git', *args], cwd=repo, check=True, capture_output=True)

    git('init', '-q', '--template=')
    git('add', '--all')
    git('-c', 'user.name=Test', '-c', 'user.email=test@example.org', 'commit', '-qm', 'baseline')
    snapshot = workspace.prepare(str(repo), ['a.txt'], [], 'apply the reviewed update')

    home = tmp_path / 'bridge'
    monkeypatch.setattr(config, 'bridge_home', lambda: str(home))
    job = jobstore.store_root(create=True) / ('a' * 32)
    job.mkdir(mode=0o700)
    work = workspace.materialize(snapshot, job)
    (work.tree / 'a.txt').write_text('reviewed value\n', encoding='utf-8')
    changes = workspace.export_diff(work)
    digest = hashlib.sha256(changes.diff).hexdigest()
    jobstore.write(job, 'patch-' + digest + '.diff', changes.diff)
    state = dict(job_id=job.name, phase='verified', repository=snapshot.root, base_commit=snapshot.base_commit,
                 profile=str(tmp_path / 'profile'), task=snapshot.task, use_api_key=False, model=None,
                 check=None, conversation_id=None, extra_deny=list(snapshot.extra_deny), write_dirs=[],
                 turn_timeout_s=5, job_timeout_s=5, changes=changes.paths, patch_sha256=digest,
                 manifest=snapshot.manifest, bridge_exit=0)
    jobstore.save(job, state)
    return job, source, work, digest


def _apply(job, digest):
    return implement.dispatch(argparse.Namespace(command='implement-apply', job=job.name, sha256=digest))


def test_can_confirm_applied_patch_after_status_save_failure(reviewed_job, monkeypatch, capsys):
    job, source, _, digest = reviewed_job
    save = jobstore.save
    phases = []

    def fail_final_state(path, state):
        phases.append(state['phase'])
        if state['phase'] == 'applied':
            raise OSError('simulated final fsync failure')
        save(path, state)

    with monkeypatch.context() as patch:
        patch.setattr(jobstore, 'save', fail_final_state)
        assert _apply(job, digest) == config.EXIT_REFUSED
    assert phases == ['applying', 'applied']
    assert 'patch published but job status could not be saved' in capsys.readouterr().err
    assert source.read_text(encoding='utf-8') == 'reviewed value\n'
    original_target = source.stat()
    assert jobstore.status(job)['phase'] == 'applying'
    assert _apply(job, '0' * 64) == config.EXIT_USAGE
    assert jobstore.status(job)['patch_sha256'] == digest
    assert _apply(job, digest) == 0
    assert jobstore.status(job)['phase'] == 'applied'
    assert source.stat().st_ino == original_target.st_ino
    assert source.stat().st_mtime_ns == original_target.st_mtime_ns
    assert _apply(job, digest) == 0


def test_apply_does_not_write_files_before_durable_pending_state(reviewed_job, monkeypatch):
    job, source, _, digest = reviewed_job
    save = jobstore.save

    def fail_intent(path, state):
        if state['phase'] == 'applying':
            raise OSError('simulated apply phase write failure')
        save(path, state)

    with monkeypatch.context() as patch:
        patch.setattr(jobstore, 'save', fail_intent)
        assert _apply(job, digest) == config.EXIT_REFUSED
    assert source.read_text(encoding='utf-8') == 'previous value\n'
    assert jobstore.status(job)['phase'] == 'verified'


def test_unfinished_apply_does_not_allow_model_resume(reviewed_job, capsys):
    job, _, _, digest = reviewed_job
    state = jobstore.load(job)
    state['phase'] = 'applying'
    jobstore.save(job, state)
    args = argparse.Namespace(command='implement-resume', job=job.name, foreground=False)
    assert implement.dispatch(args) == config.EXIT_USAGE
    assert 'unfinished apply' in capsys.readouterr().err
    assert jobstore.status(job)['patch_sha256'] == digest


@pytest.mark.parametrize('resumed', [False, True])
def test_test_infrastructure_failure_stops_before_a_model_turn(reviewed_job, resumed, monkeypatch):
    job, _, work, _ = reviewed_job
    state = jobstore.load(job)
    state.update(phase='queued', attempt=0, run_id='b' * 32, check=['/unavailable-check'])
    if resumed:
        state['baseline_check_exit'] = 0
    else:
        (work.tree / 'a.txt').write_text('previous value\n', encoding='utf-8')
    jobstore.save(job, state)
    monkeypatch.setattr(implement, '_profile', lambda *args: state['profile'])
    monkeypatch.setattr(runner, 'find_agy', lambda _: '/trusted/agy')
    monkeypatch.setattr(edit_sandbox, 'preflight', lambda **kwargs: '1.5.16')
    monkeypatch.setattr(runner, 'run_edit_agy', lambda *args, **kwargs: pytest.fail('a paid AGY turn started'))

    def check(argv, *, tree, **kwargs):
        assert argv == ['/unavailable-check']
        expected = 'reviewed value\n' if resumed else 'previous value\n'
        assert (tree / 'a.txt').read_text(encoding='utf-8') == expected
        raise edit_sandbox.InfrastructureError('untrusted details with private paths')

    monkeypatch.setattr(edit_sandbox, 'run_test', check)
    assert implement._work(job) == config.EXIT_REFUSED
    status = jobstore.status(job)
    assert status['phase'] == 'failed' and status['attempt'] == 0
    assert status['failure_kind'] == 'check_infrastructure'
    assert status['detail'] == 'offline test sandbox could not start the check'
    assert 'untrusted' not in json.dumps(status)


@pytest.mark.parametrize('sandbox_ready', [False, True])
def test_workspace_check_can_be_added_after_verified_offline_probe(reviewed_job, sandbox_ready, monkeypatch):
    job, _, work, _ = reviewed_job
    state = jobstore.load(job)
    state.update(phase='queued', attempt=0, run_id='b' * 32, check=['./checks/smoke'], write_dirs=['.'])
    (work.tree / 'a.txt').write_text('previous value\n', encoding='utf-8')
    jobstore.save(job, state)
    monkeypatch.setattr(implement, '_profile', lambda *args: state['profile'])
    monkeypatch.setattr(runner, 'find_agy', lambda _: '/trusted/agy')
    monkeypatch.setattr(edit_sandbox, 'preflight', lambda **kwargs: '1.5.16')
    steps = []

    def preflight(*, tree, **kwargs):
        assert not (tree / 'checks' / 'smoke').exists()
        steps.append('probe')
        if not sandbox_ready:
            raise edit_sandbox.InfrastructureError('offline probe failed')
        return 0, ''

    def model(*args, **kwargs):
        steps.append('model')
        helper = work.tree / 'checks' / 'smoke'
        helper.parent.mkdir()
        helper.write_text('#!/bin/sh\nexit 0\n', encoding='utf-8')
        helper.chmod(0o700)
        return SimpleNamespace(exit_code=0, error_kind=None)

    def check(argv, *, tree, **kwargs):
        assert argv == ['./checks/smoke'] and (tree / 'checks' / 'smoke').exists()
        steps.append('check')
        return 0, ''

    monkeypatch.setattr(edit_sandbox, 'preflight_test', preflight)
    monkeypatch.setattr(runner, 'run_edit_agy', model)
    monkeypatch.setattr(edit_sandbox, 'run_test', check)
    assert implement._work(job) == (0 if sandbox_ready else config.EXIT_REFUSED)
    status = jobstore.status(job)
    assert 'baseline_check_exit' not in status
    if sandbox_ready:
        assert steps == ['probe', 'model', 'check']
        assert status['phase'] == 'verified' and status['check_exit'] == 0
        # The agent wrote the check's own script, so verified is flagged.
        assert status['check_inputs_changed'] == ['checks/smoke']
    else:
        assert steps == ['probe']
        assert status['failure_kind'] == 'check_infrastructure'


def test_missing_workspace_check_outside_write_scope_is_refused(reviewed_job, monkeypatch):
    job, _, work, _ = reviewed_job
    state = jobstore.load(job)
    state.update(phase='queued', attempt=0, run_id='b' * 32, check=['./checks/smoke'], write_dirs=[])
    (work.tree / 'a.txt').write_text('previous value\n', encoding='utf-8')
    jobstore.save(job, state)
    monkeypatch.setattr(implement, '_profile', lambda *args: state['profile'])
    monkeypatch.setattr(runner, 'find_agy', lambda _: '/trusted/agy')
    monkeypatch.setattr(edit_sandbox, 'preflight', lambda **kwargs: '1.5.16')
    monkeypatch.setattr(runner, 'run_edit_agy', lambda *args, **kwargs: pytest.fail('a paid AGY turn started'))

    assert implement._work(job) == config.EXIT_REFUSED
    status = jobstore.status(job)
    assert status['phase'] == 'failed' and status['attempt'] == 0
    assert 'candidate edits outside --write-dir' in status['detail'] and 'checks/smoke' in status['detail']


def test_nested_actions_share_legacy_options_and_handlers():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest='command', required=True)
    implement.add_arguments(commands)
    group = commands.add_parser('jobs').add_subparsers(dest='action', required=True)
    implement.add_job_actions(group)

    for command in (['implement'], ['jobs', 'start']):
        start = parser.parse_args([*command, '-C', 'workspace', '-f', 'file', '--', 'task'])
        assert start.handler == implement.dispatch and start.implementation_operation == 'implement'
        assert start.repo == 'workspace' and start.file == ['file'] and start.task == ['task']
    for command in (['implement-status'], ['jobs', 'show'], ['jobs', 'status']):
        show = parser.parse_args([*command, 'a' * 32])
        assert show.handler == implement.dispatch and show.implementation_operation == 'implement-status'
    for command in (['implement-apply'], ['jobs', 'apply']):
        with pytest.raises(SystemExit):
            parser.parse_args([*command, 'a' * 32])


def _jobs(capsys, *argv):
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest='command', required=True)
    implement.add_job_actions(commands)
    args = parser.parse_args(list(argv))
    code = args.handler(args)
    return code, capsys.readouterr().err


def test_unknown_well_formed_job_id_names_the_job(reviewed_job, capsys):
    code, err = _jobs(capsys, 'show', 'b' * 32)
    assert code == config.EXIT_USAGE
    assert 'no implementation job ' + 'b' * 32 + '; list jobs with: agy-bridge jobs list' in err


def test_malformed_job_id_states_the_pattern(reviewed_job, capsys):
    code, err = _jobs(capsys, 'show', 'not-an-id')
    assert code == config.EXIT_USAGE
    assert '[0-9a-f]{32}' in err and 'jobs list' in err


def test_scope_violation_lists_paths_and_escapes_controls():
    state = dict(write_dirs=['src'], manifest=[dict(path='src/a.py')])
    paths = ['docs/x.md', 'evil\x1b[2J.txt'] + ['extra/%d.txt' % n for n in range(12)]
    with pytest.raises(config.UsageError) as info:
        implement._validate_scope(state, paths)
    text = str(info.value)
    assert text.startswith('candidate edits outside --write-dir src: docs/x.md, evil\\u001b[2J.txt, ')
    assert '\x1b' not in text and '(+4 more)' in text and 'extra/7.txt' in text and 'extra/8.txt' not in text
    assert 'rerun with a wider --write-dir or reject the job' in text
    implement._validate_scope(state, ['src/a.py', 'src/new.py'])
    with pytest.raises(config.UsageError, match='only the staged files'):
        implement._validate_scope(dict(state, write_dirs=[]), ['src/new.py'])


def test_apply_in_wrong_phase_names_the_phase(reviewed_job, capsys):
    job, _, _, digest = reviewed_job
    state = jobstore.load(job)
    state['phase'] = 'checks_failed'
    jobstore.save(job, state)
    assert _apply(job, digest) == config.EXIT_USAGE
    err = capsys.readouterr().err
    assert 'job %s is checks_failed; apply needs ready/verified (agy-bridge jobs show %s)' % (job.name, job.name) in err


def test_apply_with_wrong_hash_names_the_expected_hash(reviewed_job, capsys):
    job, source, _, digest = reviewed_job
    assert _apply(job, '0' * 64) == config.EXIT_USAGE
    err = capsys.readouterr().err
    assert '--sha256 does not match the published patch (expected %s)' % digest in err
    assert 'jobs patch %s --raw > p.diff' % job.name in err and 'shasum -a 256' in err
    assert source.read_text(encoding='utf-8') == 'previous value\n'


def _set_phase(job, phase, **extra):
    state = jobstore.load(job)
    state.update(phase=phase, **extra)
    jobstore.save(job, state)


def test_remove_refuses_ready_and_active_jobs_and_removes_applied(reviewed_job, capsys):
    import fcntl
    job, _, _, _ = reviewed_job
    code, err = _jobs(capsys, 'remove', job.name)
    assert code == config.EXIT_USAGE and 'verified and its patch was not applied' in err and job.exists()
    _set_phase(job, 'running', run_id='c' * 32)
    with open(job / 'lock', 'a') as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        code, err = _jobs(capsys, 'remove', job.name, '--force')
    assert code == config.EXIT_USAGE and 'active worker' in err and job.exists()
    _set_phase(job, 'applied')
    (job / 'baseline').chmod(0o500)
    code, err = _jobs(capsys, 'remove', job.name, '--dry-run')
    assert code == 0 and job.exists()
    code = _jobs(capsys, 'remove', job.name)[0]
    assert code == 0 and not job.exists()
    code, err = _jobs(capsys, 'remove', job.name)
    assert code == config.EXIT_USAGE and 'no implementation job ' + job.name in err


def test_remove_reports_reclaimed_bytes(reviewed_job, capsys):
    job, _, _, _ = reviewed_job
    _set_phase(job, 'applied')
    parser = argparse.ArgumentParser()
    implement.add_job_actions(parser.add_subparsers(dest='command', required=True))
    args = parser.parse_args(['remove', job.name])
    assert args.handler(args) == 0
    row = json.loads(capsys.readouterr().out)
    assert row['job_id'] == job.name and row['removed'] is True and row['bytes'] > 0 and row['phase'] == 'applied'


def test_remove_unlinks_symlinks_without_following_them(reviewed_job, tmp_path, capsys):
    job, _, work, _ = reviewed_job
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'keep.txt').write_text('keep me\n')
    (work.tree / 'escape').symlink_to(outside)
    (work.tree / 'escape-file').symlink_to(outside / 'keep.txt')
    _set_phase(job, 'failed')
    assert _jobs(capsys, 'remove', job.name)[0] == 0
    assert not job.exists()
    assert (outside / 'keep.txt').read_text() == 'keep me\n'


def _age_job(job, seconds):
    import time
    state = json.loads((job / 'state.json').read_text())
    state['updated_at'] = state['created_at'] = time.time() - seconds
    jobstore.write(job, 'state.json', json.dumps(state).encode())


def _maintain(capsys, *argv):
    parser = argparse.ArgumentParser()
    implement.add_job_actions(parser.add_subparsers(dest='command', required=True))
    args = parser.parse_args(list(argv))
    assert args.handler(args) == 0
    return json.loads(capsys.readouterr().out)


def test_prune_dry_run_lists_and_prune_removes_old_finished_jobs(reviewed_job, capsys):
    import os
    import time
    job, _, _, _ = reviewed_job
    _set_phase(job, 'canceled')
    _age_job(job, 8 * 86400)
    store = job.parent
    leftover = store / '.prepare-stale'
    leftover.mkdir(mode=0o700)
    (leftover / 'x').write_text('x')
    old = time.time() - 7200
    os.utime(leftover, (old, old))
    fresh = store / '.prepare-fresh'
    fresh.mkdir(mode=0o700)

    report = _maintain(capsys, 'prune', '--dry-run')
    assert report['dry_run'] and [row['job_id'] for row in report['removed']] == [job.name]
    assert report['leftovers'] == 1 and job.exists() and leftover.exists()

    report = _maintain(capsys, 'prune', '--older-than', '30d')
    assert report['removed'] == [] and job.exists()

    report = _maintain(capsys, 'prune')
    assert [row['job_id'] for row in report['removed']] == [job.name] and report['bytes'] > 0
    assert not job.exists() and not leftover.exists() and fresh.exists()


def test_prune_keeps_unapplied_jobs_without_force(reviewed_job):
    job, _, _, _ = reviewed_job
    _age_job(job, 8 * 86400)
    assert jobstore.prune(7 * 86400)['removed'] == [] and job.exists()
    assert [row['job_id'] for row in jobstore.prune(7 * 86400, force=True)['removed']] == [job.name]
    assert not job.exists()


def test_prune_rejects_a_bad_age(reviewed_job, capsys):
    code, err = _jobs(capsys, 'prune', '--older-than', 'soon')
    assert code == config.EXIT_USAGE and '--older-than must look like' in err


# ---------- council round 3: destructive prune paths ----------

def test_prune_keeps_a_job_with_an_active_worker_even_with_force(reviewed_job):
    import fcntl
    job, _, _, _ = reviewed_job
    _set_phase(job, 'applied')
    _age_job(job, 8 * 86400)
    with open(job / 'lock', 'a') as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        report = jobstore.prune(0, force=True)
    assert report['removed'] == [] and report['kept'] == [dict(job_id=job.name, reason='active worker')]
    assert job.is_dir() and (job / 'state.json').exists()


def test_prune_sweeps_only_old_pending_files_of_a_recent_job(reviewed_job):
    import os
    import time
    job, _, _, _ = reviewed_job
    _set_phase(job, 'applied')
    old = job / '.pending-old'
    old.write_bytes(b'x' * 10)
    past = time.time() - 2 * jobstore.LEFTOVER_AGE
    os.utime(old, (past, past))
    fresh = job / '.pending-fresh'
    fresh.write_bytes(b'y')

    report = jobstore.prune(7 * 86400, dry_run=True)
    assert report['leftovers'] == 1 and report['bytes'] == 10 and report['removed'] == []
    assert report['kept'] == [dict(job_id=job.name, reason='newer than --older-than')]
    assert old.exists() and fresh.exists()

    report = jobstore.prune(7 * 86400)
    assert report['leftovers'] == 1 and not old.exists() and fresh.exists() and job.is_dir()


def test_prune_reports_an_unreadable_state_and_keeps_it_unless_forced(reviewed_job):
    import os
    import time
    job, _, _, _ = reviewed_job
    jobstore.write(job, 'state.json', b'{not json')
    past = time.time() - 8 * 86400
    os.utime(job / 'state.json', (past, past))
    assert not (job / 'lock').exists()  # the first prune creates it, which used to make the job look new
    os.utime(job, (past, past))

    report = jobstore.prune(7 * 86400)
    assert report['removed'] == [] and job.is_dir()
    assert report['kept'] == [dict(job_id=job.name, reason='phase unreadable needs --force')]

    report = jobstore.prune(7 * 86400, force=True, dry_run=True)
    assert report['removed'] == [dict(job_id=job.name, phase='unreadable')] and job.is_dir()

    report = jobstore.prune(7 * 86400, force=True)
    assert report['removed'] == [dict(job_id=job.name, phase='unreadable')] and not job.exists()


def test_prune_reports_a_removal_error_per_job_and_continues(reviewed_job, monkeypatch):
    import shutil
    job, _, _, _ = reviewed_job
    _set_phase(job, 'applied')
    _age_job(job, 8 * 86400)
    second = job.parent / ('b' * 32)
    shutil.copytree(str(job), str(second), symlinks=True)
    state = json.loads((second / 'state.json').read_text())
    state['job_id'] = second.name
    jobstore.write(second, 'state.json', json.dumps(state).encode())

    real = jobstore._remove_tree

    def failing(parent, name, dry_run=False):
        if name == job.name:
            raise PermissionError(13, 'Permission denied', name)
        return real(parent, name, dry_run)
    monkeypatch.setattr(jobstore, '_remove_tree', failing)
    report = jobstore.prune(7 * 86400)
    assert report['kept'] == [dict(job_id=job.name, reason='could not be removed safely: PermissionError')]
    assert report['removed'] == [dict(job_id=second.name, phase='applied')]
    assert job.is_dir() and not second.exists()


def test_verified_job_that_edits_only_other_files_has_no_check_inputs(reviewed_job, monkeypatch):
    job, _, work, _ = reviewed_job
    state = jobstore.load(job)
    state.update(phase='queued', attempt=0, run_id='b' * 32, check=['python3', '-m', 'pytest', '-q'])
    (work.tree / 'a.txt').write_text('previous value\n', encoding='utf-8')
    jobstore.save(job, state)
    monkeypatch.setattr(implement, '_profile', lambda *args: state['profile'])
    monkeypatch.setattr(runner, 'find_agy', lambda _: '/trusted/agy')
    monkeypatch.setattr(edit_sandbox, 'preflight', lambda **kwargs: '1.5.16')

    def model(*args, **kwargs):
        (work.tree / 'a.txt').write_text('new value\n', encoding='utf-8')
        return SimpleNamespace(exit_code=0, error_kind=None)

    monkeypatch.setattr(runner, 'run_edit_agy', model)
    monkeypatch.setattr(edit_sandbox, 'run_test', lambda argv, **kwargs: (0, ''))
    assert implement._work(job) == 0
    status = jobstore.status(job)
    assert status['phase'] == 'verified' and status['check_inputs_changed'] == []
    assert implement.check_inputs_warning(status) is None


def test_check_inputs_match_argv_paths_after_normalisation_and_known_names():
    check = ['./scripts/../scripts/run.sh', '--config=./ci/conf.yml', '-q', '/abs/tool', '../outside.py']
    changed = ['scripts/run.sh', 'ci/conf.yml', 'src/app.py', 'sub/conftest.py', 'pyproject.toml',
               'site/x.pth', '.github/workflows/ci.yml', 'abs/tool', 'outside.py', 'Makefile', 'docs/setup.cfg.md']
    assert implement.check_inputs_changed(check, changed) == sorted([
        'scripts/run.sh', 'ci/conf.yml', 'sub/conftest.py', 'pyproject.toml', 'site/x.pth',
        '.github/workflows/ci.yml', 'Makefile'])
    assert implement.check_inputs_changed(None, ['src/app.py']) == []


def test_check_inputs_include_files_under_a_directory_argument():
    changed = ['tests/test_foo.py', 'tests/unit/test_bar.py', 'testsuite/x.py', 'src/app.py', 'lib/a.py']
    assert implement.check_inputs_changed(['pytest', 'tests'], changed) == ['tests/test_foo.py',
                                                                             'tests/unit/test_bar.py']
    assert implement.check_inputs_changed(['./run.sh', './tests/'], changed) == ['tests/test_foo.py',
                                                                                  'tests/unit/test_bar.py']
    assert implement.check_inputs_changed(['pytest', '.'], changed) == []


def test_jobs_show_and_list_warn_about_changed_check_inputs(reviewed_job, capsys):
    from agy_bridge import jobs
    job, _, _, _ = reviewed_job
    state = jobstore.load(job)
    state.update(check=['./run-tests'], check_inputs_changed=['conftest.py', 'run-tests'])
    jobstore.save(job, state)
    args = argparse.Namespace(command='implement-status', job=job.name)
    assert implement.dispatch(args) == 0
    out, err = capsys.readouterr()
    expected = ('WARN check inputs changed by the agent: conftest.py, run-tests; review them before trusting '
                'verified')
    row = json.loads(out)
    assert row['check_inputs_changed'] == ['conftest.py', 'run-tests'] and row['warning'] == expected
    assert expected in err
    _, warnings = jobs.list_jobs()
    assert any(expected in warning and job.name in warning for warning in warnings)
