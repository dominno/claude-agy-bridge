"""Findings from a real adoption (0.4.0): symlinked path components, task-prescribed citation formats and
archiving old results without a review."""
import json
import os
import time

import pytest

from agy_bridge.app import report
from agy_bridge.core import citations, stage
from agy_bridge.core.refusal import SymlinkInPath
from agy_bridge.port import safety


def _job(h, name='j1', task='Review the notes', **extra):
    return dict(dict(name=name, model='fake', task=task, files=['notes.md'], timeout='5s', context=False), **extra)


# --- 1. a symlinked path component names the cause and the fix -------------------------------------------------

def _linked_job(h, job=None):
    real = h.root / 'real'
    real.mkdir()
    (real / 'job.json').write_text(json.dumps(job or _job(h)))
    (h.root / 'link').symlink_to(real)
    return h.root / 'link' / 'job.json', real / 'job.json'


@pytest.mark.parametrize('dry', [True, False])
def test_queue_add_through_a_symlinked_folder_names_the_component_and_the_fix(h, dry):
    linked, real = _linked_job(h)
    r = h.raw('queue', 'add', *(['--dry-run'] if dry else []), str(linked), '--repo', str(h.repo))
    assert r.returncode == 4, r.stderr
    assert 'queue add refused: path component "link" is a symbolic link; pass the resolved path' in r.stderr
    assert 'pass the resolved path (%s)' % os.path.realpath(str(real)) in r.stderr
    assert ('the job on stdin: agy-bridge queue add %s- --repo %s' % ('--dry-run ' if dry else '', h.repo)
            in r.stderr)
    assert 'Not a directory' not in r.stderr
    assert not (h.repo / '.agy-bridge/queue/j1.job').exists()
    # The suggested fixes work.
    assert h.raw('queue', 'add', '--dry-run', os.path.realpath(str(real)), '--repo', str(h.repo)).returncode == 0


def test_queue_folder_with_a_symlinked_component_names_the_component(h):
    (h.repo / 'real').mkdir()
    (h.repo / 'lnk').symlink_to(h.repo / 'real')
    r = h.raw('queue', 'status', '--repo', str(h.repo), '--queue-dir', 'lnk/q')
    assert r.returncode == 4
    assert 'queue status refused: path component "lnk" is a symbolic link' in r.stderr
    assert 'pass the resolved path' in r.stderr


def test_absolute_queue_folder_through_a_symlinked_alias_says_why(h):
    (h.root / 'alias').symlink_to(h.repo)
    r = h.raw('queue', 'status', '--repo', str(h.repo), '--results-dir', str(h.root / 'alias' / 'out'))
    assert r.returncode == 1
    assert 'symbolic link' in r.stderr
    assert 'pass the resolved path (%s)' % os.path.join(os.path.realpath(str(h.repo)), 'out') in r.stderr


def test_delegate_file_through_a_symlinked_alias_of_the_repo_says_why(h):
    (h.root / 'alias').symlink_to(h.repo)
    r = h.run('dry-run', '--file', str(h.root / 'alias' / 'notes.md'))
    assert r.returncode == 4
    assert 'Rule R6: path is outside the repository' in r.stderr
    assert 'symbolic link' in r.stderr
    assert 'pass the resolved path (%s)' % os.path.join(os.path.realpath(str(h.repo)), 'notes.md') in r.stderr


def test_delegate_file_outside_the_repo_keeps_the_plain_refusal(h):
    (h.root / 'other.md').write_text('x\n')
    r = h.run('dry-run', '--file', str(h.root / 'other.md'))
    assert r.returncode == 4
    assert 'path is outside the repository' in r.stderr and 'symbolic link' not in r.stderr


def test_pinned_walk_raises_symlink_in_path_with_resolved_target(tmp_path):
    (tmp_path / 'real').mkdir()
    (tmp_path / 'link').symlink_to(tmp_path / 'real')
    with pytest.raises(SymlinkInPath) as caught:
        with safety.directory(str(tmp_path / 'link' / 'deeper'), create=True):
            pass
    assert caught.value.component == 'link'
    assert caught.value.resolved == os.path.join(os.path.realpath(str(tmp_path / 'real')), 'deeper')
    assert isinstance(caught.value, OSError)
    assert not (tmp_path / 'real' / 'deeper').exists()


def test_explain_symlink_in_path_redacts_key_shaped_names():
    err = SymlinkInPath('AKIAABCDEFGHIJKLMNOP', '/x/AKIAABCDEFGHIJKLMNOP/y', '/real/AKIAABCDEFGHIJKLMNOP/y')
    line, = stage.explain(err, 'queue add')
    assert 'AKIA' not in line and 'redacted' in line and 'symbolic link' in line


def test_out_through_a_symlinked_parent_names_the_component(tmp_path):
    (tmp_path / 'real').mkdir()
    (tmp_path / 'link').symlink_to(tmp_path / 'real')
    with pytest.raises(report.OutPathRefused) as caught:
        report.write_new_file(str(tmp_path / 'link' / 'answer.md'), 'text')
    assert 'path component "link" is a symbolic link' in str(caught.value)
    assert not (tmp_path / 'real' / 'answer.md').exists()


def test_out_refusal_names_the_temp_folder(h):
    r = h.run('dry-run', '--file', 'notes.md', '--out', str(h.root / 'answer.md'))
    assert r.returncode == 4
    assert 'temp folder (%s)' % os.path.realpath(str(h.tmpdir)) in r.stderr


# --- 2. a task that prescribes its own answer format ----------------------------------------------------------

FORMAT_TASK = 'For each finding output `QUOTE: <line> AT: path:line` on its own line.'


@pytest.mark.parametrize('task,found', [
    (FORMAT_TASK, True),
    ('Answer as FILE: <path> lines.', True),
    ('Review the parser. If a source is missing, write NEED: path/to/file on its own line.', False),
    ('Look AT the file, note the quote: it matters.', False),
])
def test_format_directive_detection(task, found):
    assert bool(citations.format_directives(task)) is found


def test_queue_add_dry_run_and_add_warn_about_a_prescribed_format(h):
    job = h.root / 'job.json'
    job.write_text(json.dumps(_job(h, task=FORMAT_TASK)))
    for dry in (True, False):
        r = h.raw('queue', 'add', *(['--dry-run'] if dry else []), str(job), '--repo', str(h.repo))
        assert r.returncode == 0, r.stderr
        assert r.stderr.count('WARN task text prescribes an answer format') == 1, r.stderr
        assert 'QUOTE: <verbatim> AT: <path>:<line>' in r.stderr


def test_queue_add_without_a_format_directive_has_no_warning(h):
    job = h.root / 'job.json'
    job.write_text(json.dumps(_job(h)))
    r = h.raw('queue', 'add', '--dry-run', str(job), '--repo', str(h.repo))
    assert r.returncode == 0 and 'WARN task text' not in r.stderr


def test_delegate_cite_dry_run_warns_and_plain_dry_run_does_not(h):
    r = h.run('dry-run', '--file', 'notes.md', '--cite', task=FORMAT_TASK)
    assert r.returncode == 0, r.stderr
    assert 'WARN task text prescribes an answer format (QUOTE:, AT:)' in r.stderr
    r = h.run('dry-run', '--file', 'notes.md', task=FORMAT_TASK)
    assert r.returncode == 0 and 'WARN task text' not in r.stderr


def quote_at(quote='some notes', where='notes.md:1'):
    return 'QUOTE: %s AT: %s' % (quote, where)


@pytest.mark.parametrize('line,reason', [
    (quote_at(), 'quote exists'),
    (quote_at(where='notes.md'), 'quote exists'),
    (quote_at('"some notes"'), 'quote exists'),
    (quote_at('`some notes`'), 'quote exists'),
    (quote_at(where='notes.md:7'), 'quote exists; not on line 7'),
    (quote_at('invented quotation'), 'quote not found'),
    (quote_at(where='other.md:1'), 'file not sent'),
    (quote_at('notes'), 'too short'),
    (quote_at(' '.join(['word'] * 26)), 'quote exceeds 25 words'),
    ('QUOTE: some notes', 'malformed citation line'),
    ('QUOTE: some notes AT: ', 'malformed citation line'),
])
def test_quote_at_form_is_checked_like_the_file_form(h, line, reason):
    h.configure(answer=line)
    r = h.delegate('--file', 'notes.md', '--cite')
    assert r.returncode == 0, r.stderr
    assert '%s (%s): %s' % ('VERIFIED' if reason.startswith('quote exists') else 'UNVERIFIED', reason, line) \
        in r.stdout


def test_quote_at_form_satisfies_require_verified(h):
    h.configure(answer=quote_at() + '\n' + 'FILE: notes.md | QUOTE: "some notes" | CLAIM: c')
    r = h.delegate('--file', 'notes.md', '--cite', '--require-verified', task=FORMAT_TASK)
    assert r.returncode == 0, r.stderr
    assert '2 VERIFIED, 0 UNVERIFIED' in r.stdout


def test_queue_job_with_a_prescribed_format_is_verified(h):
    h.configure(answer=quote_at())
    job = h.root / 'job.json'
    job.write_text(json.dumps(_job(h, task=FORMAT_TASK)))
    assert h.raw('queue', 'add', str(job), '--repo', str(h.repo)).returncode == 0
    r = h.raw('queue', 'run', '--repo', str(h.repo))
    assert r.returncode == 0, r.stderr
    text = (h.repo / '.agy-bridge/results/j1.md').read_text()
    assert 'VERIFIED (quote exists): ' + quote_at() in text and 'UNVERIFIED (malformed citation line):' not in text
    assert 'WARN task text prescribes an answer format' in text


# --- 3. archiving old results without a review ----------------------------------------------------------------

def _results(h, ages):
    """Write results/<name>.md with mtimes `days` days ago; return the folder."""
    results = h.repo / '.agy-bridge/results'
    results.mkdir(parents=True, exist_ok=True)
    now = time.time()
    for name, days in ages.items():
        path = results / (name + '.md')
        path.write_text('# answer\n')
        os.utime(str(path), (now - days * 86400, now - days * 86400))
    return results


def _day(days_ago):
    return time.strftime('%Y-%m-%d', time.localtime(time.time() - days_ago * 86400))


def _verdicts(h):
    path = h.repo / '.agy-bridge/verdicts.jsonl'
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_bulk_archive_records_archived_for_old_results_without_a_verdict(h):
    _results(h, {'old1': 30, 'old2': 20, 'reviewed': 25, 'fresh': 0})
    base = ('verdict', '--repo', str(h.repo))
    assert h.raw(*base, '--job', 'reviewed', '--verdict', 'fixes', '--by', 'r', '--evidence', 'e').returncode == 0
    r = h.raw(*base, '--bulk', '--verdict', 'archived', '--before', _day(10), '--dry-run')
    assert r.returncode == 0, r.stderr
    assert 'would archive 2 result(s): old1, old2' in r.stdout
    assert len(_verdicts(h)) == 1
    r = h.raw(*base, '--bulk', '--verdict', 'archived', '--before', _day(10))
    assert r.returncode == 0, r.stderr
    assert 'archived 2 result(s) without review: old1, old2' in r.stdout
    rows = _verdicts(h)[1:]
    assert sorted(row['job'] for row in rows) == ['old1', 'old2']
    assert all(row['verdict'] == 'archived' and row['by'] == 'bulk-archive' for row in rows)
    assert all('not reviewed' in row['evidence'] for row in rows)
    status = h.raw('status', '--repo', str(h.repo)).stdout
    assert 'results without verdict: fresh' in status
    assert 'archived without review: 2' in status
    # Idempotent: nothing left to archive.
    r = h.raw(*base, '--bulk', '--verdict', 'archived', '--before', _day(10))
    assert r.returncode == 0 and 'archived 0 result(s)' in r.stdout
    assert len(_verdicts(h)) == 3


def test_bulk_archive_keeps_by_and_evidence_when_given(h):
    _results(h, {'old1': 30})
    r = h.raw('verdict', '--repo', str(h.repo), '--bulk', '--verdict', 'archived', '--before', _day(1),
              '--by', 'lead', '--evidence', 'superseded by run 7')
    assert r.returncode == 0, r.stderr
    row, = _verdicts(h)
    row.pop('ts')
    assert row == dict(job='old1', verdict='archived', by='lead', evidence='superseded by run 7')
    shown = h.raw('verdicts', '--repo', str(h.repo)).stdout
    assert '"verdict": "archived"' in shown


@pytest.mark.parametrize('argv,message', [
    (('--bulk', '--verdict', 'pass', '--before', '2026-01-01'), '--bulk records only --verdict archived'),
    (('--bulk', '--verdict', 'archived'), '--bulk requires --before YYYY-MM-DD'),
    (('--bulk', '--verdict', 'archived', '--before', '2026-13-01'), '--before must be a date YYYY-MM-DD'),
    (('--bulk', '--verdict', 'archived', '--before', '2026-01-01', '--job', 'x'), '--bulk takes no --job or --note'),
    (('--verdict', 'archived', '--before', '2026-01-01', '--job', 'x'), '--before needs --bulk'),
    (('--verdict', 'pass', '--job', 'x'), '--by and --evidence are required'),
    (('--verdict', 'pass', '--by', 'r', '--evidence', 'e'), '--job is required'),
])
def test_verdict_bulk_usage_errors(h, argv, message):
    r = h.raw('verdict', '--repo', str(h.repo), *argv)
    assert r.returncode == 1, r.stderr
    assert message in r.stderr
    assert not _verdicts(h)


def test_archived_is_never_a_pass(h):
    from agy_bridge.app import verdicts
    _results(h, {'old1': 30})
    verdicts.record(str(h.repo), 'old1', 'archived', 'r', 'e', force=False)
    assert [row['verdict'] for row in verdicts.rows(str(h.repo))] == ['archived']
    # A pass on that job still needs its own reader verdict; archived is shown as its own count, not as passed.
    status = h.raw('status', '--repo', str(h.repo)).stdout
    assert 'results without verdict: none' in status and 'archived without review: 1' in status
