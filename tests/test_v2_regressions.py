"""Regressions for the independent v2 review and its bridgecheck probes."""
import errno
import json
import os
import signal
import time

import pytest

from agy_bridge import citations, cli, events, scan, stage


KEY = '9f8e7d6c5b4a39281706f5e4d3c2b1a0'  # invented, unshaped credential


@pytest.mark.parametrize('name', ['SERVICE_API_KEY', 'EXAMPLE_API_KEY'])
@pytest.mark.parametrize('separator', [' is ', ' -> ', ' | ', '  ', ' $ ', '\u00a0= ', '\uff1a ', ' to '])
@pytest.mark.parametrize('newline', ['', '\n\n', '\r\r', '\r\n \t\r\n'])
def test_allow_fragment_never_exempts_unshaped_value(h, name, separator, newline):
    literal = '`%s`' % name
    (h.repo / 'allow.txt').write_text(literal + '\n')
    payload = '| ' + literal + newline + separator + KEY + ' |\n'
    raw = payload.encode()
    assert scan.scan_bytes(raw)
    assert scan.scan_bytes(raw, [literal]), 'allowlist suppressed the only credential signal'
    (h.repo / 'doc.md').write_bytes(raw)
    result = h.run('delegate', '--file', 'doc.md', '--allow-fragment-file', 'allow.txt')
    assert result.returncode == 4
    assert not h.record_path.exists()
    assert KEY not in result.stdout + result.stderr


@pytest.mark.parametrize('length', [16, 32])
@pytest.mark.parametrize('token_char', ['a', 'Z', '0', '_', '-', '+', '/', '='])
def test_allow_fragment_checks_every_token_character_at_threshold(length, token_char):
    raw = ('`SERVICE_API_KEY` -> ' + token_char * length).encode()
    assert scan.scan_bytes(raw, ['`SERVICE_API_KEY`'])


def test_one_mib_hostile_citation_parses_in_under_half_second():
    fragment = ' | QUOTE: "'
    hostile = ('FILE: a' + fragment * (1024 * 1024 // len(fragment) + 1))[:1024 * 1024]
    def deadline(*_):
        raise AssertionError('citation parser exceeded 0.5 s for a 1 MiB hostile answer')
    previous = signal.signal(signal.SIGALRM, deadline)
    started = time.monotonic()
    signal.setitimer(signal.ITIMER_REAL, 0.5)
    try:
        checked = citations.check(hostile, [])
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
    assert time.monotonic() - started < 0.5
    assert checked.verified == 0 and checked.unverified == 1


@pytest.mark.parametrize('quote', ['a', 'print', '1234567', 'a      b'])
def test_short_quotes_are_unverified_even_when_present(h, quote):
    (h.repo / 'notes.md').write_text(quote)
    _, prepared = stage.prepare(str(h.repo), ['notes.md'], [], 'read')
    checked = citations.check('FILE: notes.md | QUOTE: "%s" | CLAIM: ok' % quote, prepared.entries)
    assert checked.verified == 0 and checked.unverified == 1
    assert checked.lines[0].reason == 'too short'


@pytest.mark.parametrize('suffix', ['\n\n', '\n \n\t\n', '\r\n\r\n'])
def test_trailing_blank_citation_noise_does_not_fail_required_verification(h, suffix):
    h.configure(answer='FILE: src/a.py | QUOTE: "print(\'a\')" | CLAIM: ok' + suffix)
    result = h.delegate('--cite', '--require-verified')
    assert result.returncode == 0, result.stderr
    assert '1 VERIFIED, 0 UNVERIFIED' in result.stdout
    event = json.loads((h.repo / '.agy-bridge/events.jsonl').read_text())
    assert event['cite'] == {'verified': 1, 'unverified': 0}


def invoke_in_process(h, monkeypatch, *flags):
    for name, value in h.env().items():
        monkeypatch.setenv(name, value)
    return cli.main(['delegate', '--repo', str(h.repo), '--file', 'src/a.py', *flags, '--', 'read'])


@pytest.mark.parametrize('failure', ['open', 'write', 'lock', 'chmod'])
@pytest.mark.parametrize('error', [errno.EACCES, errno.ENOSPC, errno.EROFS])
@pytest.mark.parametrize('mode,expected', [('ok', 0), ('quota', 10)])
def test_ordinary_event_io_failure_warns_and_preserves_real_delegation_exit(
        h, monkeypatch, capsys, failure, error, mode, expected):
    h.configure(mode=mode)
    original_open = events.os.open
    original_close = events.os.close
    log_fds = set()
    def tracked_close(fd):
        log_fds.discard(fd)
        return original_close(fd)
    monkeypatch.setattr(events.os, 'close', tracked_close)
    def tracked_open(path, *args, **kw):
        if path == 'events.jsonl' and failure == 'open':
            raise OSError(error, 'simulated journal failure')
        fd = original_open(path, *args, **kw)
        if path == 'events.jsonl':
            log_fds.add(fd)
        return fd
    monkeypatch.setattr(events.os, 'open', tracked_open)
    operation = {'write': 'write', 'lock': 'flock', 'chmod': 'fchmod'}.get(failure)
    if operation:
        owner = events.fcntl if failure == 'lock' else events.os
        original = getattr(owner, operation)
        def fail(fd, *args, **kw):
            if fd in log_fds:
                raise OSError(error, 'simulated journal failure')
            return original(fd, *args, **kw)
        monkeypatch.setattr(owner, operation, fail)
    code = invoke_in_process(h, monkeypatch)
    assert code == expected
    assert h.record()['listing'] == ['TASK.md', 'src', 'src/a.py']
    err = capsys.readouterr().err
    assert 'event' in err and ('warning' in err or 'not recorded' in err)
    assert 'delegation refused' not in err


def test_readonly_journal_does_not_prevent_sending(h):
    scratch = h.repo / '.agy-bridge'
    scratch.mkdir(mode=0o700)
    journal = scratch / 'events.jsonl'
    journal.write_text('')
    journal.chmod(0o400)
    try:
        result = h.delegate()
        assert result.returncode == 0, result.stderr
        assert h.record_path.exists()
        assert 'event' in result.stderr
        assert journal.read_text() == ''
    finally:
        journal.chmod(0o600)


@pytest.mark.parametrize('argument', ['absolute', 'parent', 'early-usage', 'outside-glob'])
def test_outside_paths_are_redacted_in_journal(h, argument):
    outside = str(h.root / 'private-location' / 'file.txt')
    flags = ['--file', outside]
    if argument == 'parent':
        flags = ['--file', '../private-location/file.txt']
    elif argument == 'early-usage':
        flags += ['--max-bytes', '0']
    elif argument == 'outside-glob':
        flags = ['--glob', outside + '/*']
    result = h.run('delegate', *flags)
    assert result.returncode == (1 if argument == 'early-usage' else 4)
    raw = (h.repo / '.agy-bridge/events.jsonl').read_text()
    assert outside not in raw and 'private-location' not in raw
    assert json.loads(raw)['not_sent'] == [{'path': '<outside repo>', 'rule': 'TASK' if argument == 'early-usage' else 'R6'}]
    assert not h.record_path.exists()


def test_tail_bounds_reads_and_rejects_oversized_tampered_line(h, monkeypatch):
    fd = events.open_log(str(h.repo))
    os.close(fd)
    journal = h.repo / '.agy-bridge/events.jsonl'
    journal.write_text(json.dumps({'tampered': 'x' * (1024 * 1024)}) + '\n')
    original = events.os.fdopen
    class BoundedReader:
        def __init__(self, stream):
            self.stream = stream
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.stream.close()
        def __iter__(self):
            raise AssertionError('tail must not iterate unbounded lines')
        def readline(self, size=-1):
            assert 0 < size <= 65537, 'tail read must be bounded'
            return self.stream.readline(size)
    monkeypatch.setattr(events.os, 'fdopen', lambda *a, **kw: BoundedReader(original(*a, **kw)))
    with pytest.raises(events.EventError):
        events.tail(str(h.repo), 1)
