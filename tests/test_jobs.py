import argparse
import json

from agy_bridge import config, jobs, jobstore, queue


def _run(*arguments):
    parser = argparse.ArgumentParser()
    jobs.add_arguments(parser.add_subparsers(required=True))
    args = parser.parse_args(['jobs', 'list', *arguments])
    return args.handler(args)


def _repo(path):
    (path / '.git').mkdir(parents=True)
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
    assert not result.err and 'private' not in result.out
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
    assert not (tmp_path / 'bridge-home').exists()
