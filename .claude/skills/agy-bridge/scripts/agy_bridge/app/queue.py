"""Serial job queue use cases (add, preview, run, watch, cancel, status): the delegate pipeline remains the only
sender."""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import stat
import time

from ..core import config, stage
from ..core.grading_rules import unannotate
from ..core.queue_job import (MAX_CANCEL_REASON, MAX_JOB_BYTES, MAX_NEED_CANDIDATES, MAX_NEED_LINE, MAX_NEED_TEXT,
                              RESULT_CHECKSUM, _display_name, invalid_reason, result_text)
from ..core.scan import scan_bytes
from ..port import safety
from . import queue_store, verdicts
from .delegation import DelegationRequest, execute
from .queue_store import (Paths, QueueLocation, _owned_regular, _saved_result,  # noqa: F401 (QueueLocation: callers)
                          context_files, validate, watcher_warning)


def need_files(root, answer, current):
    added = []
    candidates = candidate_text = 0
    for line in answer.splitlines():
        # Cap before stripping citation annotations or parsing paths.
        if len(line) > MAX_NEED_LINE:
            continue
        original_size = len(line.encode('utf-8')) + 1
        line = unannotate(line)
        if not line.startswith('NEED:'):
            continue
        candidates += 1
        candidate_text += original_size
        if candidates > MAX_NEED_CANDIDATES or candidate_text > MAX_NEED_TEXT:
            break
        value = line[5:].strip()
        try:
            safety.relative(value)
            rel = safety.local(root, value)
            safety.read(root, rel, config.MAX_FILE_BYTES)
        except (config.UsageError, stage.Refused, OSError):
            continue
        if rel not in current and rel not in added:
            added.append(rel)
        if len(added) == 3:
            break
    return added


def preview(paths, job):
    """Validate a job and return (header lines, delegation result) for the exact first-round input; sends nothing."""
    validate(paths.root, job)
    context = context_files(paths.root, job)
    files = list(dict.fromkeys(context + job['files']))
    exists = [rel for rel in (paths.queue+'/'+job['name']+'.job', paths.queue+'/done/'+job['name']+'.job',
                              paths.queue+'/failed/'+job['name']+'.job', paths.results+'/'+job['name']+'.md')
              if paths.exists(rel)]
    head = ['Queue job preview for %s (not added, nothing sent).' % stage.printable(job['name']),
            'context files from .agy-bridge.json: ' + (', '.join(stage.printable(f) for f in context) or 'none') +
            ('' if job.get('context', True) is not False else ' (job sets "context": false)'),
            'job files: ' + ', '.join(stage.printable(f) for f in job['files']),
            'NEED round: ' + ('yes, up to 3 more files the model names may be sent in round 2'
                              if job.get('need', False) else 'no'),
            'name: ' + ('already used; queue add will refuse it' if exists else 'free'),
            'remote agent: ' + (('yes, project ' + stage.printable(job['remote_project'])
                                 if job.get('remote_project') else 'yes, default remote project')
                                if job.get('remote') else 'no')]
    return head, execute(_request(paths.root, job, files, job['task'], paths.allow, dry=True))


def _request(root, job, files, task, allow, dry=False):
    """The delegation one queue round runs: the job's model, citations, timeout and (optional) remote agent."""
    return DelegationRequest(
        repo=root, task=task, files=files, model=job.get('model'), cite=True,
        require_verified=job.get('cite', False), timeout=str(job.get('timeout', '5m')),
        allow_non_git_root=allow, dry=dry, remote=job.get('remote', False),
        remote_project=job.get('remote_project'))


def attempt(root, job, files, task, allow=False):
    return execute(_request(root, job, files, task, allow)).as_queue_record()



def _send_round(paths, job, files, task, number, attempts):
    """One round: retry a quota exit up to 3 times with backoff and, at most twice, resend without R5-refused files."""
    quota = drops = 0
    while True:
        item = attempt(paths.root, job, files, task, paths.allow)
        item['round'] = number
        attempts.append(item)
        code = item['exit_code']
        if code == config.EXIT_QUOTA and quota < 3:
            time.sleep(5 * 2**quota)
            quota += 1
            continue
        if code == config.EXIT_REFUSED and drops < 2:
            bad = {r['path'] for r in item.get('not_sent', []) if r['rule']=='R5'}
            remaining = [f for f in files if f not in bad]
            if remaining and remaining != files:
                files = remaining
                drops += 1
                continue
        return item, files


def _send_rounds(paths, job, files):
    """Round 1, then (for a need job that succeeded and named files) round 2 with up to 3 added files."""
    task = job['task']
    rounds, attempts = [], []
    added = []
    for number in (1, 2):
        item, files = _send_round(paths, job, files, task, number, attempts)
        code = item['exit_code']
        rounds.append(dict(number=number, item=item, added=added))
        if number == 2 or code not in (0, 17) or not job.get('need', False):
            break
        added = need_files(paths.root, item['answer'], files)
        if not added:
            break
        files += added
        task += '\nSecond round: complete the original task using the added files; do not request another round.'
    return rounds, attempts, code


def _read_job(paths, rel, name):
    """The job bytes, or (None, code) when the job was withdrawn or its interrupted archive was completed."""
    with paths.publication() as src:
        try:
            info = os.stat(name, dir_fd=src, follow_symlinks=False)
        except FileNotFoundError:
            return None, None  # cancelled (or archived) after it was listed; nothing was sent
        if info.st_nlink == 2 and stat.S_ISREG(info.st_mode):
            return None, paths.recover_archive(src, name, info)
        return safety.read(paths.root, rel, MAX_JOB_BYTES), None


def run_job(paths, rel):
    name = os.path.basename(rel)
    job_name = verdicts.name(name[:-4])
    if rel != paths.queue+'/'+job_name+'.job':
        raise config.UsageError('job filename must match name.job')
    raw, code = _read_job(paths, rel, name)
    if raw is None:
        return code
    result_rel = paths.results+'/'+job_name+'.md'
    digest = hashlib.sha256(raw).hexdigest()
    for archive in ('done', 'failed'):
        if paths.exists(paths.queue+'/'+archive+'/'+name):
            raise config.UsageError('reused job name conflicts with archived job')
    code = _saved_result(paths, job_name, digest)
    if code is not None:
        paths.move(rel, code == 0)
        return code
    job = validate(paths.root, json.loads(raw))
    if job['name'] != job_name:
        raise config.UsageError('job filename must match name.job')
    files = list(dict.fromkeys(context_files(paths.root, job)+job['files']))
    paths.attempted = job_name  # from here on a refusal can no longer promise that nothing was sent
    rounds, attempts, code = _send_rounds(paths, job, files)
    success = [r for r in rounds if r['item']['exit_code']==0]
    retained = (success or rounds)[-1]['item']
    text = result_text(job, digest, rounds, attempts, code)
    checksum = hashlib.sha256(text.encode('utf-8')).hexdigest()
    safety.publish(paths.root, result_rel, text + RESULT_CHECKSUM + checksum + ' -->\n')
    paths.move(rel, bool(success))
    return 0 if success else retained['exit_code']


def snapshot(paths):
    """Read queue entry metadata and the live lock without opening any job payload."""
    warnings = []
    try:
        watcher = paths.running()
    except (OSError, stage.Refused):
        watcher = None
        warnings.append('queue watcher metadata could not be read safely')
    if watcher and watcher['pid'] == 'unknown':
        warnings.append('active queue watcher metadata is unreadable')
    elif watcher and watcher.get('job') is not None and watcher['started'] is None:
        warnings.append('active queue job start time is unreadable')
    stale = watcher_warning(watcher)
    if stale:
        warnings.append(stale)

    jobs = {}
    invalid = False
    for phase, rel in (('pending', paths.queue), ('done', paths.queue+'/done'),
                       ('failed', paths.queue+'/failed')):
        try:
            with safety.directory(os.path.join(paths.root, rel)) as directory:
                for name in sorted(os.listdir(directory)):
                    if not name.endswith('.job'):
                        continue
                    try:
                        job_id = verdicts.name(name[:-4])
                        info = os.stat(name, dir_fd=directory, follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                    except (OSError, config.UsageError):
                        invalid = True
                        continue
                    if not _owned_regular(info, 1):
                        invalid = True
                        continue
                    active = phase == 'pending' and watcher is not None and watcher.get('job') == job_id
                    # Prefer archive metadata if a move occurs while scanning.
                    jobs[(job_id, info.st_dev, info.st_ino)] = dict(
                        id=job_id, phase='running' if active else phase, active=active,
                        created_at=None, updated_at=info.st_mtime,
                        started_at=watcher['started'] if active else None,
                    )
        except FileNotFoundError:
            continue
        except OSError:
            invalid = True
    if invalid:
        warnings.append('some queue entries could not be inspected safely')

    rows = list(jobs.values())
    counts = dict.fromkeys(('pending', 'running', 'done', 'failed'), 0)
    for row in rows:
        counts[row['phase']] += 1
    return dict(repo=paths.root, queue_dir=paths.queue, active=watcher is not None,
                watcher_pid=watcher['pid'] if watcher else None, mode=watcher['mode'] if watcher else None,
                counts=counts, jobs=rows, warnings=warnings)


def list_jobs(paths):
    view = snapshot(paths)
    return view['jobs'], view['warnings']


MAX_STATUS_PENDING = 20


def status(paths, threshold=300):
    active = paths.running()
    out = ['watcher: '+('running (pid %s, %s)' % (active.get('pid'),active.get('mode')) if active else 'stopped')]
    stale = watcher_warning(active)
    if stale:
        out.append(stale)
    for label,rel in [('pending',paths.queue),('done',paths.queue+'/done'),('failed',paths.queue+'/failed')]:
        names = paths.names(rel,'.job')
        out.append('%s: %d' % (label,len(names)))
        if label == 'pending' and names:
            # A run sends every pending job, including ones other sessions added: name them.
            shown = ', '.join(_display_name(n[:-4]) for n in names[:MAX_STATUS_PENDING])
            more = len(names) - MAX_STATUS_PENDING
            out.append('pending jobs: ' + shown + (' (+%d more; agy-bridge jobs list -C %s)'
                                                    % (more, stage.shell_arg(paths.root)) if more > 0 else ''))
    if active and active.get('job') and active.get('started') is not None:
        duration = max(0,time.time()-active['started'])
        out.append('running: %s (%.1fs%s)' % (active['job'],duration,', exceeds threshold' if duration>threshold else ''))
    results = paths.names(paths.results,'.md')
    latest = None
    for name in results:
        with safety.directory(os.path.join(paths.root,paths.results)) as fd:
            entry = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            try:
                safety.regular(entry)
                info=os.fstat(entry)
            finally:
                os.close(entry)
        latest=max(latest or (0,''),(info.st_mtime,name))
    checked={r['job'] for r in verdicts.rows(paths.root)}
    # Result names come from the repository tree: escape them like pending job names.
    out += ['last result: '+(_display_name(latest[1]) if latest else 'none'),
            'results without verdict: '+(', '.join(_display_name(n[:-3]) for n in results if n[:-3] not in checked) or 'none')]
    return '\n'.join(out)


def run(paths, watch=False, poll=5):
    code=0
    skipped = set()
    with paths.lock('watch' if watch else 'run'):
        safety.sweep_temp(paths.root, paths.results)
        while True:
            # Each iteration: a temp hard link left by an interrupted queue add would otherwise make run_job's
            # nlink==2 recovery treat the job as foreign.
            safety.sweep_temp(paths.root, paths.queue)
            if watch and not os.path.isdir(queue_store.CODE_ROOT):
                print('watcher: agy-bridge code root %s no longer exists (skill or plugin updated or removed); stopping'
                      % stage.printable(queue_store.CODE_ROOT), flush=True)
                return code
            for name in paths.names(paths.queue,'.job'):
                rel = paths.queue+'/'+name
                with safety.directory(os.path.join(paths.root, paths.queue)) as fd:
                    try:
                        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                identity = (name, info.st_ino, info.st_mtime_ns, info.st_size, info.st_mode, info.st_nlink)
                if identity in skipped:
                    continue
                paths.state(name[:-4])
                paths.attempted = None
                try:
                    rc=run_job(paths,paths.queue+'/'+name)
                    if rc is None:
                        print('%s: withdrawn before it ran; nothing sent' % _display_name(name), flush=True)
                        continue
                    print('%s: exit %d' % (_display_name(name),rc),flush=True)
                    if rc: code=rc
                except (ValueError, config.UsageError, stage.Refused, OSError, RecursionError) as exc:
                    reason = invalid_reason(exc)
                    next_step = ('next: fix the job and re-add it under a new name: agy-bridge queue add -C %s '
                                 '<job.json>' % stage.shell_arg(paths.root))
                    try:
                        action = paths.failure(rel, reason, next_step,
                                               sent=paths.attempted == name[:-4])
                    except (OSError, stage.Refused, config.UsageError):
                        action = 'skipped; refusal result could not be written'
                    skipped.add(identity)
                    print('%s: invalid job (%s); %s\n%s' % (_display_name(name), reason, action, next_step),
                          flush=True)
                    code=config.EXIT_REFUSED if isinstance(exc,(stage.Refused,OSError)) else config.EXIT_USAGE
                finally:
                    paths.state()
            if not watch:
                return code
            time.sleep(poll)



class CancelRefused(config.UsageError):
    """queue cancel could not withdraw the job; the message names why and what to do."""


class CancelUnknown(CancelRefused):
    """queue cancel found no such job: a usage error (exit 1), as for verdict --job."""


def cancel(root, name, reason=None, queue_dir=None, results_dir=None, allow=False):
    """Withdraw a pending job before any runner sends it: archive it to failed/ and publish a refusal result.

    Holds the queue publication lock, which a runner also needs to read a job before sending it, so a job either
    is withdrawn first or is already running (refused). Returns the archive action text.
    """
    verdicts.name(name)
    if reason is None:
        reason = 'cancelled'
    if not isinstance(reason, str) or not reason.strip() or not reason.isprintable() or len(reason) > MAX_CANCEL_REASON:
        raise config.UsageError('--reason must be 1-%d printable characters' % MAX_CANCEL_REASON)
    if scan_bytes(reason.encode('utf-8', 'replace')):
        reason = '[redacted key-shaped reason]'
    paths = Paths(root, queue_dir, results_dir, allow)
    rel = paths.queue+'/'+name+'.job'
    with contextlib.ExitStack() as stack:
        try:
            src = stack.enter_context(paths.publication())
        except FileNotFoundError:
            src = None
        if src is None or not paths.exists(rel):
            for archive, what in (('done', 'already finished'), ('failed', 'already failed or was cancelled')):
                if paths.exists(paths.queue+'/'+archive+'/'+name+'.job'):
                    raise CancelRefused('job "%s" %s (in %s/%s/); nothing to cancel'
                                        % (name, what, paths.queue, archive))
            raise CancelUnknown('no pending job "%s" in %s/; check agy-bridge status -C %s'
                                % (name, stage.printable(paths.queue), stage.shell_arg(paths.root)))
        active = paths.running()
        if active is not None and active.get('job') == name:
            raise CancelRefused('job "%s" is running (pid %s); wait for it or stop the runner'
                                % (name, active.get('pid')))
        next_step = ('next: to run it after all, re-add it under a new name: agy-bridge queue add -C %s <job.json>'
                     % stage.shell_arg(paths.root))
        # Archive first: if the move is unsafe the error propagates, the job stays pending and nothing is published.
        paths.move(rel, False, src)
        return paths.failure(rel, 'cancelled by reader: ' + reason, next_step, prefix='', archived=True)
