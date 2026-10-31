import concurrent.futures
import hashlib
import json
import os
import stat

import pytest

from agy_bridge import events


def records(h):
    return [json.loads(line) for line in (h.repo / '.agy-bridge/events.jsonl').read_text().splitlines()]


def test_events_append_metadata_only_with_full_hashes_and_private_modes(h):
    (h.repo / 'notes.md').write_text('UNIQUE_FILE_CONTENT\n')
    h.configure(answer='UNIQUE_ANSWER_TEXT')
    result = h.delegate('--file', 'notes.md', '--model', 'test-model', task='UNIQUE_TASK_TEXT')
    assert result.returncode == 0, result.stderr
    entry, = records(h)
    assert set(entry) == {'ts', 'model', 'files', 'not_sent', 'total_bytes', 'seconds', 'exit_code', 'truncated', 'dry_run'}
    assert entry['model'] == 'test-model'
    assert entry['total_bytes'] == sum(item['bytes'] for item in entry['files'])
    assert entry['exit_code'] == 0 and not entry['truncated'] and not entry['dry_run']
    assert entry['seconds'] >= 0 and entry['ts'].endswith('Z')
    assert entry['not_sent'] == []
    for item in entry['files']:
        raw = (h.repo / item['path']).read_bytes()
        assert item['sha256'] == hashlib.sha256(raw).hexdigest() and item['bytes'] == len(raw)
    text = (h.repo / '.agy-bridge/events.jsonl').read_text()
    for forbidden in ('UNIQUE_FILE_CONTENT', 'UNIQUE_ANSWER_TEXT', 'UNIQUE_TASK_TEXT'):
        assert forbidden not in text
    assert stat.S_IMODE((h.repo / '.agy-bridge').stat().st_mode) == 0o700
    assert stat.S_IMODE((h.repo / '.agy-bridge/events.jsonl').stat().st_mode) == 0o600
    h.delegate()
    assert len(records(h)) == 2


@pytest.mark.parametrize('command,flags', [('dry-run', []), ('delegate', ['--dry-run'])])
def test_dry_run_records_no_sent_files_and_correct_exit(h, command, flags):
    result = h.run(command, '--file', 'notes.md', '--cite', *flags)
    assert result.returncode == 0
    record, = records(h)
    assert record['dry_run'] and record['exit_code'] == 0
    assert record['files'] == [] and record['total_bytes'] == 0
    assert record['cite'] == {'verified': 0, 'unverified': 0}
    assert not h.record_path.exists() and not h.log_path.exists()


@pytest.mark.parametrize('mode,exit_code', [('quota', 10), ('empty', 3), ('auth', 11), ('soft_denial', 15)])
def test_failed_provider_still_received_files_and_is_journaled(h, mode, exit_code):
    h.configure(mode=mode)
    result = h.delegate()
    assert result.returncode == exit_code
    entry, = records(h)
    assert entry['exit_code'] == exit_code
    assert entry['files'][0]['path'] == 'src/a.py'
    assert entry['not_sent'] == []


def test_unsupported_provider_never_received_files(h):
    h.configure(version='99.0.0')
    result = h.delegate()
    assert result.returncode == 14
    record, = records(h)
    assert record['exit_code'] == 14 and record['files'] == []
    assert record['not_sent'] == [{'path': 'src/a.py', 'rule': 'TASK'}]


def test_usage_error_on_a_safe_root_is_journaled(h):
    result = h.delegate('--max-bytes', '0')
    assert result.returncode == 1
    assert records(h)[0]['exit_code'] == 1


@pytest.mark.parametrize('kind', ['folder-symlink', 'file-symlink', 'hardlink', 'fifo', 'directory'])
def test_hostile_event_path_is_never_followed_modified_or_blocks(h, kind):
    scratch = h.repo / '.agy-bridge'
    target = h.root / 'target'
    if kind == 'folder-symlink':
        target.mkdir()
        scratch.symlink_to(target, target_is_directory=True)
    else:
        scratch.mkdir()
        if kind == 'file-symlink':
            target.write_text('unchanged')
            target.chmod(0o644)
            (scratch / 'events.jsonl').symlink_to(target)
        elif kind == 'hardlink':
            target.write_text('unchanged')
            target.chmod(0o644)
            os.link(target, scratch / 'events.jsonl')
        elif kind == 'fifo':
            os.mkfifo(scratch / 'events.jsonl')
        else:
            (scratch / 'events.jsonl').mkdir()
    result = h.delegate(timeout=5)
    assert result.returncode == 4 and 'event' in result.stderr
    assert not h.record_path.exists()
    if kind == 'folder-symlink':
        assert list(target.iterdir()) == []
    elif kind in ('file-symlink', 'hardlink'):
        assert target.read_text() == 'unchanged'
        assert stat.S_IMODE(target.stat().st_mode) == 0o644


def test_event_permissions_repaired_only_after_safety_checks(h):
    scratch = h.repo / '.agy-bridge'
    scratch.mkdir(mode=0o755)
    log = scratch / 'events.jsonl'
    log.write_text('')
    log.chmod(0o666)
    result = h.delegate()
    assert result.returncode == 0
    assert stat.S_IMODE(scratch.stat().st_mode) == 0o700
    assert stat.S_IMODE(log.stat().st_mode) == 0o600


def test_event_path_intermediate_symlink_is_refused(h):
    alias = h.root / 'alias'
    alias.symlink_to(h.repo, target_is_directory=True)
    # open_log must anchor every component, including parent components.
    with pytest.raises(events.EventError):
        events.open_log(str(alias))
    assert not (h.repo / '.agy-bridge').exists()


def test_events_tail_is_ordered_json_and_does_not_run_agy_or_append(h):
    for n in range(3):
        h.delegate('--model', 'model-%d' % n)
    h.record_path.unlink()
    result = h.raw('events', '--repo', str(h.repo), '--tail', '2')
    assert result.returncode == 0, result.stderr
    assert [json.loads(line)['model'] for line in result.stdout.splitlines()] == ['model-1', 'model-2']
    assert len(records(h)) == 3 and not h.record_path.exists()


def test_events_tail_absent_is_empty_and_creates_nothing(h):
    result = h.raw('events', '--repo', str(h.repo))
    assert result.returncode == 0 and result.stdout == ''
    assert not (h.repo / '.agy-bridge').exists()


@pytest.mark.parametrize('tail', ['0', '-1', 'bad', '10001', '9' * 50])
def test_events_tail_rejects_invalid_counts(h, tail):
    result = h.raw('events', '--repo', str(h.repo), '--tail', tail)
    assert result.returncode == 1


def test_events_tail_fails_plainly_on_malformed_json(h):
    h.delegate()
    (h.repo / '.agy-bridge/events.jsonl').write_text('not json\n')
    result = h.raw('events', '--repo', str(h.repo))
    assert result.returncode == 4 and 'Traceback' not in result.stderr


def test_events_tail_escapes_injected_terminal_controls(h):
    h.delegate()
    (h.repo / '.agy-bridge/events.jsonl').write_text(json.dumps({'ts': '\x1b[31mattack'}) + '\n')
    result = h.raw('events', '--repo', str(h.repo))
    assert result.returncode == 0 and '\x1b' not in result.stdout and '\\u001b' in result.stdout


def test_parallel_event_append_keeps_complete_json_lines(h):
    def append(n):
        fd = events.open_log(str(h.repo))
        try:
            events.append(fd, {'model': 'model-%d' % n})
        finally:
            os.close(fd)
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(append, range(30)))
    assert {entry['model'] for entry in records(h)} == {'model-%d' % n for n in range(30)}


def test_refused_unsafe_root_does_not_create_an_event_folder(h):
    result = h.raw('delegate', '--repo', str(h.home), '--allow-non-git-root', '--file', 'x', '--', 'read')
    assert result.returncode == 4 and 'event not recorded' in result.stderr
    assert not (h.home / '.agy-bridge/events.jsonl').exists()


def test_first_create_transient_enoent_retries_the_same_safe_open(h, monkeypatch):
    original = events.os.open
    tried = []
    def flaky(path, flags, *args, **kw):
        if path == 'events.jsonl':
            tried.append((flags, kw['dir_fd']))
            if len(tried) == 1:
                raise FileNotFoundError('simulated concurrent creation')
        return original(path, flags, *args, **kw)
    monkeypatch.setattr(events.os, 'open', flaky)
    fd = events.open_log(str(h.repo))
    os.close(fd)
    assert len(tried) == 2 and tried[0] == tried[1]
    assert tried[0][0] & os.O_NOFOLLOW


def test_append_failure_is_explicit_and_preserves_success_exit(h, monkeypatch, capsys):
    from agy_bridge import cli
    def fail_append(fd, record):
        raise events.EventError('could not append the event record')
    monkeypatch.setattr(events, 'append', fail_append)
    # Dry-run avoids subprocess setup while still exercising the full journal path.
    monkeypatch.setattr(cli, '_argv_preview', lambda *args: 'No program was run.')
    code = cli.main(['dry-run', '--repo', str(h.repo), '--file', 'notes.md', '--', 'read'])
    assert code == 0
    assert 'could not append the event record' in capsys.readouterr().err
    assert not h.record_path.exists()
