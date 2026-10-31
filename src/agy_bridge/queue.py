"""Serial job queue: the delegate pipeline remains the only sender."""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import io
import hashlib
import json
import os
import re
import time

from . import config, safety, stage, verdicts

MAX_JOB_BYTES = 128 * 1024
MAX_NEED_LINE = 1024
MAX_NEED_CANDIDATES = 100
MAX_NEED_TEXT = 64 * 1024


def validate(root, job):
    if not isinstance(job, dict) or set(job) - {'name', 'model', 'task', 'files', 'timeout', 'cite', 'context', 'need'}:
        raise config.UsageError('job has unknown fields or is not an object')
    verdicts.name(job.get('name'))
    config.validate_model(job.get('model'))
    task = job.get('task')
    if not isinstance(task, str) or not task.strip() or len(task) > config.MAX_TASK_CHARS:
        raise config.UsageError('job needs a bounded nonempty task')
    files = job.get('files')
    if not isinstance(files, list) or not files or len(files) > config.MAX_FILES:
        raise config.UsageError('job needs 1-500 file paths')
    for path in files:
        rel = safety.local(root, path)
        if rel != path:
            raise config.UsageError('job file paths must be repository relative')
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


class Paths:
    def __init__(self, root, queue_dir=None, results_dir=None, allow=False):
        self.root = root
        self.allow = allow
        stage.check_root(root, allow)
        self.queue = safety.local(root, queue_dir or '.agy-bridge/queue', metadata=True)
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
                yield
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
            safety.write(self.root, rel, json.dumps(job, indent=2)+'\n')
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
                os.ftruncate(fd, 0)
                self.lock_fd = None
            os.close(fd)

    def state(self, job=None):
        if self.lock_fd is None:
            return
        row = dict(pid=os.getpid(), mode=self.mode, job=job, started=time.time() if job else None)
        os.lseek(self.lock_fd, 0, os.SEEK_SET)
        os.ftruncate(self.lock_fd, 0)
        os.write(self.lock_fd, json.dumps(row).encode())
        os.fsync(self.lock_fd)

    def running(self):
        try:
            with safety.directory(os.path.join(self.root, self.queue)) as parent:
                fd = os.open('watcher.pid', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        except FileNotFoundError:
            return None
        try:
            safety.regular(fd)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raw = os.read(fd, 8192)
                try:
                    row = json.loads(raw) if raw else {}
                    if (not isinstance(row, dict) or type(row.get('pid')) is not int or row['pid'] < 1 or
                            row.get('mode') not in ('watch', 'run')):
                        return dict(pid='unknown', mode='unknown')
                    if row.get('job') is not None:
                        verdicts.name(row['job'])
                        if not isinstance(row.get('started'), (int, float)):
                            return dict(pid=row['pid'], mode=row['mode'])
                    return row
                except (ValueError, config.UsageError):
                    return dict(pid='unknown', mode='unknown')
            return None
        finally:
            os.close(fd)

    def exists(self, rel):
        try:
            with safety.directory(os.path.dirname(os.path.join(self.root, rel))) as fd:
                os.stat(os.path.basename(rel), dir_fd=fd, follow_symlinks=False)
            return True
        except FileNotFoundError:
            return False

    def failure(self, rel, reason):
        """Keep poison objects untouched; record their refusal and continue."""
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
        message = 'invalid job: '+reason+'; '+action
        for number in range(10000):
            result = self.results+'/'+name+('.%d' % number if number else '')+'.md'
            try:
                safety.write(self.root, result, '# Queue refusal\n\n'+message+'\n')
                return action
            except FileExistsError:
                continue
        raise OSError('no unused refusal result name')

    def move(self, rel, ok):
        dest = self.queue+('/done' if ok else '/failed')
        with safety.directory(os.path.join(self.root, self.queue)) as src:
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


def attempt(root, job, files, task, allow=False):
    from .cli import cmd_delegate
    args = argparse.Namespace(repo=root, file=files, glob=[], model=job.get('model'),
        cite=True, require_verified=job.get('cite', False), allow_fragment_file=None,
        timeout=str(job.get('timeout', '5m')), allow_non_git_root=allow,
        max_bytes=config.MAX_TOTAL_BYTES, max_file_bytes=config.MAX_FILE_BYTES,
        max_out_bytes=config.MAX_OUT_BYTES, out=None, task=[task])
    captured = {}
    # Delegate's ordinary rendering stays unchanged; retain its full in-memory answer.
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as errors:
        cmd_delegate(args, dry=False, capture=captured)
    captured['diagnostics'] = errors.getvalue()
    return captured


def run_job(paths, rel):
    with paths.publication():
        raw = safety.read(paths.root, rel, MAX_JOB_BYTES)
    job = validate(paths.root, json.loads(raw))
    if os.path.basename(rel) != job['name']+'.job':
        raise config.UsageError('job filename must match name.job')
    result_rel = paths.results+'/'+job['name']+'.md'
    digest = hashlib.sha256(raw).hexdigest()
    for archive in ('done', 'failed'):
        if paths.exists(paths.queue+'/'+archive+'/'+job['name']+'.job'):
            raise config.UsageError('reused job name conflicts with archived job')
    # Reconcile only the exact bytes saved before an interrupted archive move.
    try:
        existing = safety.read(paths.root, result_rel, 8*1024*1024).decode('utf-8')
    except config.UsageError:
        existing = None
    if existing is not None:
        header = dict(line.split(': ',1) for line in existing.split('### Attempt',1)[0].splitlines() if ': ' in line)
        code = header.get('retained_exit_code', '')
        if header.get('job_sha256') != digest or not code.isascii() or not code.isdigit() or len(code)>3:
            raise config.UsageError('reused job name conflicts with existing result; choose a new name')
        code = int(code)
        paths.move(rel, code == 0)
        return code
    files = list(dict.fromkeys(context_files(paths.root, job)+job['files']))
    task = job['task']
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
    head = ['# '+job['name'], '', 'unverified model output: verify claims against original sources before use.', '',
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
                 'seconds: '+str(a['seconds']), 'files sent: '+', '.join(f['path'] for f in a.get('files',[])),
                 'VERIFIED: '+str(cite.get('verified',0)), 'UNVERIFIED: '+str(cite.get('unverified',0))]
        head += ['FILES NOT SENT: %s (%s)' % (r['path'],r['rule']) for r in a.get('not_sent',[])]
        if a.get('diagnostics'):
            head += ['Bridge diagnostics:', a['diagnostics']]
    for r in rounds:
        head += ['', '## Round '+str(r['number']), 'files added: '+', '.join(r['added']), '',
                 r['item'].get('rendered_cite') or r['item']['answer']]
    safety.write(paths.root, result_rel, '\n'.join(head)+'\n')
    paths.move(rel, bool(success))
    return 0 if success else retained['exit_code']


def status(paths, threshold=300):
    active = paths.running()
    out = ['watcher: '+('running (pid %s, %s)' % (active.get('pid'),active.get('mode')) if active else 'stopped')]
    for label,rel in [('pending',paths.queue),('done',paths.queue+'/done'),('failed',paths.queue+'/failed')]:
        out.append('%s: %d' % (label,len(paths.names(rel,'.job'))))
    if active and active.get('job'):
        duration = max(0,time.time()-active.get('started',time.time()))
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
    out += ['last result: '+(latest[1] if latest else 'none'),
            'results without verdict: '+(', '.join(n[:-3] for n in results if n[:-3] not in checked) or 'none')]
    return '\n'.join(out)


def run(paths, watch=False, poll=5):
    code=0
    skipped = set()
    with paths.lock('watch' if watch else 'run'):
        while True:
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
                try:
                    rc=run_job(paths,paths.queue+'/'+name)
                    print('%s: exit %d' % (_display_name(name),rc),flush=True)
                    if rc: code=rc
                except (ValueError, config.UsageError, stage.Refused, OSError, RecursionError) as exc:
                    # Do not echo arbitrary exception strings or job content.
                    reason = 'reused name conflict' if isinstance(exc, config.UsageError) and 'conflict' in str(exc) else 'invalid input or unsafe filesystem entry'
                    try:
                        action = paths.failure(rel, reason)
                    except (OSError, stage.Refused, config.UsageError):
                        action = 'skipped; refusal result could not be written'
                    skipped.add(identity)
                    print('%s: invalid job; %s' % (_display_name(name), action),flush=True)
                    code=config.EXIT_REFUSED if isinstance(exc,(stage.Refused,OSError)) else config.EXIT_USAGE
                finally:
                    paths.state()
            if not watch:
                return code
            time.sleep(poll)


def _display_name(value):
    from .scan import scan_bytes
    return '[redacted key-shaped filename]' if scan_bytes(value.encode('utf-8', 'replace')) else stage.printable(value)
