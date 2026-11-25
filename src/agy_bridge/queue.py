"""Serial job queue: the delegate pipeline remains the only sender."""
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

from . import __version__, config, safety, stage, verdicts

MAX_JOB_BYTES = 128 * 1024
MAX_RESULT_BYTES = 8 * 1024 * 1024
MAX_NEED_LINE = 1024
MAX_NEED_CANDIDATES = 100
MAX_NEED_TEXT = 64 * 1024
RESULT_WARNING = 'unverified model output: verify claims against original sources before use.'
RESULT_FORMAT = '<!-- agy-bridge-result-v2 -->'
# Line 2 of a refusal result (an invalid job, a refused run or `queue cancel`). 'sent: no' means nothing of the job
# was sent to agy, so a reader such as xq can treat the job as undelivered. 'sent: maybe' marks the rare refusal
# after an agy attempt had started (for example the result could not be published).
REFUSAL_FORMAT = '<!-- agy-bridge-refusal-v1 sent: %s -->'
REFUSAL_HEADING = verdicts.REFUSAL_HEADING  # '# Queue refusal'
RESULT_CHECKSUM = '\n<!-- agy-bridge-result-sha256: '


def _owned_regular(info, links):
    return stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_nlink == links


def _same_archive_link(info, initial):
    return _owned_regular(info, 2) and (
        info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns
    ) == (initial.st_dev, initial.st_ino, initial.st_size, initial.st_mtime_ns)


# The folder holding the agy_bridge package this process runs (the plugin's src/). A detached watcher records it
# with its version, so a later invocation can tell that the watcher still runs code from before a plugin update.
CODE_ROOT = str(Path(__file__).resolve().parents[1])
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


JOB_FIELDS = ('name', 'model', 'task', 'files', 'timeout', 'cite', 'context', 'need')
MAX_SHOWN_FIELDS = 10


def validate(root, job):
    if not isinstance(job, dict):
        raise config.UsageError('job is not a JSON object')
    unknown = sorted(set(job) - set(JOB_FIELDS))
    if unknown:
        shown = ', '.join(stage.printable(field) for field in unknown[:MAX_SHOWN_FIELDS])
        if len(unknown) > MAX_SHOWN_FIELDS:
            shown += ' (+%d more)' % (len(unknown) - MAX_SHOWN_FIELDS)
        raise config.UsageError('unknown job field(s): %s; allowed: %s (see docs/QUEUE.md)'
                                % (shown, ', '.join(JOB_FIELDS)))
    verdicts.name(job.get('name'))
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


def refusal_text(message, sent=False):
    """A refusal result: the heading (kept on line 1 for older readers), the machine marker and the message."""
    return '%s\n%s\n\n%s\n' % (REFUSAL_HEADING, REFUSAL_FORMAT % ('maybe' if sent else 'no'), message)


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
                        job = verdicts.name(row['job'])
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
            verdicts.name(name)
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


def need_files(root, answer, current):
    from .grading import unannotate
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
    from .delegation import DelegationRequest, execute
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
            'name: ' + ('already used; queue add will refuse it' if exists else 'free')]
    request = DelegationRequest(
        repo=paths.root, task=job['task'], files=files, model=job.get('model'), cite=True,
        require_verified=job.get('cite', False), timeout=str(job.get('timeout', '5m')),
        allow_non_git_root=paths.allow, dry=True)
    return head, execute(request)


def attempt(root, job, files, task, allow=False):
    from .delegation import DelegationRequest, execute
    request = DelegationRequest(
        repo=root, task=task, files=files, model=job.get('model'), cite=True,
        require_verified=job.get('cite', False), timeout=str(job.get('timeout', '5m')),
        allow_non_git_root=allow,
    )
    return execute(request).as_queue_record()


def _interrupted(text, name, digest):
    """A result this exact job began to write but whose checksum footer is missing or wrong."""
    start = '# '+name+'\n\n'+RESULT_WARNING+'\n\n'+RESULT_FORMAT+'\njob_sha256: '+digest+'\n'
    if not text.startswith(start):
        return False
    body, separator, checksum = text.rpartition(RESULT_CHECKSUM)
    return not separator or checksum != hashlib.sha256(body.encode('utf-8')).hexdigest() + ' -->\n'


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


def run_job(paths, rel):
    name = os.path.basename(rel)
    job_name = verdicts.name(name[:-4])
    if rel != paths.queue+'/'+job_name+'.job':
        raise config.UsageError('job filename must match name.job')
    with paths.publication() as src:
        try:
            info = os.stat(name, dir_fd=src, follow_symlinks=False)
        except FileNotFoundError:
            return None  # cancelled (or archived) after it was listed; nothing was sent
        if info.st_nlink == 2 and stat.S_ISREG(info.st_mode):
            return paths.recover_archive(src, name, info)
        raw = safety.read(paths.root, rel, MAX_JOB_BYTES)
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
    task = job['task']
    paths.attempted = job_name  # from here on a refusal can no longer promise that nothing was sent
    rounds, attempts = [], []
    added = []
    for number in (1, 2):
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
            break
        rounds.append(dict(number=number, item=item, added=added))
        if number == 2 or code not in (0, 17) or not job.get('need', False):
            break
        added = need_files(paths.root, item['answer'], files)
        if not added:
            break
        files += added
        task += '\nSecond round: complete the original task using the added files; do not request another round.'
    success = [r for r in rounds if r['item']['exit_code']==0]
    retained = (success or rounds)[-1]['item']
    sent = {f['path']:f['bytes'] for a in attempts for f in a.get('files', [])}
    head = ['# '+job['name'], '', RESULT_WARNING, '', RESULT_FORMAT,
            'job_sha256: '+digest, 'model: '+(job.get('model') or 'default'), 'round: '+str(len(rounds)),
            'exit_code: '+str(code), 'retained_exit_code: '+str(retained['exit_code']),
            'bytes: '+str(sum(sent.values())), 'seconds: '+str(retained['seconds']),
            'quota exits: '+str(sum(a['exit_code']==10 for a in attempts)),
            'timeout exits: '+str(sum(a['exit_code']==12 for a in attempts))]
    if len(rounds)==2 and code != 0:
        head.append('status: round 2 failed; earlier answer retained when successful')
    for r in rounds:
        head += ['round %d bytes: %s' % (r['number'],r['item'].get('total_bytes',0)),
                 'round %d seconds: %s' % (r['number'],r['item']['seconds'])]
    for n,a in enumerate(attempts,1):
        cite = a.get('cite', {})
        head += ['', '### Attempt %d (round %d)' % (n,a['round']),
                 'exit_code: '+str(a['exit_code']), 'bytes: '+str(a.get('total_bytes',0)),
                 'seconds: '+str(a['seconds']), 'files sent: '+', '.join(stage.printable(f['path']) for f in a.get('files',[])),
                 'VERIFIED: '+str(cite.get('verified',0)), 'UNVERIFIED: '+str(cite.get('unverified',0))]
        head += ['FILES NOT SENT: %s (%s)' % (stage.printable(r['path']), stage.printable(r['rule']))
                 for r in a.get('not_sent',[])]
        if a.get('diagnostics'):
            head += ['Bridge diagnostics:', a['diagnostics']]
    for r in rounds:
        head += ['', '## Round '+str(r['number']), 'files added: '+', '.join(stage.printable(f) for f in r['added']), '',
                 r['item'].get('rendered_cite') or r['item']['answer']]
    text = '\n'.join(head)+'\n'
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
            if watch and not os.path.isdir(CODE_ROOT):
                print('watcher: agy-bridge code root %s no longer exists (plugin updated or removed); stopping'
                      % stage.printable(CODE_ROOT), flush=True)
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


MAX_CANCEL_REASON = 200


class CancelRefused(config.UsageError):
    """queue cancel could not withdraw the job; the message names why and what to do."""


class CancelUnknown(CancelRefused):
    """queue cancel found no such job: a usage error (exit 1), as for verdict --job."""


def cancel(root, name, reason=None, queue_dir=None, results_dir=None, allow=False):
    """Withdraw a pending job before any runner sends it: archive it to failed/ and publish a refusal result.

    Holds the queue publication lock, which a runner also needs to read a job before sending it, so a job either
    is withdrawn first or is already running (refused). Returns the archive action text.
    """
    from .scan import scan_bytes

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


MAX_REASON_CHARS = 600


def invalid_reason(exc):
    """The specific, sanitized reason a queued job was refused: escaped, bounded and never key-shaped.

    JSON decoder messages can quote job bytes, so they become 'invalid JSON'.
    """
    from .scan import scan_bytes

    if isinstance(exc, json.JSONDecodeError):
        return 'invalid JSON'
    if isinstance(exc, RecursionError):
        return 'job JSON is nested too deeply'
    if isinstance(exc, config.UsageError) and 'conflict' in str(exc):
        return 'reused name conflict'
    prefix = 'queue run refused: '
    lines = [line.strip() for line in stage.explain(exc, 'queue run')]
    if isinstance(exc, stage.Refused):
        lines = lines[1:]  # drop the "refused by a rule (N problem(s)):" heading; keep each rule line
    elif lines[0].startswith(prefix):
        lines[0] = lines[0][len(prefix):]
    # stage.explain already escaped every name and detail with stage.printable().
    reason = '; '.join(line[2:] if line.startswith('- ') else line for line in lines if line)
    if len(reason) > MAX_REASON_CHARS:
        reason = reason[:MAX_REASON_CHARS] + '...'
    # explain() replaces an empty or key-shaped detail with the exception type name.
    if not reason or reason == type(exc).__name__ or scan_bytes(reason.encode('utf-8', 'replace')):
        return 'invalid input (detail redacted)'
    return reason


def _display_name(value):
    from .scan import scan_bytes
    return '[redacted key-shaped filename]' if scan_bytes(value.encode('utf-8', 'replace')) else stage.printable(value)
