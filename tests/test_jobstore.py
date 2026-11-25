import argparse
import json
from types import SimpleNamespace

import pytest

from agy_bridge.adapters import agy, edit_sandbox, jobstore, workspace
from agy_bridge.app import implement, implement_work
from agy_bridge.core import config


@pytest.fixture
def state_home(tmp_path, monkeypatch):
    home = tmp_path / 'private'
    monkeypatch.setattr(config, 'bridge_home', lambda: str(home))
    return home


def _record(store, job_id, phase='running'):
    job = store / job_id
    job.mkdir(mode=0o700)
    state = {'job_id': job_id, 'repository': str(store.parent / 'project'), 'phase': phase,
             'run_id': '1' * 32, 'created_at': 1, 'attempt': 2, 'task': 'task contents',
             'profile': '/private-profile', 'check': ['private-command'], 'conversation_id': 'private-conversation',
             'detail': 'private diagnostics', 'patch_sha256': '2' * 64, 'changes': [['added', 'private.txt']]}
    # Emulate the original version of the store, before it saved a schema marker.
    (job / 'state.json').write_text(json.dumps(state))
    return job, state


def test_empty_inventory_does_not_create_store(state_home):
    assert list(jobstore.iter_status()) == []
    assert not state_home.exists()


def test_inventory_isolates_bad_entries_and_projects_only_summary_fields(state_home):
    store = jobstore.store_root(create=True)
    active, state = _record(store, 'a' * 32)
    stale, _ = _record(store, 'b' * 32)
    bad, _ = _record(store, 'c' * 32)
    (bad / 'state.json').write_text('os error contains a private path')
    (store / ('d' * 32)).symlink_to(active, target_is_directory=True)
    (store / '.prepare-future').mkdir(mode=0o700)

    with jobstore.lock(active):
        jobstore.request_cancel(active)
        rows = {job_id: (summary, warning) for job_id, summary, warning in jobstore.iter_status()}
        assert rows[active.name] == ({'job_id': active.name, 'phase': 'running', 'repository': state['repository'],
                                     'created_at': 1, 'attempt': 2, 'worker_active': True, 'cancel_requested': True}, None)
        status = jobstore.read_status(active.name)
        assert status['patch_sha256'] == '2' * 64 and status['conversation_id'] == 'private-conversation'
        assert status['cancel_requested'] is True

    assert rows[stale.name][0]['phase'] == 'interrupted'
    assert rows[stale.name][0]['worker_active'] is False
    assert not (stale / 'lock').exists()
    for job_id in ('c' * 32, 'd' * 32):
        assert rows[job_id] == (None, 'implementation job could not be read safely')
    assert len(rows) == 4


@pytest.mark.parametrize('state', [[], {'job_id': 'a' * 32}, {'job_id': 'a' * 32, 'phase': {}}])
def test_single_status_refuses_malformed_state_without_traceback(state_home, state, capsys):
    job, _ = _record(jobstore.store_root(create=True), 'a' * 32)
    (job / 'state.json').write_text(json.dumps(state))
    assert implement.dispatch(argparse.Namespace(command='implement-status', job=job.name)) == config.EXIT_USAGE
    result = capsys.readouterr()
    assert result.out == ''
    assert result.err == 'agy-bridge: invalid implementation job state\n'


def test_stale_cancel_is_refused_without_writing_marker_or_lock(state_home, capsys):
    job, _ = _record(jobstore.store_root(create=True), 'a' * 32)
    assert implement.dispatch(argparse.Namespace(command='implement-cancel', job=job.name)) == config.EXIT_USAGE
    assert 'no active execution to cancel' in capsys.readouterr().err
    assert sorted(path.name for path in job.iterdir()) == ['state.json']


def test_cancellation_of_old_run_does_not_cancel_replacement(state_home, monkeypatch):
    job, state = _record(jobstore.store_root(create=True), 'a' * 32)
    write = jobstore.write

    def replace_before_marker(path, name, data):
        if name.startswith('cancel-'):
            jobstore.save(job, {**state, 'run_id': '4' * 32})
        write(path, name, data)

    with jobstore.lock(job):
        monkeypatch.setattr(jobstore, 'write', replace_before_marker)
        with pytest.raises(config.UsageError, match='execution ended'):
            jobstore.request_cancel(job)
        assert jobstore.cancel_pending(job, '1' * 32)
        assert not jobstore.cancel_pending(job, '4' * 32)
        assert jobstore.read_status(job.name)['cancel_requested'] is False


def test_unknown_state_version_returns_safe_inventory_warning(state_home):
    job, state = _record(jobstore.store_root(create=True), 'a' * 32)
    jobstore.save(job, state)
    assert json.loads((job / 'state.json').read_bytes())['schema_version'] == 1
    state['schema_version'] = 999
    (job / 'state.json').write_text(json.dumps(state))
    assert list(jobstore.iter_status()) == [(job.name, None, 'implementation job could not be read safely')]


def _operational_job(store):
    job, state = _record(store, 'a' * 32)
    state.update(base_commit=None, model=None, turn_timeout_s=5, job_timeout_s=5, write_dirs=[], extra_deny=[],
                 use_api_key=False, manifest=[{'path': 'candidate.txt'}], conversation_id=None)
    jobstore.save(job, state)
    return job, state


def test_resume_resets_last_activity_and_records_new_start(state_home, monkeypatch):
    job, state = _operational_job(jobstore.store_root(create=True))
    state.update(phase='canceled', started_at=2, last_agy_activity_at=3)
    jobstore.save(job, state)
    monkeypatch.setattr(implement_work, '_work', lambda _: 0)
    assert implement._launch(job, foreground=True) == 0
    status = jobstore.read_status(job.name)
    assert status['started_at'] > 3 and 'last_agy_activity_at' not in status
    assert jobstore.load(job)['run_id'] != state['run_id']


def test_worker_reports_job_deadline_without_starting_an_edit(state_home, monkeypatch):
    job, state = _operational_job(jobstore.store_root(create=True))
    work = SimpleNamespace(tree=job / 'tree')
    monkeypatch.setattr(workspace, 'load', lambda *args: work)
    monkeypatch.setattr(workspace, 'export_diff', lambda _: SimpleNamespace(paths=()))
    monkeypatch.setattr(implement_work, '_profile', lambda *args: state['profile'])
    monkeypatch.setattr(agy, 'find_agy', lambda _: '/trusted/agy')

    def stop_before_edit(**kwargs):
        assert 'deadline' in kwargs
        raise edit_sandbox.DeadlineExceeded('job deadline reached')

    monkeypatch.setattr(edit_sandbox, 'preflight', stop_before_edit)
    assert implement_work._work(job) == config.EXIT_TIMEOUT
    assert jobstore.read_status(job.name)['phase'] == 'timed_out'
