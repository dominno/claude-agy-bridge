import hashlib
import json
import os

import pytest

from agy_bridge import config, queue, stage


def _job(name="example"):
    return dict(name=name, task="Review private task text", files=["notes.md"])


def _answer(code=0):
    return dict(exit_code=code, answer="review complete" if code == 0 else "",
                files=[], not_sent=[], seconds=1, cite={})


def test_snapshot_observes_custom_queue_without_reading_job_payload(h, monkeypatch):
    paths = queue.Paths(str(h.repo), "custom/queue", "custom/results")
    assert queue.snapshot(paths)["jobs"] == []
    assert not (h.repo / "custom").exists()
    paths.add(_job("busy"))
    paths.add(_job("waiting"))
    paths.move(paths.add(_job("complete")), True)
    paths.move(paths.add(_job("refused")), False)
    inbox = h.repo / paths.queue
    (inbox / "unsafe.job").symlink_to(h.repo / "notes.md")
    os.mkfifo(inbox / "pipe.job")
    (inbox / "sk-{}.job".format("q" * 32)).write_text("unsafe", encoding="utf-8")
    (h.repo / "watch-only.job").write_text("outside scope", encoding="utf-8")
    monkeypatch.setattr(queue.safety, "read", lambda *args: pytest.fail("opened a job payload"))

    with paths.lock("watch"):
        paths.state("busy")
        view = queue.snapshot(paths)
        assert view["active"] and view["watcher_pid"] == os.getpid()
        assert view["queue_dir"] == "custom/queue"
        assert view["counts"] == dict(pending=1, running=1, done=1, failed=1)
        rows, warnings = queue.list_jobs(paths)
        assert {row["id"]: (row["phase"], row["active"]) for row in rows} == {
            "busy": ("running", True), "waiting": ("pending", False),
            "complete": ("done", False), "refused": ("failed", False),
        }
        assert {row["id"]: row["started_at"] is not None for row in rows} == {
            "busy": True, "waiting": False, "complete": False, "refused": False,
        }
        assert warnings and "private task text" not in json.dumps(view)
    (inbox / "watcher.pid").write_text(
        json.dumps(dict(pid=os.getpid(), mode="watch", job="busy", started=1)),
        encoding="utf-8",
    )
    view = queue.snapshot(paths)
    assert not view["active"] and view["counts"]["pending"] == 2


def test_snapshot_ignores_unreadable_active_watcher_details(h):
    paths = queue.Paths(str(h.repo))
    paths.add(_job())
    with paths.lock("watch"):
        os.ftruncate(paths.lock_fd, 0)
        os.lseek(paths.lock_fd, 0, os.SEEK_SET)
        os.write(paths.lock_fd, b'{"unexpected":' + b'[' * 2000)
        view = queue.snapshot(paths)
    assert view["active"]
    assert view["warnings"]
    assert [(row["id"], row["phase"]) for row in view["jobs"]] == [("example", "pending")]


def test_incomplete_old_result_is_not_successfully_reconciled(h, monkeypatch):
    paths = queue.Paths(str(h.repo))
    rel = paths.add(_job())
    digest = hashlib.sha256((h.repo / rel).read_bytes()).hexdigest()
    report = h.repo / paths.results / "example.md"
    report.parent.mkdir()
    incomplete = f"job_sha256: {digest}\nretained_exit_code: 0\n"
    report.write_text(incomplete, encoding="utf-8")
    monkeypatch.setattr(queue, "attempt", lambda *args: pytest.fail("resent a conflicting job"))

    assert queue.run(paths) == config.EXIT_USAGE
    assert report.read_text(encoding="utf-8") == incomplete
    assert not (h.repo / paths.queue / "done/example.job").exists()
    assert (h.repo / paths.queue / "failed/example.job").exists()


@pytest.mark.parametrize("leave_old_format", [False, True])
def test_complete_legacy_only_is_valid_without_a_checksum(h, monkeypatch, leave_old_format):
    paths = queue.Paths(str(h.repo))
    rel = paths.add(_job())
    monkeypatch.setattr(queue, "attempt", lambda *args: _answer())
    assert queue.run_job(paths, rel) == 0
    report = h.repo / paths.results / "example.md"
    text = report.read_text(encoding="utf-8").split(queue.RESULT_CHECKSUM, 1)[0]
    if leave_old_format:
        text = text.replace(queue.RESULT_FORMAT + "\n", "", 1)
    report.write_text(text, encoding="utf-8")
    (h.repo / paths.queue / "done/example.job").rename(h.repo / rel)

    if leave_old_format:
        monkeypatch.setattr(queue, "attempt", lambda *args: pytest.fail("resent a saved job"))
        assert queue.run_job(paths, rel) == 0
    else:
        # A v2 result of this exact job without its checksum footer is an interrupted write: set aside, re-run.
        sent = []
        monkeypatch.setattr(queue, "attempt", lambda *args: sent.append(1) or _answer())
        assert queue.run_job(paths, rel) == 0
        assert sent == [1] and (h.repo / paths.results / "example.md.partial").read_text(encoding="utf-8") == text
        assert (h.repo / paths.queue / "done/example.job").exists()


@pytest.mark.parametrize("code, archive", [(0, "done"), (config.EXIT_TIMEOUT, "failed")])
def test_recovers_only_the_exact_archived_inode(h, monkeypatch, code, archive):
    paths = queue.Paths(str(h.repo))
    rel = paths.add(_job())
    monkeypatch.setattr(queue, "attempt", lambda *args: _answer(code))
    assert queue.run_job(paths, rel) == code
    inbox = h.repo / rel
    archived = h.repo / paths.queue / archive / inbox.name
    os.link(archived, inbox)
    inode = archived.stat().st_ino
    monkeypatch.setattr(queue, "attempt", lambda *args: pytest.fail("resent a saved job"))

    unrelated = h.root / "unexpected-link"
    os.link(archived, unrelated)
    with pytest.raises(stage.Refused):
        queue.run_job(paths, rel)
    assert inbox.exists() and archived.exists() and unrelated.exists()
    unrelated.unlink()

    assert queue.run_job(paths, rel) == code
    assert not inbox.exists()
    assert archived.stat().st_ino == inode and archived.stat().st_nlink == 1


def test_archive_alias_needs_a_matching_saved_result(h, monkeypatch):
    paths = queue.Paths(str(h.repo))
    rel = paths.add(_job())
    inbox = h.repo / rel
    archived = h.repo / paths.queue / "done/example.job"
    archived.parent.mkdir()
    os.link(inbox, archived)
    monkeypatch.setattr(queue, "attempt", lambda *args: pytest.fail("resent a linked job"))

    with pytest.raises(config.UsageError, match="conflicts with existing result"):
        queue.run_job(paths, rel)
    assert archived.stat().st_ino == inbox.stat().st_ino
    assert archived.stat().st_nlink == 2


# ---------- council round 1: atomic result publication ----------

def _interrupted_queue(h):
    import hashlib
    import json as _json
    from agy_bridge import queue as q
    paths = q.Paths(str(h.repo))
    job = dict(name='example', model='fake', task='review', files=['notes.md'], timeout='5s', context=False)
    rel = paths.add(job)
    raw = (h.repo / rel).read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    results = h.repo / '.agy-bridge/results'
    results.mkdir(parents=True, exist_ok=True)
    head = '# example\n\n%s\n\n%s\njob_sha256: %s\nmodel: fake\nround: 1\nexit_code: 0\n' % (
        q.RESULT_WARNING, q.RESULT_FORMAT, digest)
    return paths, rel, results, head


def test_truncated_result_of_the_same_job_is_set_aside_and_rerun(h, capsys, monkeypatch):
    from agy_bridge import queue as q
    paths, rel, results, head = _interrupted_queue(h)
    monkeypatch.setattr(q, "attempt", lambda *args, **kw: _answer())
    (results / 'example.md').write_text(head + 'retained_exit_')  # writer died before the checksum footer
    assert q.run(paths) == 0
    assert 'interrupted result write; re-running' in capsys.readouterr().out
    assert (results / 'example.md.partial').read_text() == head + 'retained_exit_'
    text = (results / 'example.md').read_text()
    assert q._saved_result(paths, 'example', text.split('job_sha256: ')[1].split('\n')[0]) == 0
    assert (h.repo / '.agy-bridge/queue/done/example.job').exists()
    assert not [p for p in results.iterdir() if p.name.startswith('.agy-tmp-')]


def test_different_job_reusing_a_name_still_conflicts(h, monkeypatch):
    from agy_bridge import queue as q
    paths, rel, results, head = _interrupted_queue(h)
    monkeypatch.setattr(q, "attempt", lambda *args: pytest.fail("resent a conflicting job"))
    (results / 'example.md').write_text(head.replace('job_sha256: ', 'job_sha256: 0') + 'partial')
    assert q.run(paths) == config.EXIT_USAGE
    assert not (results / 'example.md.partial').exists()


def test_publish_never_replaces_and_leaves_no_temp(tmp_path):
    from agy_bridge import safety
    (tmp_path / 'r').mkdir()
    safety.publish(str(tmp_path), 'r/a.md', 'first\n')
    with pytest.raises(FileExistsError):
        safety.publish(str(tmp_path), 'r/a.md', 'second\n')
    assert (tmp_path / 'r/a.md').read_text() == 'first\n'
    assert sorted(p.name for p in (tmp_path / 'r').iterdir()) == ['a.md']
    assert (tmp_path / 'r/a.md').stat().st_mode & 0o777 == 0o600


def test_sweep_removes_old_and_already_published_temps_only(tmp_path):
    import time as _time
    from agy_bridge import safety
    folder = tmp_path / 'r'
    folder.mkdir()
    old = folder / '.agy-tmp-old.md-1'
    old.write_text('x')
    os.utime(old, (_time.time() - 7200, _time.time() - 7200))
    young = folder / '.agy-tmp-young.md-2'
    young.write_text('x')
    linked = folder / '.agy-tmp-done.md-3'
    linked.write_text('x')
    os.link(linked, folder / 'done.md')
    assert safety.sweep_temp(str(tmp_path), 'r') == 2
    assert sorted(p.name for p in folder.iterdir()) == ['.agy-tmp-young.md-2', 'done.md']
