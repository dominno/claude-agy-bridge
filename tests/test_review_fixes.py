"""Independent, offline regressions for the important security review findings."""
import os
import signal
import stat
import sys
import time

import pytest

from agy_bridge import config, report, runner, stage


def prepare(repo, files=(), globs=()):
    return stage.prepare(str(repo), files, globs, 'summarise', allow_non_git=True)


@pytest.mark.parametrize('name', ['ghp_abcdefghijklmnopqrstuv.txt', 'line\nbreak.txt', 'bad\udcff.txt'])
def test_selected_names_are_validated(tmp_path, name):
    if '\udcff' not in name:
        (tmp_path / name).write_text('ordinary text')
    with pytest.raises(stage.Refused):
        prepare(tmp_path, [name])


def test_generated_task_is_scanned_and_paths_are_quoted(tmp_path, monkeypatch):
    (tmp_path / 'a"b.txt').write_text('ordinary text')
    original = stage.scan_bytes
    scanned = []
    def scan(data):
        scanned.append(data)
        return original(data)
    monkeypatch.setattr(stage, 'scan_bytes', scan)
    _, prepared = prepare(tmp_path, ['a"b.txt'])
    text = stage.task_file_text(prepared.task, ['a"b.txt'])
    assert text.encode() in scanned
    assert '- "a\\"b.txt"' in text
    assert stage.printable('a\nb') != stage.printable('a?b')


def test_repository_interpreter_and_alias_are_excluded(h, monkeypatch):
    alias = h.root / 'alias'
    alias.symlink_to(h.repo, target_is_directory=True)
    planted = h.repo / 'review-interpreter'
    planted.write_text('#!/bin/sh\nif [ "$2" = "--version" ]; then echo 1.4.3; else echo planted; fi\n')
    planted.chmod(0o755)
    trusted = h.bindir / 'agy'
    trusted.write_text('#!/usr/bin/env review-interpreter\n')
    monkeypatch.setenv('PATH', os.pathsep.join([str(alias), str(h.repo), str(h.bindir), '/usr/bin', '/bin']))
    # Exercise the actual CLI environment and external env-based launcher.
    result = h.delegate(extra_env={'PATH': os.environ['PATH']})
    assert 'planted' not in result.stdout
    assert result.returncode != 0


def test_root_replacement_after_validation_is_refused(tmp_path, monkeypatch):
    root = tmp_path / 'repo'
    outside = tmp_path / 'outside'
    root.mkdir(); outside.mkdir()
    (root / 'a.txt').write_text('safe')
    (outside / 'a.txt').write_text('outside data')
    original = stage.check_root
    def swap(path, *args):
        original(path, *args)
        root.rename(tmp_path / 'old')
        root.symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(stage, 'check_root', swap)
    with pytest.raises(stage.Refused):
        prepare(root, ['a.txt'])


def test_output_parent_replacement_is_refused(h):
    parent = h.repo / '.agy-bridge' / 'nested'
    parent.mkdir(parents=True)
    path = report.resolve_out_path(str(parent / 'answer.txt'), str(h.repo))
    outside = h.root / 'outside'
    outside.mkdir()
    parent.rmdir()
    parent.symlink_to(outside, target_is_directory=True)
    with pytest.raises((report.OutPathRefused, OSError)):
        report.write_new_file(path, 'answer')
    assert not (outside / 'answer.txt').exists()


@pytest.mark.parametrize('kind', ['fifo', 'hardlink'])
def test_abnormal_log_is_refused_without_blocking_or_chmod(h, monkeypatch, kind):
    h.log_path.parent.mkdir(parents=True)
    monkeypatch.setattr(config, 'bridge_home', lambda: str(h.log_path.parent))
    target = h.root / 'untouched'
    target.write_text('keep')
    target.chmod(0o644)
    if kind == 'fifo':
        os.mkfifo(h.log_path)
    else:
        os.link(target, h.log_path)
    def alarm(*_):
        raise TimeoutError('log blocked')
    old = signal.signal(signal.SIGALRM, alarm)
    signal.alarm(2)
    started = time.monotonic()
    try:
        assert report.write_log({'exit': 4}) is not None
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)
    assert time.monotonic() - started < 1
    assert target.read_text() == 'keep'
    assert stat.S_IMODE(target.stat().st_mode) == 0o644


def test_late_stderr_marker_is_detected_across_reads(tmp_path):
    script = tmp_path / 'fake.py'
    script.write_text("import os, time\nos.write(2, b'x' * 70000)\nos.write(2, b'auto-')\ntime.sleep(.1)\nos.write(2, b'denied')\nprint('partial answer')\n")
    result = runner.run_agy([sys.executable, str(script)], str(tmp_path), config.build_agy_env(), 5, 10000)
    assert result.err_markers['soft_deny']
    assert result.err_bytes == 70011
    assert report.classify(result, report.clean_answer(result.out)) == config.EXIT_SOFT_DENY


def test_capped_result_is_incomplete_even_with_answer():
    result = runner.RunResult(0, b'partial', 0, dict(quota=False, auth=False, soft_deny=False), False, True, 0)
    assert report.classify(result, 'partial') != config.EXIT_OK


def test_glob_does_not_visit_unrelated_directories(tmp_path, monkeypatch):
    (tmp_path / 'src').mkdir()
    (tmp_path / 'src' / 'a.py').write_text('safe')
    (tmp_path / 'unrelated').mkdir()
    for i in range(10):
        (tmp_path / 'unrelated' / str(i)).mkdir()
    monkeypatch.setattr(stage, '_WALK_LIMIT', 5)
    assert prepare(tmp_path, globs=['src/*.py'])[1].file_count == 1
    with pytest.raises(config.UsageError):
        prepare(tmp_path, globs=['**/*.py'])


def test_glob_selected_entries_bounded_during_expansion(tmp_path, monkeypatch):
    for i in range(4):
        (tmp_path / ('%s.txt' % i)).write_text('safe')
    monkeypatch.setattr(config, 'MAX_FILES', 2)
    with pytest.raises(config.UsageError):
        stage._expand_glob(str(tmp_path), '*.txt', ())


def test_glob_preparation_deadline(tmp_path, monkeypatch):
    (tmp_path / 'a.txt').write_text('safe')
    ticks = iter([0, 1000, 1001, 1002])
    monkeypatch.setattr(stage.time, 'monotonic', lambda: next(ticks))
    with pytest.raises(config.UsageError):
        prepare(tmp_path, globs=['*.txt'])


def test_root_descriptor_is_held_for_all_reads(tmp_path, monkeypatch):
    root = tmp_path / 'repo'
    outside = tmp_path / 'outside'
    root.mkdir(); outside.mkdir()
    for name in ('a.txt', 'b.txt'):
        (root / name).write_text('safe')
        (outside / name).write_text('outside data')
    original = stage._read_regular
    swapped = []
    def read(fd, rel, limit):
        if not swapped:
            root.rename(tmp_path / 'old')
            root.symlink_to(outside, target_is_directory=True)
            swapped.append(True)
        return original(fd, rel, limit)
    monkeypatch.setattr(stage, '_read_regular', read)
    assert [entry.data for entry in prepare(root, ['a.txt', 'b.txt'])[1].entries] == [b'safe', b'safe']


@pytest.mark.parametrize('limit', ['_DEPTH_LIMIT', '_DIRECTORY_LIMIT'])
def test_directory_and_depth_limits(tmp_path, monkeypatch, limit):
    (tmp_path / 'a' / 'b' / 'c').mkdir(parents=True)
    monkeypatch.setattr(stage, limit, 2)
    with pytest.raises(config.UsageError):
        prepare(tmp_path, globs=['**/*.txt'])


def test_generated_task_scan_hit_refuses(tmp_path, monkeypatch):
    (tmp_path / 'a.txt').write_text('safe')
    monkeypatch.setattr(stage, 'scan_bytes', lambda data: {'fixture': 1} if b'Files in this folder' in data else {})
    with pytest.raises(stage.Refused):
        prepare(tmp_path, ['a.txt'])


def test_partial_denial_is_visible_and_unsuccessful(h):
    (h.bindir / 'agy').write_text(
        '#!/bin/sh\nif [ "$1" = "--version" ]; then echo 1.4.3; exit; fi\n'
        'echo partial-answer\necho auto-denied >&2\n')
    result = h.delegate()
    assert result.returncode == config.EXIT_SOFT_DENY
    assert 'partial-answer' in result.stdout
    assert 'refused' in result.stderr and 'incomplete' in result.stderr


def test_adjacent_recursive_globs(tmp_path):
    (tmp_path / 'src').mkdir()
    (tmp_path / 'src' / 'a.py').write_text('safe')
    assert prepare(tmp_path, globs=['**/**/src/*.py'])[1].file_count == 1


def test_multiple_globs_share_the_pinned_root(tmp_path):
    (tmp_path / 'a.txt').write_text('safe')
    (tmp_path / 'b.py').write_text('safe')
    assert [entry.rel for entry in prepare(tmp_path, globs=['*.txt', '*.py'])[1].entries] == ['a.txt', 'b.py']
