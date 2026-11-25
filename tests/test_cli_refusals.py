"""Refusals name the rule or the failing operation; verdicts need an existing job."""
import json
import os

import pytest

from agy_bridge.core import stage

R6_TEXT = 'Rule R6: the repository folder has no .git entry'


@pytest.mark.parametrize('argv', [('queue', 'status'), ('status',), ('ps',), ('jobs',), ('jobs', 'list'),
                                  ('events',), ('verdicts',)])
def test_non_git_folder_refusal_names_rule_r6(h, argv):
    plain = h.root / 'plain'
    plain.mkdir()
    r = h.raw(*argv, '-C', str(plain))
    assert r.returncode == 4, r.stderr
    assert R6_TEXT in r.stderr and '--allow-non-git-root' in r.stderr
    assert 'inspect paths and permissions' not in r.stderr and not (plain / '.agy-bridge').exists()


def test_bare_jobs_with_repo_lists_jobs(h):
    r = h.raw('jobs', '-C', str(h.repo))
    assert r.returncode == 0, r.stderr
    assert 'No jobs.' in r.stdout
    r = h.raw('jobs', '-C', str(h.repo), '--json')
    assert r.returncode == 0 and json.loads(r.stdout)['schema_version'] == 1
    assert 'with no subcommand, lists jobs' in h.raw('jobs', '--help').stdout


def test_queue_parsers_have_help(h):
    top = h.raw('--help').stdout
    assert 'same as queue status' in top and "reader's verdict" in top
    assert 'validate a JSON job' in h.raw('queue', '--help').stdout


def test_verdict_for_unknown_job_is_refused_without_creating_metadata(h):
    r = h.raw('verdict', '--repo', str(h.repo), '--job', 'nosuch', '--verdict', 'pass', '--by', 'r', '--evidence', 'e')
    assert r.returncode == 1, r.stderr
    assert 'no job "nosuch"' in r.stderr and 'agy-bridge status -C' in r.stderr and '--force' in r.stderr
    assert not (h.repo / '.agy-bridge').exists()
    r = h.raw('verdict', '--repo', str(h.repo), '--job', 'nosuch', '--verdict', 'pass', '--by', 'r', '--evidence', 'e',
              '--force')
    assert r.returncode == 0, r.stderr
    assert json.loads((h.repo / '.agy-bridge/verdicts.jsonl').read_text())['job'] == 'nosuch'


@pytest.mark.parametrize('verdict', ['reject', 'fixes'])
def test_verdict_reject_or_fixes_for_a_job_without_result_warns_and_records(h, verdict):
    # A delegate refused before sending (exit 4) writes no result file; its reader must still reject it.
    r = h.raw('verdict', '--repo', str(h.repo), '--job', 't1', '--verdict', verdict, '--by', 'r', '--evidence', 'exit 4')
    assert r.returncode == 0, r.stderr
    assert 'warning: no job "t1"' in r.stderr and 'invalid workflow input' not in r.stderr
    assert json.loads((h.repo / '.agy-bridge/verdicts.jsonl').read_text())['verdict'] == verdict


def test_verdict_unknown_job_error_has_no_workflow_input_prefix(h):
    r = h.raw('verdict', '--repo', str(h.repo), '--job', 'nosuch', '--verdict', 'pass', '--by', 'r', '--evidence', 'e')
    assert r.returncode == 1 and 'agy-bridge: verdict: no job "nosuch"' in r.stderr
    assert 'invalid workflow input' not in r.stderr


def test_verdict_names_recent_jobs_and_accepts_known_ones(h):
    results = h.repo / '.agy-bridge/results'
    results.mkdir(parents=True)
    (results / 'alpha.md').write_text('x')
    done = h.repo / '.agy-bridge/queue/done'
    done.mkdir(parents=True)
    (done / 'beta.job').write_text('{}')
    base = ('verdict', '--repo', str(h.repo), '--verdict', 'pass', '--by', 'r', '--evidence', 'e')
    r = h.raw(*base, '--job', 'gamma')
    assert r.returncode == 1 and 'known recent:' in r.stderr and 'alpha' in r.stderr and 'beta' in r.stderr
    assert not (h.repo / '.agy-bridge/verdicts.jsonl').exists()
    assert h.raw(*base, '--job', 'alpha').returncode == 0
    assert h.raw(*base, '--job', 'beta').returncode == 0


def test_os_error_names_operation_path_and_reason():
    err = FileNotFoundError(2, 'No such file or directory', '/tmp/x\nexit_code: 0')
    line, = stage.explain(err, 'queue run')
    assert line.startswith('queue run failed at ') and '\\n' in line and '\n' not in line
    assert 'No such file or directory' in line


def test_os_error_with_key_shaped_path_is_redacted():
    err = PermissionError(13, 'Permission denied', '/tmp/AKIAABCDEFGHIJKLMNOP')
    line, = stage.explain(err, 'status')
    assert 'AKIA' not in line and 'redacted' in line


def test_symlinked_metadata_refusal_explains_the_operation(h):
    base = h.repo / '.agy-bridge'
    base.mkdir()
    (base / 'queue').symlink_to(h.root / 'outside')
    r = h.raw('status', '--repo', str(h.repo))
    assert r.returncode == 4 and r.stderr.startswith('agy-bridge: status ')
    assert 'inspect paths and permissions' not in r.stderr


def test_verdict_uses_custom_queue_and_results_folders(h):
    job = h.root / 'job.json'
    job.write_text(json.dumps(dict(name='custom1', model='fake', task='review', files=['notes.md'], timeout='5s',
                                   context=False)))
    folders = ('--queue-dir', 'work/q', '--results-dir', 'work/out')
    assert h.raw('queue', 'add', str(job), '--repo', str(h.repo), *folders).returncode == 0
    r = h.raw('queue', 'run', '--repo', str(h.repo), *folders)
    assert r.returncode == 0, r.stderr
    assert (h.repo / 'work/out/custom1.md').exists()
    base = ('verdict', '--repo', str(h.repo), '--job', 'custom1', '--verdict', 'pass', '--by', 'r', '--evidence', 'e')
    r = h.raw(*base)
    assert r.returncode == 1
    assert '--queue-dir .agy-bridge/queue and --results-dir .agy-bridge/results' in r.stderr
    assert not (h.repo / '.agy-bridge/verdicts.jsonl').exists()
    r = h.raw(*base, *folders)
    assert r.returncode == 0, r.stderr
    assert json.loads((h.repo / '.agy-bridge/verdicts.jsonl').read_text())['job'] == 'custom1'
    r = h.raw(*base[:4], 'other', *base[5:], '--results-dir', 'work/out')
    assert r.returncode == 1 and '--results-dir work/out' in r.stderr and 'custom1' in r.stderr


VERDICT_PASS = ('--verdict', 'pass', '--by', 'r', '--evidence', 'e')


def test_verdict_pass_accepts_a_delegate_answer_file(h):
    delegate = h.repo / '.agy-bridge/delegate'
    delegate.mkdir(parents=True)
    (delegate / 'review1.md').write_text('answer\n')
    r = h.raw('verdict', '--repo', str(h.repo), '--job', 'review1', *VERDICT_PASS)
    assert r.returncode == 0, r.stderr
    assert json.loads((h.repo / '.agy-bridge/verdicts.jsonl').read_text())['job'] == 'review1'


def test_verdict_pass_ignores_a_symlinked_delegate_answer(h):
    delegate = h.repo / '.agy-bridge/delegate'
    delegate.mkdir(parents=True)
    (h.root / 'elsewhere.md').write_text('answer\n')
    (delegate / 'review2.md').symlink_to(h.root / 'elsewhere.md')
    r = h.raw('verdict', '--repo', str(h.repo), '--job', 'review2', *VERDICT_PASS)
    assert r.returncode == 1 and 'no job "review2"' in r.stderr and '.agy-bridge/delegate/' in r.stderr
    assert not (h.repo / '.agy-bridge/verdicts.jsonl').exists()


def test_verdict_unknown_job_lists_recent_delegate_answers(h):
    delegate = h.repo / '.agy-bridge/delegate'
    delegate.mkdir(parents=True)
    (delegate / 'recent1.md').write_text('answer\n')
    r = h.raw('verdict', '--repo', str(h.repo), '--job', 'other', *VERDICT_PASS)
    assert r.returncode == 1 and 'recent1' in r.stderr


def _pending(h, name):
    queue = h.repo / '.agy-bridge/queue'
    queue.mkdir(parents=True, exist_ok=True)
    (queue / (name + '.job')).write_text('{}')


def test_verdict_pass_on_a_pending_job_is_refused(h):
    _pending(h, 'waiting1')
    r = h.raw('verdict', '--repo', str(h.repo), '--job', 'waiting1', *VERDICT_PASS)
    assert r.returncode == 1, r.stderr
    assert 'job "waiting1" is still pending (no result yet)' in r.stderr and 'queue run -C' in r.stderr
    assert '.agy-bridge/results/waiting1.md' in r.stderr and '--force' in r.stderr
    assert not (h.repo / '.agy-bridge/verdicts.jsonl').exists()


def test_verdict_pass_on_a_failed_only_job_is_refused(h):
    failed = h.repo / '.agy-bridge/queue/failed'
    failed.mkdir(parents=True)
    (failed / 'broken1.job').write_text('{}')
    r = h.raw('verdict', '--repo', str(h.repo), '--job', 'broken1', *VERDICT_PASS)
    assert r.returncode == 1 and 'job "broken1" failed or was cancelled' in r.stderr
    assert 'record fixes or reject, or pass --force' in r.stderr
    r = h.raw('verdict', '--repo', str(h.repo), '--job', 'broken1', '--verdict', 'reject', '--by', 'r',
              '--evidence', 'e')
    assert r.returncode == 0 and 'warning' not in r.stderr


def test_verdict_fixes_on_a_pending_job_is_recorded_with_a_warning(h):
    _pending(h, 'waiting2')
    r = h.raw('verdict', '--repo', str(h.repo), '--job', 'waiting2', '--verdict', 'fixes', '--by', 'r',
              '--evidence', 'e')
    assert r.returncode == 0, r.stderr
    assert 'warning: job "waiting2" is still pending' in r.stderr
    assert json.loads((h.repo / '.agy-bridge/verdicts.jsonl').read_text())['verdict'] == 'fixes'
