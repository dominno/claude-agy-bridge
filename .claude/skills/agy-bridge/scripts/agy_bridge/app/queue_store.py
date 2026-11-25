"""The on-disk queue of one repository: job validation, its folders, the runner lock and metadata, archives and
saved results."""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import stat
import sys
import time

from pathlib import Path

from .. import __version__
from ..core import config, stage
from ..core.queue_job import (JOB_FIELDS, MAX_JOB_BYTES, MAX_RESULT_BYTES, MAX_SHOWN_FIELDS, RESULT_CHECKSUM,
                              RESULT_FORMAT, RESULT_WARNING, _display_name, _interrupted, refusal_text)
from ..core.verdict import name as job_name
from ..port import safety


def _owned_regular(info, links):
    return stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_nlink == links


def _same_archive_link(info, initial):
    return _owned_regular(info, 2) and (
        info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns
    ) == (initial.st_dev, initial.st_ino, initial.st_size, initial.st_mtime_ns)


# The folder holding the agy_bridge package this process runs (the skill's scripts/). A detached watcher records it
# with its version, so a later invocation can tell that the watcher still runs code from before a skill update.
CODE_ROOT = str(Path(__file__).resolve().parents[2])
MAX_RUNNER_TEXT = 4096


def watcher_warning(active):
    """The WARN line when the live runner runs a different agy-bridge version or code root than this process."""
    if not active or type(active.get('pid')) is not int or active['pid'] == os.getpid():
        return None
    version, code_root = active.get('version'), active.get('code_root')
    if version == __version__ and code_root == CODE_ROOT:
        return None
    return ('WARN watcher pid %d runs agy-bridge %s from %s; this is %s; stop it (kill %d) and restart with queue '
            'watch --detach' % (active['pid'], stage.printable(version or 'unknown'),
                                stage.printable(code_root or 'unknown'), __version__, active['pid']))


def validate(root, job):
    if not isinstance(job, dict):
        raise config.UsageError('job is not a JSON object')
    unknown = sorted(set(job) - set(JOB_FIELDS))
    if unknown:
        shown = ', '.join(stage.printable(field) for field in unknown[:MAX_SHOWN_FIELDS])
        if len(unknown) > MAX_SHOWN_FIELDS:
            shown += ' (+%d more)' % (len(unknown) - MAX_SHOWN_FIELDS)
        raise config.UsageError('unknown job field(s): %s; allowed: %s (see references/QUEUE.md)'
                                % (shown, ', '.join(JOB_FIELDS)))
    job_name(job.get('name'))
    config.validate_model(job.get('model'))
    task = job.get('task')
    if not isinstance(task, str) or not task.strip() or len(task) > config.MAX_TASK_CHARS:
        raise config.UsageError('job needs a bounded nonempty task')
    files = job.get('files')
    if not isinstance(files, list) or not files or len(files) > config.MAX_FILES:
        raise config.UsageError('job needs 1-500 file paths')
    for path in files:
        if not isinstance(path, str) or not path.isprintable():
            raise config.UsageError('job file paths must be printable text without control characters')
        try:
            rel = safety.local(root, path)
        except config.UsageError as exc:
            raise config.UsageError('%s: %s' % (exc, stage.printable(path))) from None
        if rel != path:
            raise config.UsageError('job file paths must be repository relative: ' + stage.printable(path))
        safety.read(root, rel, config.MAX_FILE_BYTES)
    timeout = job.get('timeout', '5m')
    if isinstance(timeout, bool) or not isinstance(timeout, (str, int)):
        raise config.UsageError('invalid job timeout')
    config.parse_duration(str(timeout))
    if 'need' in job and type(job['need']) is not bool:
        raise config.UsageError('need must be a boolean')
    if 'cite' in job and type(job['cite']) is not bool:
        raise config.UsageError('cite must be a boolean')
    if 'context' in job and not (type(job['context']) is bool or job['context'] == 'short'):
        raise config.UsageError('context must be true, false or short')
    return job


def context_files(root, job):
    if job.get('context', True) is False:
        return []
    files = config.load_repo_config(root).get('context_files', [])
    if job.get('context') == 'short':
        files = files[:1]
    for p in files:
        safety.relative(p)
        safety.local(root, p)
        safety.read(root, p, config.MAX_FILE_BYTES)
    return files


RUNNING_RETRY_S = .05


def _parses(raw):
    try:
        return bool(raw) and isinstance(json.loads(raw), dict)
    except (ValueError, RecursionError):
        return False


class QueueLocation:
    """Locate one queue and read its runner metadata independently of result paths."""

    def __init__(self, root, queue_dir=None, allow=False):
        self.root = root
        stage.check_root(root, allow)
        self.queue = safety.local(root, queue_dir or '.agy-bridge/queue', metadata=True)

    def running(self):
        try:
            with safety.directory(os.path.join(self.root, self.queue)) as parent:
                fd = os.open('watcher.pid', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        except FileNotFoundError:
            return None
        try:
            safety.regular(fd)
            try:
                # Other readers can hold shared locks, but only a runner holds this lock exclusively.
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                raw = os.pread(fd, 8192, 0)
                if not _parses(raw):
                    # A runner from before the gap-free write (or one between pwrite and ftruncate) can leave a
                    # short read; look once more before reporting the pid as unknown.
                    time.sleep(RUNNING_RETRY_S)
                    raw = os.pread(fd, 8192, 0)
                try:
                    row = json.loads(raw) if raw else {}
                    if (not isinstance(row, dict) or type(row.get('pid')) is not int or row['pid'] < 1 or
                            row.get('mode') not in ('watch', 'run')):
                        return dict(pid='unknown', mode='unknown')
                    watcher = dict(pid=row['pid'], mode=row['mode'])
                    for field in ('version', 'code_root'):
                        value = row.get(field)
                        watcher[field] = (value if isinstance(value, str) and value.isprintable() and
                                          0 < len(value) <= MAX_RUNNER_TEXT else None)
                    if row.get('job') is not None:
                        job = job_name(row['job'])
                        started = row.get('started')
                        if type(started) not in (int, float) or not 0 <= started <= sys.float_info.max:
                            started = None
                        watcher.update(job=job, started=started)
                    return watcher
                except (ValueError, config.UsageError, RecursionError):
                    return dict(pid='unknown', mode='unknown')
            return None
        finally:
            os.close(fd)


class Paths(QueueLocation):
    def __init__(self, root, queue_dir=None, results_dir=None, allow=False):
        super().__init__(root, queue_dir, allow)
        self.allow = allow
        self.attempted = None  # the job whose agy attempt started (run_job), for the refusal 'sent' marker
        self.results = safety.local(root, results_dir or '.agy-bridge/results', metadata=True)
        # These folders must not overlap: job moves/results must have distinct destinations.
        if self.queue == self.results or self.queue.startswith(self.results+'/') or self.results.startswith(self.queue+'/'):
            raise config.UsageError('queue and results directories must be separate')
        for rel in (self.queue, self.queue+'/done', self.queue+'/failed', self.results):
            try:
                with safety.directory(os.path.join(root, rel)):
                    pass
            except FileNotFoundError:
                pass
        self.lock_fd = None
        self.mode = None

    def names(self, rel, suffix):
        try:
            with safety.directory(os.path.join(self.root, rel)) as fd:
                return sorted(n for n in os.listdir(fd) if n.endswith(suffix))
        except FileNotFoundError:
            return []

    @contextlib.contextmanager
    def publication(self, create=False):
        # Cooperating add/read operations cannot observe a half-written .job.
        with safety.directory(os.path.join(self.root, self.queue), create=create) as fd:
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                yield fd
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)

    def add(self, job):
        validate(self.root, job)
        context_files(self.root, job)
        with self.publication(create=True):
            name = job['name']
            for rel in (self.queue+'/'+name+'.job', self.queue+'/done/'+name+'.job', self.queue+'/failed/'+name+'.job', self.results+'/'+name+'.md'):
                if self.exists(rel):
                    raise config.UsageError('job name already exists; choose a new name')
            rel = self.queue+'/'+name+'.job'
            # Atomic: a runner never sees a half-written .job, and an interrupted add leaves no job behind.
            try:
                safety.publish(self.root, rel, json.dumps(job, indent=2)+'\n')
            except FileExistsError:
                raise config.UsageError('job name already exists; choose a new name') from None
            return rel

    @contextlib.contextmanager
    def lock(self, mode):
        with safety.directory(os.path.join(self.root, self.queue), create=True) as parent:
            fd = os.open('watcher.pid', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=parent)
        try:
            safety.regular(fd)
            # Status briefly probes this same lock. Tolerate that window without
            # mistaking it for an active runner; real runners still exclude us.
            deadline = time.monotonic() + .2
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise stage.Refused([stage.Refusal('R6', 'queue already has an active runner', 'watcher.pid')])
                    time.sleep(.02)
            self.lock_fd, self.mode = fd, mode
            self.state()
            yield
        finally:
            if self.lock_fd == fd:
                # Never truncate to empty while the lock is held: a reader would see no record. The row stays
                # after exit; readers trust it only while a runner holds the lock.
                with contextlib.suppress(OSError):
                    self.state(stopping=True)
                self.lock_fd = None
            os.close(fd)

    def state(self, job=None, stopping=False):
        if self.lock_fd is None:
            return
        row = dict(pid=os.getpid(), mode=self.mode, job=job, started=time.time() if job else None,
                   version=__version__, code_root=CODE_ROOT)
        if stopping:
            row['stopping'] = True
        data = json.dumps(row).encode()
        # Overwrite in place, then cut any longer tail: a concurrent reader never sees an empty file.
        os.pwrite(self.lock_fd, data, 0)
        os.ftruncate(self.lock_fd, len(data))
        os.fsync(self.lock_fd)

    def exists(self, rel):
        try:
            with safety.directory(os.path.dirname(os.path.join(self.root, rel))) as fd:
                os.stat(os.path.basename(rel), dir_fd=fd, follow_symlinks=False)
            return True
        except FileNotFoundError:
            return False

    def failure(self, rel, reason, next_step=None, prefix='invalid job: ', archived=False, sent=False):
        """Keep poison objects untouched; record their refusal and continue.

        archived=True when the caller already moved rel to failed/ (queue cancel, under publication()). sent=True
        when an agy attempt for this job had already started, so the marker says 'sent: maybe'."""
        if archived:
            action = 'archived as failed'
        else:
            try:
                self.move(rel, False)
                action = 'archived as failed'
            except (OSError, stage.Refused):
                action = 'skipped; unsafe or unavailable archive operation'
        name = os.path.basename(rel)[:-4]
        try:
            job_name(name)
        except config.UsageError:
            name = 'invalid-job'
        message = prefix+reason+'; '+action
        if next_step:
            message += '\n' + next_step
        for number in range(10000):
            result = self.results+'/'+name+('.%d' % number if number else '')+'.md'
            try:
                safety.publish(self.root, result, refusal_text(message, sent))
                return action
            except FileExistsError:
                continue
        raise OSError('no unused refusal result name')

    def move(self, rel, ok, src=None):
        """Archive rel to done/ or failed/; src is the queue folder descriptor when publication() is held."""
        if src is None:
            with self.publication() as held:
                return self.move(rel, ok, held)
        dest = self.queue+('/done' if ok else '/failed')
        with safety.directory(os.path.join(self.root, dest), create=True) as dst:
            name = os.path.basename(rel)
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=src)
            try:
                safety.regular(fd)
            finally:
                os.close(fd)
            # Link then unlink provides no-overwrite movement within the repo.
            for number in range(10000):
                archived = name if number == 0 else name[:-4]+'.%d.job' % number
                try:
                    os.link(name, archived, src_dir_fd=src, dst_dir_fd=dst, follow_symlinks=False)
                    os.unlink(name, dir_fd=src)
                    return
                except FileExistsError:
                    continue
            raise OSError('no unused archive name')

    def recover_archive(self, src, name, initial):
        """Complete an interrupted link/unlink for this exact job and saved result."""
        if not _owned_regular(initial, 2) or initial.st_size > MAX_JOB_BYTES:
            raise stage.Refused([stage.Refusal('R6', 'unsafe pending job', name)])
        for archive in ('done', 'failed'):
            try:
                with safety.directory(os.path.join(self.root, self.queue, archive)) as dst:
                    archived = os.stat(name, dir_fd=dst, follow_symlinks=False)
                    if not _same_archive_link(archived, initial):
                        continue
                    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=src)
                    with os.fdopen(fd, 'rb') as stream:
                        if not _same_archive_link(os.fstat(stream.fileno()), initial):
                            raise OSError('pending archive pair changed')
                        data = stream.read(MAX_JOB_BYTES + 1)
                        if len(data) != initial.st_size or not _same_archive_link(os.fstat(stream.fileno()), initial):
                            raise OSError('pending archive pair changed')
                    code = _saved_result(self, name[:-4], hashlib.sha256(data).hexdigest())
                    if code is None or (code == 0) != (archive == 'done'):
                        raise config.UsageError('archived job conflicts with existing result')
                    current = os.stat(name, dir_fd=src, follow_symlinks=False)
                    archived = os.stat(name, dir_fd=dst, follow_symlinks=False)
                    if not _same_archive_link(current, initial) or not _same_archive_link(archived, initial):
                        raise OSError('pending archive pair changed')
                    os.unlink(name, dir_fd=src)
                    return code
            except FileNotFoundError:
                continue
        raise stage.Refused([stage.Refusal('R6', 'pending job has unrelated hard links', name)])



def _set_aside(paths, name):
    """Keep an interrupted result as <name>.md.partial[.N] (never replacing one) and free the result name."""
    with safety.directory(os.path.join(paths.root, paths.results)) as fd:
        for number in range(10000):
            aside = name+'.md.partial'+('.%d' % number if number else '')
            try:
                os.link(name+'.md', aside, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
            except FileExistsError:
                continue
            os.unlink(name+'.md', dir_fd=fd)
            os.fsync(fd)
            return aside
    raise OSError('no unused partial result name')


def _saved_result(paths, name, digest):
    try:
        raw = safety.read(paths.root, paths.results+'/'+name+'.md', MAX_RESULT_BYTES)
    except config.UsageError:
        return None
    if _interrupted(raw.decode('utf-8', 'replace'), name, digest):
        _set_aside(paths, name)
        print('%s: interrupted result write; re-running' % _display_name(name), flush=True)
        return None
    text = raw.decode('utf-8')
    invalid = config.UsageError('reused job name conflicts with existing result; choose a new name')
    prefix = '# '+name+'\n\n'+RESULT_WARNING+'\n\n'
    if not text.startswith(prefix) or not text.endswith('\n'):
        raise invalid

    if text.startswith(prefix + RESULT_FORMAT + '\n'):
        text, separator, checksum = text.rpartition(RESULT_CHECKSUM)
        expected = hashlib.sha256(text.encode('utf-8')).hexdigest()
        if not separator or checksum != expected + ' -->\n':
            raise invalid

    header, attempt_marker, body = text.partition('\n### Attempt 1 (round 1)\n')
    fields = dict(line.split(': ', 1) for line in header.splitlines() if ': ' in line)
    code = fields.get('retained_exit_code', '')
    if (not attempt_marker or fields.get('job_sha256') != digest or
            not re.fullmatch(r'[0-9]{1,3}', code) or fields.get('round') not in ('1', '2') or
            not re.match(r'exit_code: [0-9]{1,3}\nbytes: [0-9]+\nseconds: [^\n]+\n'
                         r'files sent: [^\n]*\nVERIFIED: [0-9]+\nUNVERIFIED: [0-9]+\n', body)):
        raise invalid
    _, round_marker, answer = body.partition('\n## Round 1\nfiles added: \n\n')
    if not round_marker:
        raise invalid
    if fields['round'] == '2':
        second_round = re.search(r'\n## Round 2\nfiles added: [^\n]+\n\n', answer)
        if second_round is None:
            raise invalid
        has_answer = bool(answer[:second_round.start()].strip() or answer[second_round.end():].strip())
    else:
        has_answer = bool(answer.strip())
    if int(code) == 0 and not has_answer:
        raise invalid
    return int(code)

