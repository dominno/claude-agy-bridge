"""C1, bidi and line-separator characters never reach answers, results or cite rows; model names are exact."""
import json
import re

import pytest

from agy_bridge.app import report
from agy_bridge.core import config

UNSAFE = re.compile('[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f‎‏‪-‮⁦-⁩  ]')
NASTY = 'a‮b\u009b31mc⁦d'


def test_clean_answer_strips_c1_and_bidi_controls():
    cleaned = report.clean_answer(NASTY.encode('utf-8'))
    assert not UNSAFE.search(cleaned) and cleaned == 'ab31mcd'
    # LRM/RLM belong to the invisible class, so their removal is noted.
    assert report.clean_answer('x‏y‎z\x7f'.encode('utf-8')) == 'xyz\nnote: 2 invisible Unicode characters removed'


def test_line_separators_become_newlines():
    text = report.clean_answer('one\u0085two three four'.encode('utf-8'), preserve_lines=True)
    assert text == 'one\ntwo\nthree\nfour'


def test_delegate_answer_is_clean(h):
    h.configure(answer='summary ' + NASTY)
    r = h.delegate()
    assert r.returncode == 0, r.stderr
    assert 'summary ab31mcd' in r.stdout and not UNSAFE.search(r.stdout)


def test_queue_result_written_from_such_an_answer_is_clean(h):
    h.configure(answer='queued ' + NASTY)
    path = h.root / 'job.json'
    path.write_text(json.dumps(dict(name='hyg', model='fake', task='review', files=['notes.md'], timeout='5s',
                                    context=False)))
    assert h.raw('queue', 'add', str(path), '--repo', str(h.repo)).returncode == 0
    assert h.raw('queue', 'run', '--repo', str(h.repo)).returncode == 0
    text = (h.repo / '.agy-bridge/results/hyg.md').read_text()
    assert 'queued ab31mcd' in text and not UNSAFE.search(text)


def test_cite_row_is_clean(h):
    h.configure(answer='FILE: notes.md | QUOTE: "some notes" | CLAIM: ok‮ evil⁦')
    r = h.delegate('--file', 'notes.md', '--cite')
    assert r.returncode == 0, r.stderr
    assert 'VERIFIED (quote exists): FILE: notes.md | QUOTE: "some notes" | CLAIM: ok evil' in r.stdout
    assert not UNSAFE.search(r.stdout)


@pytest.mark.parametrize('name', ['gemini-pro\n', 'x\n', 'gemini‮', ' gemini', 'a' * 65])
def test_validate_model_refuses_trailing_newline_and_controls(name):
    with pytest.raises(config.UsageError):
        config.validate_model(name)


def test_validate_model_accepts_ordinary_names():
    assert config.validate_model('gemini-3.8-flash-low') == 'gemini-3.8-flash-low'
    assert config.validate_model('models/gemini:pro') == 'models/gemini:pro'


def test_queue_add_refuses_a_model_with_a_trailing_newline(h):
    path = h.root / 'job.json'
    path.write_text(json.dumps(dict(name='m1', model='x\n', task='review', files=['notes.md'], context=False)))
    r = h.raw('queue', 'add', str(path), '--repo', str(h.repo))
    assert r.returncode == 1 and not (h.repo / '.agy-bridge/queue/m1.job').exists()


def test_delegate_refuses_a_model_with_a_trailing_newline(h):
    r = h.delegate('--model', 'x\n')
    assert r.returncode == 1 and not h.record_path.exists()


TAGGED = 'safe\U000e0049\U000e0047\U000e004e text​'


def test_clean_answer_strips_invisible_unicode_and_adds_a_note():
    assert report.clean_answer(TAGGED.encode('utf-8')) == 'safe text\nnote: 4 invisible Unicode characters removed'
    assert report.clean_answer(b'plain') == 'plain'
    # Only invisible characters: the classification still sees an empty answer.
    text, removed = report.clean_counted('\U000e0041'.encode('utf-8'))
    assert text == '' and removed == 1


def test_delegate_answer_with_tag_characters_is_cleaned_with_a_note(h):
    h.configure(answer='summary ' + TAGGED)
    r = h.delegate()
    assert r.returncode == 0, r.stderr
    assert 'summary safe text' in r.stdout and 'note: 4 invisible Unicode characters removed' in r.stdout
    assert '\U000e0049' not in r.stdout


def test_queue_result_with_tag_characters_is_cleaned(h):
    h.configure(answer='queued ' + TAGGED)
    path = h.root / 'job.json'
    path.write_text(json.dumps(dict(name='tags', model='fake', task='review', files=['notes.md'], timeout='5s',
                                    context=False)))
    assert h.raw('queue', 'add', str(path), '--repo', str(h.repo)).returncode == 0
    assert h.raw('queue', 'run', '--repo', str(h.repo)).returncode == 0
    text = (h.repo / '.agy-bridge/results/tags.md').read_text()
    assert 'queued safe text' in text and '\U000e0049' not in text and '​' not in text
    assert 'note: 4 invisible Unicode characters removed' in text


def test_check_output_tail_is_cleaned():
    from agy_bridge.app import implement_work
    tail = implement_work._tail('FAILED test\U000e0041 x\n')
    assert '\U000e0041' not in tail and 'note: 1 invisible Unicode characters removed' in tail
