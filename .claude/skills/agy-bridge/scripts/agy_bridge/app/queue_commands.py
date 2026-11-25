"""The queue add and `queue watch --detach` use cases that the cli queue handler drives."""
from __future__ import annotations

import json
import os
import signal
import sys

from ..core import config, stage
from ..core.config import UsageError
from ..port import launcher, safety
from . import queue, report
from .console import fail, say


def add(paths, job_file, dry_run):
    """Queue (or, with dry_run, preview) the job read from job_file ('-' is stdin); return the exit code."""
    from . import grading

    if job_file != '-' and not os.path.lexists(job_file):
        raise UsageError('job file not found: ' + stage.printable(job_file))
    raw = sys.stdin.read(queue.MAX_JOB_BYTES + 1) if job_file == '-' else grading.read_input(job_file)
    if len(raw.encode('utf-8')) > queue.MAX_JOB_BYTES:
        raise UsageError('job is over 128 KiB')
    job = json.loads(raw)
    if dry_run:
        head, result = queue.preview(paths, job)
        for line in head + list(result.preview):
            say(line)
        sys.stderr.writelines(result.diagnostics)
        if result.not_sent:
            info = result.metadata
            say(report.render('', info['model'], len(info['files']), info['total_bytes'], info['seconds'],
                              info['truncated'], None, result.not_sent, None))
        return result.exit_code
    say(paths.add(job))
    try:
        runner = paths.running()
    except (OSError, stage.Refused):
        runner = None
    if runner is not None:
        # An add is a send when a watcher drains the queue: tell the caller it is not just a queued job.
        if runner.get('mode') == 'run':
            say('NOTE runner pid %s (queue run) is running; it may send this job' % runner.get('pid'))
        else:
            say('NOTE watcher pid %s is running; this job will be sent now' % runner.get('pid'))
    stale = queue.watcher_warning(runner)
    if stale:
        fail(stale)
    return config.EXIT_OK


WATCHER_LOG = 'watcher.log'
DETACH_WAIT_S = 3.0


def detach_watcher(paths, poll):
    """Start `queue watch` in a new session with its output appended to <queue>/watcher.log.

    Refuses (R6) when a runner already holds the queue lock. Succeeds only once the new watcher holds the lock.
    """
    if paths.running() is not None:
        raise stage.Refused([stage.Refusal('R6', 'queue already has an active runner; see status for its PID',
                                           'watcher.pid')])
    with safety.directory(os.path.join(paths.root, paths.queue), create=True) as parent:
        log = os.open(WATCHER_LOG, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600,
                      dir_fd=parent)
    log_path = os.path.join(paths.root, paths.queue, WATCHER_LOG)
    try:
        safety.regular(log)
        arguments = ['queue', 'watch', '--repo=' + paths.root, '--queue-dir=' + paths.queue,
                     '--results-dir=' + paths.results, '--poll=' + repr(float(poll))]
        if paths.allow:
            arguments.append('--allow-non-git-root')
        # An ignored SIGHUP survives exec, so the watcher keeps it ignored (nohup convention).
        previous = signal.signal(signal.SIGHUP, signal.SIG_IGN)
        try:
            proc = launcher.use().start_worker(arguments, output=log, cwd=paths.root)
        finally:
            signal.signal(signal.SIGHUP, previous)
    finally:
        os.close(log)
    return _await_watcher(paths, proc, log_path)


def _await_watcher(paths, proc, log_path):
    """Report the detached watcher once it holds the queue lock; stop it if it misses DETACH_WAIT_S."""
    import time

    deadline = time.monotonic() + DETACH_WAIT_S
    while time.monotonic() < deadline:
        active = paths.running()
        if active is not None and active.get('pid') == proc.pid:
            say('watcher started pid %d; log %s; stop with: kill %d'
                % (proc.pid, stage.printable(log_path), proc.pid))
            return config.EXIT_OK
        if proc.poll() is not None:
            break
        time.sleep(.05)
    code = proc.poll()
    if code is None:
        active = paths.running()
        if active is not None and active.get('pid') == proc.pid:
            say('watcher started pid %d; log %s; stop with: kill %d'
                % (proc.pid, stage.printable(log_path), proc.pid))
            return config.EXIT_OK
        # A start reported as failed must not keep running and drain the queue unattended.
        launcher.use().stop_detached(proc)
        reason = 'did not take the queue lock within %gs and was stopped' % DETACH_WAIT_S
    else:
        reason = 'exited with code %d' % code
    fail('watcher pid %d %s; read the end of %s and check: agy-bridge status -C %s'
         % (proc.pid, reason, stage.printable(log_path), stage.shell_arg(paths.root)))
    return config.EXIT_AGY_FAILED
