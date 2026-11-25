import argparse
import fcntl
import json

from agy_bridge.adapters import jobstore
from agy_bridge.app import jobs, queue, queue_store
from agy_bridge.cli import parser_jobs
from agy_bridge.core import config


def _run(*arguments):
    return _run_command('jobs', 'list', *arguments)


def _run_command(*arguments):
    parser = argparse.ArgumentParser()
    parser_jobs.add_arguments(parser.add_subparsers(required=True))
    args = parser.parse_args(arguments)
    return args.handler(args)


def _repo(path, *, git=True):
    path.mkdir()
    if git:
        (path / '.git').mkdir()
    (path / 'source.py').write_text('x = 1\n')
    return path.resolve()


def _implementation(job_id, repository, *, phase='running', active=True, cancel=False):
    return (job_id, {
        'repository': str(repository), 'phase': phase, 'worker_active': active, 'cancel_requested': cancel,
        'created_at': 10, 'updated_at': 20, 'attempt': 1, 'task': 'private prompt',
        'profile': 'private profile', 'conversation_id': 'private conversation',
    }, None)


def test_empty_inventory_does_not_create_home_store_or_queue(tmp_path, monkeypatch, capsys):
    home = tmp_path / 'bridge-home'
    monkeypatch.setattr(config, 'bridge_home', lambda: str(home))
    repository = _repo(tmp_path / 'repo')

    assert _run('--repo', str(repository), '--json') == 0
    assert json.loads(capsys.readouterr().out) == {'schema_version': 1, 'jobs': [], 'warnings': []}
    assert not home.exists()
    assert not (repository / '.agy-bridge').exists()


def test_global_filter_and_sanitized_normalized_summary(tmp_path, monkeypatch, capsys):
    first = _repo(tmp_path / 'one')
    second = _repo(tmp_path / 'two')
    rows = [
        _implementation('1' * 32, first, phase='checking', cancel=True),
        _implementation('2' * 32, second, phase='queued'),
        _implementation('3' * 32, first, phase='interrupted', active=False),
        ('4' * 32, None, 'invalid state'),
    ]
    monkeypatch.setattr(jobstore, 'iter_status', lambda: iter(rows))

    assert _run('--json') == 0
    result = capsys.readouterr()
    rendered = json.loads(result.out)
    assert not result.err
    assert all(secret not in result.out for secret in ('private prompt', 'private profile', 'private conversation'))
    assert rendered['schema_version'] == 1
    assert rendered['warnings'] == [f"implementation {'4' * 32}: invalid state"]
    assert [(row['phase'], row['active']) for row in rendered['jobs']] == [
        ('cancel_requested', True), ('queued', True), ('interrupted', False),
    ]
    assert rendered['jobs'][0]['cancel_requested'] is True
    assert rendered['jobs'][0]['attempt'] == 1
    assert rendered['jobs'][0]['queue_dir'] is None

    assert _run('--repo', str(first), '--active', '--json') == 0
    scoped = json.loads(capsys.readouterr().out)
    assert [row['job_id'] for row in scoped['jobs']] == ['1' * 32]


def test_custom_queue_inventory_keeps_inbox_and_live_job_distinct(tmp_path, monkeypatch, capsys):
    repository = _repo(tmp_path / 'repo')
    monkeypatch.setattr(config, 'bridge_home', lambda: str(tmp_path / 'bridge-home'))
    first = queue.Paths(str(repository), queue_dir='queues/one')
    second = queue.Paths(str(repository), queue_dir='queues/two')
    for paths, name in ((first, 'same-name'), (second, 'same-name'), (second, 'not-started')):
        paths.add({'name': name, 'task': 'do not output this prompt', 'files': ['source.py']})

    with first.lock('watch'), second.lock('watch'):
        first.state('same-name')
        second.state('same-name')
        assert _run('--repo', str(repository), '--queue-dir', 'queues/two', '--json') == 0
        result = capsys.readouterr()
        rows = json.loads(result.out)['jobs']
        assert 'do not output' not in result.out
        assert [(row['job_id'], row['phase'], row['active']) for row in rows] == [
            ('same-name', 'running', True), ('not-started', 'pending', False),
        ]
        assert all(row['kind'] == 'delegate' and row['queue_dir'] == 'queues/two' for row in rows)
        assert int(rows[0]['started_at']) > 0

        assert _run('--repo', str(repository), '--queue-dir', 'queues/two', '--active') == 0
        table = capsys.readouterr().out
        assert 'PHASE' in table and 'same-name' in table and '[queues/two]' in table
        assert 'not-started' not in table

        assert _run('--json') == 0
        assert json.loads(capsys.readouterr().out)['jobs'] == []


def test_bad_scope_does_not_read_storage(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(config, 'bridge_home', lambda: str(tmp_path / 'bridge-home'))

    assert _run('--queue-dir', 'queues/custom') == config.EXIT_USAGE
    assert '--queue-dir requires --repo' in capsys.readouterr().err
    assert _run('--allow-non-git-root') == config.EXIT_USAGE
    assert '--allow-non-git-root requires --repo' in capsys.readouterr().err
    assert not (tmp_path / 'bridge-home').exists()


def test_inventory_ignores_writer_results_configuration(tmp_path, monkeypatch, capsys):
    repository = _repo(tmp_path / 'repo')
    monkeypatch.setattr(config, 'bridge_home', lambda: str(tmp_path / 'bridge-home'))
    queue_dir = '.agy-bridge/results'
    paths = queue.Paths(str(repository), queue_dir=queue_dir, results_dir='.agy-bridge/custom-results')
    paths.add({'name': 'review', 'task': 'review code', 'files': ['source.py']})

    assert _run('-C', str(repository), '--queue-dir', queue_dir, '--json') == 0
    result = json.loads(capsys.readouterr().out)
    assert [(row['job_id'], row['queue_dir']) for row in result['jobs']] == [('review', queue_dir)]
    assert not result['warnings']


def test_inventory_respects_non_git_opt_in(tmp_path, monkeypatch, capsys):
    repository = _repo(tmp_path / 'repo', git=False)
    monkeypatch.setattr(config, 'bridge_home', lambda: str(tmp_path / 'bridge-home'))
    paths = queue.Paths(str(repository), allow=True)
    paths.add({'name': 'review', 'task': 'review code', 'files': ['source.py']})

    assert _run('-C', str(repository), '--json') == config.EXIT_REFUSED
    assert not capsys.readouterr().out
    assert _run('-C', str(repository), '--allow-non-git-root', '--json') == 0
    assert [row['job_id'] for row in json.loads(capsys.readouterr().out)['jobs']] == ['review']


def test_inventory_does_not_follow_archive_or_result_symlinks(tmp_path, monkeypatch, capsys):
    repository = _repo(tmp_path / 'repo')
    monkeypatch.setattr(config, 'bridge_home', lambda: str(tmp_path / 'bridge-home'))
    paths = queue.Paths(str(repository))
    paths.add({'name': 'review', 'task': 'review code', 'files': ['source.py']})
    external = tmp_path / 'external'
    external.mkdir()
    (external / 'should-not-appear.job').write_text('{}')
    (repository / paths.results).symlink_to(external, target_is_directory=True)
    (repository / paths.queue / 'done').symlink_to(external, target_is_directory=True)

    assert _run('-C', str(repository), '--json') == 0
    result = json.loads(capsys.readouterr().out)
    assert [row['job_id'] for row in result['jobs']] == ['review']
    assert result['warnings'] == ['some queue entries could not be inspected safely']


def test_shared_watcher_probe_does_not_mark_a_stale_job_live(tmp_path):
    repository = _repo(tmp_path / 'repo')
    paths = queue.Paths(str(repository))
    paths.add({'name': 'review', 'task': 'review code', 'files': ['source.py']})
    watcher = repository / paths.queue / 'watcher.pid'
    with watcher.open('w') as stream:
        json.dump({'pid': 42, 'mode': 'watch', 'job': 'review', 'started': 10}, stream)
        stream.flush()
        fcntl.flock(stream, fcntl.LOCK_SH | fcntl.LOCK_NB)

        view = queue.snapshot(queue.QueueLocation(str(repository)))
        assert not view['active']
        assert [(row['phase'], row['active']) for row in view['jobs']] == [('pending', False)]
        assert queue.status(paths).splitlines()[0] == 'watcher: stopped'


def test_out_of_range_watcher_timestamp_is_unavailable(tmp_path):
    repository = _repo(tmp_path / 'repo')
    paths = queue.Paths(str(repository))
    paths.add({'name': 'review', 'task': 'review code', 'files': ['source.py']})

    with paths.lock('run'):
        watcher = repository / paths.queue / 'watcher.pid'
        watcher.write_text(json.dumps({'pid': 42, 'mode': 'run', 'job': 'review', 'started': 10**1000,
                                       'version': queue_store.__version__, 'code_root': queue_store.CODE_ROOT}))

        view, warnings = queue.list_jobs(queue.QueueLocation(str(repository)))
        assert [(row['phase'], row['started_at']) for row in view] == [('running', None)]
        assert warnings == ['active queue job start time is unreadable']
        report = queue.status(paths)
        assert report.startswith('watcher: running (pid 42, run)')
        assert '\nrunning: ' not in report


def test_bare_jobs_and_ps_select_expected_views(tmp_path, monkeypatch, capsys):
    repository = _repo(tmp_path / 'repo')
    rows = [
        _implementation('1' * 32, repository),
        _implementation('2' * 32, repository, phase='interrupted', active=False),
    ]
    monkeypatch.setattr(jobstore, 'iter_status', lambda: iter(rows))

    assert _run_command('jobs') == 0
    report = capsys.readouterr().out
    assert '1' * 32 in report and '2' * 32 in report

    assert _run_command('ps', '--json') == 0
    assert [row['job_id'] for row in json.loads(capsys.readouterr().out)['jobs']] == ['1' * 32]

    assert _run_command('ps', '--all', '-C', str(repository), '--json') == 0
    assert [row['job_id'] for row in json.loads(capsys.readouterr().out)['jobs']] == ['1' * 32, '2' * 32]


def _empty_store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'bridge_home', lambda: str(tmp_path / 'bridge-home'))
    monkeypatch.setattr(jobstore, 'iter_status', lambda: iter(()))
    outside = tmp_path / 'outside'
    outside.mkdir()
    monkeypatch.chdir(outside)


def test_list_without_repo_says_queues_were_not_searched(tmp_path, monkeypatch, capsys):
    _empty_store(tmp_path, monkeypatch)
    assert _run() == 0
    assert capsys.readouterr().out == ('No implementation jobs. Delegation queues are listed per repository: '
                                       'agy-bridge jobs list -C <repo>\n')
    assert _run_command('ps') == 0
    assert capsys.readouterr().out == ('No active implementation jobs. Delegation queues are listed per repository: '
                                       'agy-bridge ps -C <repo>\n')
    assert _run('--json') == 0
    assert json.loads(capsys.readouterr().out)['warnings'] == [
        'No implementation jobs. Delegation queues are listed per repository: agy-bridge jobs list -C <repo>']


def test_list_without_repo_notes_pending_queue_jobs_in_the_cwd_repository(tmp_path, monkeypatch, capsys):
    _empty_store(tmp_path, monkeypatch)
    repository = _repo(tmp_path / 'my repo')
    pending = repository / '.agy-bridge' / 'queue'
    pending.mkdir(parents=True)
    (pending / 'one.job').write_text('{}')
    (pending / 'two.job').write_text('{}')
    (pending / 'done').mkdir()
    (pending / 'done' / 'old.job').write_text('{}')
    (repository / 'sub').mkdir()
    monkeypatch.chdir(repository / 'sub')
    assert _run() == 0
    out = capsys.readouterr().out
    assert "NOTE 2 queue job(s) pending in %s; use -C (agy-bridge jobs list -C '%s')" % (repository, repository) in out
    assert not (repository / 'sub' / '.agy-bridge').exists()
    assert _run('--json') == 0
    assert any(w.startswith('NOTE 2 queue job(s) pending') for w in json.loads(capsys.readouterr().out)['warnings'])
    # With -C the queue is listed and no note is added.
    assert _run('-C', str(repository)) == 0
    assert 'NOTE' not in capsys.readouterr().out


def test_inventory_options_before_list_are_kept(tmp_path, monkeypatch, capsys):
    _empty_store(tmp_path, monkeypatch)
    repository = _repo(tmp_path / 'repo')
    pending = repository / '.agy-bridge' / 'queue'
    pending.mkdir(parents=True)
    (pending / 'x1.job').write_text('{}')
    expected = None
    for arguments in (('jobs', '-C', str(repository)), ('jobs', 'list', '-C', str(repository)),
                      ('jobs', '-C', str(repository), 'list')):
        assert _run_command(*arguments) == 0
        out = capsys.readouterr().out
        assert 'x1' in out and 'No implementation jobs. Delegation queues' not in out, (arguments, out)
        expected = out if expected is None else expected
        assert out == expected
    assert _run_command('jobs', '--json', '-C', str(repository), 'list') == 0
    assert json.loads(capsys.readouterr().out)['schema_version'] == jobs.SCHEMA_VERSION
    # A later option still wins over the same option given before `list`.
    assert _run_command('jobs', '-C', str(tmp_path / 'outside'), 'list', '-C', str(repository)) == 0
    assert 'x1' in capsys.readouterr().out


def test_malformed_implement_id_that_looks_like_a_queue_name_gets_a_hint():
    import pytest
    with pytest.raises(config.UsageError) as info:
        jobstore.job_path('review-auth')
    assert str(info.value).endswith('list jobs with: agy-bridge jobs list; queue job names are shown by '
                                    'agy-bridge jobs list -C <repo>')
    with pytest.raises(config.UsageError) as info:
        jobstore.job_path('../bad')
    assert 'queue job names' not in str(info.value)
