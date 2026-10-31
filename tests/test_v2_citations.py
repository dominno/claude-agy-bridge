import hashlib
import json

import pytest

from agy_bridge import citations, stage


def citation(path='notes.md', quote='some notes', claim='A model claim'):
    return 'FILE: %s | QUOTE: "%s" | CLAIM: %s' % (path, quote, claim)


def test_cite_instruction_is_staged_and_only_quote_presence_is_verified(h):
    # This false claim must never acquire a truth endorsement from the bridge.
    answer = citation(claim='The moon is made of cheese')
    h.configure(answer=answer)
    result = h.delegate('--file', 'notes.md', '--cite', '--require-verified')
    assert result.returncode == 0, result.stderr
    expected = stage.task_file_text('summarise these files' + citations.INSTRUCTION, ['src/a.py', 'notes.md'])
    assert h.record()['digests']['TASK.md'] == hashlib.sha256(expected.encode()).hexdigest()
    assert 'VERIFIED (quote exists): ' + answer in result.stdout
    assert '1 VERIFIED, 0 UNVERIFIED' in result.stdout
    assert 'CLAIM is not verified' in result.stdout
    event = json.loads((h.repo / '.agy-bridge/events.jsonl').read_text())
    assert event['cite'] == {'verified': 1, 'unverified': 0}
    assert 'cheese' not in json.dumps(event)


@pytest.mark.parametrize('path,quote,reason', [
    ('notes.md', 'invented quotation', 'quote not found'),
    ('not-sent.md', 'some notes', 'file not sent'),
    ('../notes.md', 'some notes', 'file not sent'),
    ('/notes.md', 'some notes', 'file not sent'),
    ('NOTES.md', 'some notes', 'file not sent'),
    ('TASK.md', 'summarise these files', 'file not sent'),
    ('notes.md', '', 'empty quote'),
    ('notes.md', ' '.join(['word'] * 26), 'quote exceeds 25 words'),
])
@pytest.mark.parametrize('require', [False, True])
def test_each_unverified_reason_and_opt_in_exit(h, path, quote, reason, require):
    h.configure(answer=citation(path, quote))
    flags = ['--cite'] + (['--require-verified'] if require else [])
    result = h.delegate('--file', 'notes.md', *flags)
    assert result.returncode == (17 if require else 0), result.stderr
    assert 'UNVERIFIED (%s)' % reason in result.stdout
    assert '0 VERIFIED, 1 UNVERIFIED' in result.stdout


def test_all_lines_including_malformed_and_blank_lines_remain_visible(h):
    answer = 'preface\n' + citation() + '\n\n' + citation(quote='wrong') + '\nclosing words'
    h.configure(answer=answer)
    result = h.delegate('--file', 'notes.md', '--cite', '--require-verified')
    assert result.returncode == 17
    assert '1 VERIFIED, 4 UNVERIFIED' in result.stdout
    for line in answer.splitlines():
        assert ': ' + line + '\n' in result.stdout
    assert 'UNVERIFIED (malformed citation line): \n' in result.stdout


def test_whitespace_normalised_quotes_but_no_case_or_punctuation_folding(h):
    (h.repo / 'notes.md').write_text('A\t  multi-line\n quotation.\n')
    h.configure(answer=citation(quote='A multi-line quotation.') + '\n' + citation(quote='a multi-line quotation'))
    result = h.delegate('--file', 'notes.md', '--cite')
    assert result.returncode == 0
    assert '1 VERIFIED, 1 UNVERIFIED' in result.stdout


def test_25_words_allowed_and_26_rejected_even_when_both_exist(h):
    (h.repo / 'notes.md').write_text(' '.join(['word'] * 26))
    h.configure(answer=citation(quote=' '.join(['word'] * 25)) + '\n' + citation(quote=' '.join(['word'] * 26)))
    result = h.delegate('--file', 'notes.md', '--cite', '--require-verified')
    assert result.returncode == 17 and '1 VERIFIED, 1 UNVERIFIED' in result.stdout


def test_citation_check_uses_exact_scanned_buffer_after_source_is_changed(h):
    _, prepared = stage.prepare(str(h.repo), ['notes.md'], [], 'summarise')
    (h.repo / 'notes.md').write_text('replacement')
    check = citations.check(citation() + '\n' + citation(quote='replacement'), prepared.entries)
    assert check.verified == 1 and check.unverified == 1
    assert check.lines[0].verified and not check.lines[1].verified


def test_no_lines_lost_to_long_answer_preview_or_out_file(h):
    lines = [citation(claim='Claim number %d' % n) for n in range(150)]
    h.configure(answer='\n'.join(lines))
    out = h.repo / '.agy-bridge/answer.txt'
    result = h.delegate('--file', 'notes.md', '--cite', '--out', str(out))
    assert result.returncode == 0, result.stderr
    assert '150 VERIFIED, 0 UNVERIFIED' in result.stdout
    for line in lines:
        assert line in result.stdout and line in out.read_text()
    assert 'more characters not shown' not in result.stdout


@pytest.mark.parametrize('mode,exit_code', [('quota', 10), ('auth', 11), ('empty', 3), ('soft_denial', 15)])
def test_require_verified_preserves_existing_provider_failure_codes(h, mode, exit_code):
    h.configure(mode=mode)
    result = h.delegate('--cite', '--require-verified')
    assert result.returncode == exit_code
    assert '0 VERIFIED, 0 UNVERIFIED' in result.stdout


def test_cite_cap_failure_takes_precedence_over_unverified_code(h):
    h.configure(mode='oversize', oversize_bytes=40000)
    result = h.delegate('--cite', '--require-verified', '--max-out-bytes', '5000')
    assert result.returncode == 16
    record = json.loads((h.repo / '.agy-bridge/events.jsonl').read_text())
    assert record['truncated'] and record['exit_code'] == 16
    assert record['cite']['unverified'] == 1


def test_require_verified_needs_cite(h):
    result = h.delegate('--require-verified')
    assert result.returncode == 1 and '--require-verified requires --cite' in result.stderr
    assert not h.record_path.exists()


def test_no_cite_does_not_change_answer_or_event_schema(h):
    h.configure(answer='ordinary answer')
    result = h.delegate()
    assert result.returncode == 0 and 'Citation check' not in result.stdout
    record = json.loads((h.repo / '.agy-bridge/events.jsonl').read_text())
    assert 'cite' not in record


def test_leading_blank_line_is_checked_and_trailing_noise_is_ignored(h):
    h.configure(answer='\n' + citation() + '\n')
    result = h.delegate('--file', 'notes.md', '--cite', '--require-verified')
    assert result.returncode == 17
    assert '1 VERIFIED, 1 UNVERIFIED' in result.stdout
    assert result.stdout.count('UNVERIFIED (malformed citation line): \n') == 1


def test_provider_failure_does_not_claim_unwritten_out_file_exists(h):
    h.configure(mode='quota')
    out = h.repo / '.agy-bridge/answer.txt'
    result = h.delegate('--cite', '--out', str(out))
    assert result.returncode == 10 and not out.exists()
    assert 'full answer written to:' not in result.stdout
