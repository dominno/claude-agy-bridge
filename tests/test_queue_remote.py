"""Queue jobs can run on an agy remote agent: the "remote" and "remote_project" job fields."""
import json

import pytest

from agy_bridge.app import queue
from agy_bridge.core import config


def job(**kw):
    return dict(dict(name='example', model='fake', task='review', files=['notes.md'], timeout='5s', context=False), **kw)


def test_remote_fields_are_validated(h):
    assert queue.validate(str(h.repo), job(remote=True))['remote'] is True
    assert queue.validate(str(h.repo), job(remote=True, remote_project='Bridge Tests'))['remote_project'] == 'Bridge Tests'
    with pytest.raises(config.UsageError, match='remote must be a boolean'):
        queue.validate(str(h.repo), job(remote='yes'))
    with pytest.raises(config.UsageError, match='needs "remote": true'):
        queue.validate(str(h.repo), job(remote_project='Bridge Tests'))
    with pytest.raises(config.UsageError, match='needs "remote": true'):
        queue.validate(str(h.repo), job(remote=True, remote_project=7))
    with pytest.raises(config.UsageError, match='printable project name'):
        queue.validate(str(h.repo), job(remote=True, remote_project='-x'))


def test_queue_rounds_pass_the_remote_choice_to_the_delegation(h, monkeypatch):
    seen = []

    class Result:
        def as_queue_record(self):
            return {'exit_code': 0}

    monkeypatch.setattr(queue, 'execute', lambda request: seen.append(request) or Result())
    queue.attempt(str(h.repo), job(remote=True, remote_project='P1'), ['notes.md'], 'review')
    queue.attempt(str(h.repo), job(), ['notes.md'], 'review')
    assert (seen[0].remote, seen[0].remote_project, seen[0].dry) == (True, 'P1', False)
    assert (seen[1].remote, seen[1].remote_project) == (False, None)


def test_queue_add_dry_run_shows_the_remote_agent(h):
    path = h.root / 'input.json'
    path.write_text(json.dumps(job(remote=True, remote_project='Bridge Tests')))
    r = h.raw('queue', 'add', str(path), '--repo', str(h.repo), '--dry-run')
    assert 'remote agent: yes, project Bridge Tests' in r.stdout, r.stdout + r.stderr
    path.write_text(json.dumps(job()))
    r = h.raw('queue', 'add', str(path), '--repo', str(h.repo), '--dry-run')
    assert 'remote agent: no' in r.stdout, r.stdout + r.stderr
