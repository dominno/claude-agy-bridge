"""Append-only reader verdicts; no task or answer text in the journal."""
from __future__ import annotations

import json
import os
import re

from . import config, events, safety, stage
from .scan import scan_bytes

NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$')


class UnknownJob(config.UsageError):
    """No result or queue job file names the job; the message lists recent jobs."""


def name(value):
    if not isinstance(value, str) or not NAME.fullmatch(value):
        raise config.UsageError('job name must be 1-120 ASCII letters, digits, dots, underscores or hyphens, starting with a letter or digit')
    if scan_bytes(value.encode('utf-8')):
        raise config.UsageError('key-shaped job name refused')
    return value


DELEGATE_DIR = config.SCRATCH_DIR + '/delegate'


def _delegate_result(root, job):
    """True for a regular, owned, single-link, no-follow <repo>/.agy-bridge/delegate/<job>.md (xq's delegate
    recipe writes the answer there)."""
    try:
        with safety.directory(os.path.join(root, DELEGATE_DIR)) as parent:
            fd = os.open(job + '.md', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            try:
                safety.regular(fd)
            finally:
                os.close(fd)
        return True
    except OSError:
        return False


REFUSAL_HEADING = '# Queue refusal'
REFUSAL_MARKER = '<!-- agy-bridge-refusal-v1 '


def _refusal_result(root, rel):
    """True when <rel> (a results/<job>.md) is a queue refusal: line 1 is the refusal heading or line 2 the
    refusal marker. Reads at most 4 KiB from a regular, no-follow file; unreadable files are not refusals."""
    try:
        with safety.directory(os.path.join(root, os.path.dirname(rel))) as parent:
            fd = os.open(os.path.basename(rel), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            try:
                safety.regular(fd)
                head = os.read(fd, 4096)
            finally:
                os.close(fd)
    except OSError:
        return False
    lines = head.decode('utf-8', 'replace').split('\n')
    return (lines[0].rstrip('\r') == REFUSAL_HEADING
            or (len(lines) > 1 and lines[1].startswith(REFUSAL_MARKER)))


def _job_state(paths, job):
    """'result' for a done archive; 'failed' for a failed/ archive without a done one, even when results/<job>.md
    exists (a failed run, an invalid job and `queue cancel` all publish a result there); 'failed' also when
    results/<job>.md is a queue refusal (heading or refusal marker) whose archive move did not happen; else 'result'
    when results/<job>.md or the delegate answer file exists, 'pending' for a queued job file; else raise UnknownJob
    naming the searched folders and recent jobs. Never creates folders."""
    root = paths.root
    if paths.exists(paths.queue+'/done/'+job+'.job'):
        return 'result'
    if paths.exists(paths.queue+'/failed/'+job+'.job'):
        return 'failed'
    if paths.exists(paths.results+'/'+job+'.md'):
        return 'failed' if _refusal_result(root, paths.results+'/'+job+'.md') else 'result'
    if _delegate_result(root, job):
        return 'result'
    if paths.exists(paths.queue+'/'+job+'.job'):
        return 'pending'
    recent = []
    for rel, suffix in ((paths.results, '.md'), (paths.queue+'/done', '.job'), (paths.queue+'/failed', '.job'),
                        (paths.queue, '.job'), (DELEGATE_DIR, '.md')):
        try:
            entries = paths.names(rel, suffix)
        except OSError:
            continue
        for entry in entries:
            try:
                with safety.directory(os.path.join(root, rel)) as fd:
                    mtime = os.stat(entry, dir_fd=fd, follow_symlinks=False).st_mtime
            except OSError:
                continue
            recent.append((mtime, entry[:-len(suffix)]))
    names = []
    for _, item in sorted(recent, reverse=True):
        if item not in names and NAME.fullmatch(item) and not scan_bytes(item.encode('utf-8')):
            names.append(item)
    folders = '--queue-dir %s and --results-dir %s' % (paths.queue, paths.results)
    raise UnknownJob('no job "%s" in %s (searched %s: pending, done, failed and results, and %s/); '
                     'known recent: %s; run agy-bridge status -C %s with the same folder options '
                     '(or pass --force to record anyway)'
                     % (job, stage.printable(root), folders, DELEGATE_DIR, ', '.join(names[:5]) or 'none',
                        stage.shell_arg(root)))


def _folder_options(paths):
    options = ''
    if paths.queue != '.agy-bridge/queue':
        options += ' --queue-dir ' + stage.shell_arg(paths.queue)
    if paths.results != '.agy-bridge/results':
        options += ' --results-dir ' + stage.shell_arg(paths.results)
    return options


def record(root, job, verdict, by, evidence, note=None, force=True, allow=False, queue_dir=None, results_dir=None):
    """Append one verdict and return a warning string or None.

    With force=False a pass needs a result for the job: <results>/<job>.md, a done archive <queue>/done/<job>.job
    or a delegate answer .agy-bridge/delegate/<job>.md (queue_dir and results_dir default to .agy-bridge/queue and
    .agy-bridge/results). A pass on a job that is only pending or only failed is refused. A fixes or reject
    verdict on an unknown job is recorded with a warning instead: a delegate that failed or was refused (for example
    exit 4) writes no result file, and its reader must still be able to reject it.
    """
    name(job)
    if verdict not in ('pass', 'fixes', 'reject'):
        raise config.UsageError('verdict must be pass, fixes or reject')
    for text in (by, evidence):
        if not isinstance(text, str) or not text.strip() or len(text) > 8000:
            raise config.UsageError('reader and evidence must be nonempty strings of at most 8000 characters')
    if note is not None and (not isinstance(note, str) or len(note) > 8000):
        raise config.UsageError('note must be at most 8000 characters')
    row = dict(job=job, verdict=verdict, by=by, evidence=evidence)
    if note is not None:
        row['note'] = note
    if len(json.dumps(row, ensure_ascii=True).encode('utf-8')) > events.MAX_EVENT_LINE_BYTES - 100:
        raise config.UsageError('verdict is too large for the bounded journal reader')
    warning = None
    if not force:
        from . import queue

        paths = queue.Paths(root, queue_dir, results_dir, allow)
        try:
            state = _job_state(paths, job)
        except UnknownJob as exc:
            if verdict == 'pass':
                raise
            warning = str(exc).split(';', 1)[0] + '; recorded anyway because a %s verdict needs no result' % verdict
        else:
            if state == 'pending':
                message = ('job "%s" is still pending (no result yet); run agy-bridge queue run -C %s%s, then read '
                           '%s/%s.md (or pass --force)' % (job, stage.shell_arg(root), _folder_options(paths),
                                                    stage.printable(paths.results), job))
                if verdict == 'pass':
                    raise UnknownJob(message)
                warning = message.split(';', 1)[0] + '; recorded anyway because a %s verdict needs no result' % verdict
            elif state == 'failed' and verdict == 'pass':
                raise UnknownJob('job "%s" failed or was cancelled (archived in %s/failed/, or its result in %s/ '
                                 'is a queue refusal; a failure or refusal report is not an answer); record fixes or reject, or '
                                 'pass --force' % (job, paths.queue, paths.results))
    fd = events.open_log(root, name='verdicts.jsonl')
    try:
        events.append(fd, row)
    finally:
        os.close(fd)
    return warning


def rows(root, count=None):
    output = []
    for line in events.tail(root, count, name='verdicts.jsonl'):
        row = json.loads(line)
        if not isinstance(row, dict) or not isinstance(row.get('job'), str) or row.get('verdict') not in ('pass', 'fixes', 'reject'):
            raise events.EventError('invalid verdict record')
        output.append(row)
    return output
