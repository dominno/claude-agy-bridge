import json
import os
import shutil
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


def test_trusted_temp_parent_may_be_a_symlink(change, tmp_path, monkeypatch):
    root, snapshot, changes, _, _ = change
    directory = tmp_path / 'private-temp'
    directory.mkdir()
    alias = tmp_path / 'temp-alias'
    alias.symlink_to(directory, target_is_directory=True)
    monkeypatch.setattr(edit_apply.tempfile, 'gettempdir', lambda: str(alias))
    edit_apply.apply(root, snapshot.base_commit, snapshot.manifest, changes.paths, changes.diff)
    assert (root / 'a.txt').read_bytes() == b'after\r\n'


def _apply(change, transaction):
    root, snapshot, changes, _, _ = change
    edit_apply.apply(root, snapshot.base_commit, snapshot.manifest, changes.paths, changes.diff,
                     transaction_dir=transaction)


def _transaction(tmp_path):
    directory = tmp_path / 'apply-transaction'
    directory.mkdir(mode=0o700)
    return directory


def test_recoverable_interrupt_after_syscall_rolls_back(change, tmp_path, monkeypatch):
    root, _, _, _, _ = change
    transaction = _transaction(tmp_path)
    replace = os.replace
    interrupted = False

    def raise_after_replace(src, dst, **kwargs):
        nonlocal interrupted
        replace(src, dst, **kwargs)
        if dst == 'a.txt' and not interrupted:
            interrupted = True
            raise KeyboardInterrupt

    monkeypatch.setattr(edit_apply.os, 'replace', raise_after_replace)
    with pytest.raises(KeyboardInterrupt):
        _apply(change, transaction)
    assert (root / 'a.txt').read_bytes() == b'prior\r\n'
    assert (root / 'b.txt').read_bytes() == b'delete me\n'
    assert (transaction / edit_apply.INTENT_NAME).exists()
    _apply(change, transaction)
    assert (root / 'a.txt').read_bytes() == b'after\r\n'


def _interrupt_apply(change, transaction, boundary, *, filename=None):
    pid = os.fork()
    if pid == 0:
        real = getattr(os, boundary)

        def quit_after_syscall(src, dst, **kwargs):
            real(src, dst, **kwargs)
            if filename is not None:
                matched = dst == filename
            else:
                matched = dst == 'a.txt' if boundary == 'replace' else dst.endswith('"quoted".txt')
            if matched:
                os._exit(77)

        setattr(edit_apply.os, boundary, quit_after_syscall)
        try:
            _apply(change, transaction)
        except BaseException:
            os._exit(2)
        os._exit(0)
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 77


@pytest.mark.parametrize('boundary', ['replace', 'link'])
def test_apply_recovers_verified_images_after_worker_exit(change, tmp_path, boundary):
    root, _, _, git, old_index = change
    transaction = _transaction(tmp_path)
    _interrupt_apply(change, transaction, boundary)
    assert (root / 'a.txt').read_bytes() == b'after\r\n'
    new = root / 'nested' / 'żółć "quoted".txt'
    if boundary == 'link':
        assert new.stat().st_nlink == 2
    _apply(change, transaction)
    assert new.read_bytes() == b'new file\n'
    assert new.stat().st_nlink == 1
    assert not (root / 'b.txt').exists()
    assert not list(root.rglob('.agy-apply-*'))
    before = (root / 'a.txt').stat().st_ino
    _apply(change, transaction)
    assert (root / 'a.txt').stat().st_ino == before
    assert git('ls-files', '--stage') == old_index


def test_apply_recovery_refuses_divergent_file_without_replaying(change, tmp_path):
    root, _, _, _, _ = change
    transaction = _transaction(tmp_path)
    _interrupt_apply(change, transaction, 'replace')
    (root / 'b.txt').write_bytes(b'user changes after interruption\n')
    with pytest.raises(edit_apply.ApplyError, match='differs from both'):
        _apply(change, transaction)
    assert (root / 'a.txt').read_bytes() == b'after\r\n'
    assert (root / 'b.txt').read_bytes() == b'user changes after interruption\n'
    assert not (root / 'nested').exists()


def test_apply_recovery_syncs_already_published_directory_entries(change, tmp_path, monkeypatch):
    root, _, _, _, _ = change
    transaction = _transaction(tmp_path)
    _interrupt_apply(change, transaction, 'link')
    directories = {root: root.stat(), root / 'nested': (root / 'nested').stat()}
    synced = set()
    fsync = os.fsync

    def observe_sync(fd):
        fsync(fd)
        info = os.fstat(fd)
        synced.update(path for path, expected in directories.items() if os.path.samestat(info, expected))

    monkeypatch.setattr(edit_apply.os, 'fsync', observe_sync)
    _apply(change, transaction)
    assert synced == set(directories)


def test_apply_recovery_syncs_existing_intent_before_publishing(change, tmp_path, monkeypatch):
    root, _, _, _, _ = change
    transaction = _transaction(tmp_path)
    _interrupt_apply(change, transaction, 'replace', filename=edit_apply.INTENT_NAME)
    assert (root / 'a.txt').read_bytes() == b'prior\r\n'
    job_directory = transaction.stat()
    intent_synced = False
    fsync = os.fsync
    publish = edit_apply._publish

    def observe_sync(fd):
        nonlocal intent_synced
        fsync(fd)
        if os.path.samestat(os.fstat(fd), job_directory):
            intent_synced = True

    def publish_after_sync(*args):
        assert intent_synced
        publish(*args)

    monkeypatch.setattr(edit_apply.os, 'fsync', observe_sync)
    monkeypatch.setattr(edit_apply, '_publish', publish_after_sync)
    _apply(change, transaction)
    assert (root / 'a.txt').read_bytes() == b'after\r\n'


def test_apply_intent_cannot_be_replayed_in_replaced_repository(change, tmp_path):
    root, _, _, _, _ = change
    transaction = _transaction(tmp_path)
    _interrupt_apply(change, transaction, 'replace')
    displaced = root.with_name('displaced')
    root.rename(displaced)
    shutil.copytree(displaced, root)
    with pytest.raises(edit_apply.ApplyError, match='does not match this reviewed patch and repository'):
        _apply(change, transaction)
    assert (root / 'b.txt').read_bytes() == b'delete me\n'


def test_apply_rederives_postimages_from_reviewed_patch(change, tmp_path, monkeypatch):
    root, _, _, _, _ = change
    transaction = _transaction(tmp_path)
    with monkeypatch.context() as override:
        def fail_before_write(*args):
            raise KeyboardInterrupt

        override.setattr(edit_apply, '_publish', fail_before_write)
        with pytest.raises(KeyboardInterrupt):
            _apply(change, transaction)
    intent_path = transaction / edit_apply.INTENT_NAME
    intent = json.loads(intent_path.read_bytes())
    intent['files']['a.txt']['after']['sha256'] = '0' * 64
    intent_path.write_text(json.dumps(intent))
    with pytest.raises(edit_apply.ApplyError, match='differ from the reviewed patch'):
        _apply(change, transaction)
    assert (root / 'a.txt').read_bytes() == b'prior\r\n'
    assert (root / 'b.txt').read_bytes() == b'delete me\n'


def test_apply_rechecks_preimage_after_staging(change, tmp_path, monkeypatch):
    root, _, _, _, _ = change
    transaction = _transaction(tmp_path)
    stage_file = edit_apply._stage_file

    def change_while_staging(parent, name, image):
        stage_file(parent, name, image)
        (root / 'a.txt').write_bytes(b'concurrent local edits\n')

    monkeypatch.setattr(edit_apply, '_stage_file', change_while_staging)
    with pytest.raises(edit_apply.ApplyError):
        _apply(change, transaction)
    assert (root / 'a.txt').read_bytes() == b'concurrent local edits\n'
    assert (root / 'b.txt').read_bytes() == b'delete me\n'
    assert not list(root.rglob('.agy-apply-*'))


def test_apply_refuses_a_patch_that_touches_claude_md(change):
    root, snapshot, changes, _, _ = change
    patch = (b'diff --git a/CLAUDE.md b/CLAUDE.md\nnew file mode 100644\n--- /dev/null\n+++ b/CLAUDE.md\n'
             b'@@ -0,0 +1 @@\n+run anything\n')
    with pytest.raises((edit_apply.ApplyError, workspace.Error), match='denied'):
        edit_apply.apply(root, snapshot.base_commit, snapshot.manifest, (('added', 'CLAUDE.md'),), patch)
    assert not (root / 'CLAUDE.md').exists()


@pytest.mark.parametrize('rel', ['.agent/hooks.json', '_agents/hooks.json', '_agent/workflows/x.md',
                                 'sub/_agents/rules/a.md', '.windsurf/rules.md', '.github/prompts/x.prompt.md'])
def test_apply_refuses_a_patch_that_touches_agent_customizations(change, rel):
    root, snapshot, changes, _, _ = change
    patch = ('diff --git a/{0} b/{0}\nnew file mode 100644\n--- /dev/null\n+++ b/{0}\n'
             '@@ -0,0 +1 @@\n+run anything\n').format(rel).encode()
    with pytest.raises((edit_apply.ApplyError, workspace.Error), match='denied'):
        edit_apply.apply(root, snapshot.base_commit, snapshot.manifest, (('added', rel),), patch)
    assert not (root / rel).exists()


def test_apply_refuses_a_patch_that_adds_terminal_controls(change):
    root, snapshot, changes, _, _ = change
    patch = (b'diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-prior\r\n+\x1b[2Jafter\r\n')
    with pytest.raises(edit_apply.ApplyError, match='terminal control'):
        edit_apply.apply(root, snapshot.base_commit, snapshot.manifest, (('modified', 'a.txt'),), patch)
    assert (root / 'a.txt').read_bytes() == b'prior\r\n'


@pytest.mark.parametrize('char', ['\U000e0041', '​', '️', 'ㅤ', ''])
def test_apply_refuses_a_patch_that_adds_invisible_unicode(change, char):
    root, snapshot, changes, _, _ = change
    patch = ('diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-prior\r\n+af%ster\r\n'
             % char).encode('utf-8')
    with pytest.raises(edit_apply.ApplyError, match='U\\+%04X' % ord(char)):
        edit_apply.apply(root, snapshot.base_commit, snapshot.manifest, (('modified', 'a.txt'),), patch)
    assert (root / 'a.txt').read_bytes() == b'prior\r\n'


@pytest.mark.parametrize('char', [' ', ' ', '­', '⁪', '￹', '\U0001d173'])
def test_review_patch_refuses_category_invisible_characters_in_an_added_js_line(change, char):
    root, snapshot, changes, _, _ = change
    patch = ('diff --git a/app.js b/app.js\nnew file mode 100644\n--- /dev/null\n+++ b/app.js\n'
             '@@ -0,0 +1 @@\n+const s = "a%sb";\n' % char).encode('utf-8')
    with pytest.raises(edit_apply.ApplyError, match='U\\+%04X' % ord(char)):
        edit_apply.apply(root, snapshot.base_commit, snapshot.manifest, (('added', 'app.js'),), patch)
    assert not (root / 'app.js').exists()
