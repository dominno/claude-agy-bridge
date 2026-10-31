import json
import os

import pytest

from agy_bridge import stage
from agy_bridge.scan import EXAMPLE_FRAGMENTS


def event(h):
    return json.loads((h.repo / '.agy-bridge/events.jsonl').read_text().splitlines()[-1])


@pytest.mark.parametrize('kind,rule,reason,exit_code', [
    ('denied', 'R1', 'denylist', 4),
    ('large', 'R6', 'size limit', 4),
    ('link', 'R6', 'symlink', 4),
    ('parent-link', 'R6', 'symlink', 4),
    ('missing', 'R6', 'not found', 1),
    ('binary', 'R6', 'binary', 4),
    ('hardlink', 'R6', 'hard links', 4),
])
def test_notice_and_event_for_each_refused_input(h, kind, rule, reason, exit_code):
    path = 'input.txt'
    if kind == 'denied':
        path = '.env'
        (h.repo / path).write_text('not a key')
    elif kind == 'large':
        (h.repo / path).write_text('x' * 205000)
    elif kind == 'link':
        (h.repo / path).symlink_to(h.repo / 'notes.md')
    elif kind == 'parent-link':
        (h.repo / 'alias').symlink_to(h.repo / 'src', target_is_directory=True)
        path = 'alias/a.py'
    elif kind == 'hardlink':
        os.link(h.repo / 'notes.md', h.repo / path)
    elif kind == 'binary':
        (h.repo / path).write_bytes(b'a\0b')
    result = h.run('delegate', '--file', path)
    assert result.returncode == exit_code, result.stderr
    assert 'FILES NOT SENT: %s (%s:' % (path, rule) in result.stdout
    assert reason in result.stdout
    assert event(h)['not_sent'] == [{'path': path, 'rule': rule}]
    assert event(h)['files'] == []
    assert not h.record_path.exists()


def test_r5_notice_first_hit_line_not_first_regex_or_matched_text(h):
    key = 'AKIAABCDEFGHIJKLMNOP'
    (h.repo / 'example.txt').write_text('intro\n' + key + '\n\nSERVICE_API_KEY\n')
    result = h.delegate('--file', 'example.txt')
    assert result.returncode == 4
    assert 'FILES NOT SENT: example.txt (R5: key-shaped string; first hit line 2)' in result.stdout
    assert 'FILES NOT SENT: src/a.py (TASK:' in result.stdout
    combined = result.stdout + result.stderr + (h.repo / '.agy-bridge/events.jsonl').read_text()
    assert key not in combined and 'SERVICE_API_KEY' not in combined
    assert event(h)['not_sent'] == [
        {'path': 'example.txt', 'rule': 'R5'}, {'path': 'src/a.py', 'rule': 'TASK'}]


@pytest.mark.parametrize('command', ['delegate', 'dry-run'])
def test_glob_skips_are_named_without_walking_denied_directories(h, command):
    (h.repo / '.env').write_text('not a key')
    (h.repo / 'node_modules').mkdir()
    (h.repo / 'node_modules/hidden.txt').write_text('must not read')
    result = h.run(command, '--glob', '**/*')
    assert result.returncode == 0, result.stderr
    assert 'FILES NOT SENT: .env (R1:' in result.stdout
    assert 'FILES NOT SENT: node_modules (R1: denylisted directory pruned' in result.stdout
    assert 'hidden.txt' not in result.stdout
    assert {'path': '.env', 'rule': 'R1'} in event(h)['not_sent']
    if command == 'delegate':
        assert '.env' not in h.record()['listing']
        assert 'node_modules/hidden.txt' not in h.record()['listing']
        assert '.agy-bridge/events.jsonl' not in h.record()['listing']


def test_glob_key_hit_is_refused_with_notice(h):
    (h.repo / 'bad.txt').write_text('line1\nsk-proj-abcdefghijklmnopqrstuvwx\n')
    result = h.run('delegate', '--glob', '*.txt')
    assert result.returncode == 4
    assert 'bad.txt (R5: key-shaped string; first hit line 2)' in result.stdout
    assert not h.record_path.exists()


def test_no_glob_match_is_reported_with_existing_usage_exit(h):
    result = h.run('delegate', '--glob', 'no-such-*.txt')
    assert result.returncode == 1
    assert 'FILES NOT SENT: no-such-*.txt (R6: glob matched no sendable files)' in result.stdout


def test_key_shaped_filename_never_printed_or_journaled(h):
    name = 'ghp_abcdefghijklmnopqrstuv.txt'
    (h.repo / name).write_text('ordinary content')
    result = h.run('delegate', '--file', name)
    assert result.returncode == 4
    assert name not in result.stdout + result.stderr
    assert event(h)['not_sent'] == [{'path': '[redacted key-shaped filename]', 'rule': 'R5'}]


@pytest.mark.parametrize('fragment', sorted(EXAMPLE_FRAGMENTS))
def test_supported_exact_example_can_be_allowed_without_staging_allow_file(h, fragment):
    assert len(fragment) >= 12
    (h.repo / 'allow.txt').write_text(fragment + '\n')
    (h.repo / 'example.md').write_text('Document the variable ' + fragment + ' here.\n')
    refused = h.run('delegate', '--file', 'example.md')
    assert refused.returncode == 4
    result = h.run('delegate', '--file', 'example.md', '--allow-fragment-file', 'allow.txt')
    assert result.returncode == 0, result.stderr
    assert 'allow.txt' not in h.record()['listing']


@pytest.mark.parametrize('fragment', ['short', 'ordinary literal', 'sk-proj-abcdefghijklmnopqrstuvwx',
                                     'AKIAABCDEFGHIJKLMNOP', 'service_api_key', ' SERVICE_API_KEY ', ''])
def test_allow_file_rejects_every_other_literal_without_echoing_it(h, fragment):
    (h.repo / 'allow.txt').write_text(fragment + '\n')
    result = h.delegate('--allow-fragment-file', 'allow.txt')
    assert result.returncode == 1
    assert 'unsupported example' in result.stderr
    assert not h.record_path.exists()
    if fragment:
        assert fragment not in result.stdout + result.stderr


@pytest.mark.parametrize('content', [
    'SERVICE_API_KEY=sk-proj-abcdefghij1234567890ABCDEFG',
    'SERVICE_API_KEY = "Ab3dEf6hIj9kLm2nOp5qRs8tUv1wXy4z"',
    'EXAMPLE_API_KEY\nghp_abcdefghijklmnopqrstuvwx',
    'PREFIX_EXAMPLE_API_KEY_SUFFIX',
    '"SERVICE_API_KEY": "Ab3dEf6hIj9kLm2nOp5qRs8tUv1wXy4z"',
    '-----BEGIN RSA PRIVATE KEY-----',
])
def test_allow_file_cannot_exempt_values_provider_shapes_or_partial_names(h, content):
    (h.repo / 'allow.txt').write_text('`SERVICE_API_KEY`\n`EXAMPLE_API_KEY`\n')
    (h.repo / 'example.md').write_text(content)
    result = h.run('delegate', '--file', 'example.md', '--allow-fragment-file', 'allow.txt')
    assert result.returncode == 4
    assert 'R5' in result.stdout
    assert content not in result.stdout + result.stderr
    assert not h.record_path.exists()


@pytest.mark.parametrize('kind', ['outside', 'denylisted', 'symlink', 'parent-symlink', 'hardlink', 'binary', 'large'])
def test_allow_file_obeys_path_and_read_safety_rules(h, kind):
    (h.repo / 'allow.txt').write_text('`SERVICE_API_KEY`\n')
    path = 'allow.txt'
    if kind == 'outside':
        target = h.root / 'allow.txt'
        target.write_text('`SERVICE_API_KEY`\n')
        path = str(target)
    elif kind == 'denylisted':
        path = '.env'
        (h.repo / path).write_text('`SERVICE_API_KEY`\n')
    elif kind == 'symlink':
        path = 'link.txt'
        (h.repo / path).symlink_to(h.repo / 'allow.txt')
    elif kind == 'parent-symlink':
        (h.repo / 'src/allow.txt').write_text('`SERVICE_API_KEY`\n')
        (h.repo / 'alias').symlink_to(h.repo / 'src', target_is_directory=True)
        path = 'alias/allow.txt'
    elif kind == 'hardlink':
        os.link(h.repo / path, h.repo / 'other.txt')
    elif kind == 'binary':
        (h.repo / path).write_bytes(b'\xff')
    elif kind == 'large':
        (h.repo / path).write_text('`SERVICE_API_KEY`\n' * 2000)
    result = h.delegate('--allow-fragment-file', path)
    assert result.returncode in (1, 4)
    assert not h.record_path.exists()


def test_allow_file_never_exempts_denylisted_input_or_task(h):
    (h.repo / 'allow.txt').write_text('`SERVICE_API_KEY`\n')
    (h.repo / '.env').write_text('SERVICE_API_KEY')
    result = h.run('delegate', '--file', '.env', '--allow-fragment-file', 'allow.txt')
    assert result.returncode == 4 and 'R1' in result.stdout
    result = h.delegate('--allow-fragment-file', 'allow.txt', task='Explain SERVICE_API_KEY')
    assert result.returncode == 4


def test_allowed_examples_do_not_hide_line_of_a_remaining_key(h):
    (h.repo / 'allow.txt').write_text('`SERVICE_API_KEY`\n')
    (h.repo / 'example.md').write_text('`SERVICE_API_KEY`\ntext\nAKIAABCDEFGHIJKLMNOP\n')
    result = h.run('delegate', '--file', 'example.md', '--allow-fragment-file', 'allow.txt')
    assert result.returncode == 4 and 'first hit line 3' in result.stdout


@pytest.mark.parametrize('content', ['`SERVICE_API_KEY`=short', '`SERVICE_API_KEY`\n : short',
                                     '`SERVICE_API_KEY`_suffix', 'prefix`SERVICE_API_KEY`'])
def test_allowed_literal_cannot_be_part_of_assignment_or_larger_identifier(h, content):
    (h.repo / 'allow.txt').write_text('`SERVICE_API_KEY`\n')
    (h.repo / 'example.md').write_text(content)
    result = h.run('delegate', '--file', 'example.md', '--allow-fragment-file', 'allow.txt')
    assert result.returncode == 4
    assert not h.record_path.exists()


def test_total_size_refusal_identifies_the_file_that_crossed_the_limit(h):
    result = h.delegate('--file', 'notes.md', '--max-bytes', '15')
    assert result.returncode == 4
    assert 'FILES NOT SENT: notes.md (R6: total input is over the size limit)' in result.stdout
    assert 'FILES NOT SENT: src/a.py (TASK:' in result.stdout


def test_r5_line_numbers_count_crlf_and_cr(h):
    (h.repo / 'example.md').write_bytes(b'one\r\ntwo\rAKIAABCDEFGHIJKLMNOP')
    result = h.run('delegate', '--file', 'example.md')
    assert result.returncode == 4 and 'first hit line 3' in result.stdout


def test_allow_filename_containing_a_key_is_not_echoed(h):
    name = 'AKIAABCDEFGHIJKLMNOP.txt'
    (h.repo / name).write_text('`SERVICE_API_KEY`\n')
    result = h.delegate('--allow-fragment-file', name)
    assert result.returncode == 4
    assert name not in result.stdout + result.stderr
    assert not h.record_path.exists()


def test_early_missing_file_still_lists_all_requested_paths_without_duplicates(h):
    result = h.run('delegate', '--file', 'absent.txt', '--file', './notes.md',
                   '--file', str(h.repo / 'src/a.py'))
    assert result.returncode == 1
    assert 'FILES NOT SENT: absent.txt (R6: file not found)' in result.stdout
    assert result.stdout.count('FILES NOT SENT: notes.md (TASK:') == 1
    assert result.stdout.count('FILES NOT SENT: src/a.py (TASK:') == 1
    assert len(event(h)['not_sent']) == 3
