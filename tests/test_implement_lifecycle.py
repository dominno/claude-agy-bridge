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
    assert 'outside the approved paths' in status['detail']


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
