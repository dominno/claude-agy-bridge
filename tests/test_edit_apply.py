import os
import stat
import subprocess

import pytest

from agy_bridge import edit_apply, workspace


@pytest.fixture
def change(tmp_path):
    root = tmp_path / 'source'
    root.mkdir()

    def git(*args):
        return subprocess.run(['git', *args], cwd=root, check=True, capture_output=True).stdout

    git('init', '-q', '--template=')
    (root / 'a.txt').write_bytes(b'prior\r\n')
    (root / 'b.txt').write_bytes(b'delete me\n')
    (root / '.gitattributes').write_text('*.txt text eol=lf\n')
    git('add', '--all')
    git('-c', 'user.name=Test', '-c', 'user.email=test@example.org', 'commit', '-qm', 'base')
    old_index = git('ls-files', '--stage')
    snapshot = workspace.prepare(str(root), ['a.txt', 'b.txt'], [], 'implement a fixture change')
    job = tmp_path / 'job'
    job.mkdir()
    work = workspace.materialize(snapshot, job)
    (work.tree / 'a.txt').write_bytes(b'after\r\n')
    (work.tree / 'a.txt').chmod(0o700)
    (work.tree / 'b.txt').unlink()
    new = work.tree / 'nested' / 'żółć "quoted".txt'
    new.parent.mkdir()
    new.write_bytes(b'new file\n')
    changes = workspace.export_diff(work)
    return root, snapshot, changes, git, old_index


def test_apply_uses_exact_reviewed_bytes_and_does_not_stage(change):
    root, snapshot, changes, git, old_index = change
    edit_apply.apply(root, snapshot.base_commit, snapshot.manifest, changes.paths, changes.diff)
    assert (root / 'a.txt').read_bytes() == b'after\r\n'
    assert (root / 'a.txt').stat().st_mode & stat.S_IXUSR
    assert not (root / 'b.txt').exists()
    assert (root / 'nested' / 'żółć "quoted".txt').read_bytes() == b'new file\n'
    assert git('ls-files', '--stage') == old_index


def test_apply_rejects_preimage_drift_before_modifying_any_file(change):
    root, snapshot, changes, _, _ = change
    (root / 'a.txt').write_bytes(b'local unapproved changes\n')
    with pytest.raises(edit_apply.ApplyError, match='approved file changed'):
        edit_apply.apply(root, snapshot.base_commit, snapshot.manifest, changes.paths, changes.diff)
    assert (root / 'b.txt').read_bytes() == b'delete me\n'
    assert not (root / 'nested').exists()


def test_apply_rejects_path_swapped_for_symlink(change, tmp_path):
    root, snapshot, changes, _, _ = change
    outside = tmp_path / 'outside'
    outside.mkdir()
    (root / 'nested').symlink_to(outside, target_is_directory=True)
    with pytest.raises(edit_apply.ApplyError):
        edit_apply.apply(root, snapshot.base_commit, snapshot.manifest, changes.paths, changes.diff)
    assert (root / 'a.txt').read_bytes() == b'prior\r\n'
    assert list(outside.iterdir()) == []


def test_apply_rejects_patch_with_unreviewed_path(change):
    root, snapshot, changes, _, _ = change
    with pytest.raises(edit_apply.ApplyError, match='unapproved path'):
        edit_apply.apply(root, snapshot.base_commit, snapshot.manifest, changes.paths[:2], changes.diff)
    assert (root / 'b.txt').read_bytes() == b'delete me\n'
    assert not (root / 'nested').exists()


def test_apply_rolls_back_when_publication_fails(change, monkeypatch):
    root, snapshot, changes, _, _ = change
    real_link = os.link

    def link(src, dst, **kwargs):
        if dst.endswith('"quoted".txt'):
            raise PermissionError('simulated final write failure')
        return real_link(src, dst, **kwargs)

    monkeypatch.setattr(edit_apply.os, 'link', link)
    with pytest.raises(edit_apply.ApplyError):
        edit_apply.apply(root, snapshot.base_commit, snapshot.manifest, changes.paths, changes.diff)
    assert (root / 'a.txt').read_bytes() == b'prior\r\n'
    assert not (root / 'a.txt').stat().st_mode & stat.S_IXUSR
    assert (root / 'b.txt').read_bytes() == b'delete me\n'
    assert not (root / 'nested').exists()
    assert list(root.glob('.agy-apply-*')) == []
