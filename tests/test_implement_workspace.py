import os
import subprocess

import pytest

from agy_bridge import stage, workspace


def git(root, *args, data=b''):
    return subprocess.run([workspace.git_executable(), *args], cwd=root, input=data,
                          env=workspace.git_env(root), check=True, capture_output=True).stdout


@pytest.fixture
def worktree(tmp_path):
    repo = tmp_path / 'repo'
    repo.mkdir()
    git(repo, 'init', '--quiet', '--template=')
    (repo / 'tool script.sh').write_bytes(b'#!/bin/sh\nexit 1\n')
    (repo / 'tool script.sh').chmod(0o700)
    (repo / 'empty.txt').touch()
    git(repo, 'add', '--', 'tool script.sh', 'empty.txt')
    git(repo, '-c', 'user.name=Fixture', '-c', 'user.email=test@invalid', 'commit', '--quiet', '-m', 'fixture')
    job = tmp_path / 'job'
    job.mkdir(mode=0o700)
    return repo, job


def test_snapshot_and_diff_are_byte_accurate_and_apply(worktree):
    repo, job = worktree
    (repo / 'tool script.sh').write_bytes(b'#!/bin/sh\r\nexit 2\r\n')
    (repo / '.agents').mkdir()
    (repo / '.agents' / 'hooks.json').write_text('{}')
    (repo / 'AGENTS.md').write_text('instructions')
    snapshot = workspace.prepare(repo, [], ['**'], 'Implement the requested fix')
    assert snapshot.base_commit == git(repo, 'rev-parse', 'HEAD').decode().strip()
    assert [item['path'] for item in snapshot.manifest] == ['empty.txt', 'tool script.sh']
    assert snapshot.manifest[1]['executable']
    assert {item.where for item in snapshot.not_sent} >= {'.agents', 'AGENTS.md'}
    state = workspace.materialize(snapshot, job)
    assert not (state.baseline / 'tool script.sh').stat().st_mode & 0o200
    assert (state.tree / 'tool script.sh').stat().st_mode & 0o100
    assert (state.tree / 'tool script.sh').read_bytes() == b'#!/bin/sh\r\nexit 2\r\n'
    assert workspace.export_diff(state).paths == ()
    initial_copy = workspace.copy_candidate(state, job / 'initial-check')
    assert (initial_copy / 'tool script.sh').stat().st_mode & 0o100

    (state.tree / 'tool script.sh').write_bytes(b'#!/bin/sh\r\nexit 0')
    (state.tree / 'tool script.sh').chmod(0o600)
    (state.tree / 'empty.txt').unlink()
    (state.tree / 'caf\xe9 notes.txt').write_text('new result')
    reloaded = workspace.load(job, snapshot.manifest, snapshot.base_commit, snapshot.extra_deny)
    result = workspace.export_diff(reloaded)
    assert result.paths == (('added', 'caf\xe9 notes.txt'), ('deleted', 'empty.txt'), ('modified', 'tool script.sh'))
    assert (repo / 'tool script.sh').read_bytes() == b'#!/bin/sh\r\nexit 2\r\n'
    git(repo, 'apply', '--check', data=result.diff)
    git(repo, 'apply', data=result.diff)
    assert (repo / 'tool script.sh').read_bytes() == b'#!/bin/sh\r\nexit 0'
    assert not (repo / 'tool script.sh').stat().st_mode & 0o100
    assert (repo / 'caf\xe9 notes.txt').read_text() == 'new result'
    assert not (repo / 'empty.txt').exists()

    (state.tree / 'tool script.sh').write_text('subsequent change')
    exact = workspace.copy_candidate(reloaded, job / 'checks', changes=result)
    assert (exact / 'tool script.sh').read_bytes() == b'#!/bin/sh\r\nexit 0'
    (exact / 'tool script.sh').write_text('test-generated change')
    assert (state.tree / 'tool script.sh').read_text() == 'subsequent change'


@pytest.mark.parametrize('relative, kind', [('.agents/hooks.json', 'file'), ('.git', 'directory'),
                                             ('link.txt', 'symlink'), ('hard.txt', 'hardlink'),
                                             ('pipe.txt', 'fifo'), ('binary.txt', 'binary'),
                                             ('EMPTY.txt', 'file')])
def test_unsupported_candidate_paths_are_refused(worktree, relative, kind):
    repo, job = worktree
    state = workspace.materialize(workspace.prepare(repo, ['empty.txt'], [], 'Modify text'), job)
    path = state.tree / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    if kind == 'directory':
        path.mkdir()
    elif kind == 'symlink':
        path.symlink_to(repo / 'empty.txt')
    elif kind == 'hardlink':
        os.link(repo / 'empty.txt', path)
    elif kind == 'fifo':
        os.mkfifo(path)
    elif kind == 'binary':
        path.write_bytes(b'hello\x00')
    else:
        if path.exists():
            pytest.skip('case-insensitive filesystem')
        path.write_text('unapproved')
    with pytest.raises(workspace.Error):
        workspace.validate_candidate(state)
    with pytest.raises(workspace.Error):
        workspace.export_diff(state)


def test_explicit_discovery_file_and_tampered_baseline_are_refused(worktree):
    repo, job = worktree
    (repo / 'GEMINI.md').write_text('instructions')
    with pytest.raises(stage.Refused):
        workspace.prepare(repo, ['GEMINI.md'], [], 'Modify text')
    state = workspace.materialize(workspace.prepare(repo, ['empty.txt'], [], 'Modify text'), job)
    marker = state.baseline / 'empty.txt'
    marker.chmod(0o600)
    marker.write_text('tampered')
    with pytest.raises(workspace.Error, match='no longer matches'):
        workspace.export_diff(state)


def test_all_candidate_files_can_be_deleted(worktree):
    repo, job = worktree
    state = workspace.materialize(workspace.prepare(repo, ['empty.txt'], [], 'Remove file'), job)
    (state.tree / 'empty.txt').unlink()
    result = workspace.export_diff(state)
    assert result.paths == (('deleted', 'empty.txt'),)
    git(repo, 'apply', '--check', data=result.diff)
