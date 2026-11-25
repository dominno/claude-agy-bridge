"""Delegation and built-in workflows. Existing exit codes are listed in config.py."""
from __future__ import annotations

import argparse
import json
import math
import os
import signal
import stat
import subprocess
import sys
import tempfile
from typing import List, Optional

from . import __version__, config, delegation, events, report, runner, stage
from .config import UsageError


class _Parser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("allow_abbrev", False)
        super().__init__(*args, **kwargs)

    def error(self, message):  # argparse exits 2 by default; 2 means "agy failed" here
        self.print_usage(sys.stderr)
        sys.stderr.write("agy-bridge: usage error: %s\n" % message)
        raise SystemExit(config.EXIT_USAGE)


def _parser() -> argparse.ArgumentParser:
    p = _Parser(prog="agy-bridge", description="Delegate read-only tasks or isolated implementation jobs to AGY.")
    p.add_argument("--version", action="version", version="agy-bridge " + __version__)
    sub = p.add_subparsers(dest="command", required=True, parser_class=_Parser)
    delegate_help = {"delegate": "send selected files and a task to agy and print its read-only answer",
                     "dry-run": "show the files and agy command a delegation would use; send nothing"}
    for name in ("delegate", "dry-run"):
        d = sub.add_parser(name, help=delegate_help[name])
        d.set_defaults(handler=cmd_delegate)
        d.add_argument("-C", "--repo", default=".", help="repository root (default: current folder)")
        d.add_argument("-f", "--file", action="append", default=[], help="file to include (repeatable)")
        d.add_argument("--glob", action="append", default=[], help="glob inside the repo (repeatable)")
        d.add_argument("--model")
        d.add_argument("--cite", action="store_true", help="check source quotations in every answer line")
        d.add_argument("--require-verified", action="store_true", help="with --cite, fail for any unverified line")
        d.add_argument("--allow-fragment-file", help="repo-local list of supported safe example literals")
        d.add_argument("--timeout", default="%ds" % config.DEFAULT_TIMEOUT_S)
        d.add_argument("--allow-non-git-root", action="store_true",
                       help="accept a --repo without a .git entry (never for /, HOME or above, "
                            "or a folder on the denylist)")
        d.add_argument("--max-bytes", type=int, default=config.MAX_TOTAL_BYTES)
        d.add_argument("--max-file-bytes", type=int, default=config.MAX_FILE_BYTES)
        d.add_argument("--max-out-bytes", type=int, default=config.MAX_OUT_BYTES)
        d.add_argument("--out", help="write the full answer here (temp folder or <repo>/.agy-bridge/)")
        if name == "delegate":
            d.add_argument("--dry-run", action="store_true", help="show what would be sent and stop")
        d.add_argument("task", nargs="*", help='put the task after "--"')
    ev = sub.add_parser("events", help="show recent metadata records from <repo>/.agy-bridge/events.jsonl")
    ev.set_defaults(handler=cmd_events)
    ev.add_argument("-C", "--repo", default=".")
    ev.add_argument("--tail", type=int, default=10)
    ev.add_argument("--allow-non-git-root", action="store_true")
    doc = sub.add_parser("doctor", help="read-only health check of agy and the bridge setup")
    doc.set_defaults(handler=cmd_doctor)
    doc.add_argument("-C", "--repo", help="repository folder; agy must resolve outside it")
    doc.add_argument("--offline", action="store_true", help="skip the check that needs network and sign-in")
    doc.add_argument("--implementation", action="store_true", help="probe AGY inside the implementation sandbox")
    doc.add_argument("--profile", help="dedicated AGY home for the implementation probe")
    def repo_options(parser, folders=False):
        parser.add_argument('-C', '--repo', default='.')
        parser.add_argument('--allow-non-git-root', action='store_true')
        if folders:
            parser.add_argument('--queue-dir')
            parser.add_argument('--results-dir')
    q = sub.add_parser('queue', help='serial read-only job queue (add, run, watch, status, cancel)')
    qs = q.add_subparsers(dest='queue_command', required=True, parser_class=_Parser)
    queue_help = {'run': 'run every pending job once, then stop', 'watch': 'keep running jobs as they arrive',
                  'status': 'show queue counts, stale work and results awaiting a verdict',
                  'add': 'validate a JSON job and add it to the queue',
                  'cancel': 'withdraw a pending job before it is sent (archived as failed with a result)'}
    for name in ('run', 'watch', 'status', 'add', 'cancel'):
        description = queue_help[name]
        if name == 'add':
            from .queue import JOB_FIELDS
            description += ('; job fields: %s (name, task and files are required; see docs/QUEUE.md)'
                            % ', '.join(JOB_FIELDS))
        cmd = qs.add_parser(name, help=queue_help[name], description=description)
        cmd.set_defaults(handler=cmd_workflow, workflow=cmd_queue)
        repo_options(cmd, folders=True)
        if name == 'add':
            cmd.add_argument('job_file', help='JSON job file, or - for stdin')
            cmd.add_argument('--dry-run', action='store_true',
                             help='validate the job and show the exact files a run would send '
                                  '(context files included); add nothing, send nothing')
        if name == 'cancel':
            cmd.add_argument('name', help='pending job name')
            cmd.add_argument('--reason', help='short printable reason recorded in the result (default: cancelled)')
        if name == 'watch':
            cmd.add_argument('--poll', type=float, default=5)
            cmd.add_argument('--detach', action='store_true',
                             help='start the watcher in a new session that survives this terminal, log to '
                                  '<queue>/watcher.log, print its PID and return; stop it with kill PID')
        if name == 'status':
            cmd.add_argument('--threshold', type=float, default=300)
    st = sub.add_parser('status', help='same as queue status')
    st.set_defaults(handler=cmd_workflow, workflow=cmd_queue, queue_command='status')
    repo_options(st, folders=True)
    st.add_argument('--threshold', type=float, default=300)
    v = sub.add_parser('verdict', help="record the reader's verdict on a finished job",
                       description="record the reader's verdict on a finished job; a pass needs a result: "
                                   "<results>/<job>.md, <queue>/done/<job>.job or .agy-bridge/delegate/<job>.md "
                                   "(--queue-dir/--results-dir, default <repo>/.agy-bridge/queue and results) "
                                   "unless --force; a pass on a pending or failed-only job is refused; "
                                   "fixes/reject on an unknown or pending job "
                                   "(e.g. a failed delegate with no result) warn and record")
    v.set_defaults(handler=cmd_workflow, workflow=cmd_verdict)
    repo_options(v, folders=True)
    v.add_argument('--job', required=True)
    v.add_argument('--verdict', choices=('pass','fixes','reject'), required=True)
    v.add_argument('--by', required=True)
    v.add_argument('--evidence', required=True)
    v.add_argument('--note', nargs='?', const='')
    v.add_argument('--force', action='store_true', help='record a pass even when no result for --job exists '
                                                     '(fixes/reject on an unknown job only warn)')
    vs = sub.add_parser('verdicts', help='show recent recorded verdicts')
    vs.set_defaults(handler=cmd_workflow, workflow=cmd_verdicts)
    repo_options(vs)
    vs.add_argument('--tail', type=int, default=10)
    g = sub.add_parser('grade-mutants', help='run a trusted case against model-written mutants in an OS sandbox')
    g.set_defaults(handler=cmd_workflow, workflow=cmd_grading)
    repo_options(g)
    g.add_argument('result')
    g.add_argument('--case', required=True)
    g.add_argument('--record', action='store_true')
    g.add_argument('--by')
    g.add_argument('--unsafe-no-sandbox', action='store_true', help='run model-written code WITHOUT a sandbox, with your privileges and network access')
    d = sub.add_parser('apply-diff', help='apply a model-written diff to a scratch copy and run a trusted case in an OS sandbox')
    d.set_defaults(handler=cmd_workflow, workflow=cmd_grading)
    repo_options(d)
    d.add_argument('input', help='result.md, file.diff, or table')
    d.add_argument('records', nargs='*')
    d.add_argument('--case')
    d.add_argument('--unsafe-no-sandbox', action='store_true', help='run model-written code WITHOUT a sandbox, with your privileges and network access')
    t = sub.add_parser('stats', help='per-model timing statistics from events and results')
    t.set_defaults(handler=cmd_workflow, workflow=cmd_stats)
    repo_options(t)
    t.add_argument('--results-dir')
    t.add_argument('--target', type=float, default=180)
    from . import implement, jobs
    implement.add_arguments(sub)
    implement.add_job_actions(jobs.add_arguments(sub))
    return p


def _say(text: str) -> None:
    sys.stdout.write(text + ("" if text.endswith("\n") else "\n"))


def _fail(text: str) -> None:
    sys.stderr.write("agy-bridge: " + text + "\n")


def _refused(exc: BaseException, operation: str, out: Optional[str] = None) -> None:
    """Name the rule, or the failing operation and path, instead of a generic refusal."""
    if isinstance(exc, report.OutPathRefused):
        _fail("--out %s refused: %s (allowed: the temp folder or <repo>/%s/)"
              % (stage.printable(out) if out else "value", stage.printable(str(exc)), report.SCRATCH_DIR))
        return
    lines = stage.explain(exc, operation)
    _fail(lines[0])
    for extra in lines[1:]:
        sys.stderr.write(extra + "\n")


def cmd_delegate(args) -> int:
    request = delegation.DelegationRequest(
        repo=args.repo, task=" ".join(args.task), files=args.file, globs=args.glob,
        model=args.model, cite=args.cite, require_verified=args.require_verified,
        allow_fragment_file=args.allow_fragment_file, timeout=args.timeout,
        allow_non_git_root=args.allow_non_git_root, max_bytes=args.max_bytes,
        max_file_bytes=args.max_file_bytes, max_out_bytes=args.max_out_bytes, out=args.out,
        dry=args.command == "dry-run" or getattr(args, "dry_run", False), save_large_answer=True)
    result = delegation.execute(request)
    for line in result.preview:
        _say(line)
    sys.stderr.writelines(result.diagnostics)
    if result.answer or result.not_sent or result.checked is not None:
        info = result.metadata
        _say(report.render(result.answer, info["model"], len(info["files"]), info["total_bytes"],
                           info["seconds"], info["truncated"], result.written_path, result.not_sent, result.checked))
    return result.exit_code


def cmd_events(args):
    try:
        if not 1 <= args.tail <= 10000:
            raise UsageError("--tail must be between 1 and 10000")
        root = stage.resolve_repo(args.repo)
        stage.check_root(root, args.allow_non_git_root)
        for line in events.tail(root, args.tail):
            _say(line)
        return config.EXIT_OK
    except UsageError as exc:
        _fail(str(exc))
        return config.EXIT_USAGE
    except (stage.Refused, events.EventError, OSError) as exc:
        _refused(exc, "events")
        return config.EXIT_REFUSED


def cmd_doctor(args) -> int:
    if args.implementation:
        return _doctor_implementation(args)
    if args.profile:
        _fail('--profile requires --implementation')
        return config.EXIT_USAGE
    failures: List[int] = []

    def line(ok: bool, name: str, why: str, code: int = config.EXIT_AGY_FAILED) -> None:
        _say("%-4s %s: %s" % ("ok" if ok else "fail", name, why))
        if not ok:
            failures.append(code)

    line(sys.version_info >= (3, 9), "python", "%d.%d" % sys.version_info[:2], config.EXIT_UNSUPPORTED)
    try:
        with tempfile.TemporaryDirectory(prefix="agy-bridge-doctor-") as tmp:
            line(True, "temp dir", "writable")
            env = config.build_agy_env(args.repo)
            try:
                exe = runner.find_agy(stage.canonical_path(args.repo) if args.repo else None)
                line(True, "agy on PATH", exe)
                version = runner.detect_version(exe, env)
                try:
                    runner.build_argv(version, exe, None)
                    line(True, "agy version", "%d.%d.%d is a known major (still unverified)" % version)
                except runner.UnsupportedAgy as exc:
                    line(False, "agy version", str(exc), config.EXIT_UNSUPPORTED)
                if args.offline:
                    _say("skip agy models: --offline")
                else:
                    _check_models(exe, env, tmp, line)
            except runner.AgyMissing as exc:
                line(False, "agy on PATH", str(exc), config.EXIT_AGY_MISSING)
            except runner.UnsupportedAgy as exc:
                line(False, "agy version", str(exc), config.EXIT_UNSUPPORTED)
    except OSError:
        line(False, "temp dir", "not writable")
    log = os.path.join(config.bridge_home(), "log.jsonl")
    if os.path.lexists(log):
        mode = stat.S_IMODE(os.lstat(log).st_mode)
        line(mode == 0o600, "log file mode", oct(mode))
    else:
        line(True, "log file mode", "no log yet")
    _say("info: ~/.gemini %s" % ("exists" if os.path.isdir(os.path.expanduser("~/.gemini")) else "not found"))
    if args.repo:
        _doctor_watcher(args.repo)
    if not failures:
        return config.EXIT_OK
    return next((c for c in failures if c in (config.EXIT_AGY_MISSING, config.EXIT_UNSUPPORTED)), failures[0])


def _doctor_watcher(repo) -> None:
    """Warn (not fail) when the repository's default queue runner runs another agy-bridge version or code root."""
    from . import queue

    try:
        location = queue.QueueLocation(stage.resolve_repo(repo), allow=True)
        active = location.running()
    except (UsageError, stage.Refused, OSError):
        return
    stale = queue.watcher_warning(active)
    if stale:
        _say("warn queue watcher: " + stale)


def _doctor_implementation(args) -> int:
    from . import edit_sandbox, implement

    if not args.profile:
        _fail('--implementation requires --profile')
        return config.EXIT_USAGE
    try:
        repo = stage.resolve_repo(args.repo or '.')
        profile = implement._profile(args.profile, repo)
        exe = runner.find_agy(repo)
        with tempfile.TemporaryDirectory(prefix='agy-bridge-edit-doctor-') as tree:
            version = runner.parse_version(edit_sandbox.preflight(tree=tree, profile=profile, exe=exe))
        runner.require_edit_version(version)
    except runner.AgyMissing as exc:
        _fail(str(exc))
        return config.EXIT_AGY_MISSING
    except runner.UnsupportedAgy as exc:
        _fail(str(exc))
        return config.EXIT_UNSUPPORTED
    except edit_sandbox.Unavailable as exc:
        _fail(str(exc))
        return config.EXIT_REFUSED
    except UsageError as exc:
        _fail('implementation profile or repository could not be validated: ' + stage.printable(str(exc)))
        return config.EXIT_REFUSED
    except (stage.Refused, OSError) as exc:
        _refused(exc, 'doctor --implementation')
        return config.EXIT_REFUSED
    _say('ok   implementation sandbox: agy %d.%d.%d' % version)
    return config.EXIT_OK


def _check_models(exe, env, tmp, line) -> None:
    try:
        proc = runner.run_probe([exe, "models"], env, tmp, timeout=30)  # same five-name env
        names = [ln for ln in proc.stdout.decode("utf-8", "replace").splitlines() if ln.strip()]
        line(proc.returncode == 0 and bool(names), "agy models", "%d line(s) listed" % len(names))
    except (OSError, subprocess.TimeoutExpired, runner.UnsupportedAgy) as exc:
        line(False, "agy models", "could not run: %s" % exc)


STOP_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)


def install_stop_handlers() -> None:
    """The first stop signal unwinds through the cleanup code; later ones are ignored.

    Only an inherited SIGHUP ignore is kept (the nohup convention), so a watcher started with nohup or
    `queue watch --detach` survives the end of the terminal session. SIGINT and SIGTERM always get the cleanup
    handler: a non-interactive shell starts background jobs with SIGINT ignored, and that must not disable cleanup.
    """
    def on_stop(signum, _frame):
        for sig in STOP_SIGNALS:
            signal.signal(sig, signal.SIG_IGN)
        raise KeyboardInterrupt

    for sig in STOP_SIGNALS:
        if sig == signal.SIGHUP and signal.getsignal(sig) is signal.SIG_IGN:
            continue
        signal.signal(sig, on_stop)


def main(argv: Optional[List[str]] = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if arguments and arguments[0] == '_implement-worker':
        from . import implement
        parser = _Parser(prog='agy-bridge _implement-worker', add_help=False)
        parser.add_argument('job')
        parser.add_argument('lock_fd', type=int)
        parser.set_defaults(command='_implement-worker', handler=implement.dispatch)
        args = parser.parse_args(arguments[1:])
    else:
        args = _parser().parse_args(arguments)
    install_stop_handlers()
    try:
        return args.handler(args)
    except KeyboardInterrupt:
        _fail("interrupted; the staging folder was removed")
        return 130


def cmd_queue(args, root):
    from . import grading, queue

    if args.queue_command == 'cancel':
        try:
            action = queue.cancel(root, args.name, args.reason, args.queue_dir, args.results_dir,
                                  args.allow_non_git_root)
        except queue.CancelRefused as exc:
            _fail('queue cancel: ' + report.clean_answer(str(exc).encode('utf-8', 'replace')))
            return config.EXIT_USAGE if isinstance(exc, queue.CancelUnknown) else config.EXIT_REFUSED
        _say('cancelled %s; %s; nothing was sent' % (args.name, action))
        return config.EXIT_OK
    paths = queue.Paths(root, args.queue_dir, args.results_dir, args.allow_non_git_root)
    if args.queue_command == 'status':
        if not math.isfinite(args.threshold) or args.threshold < 0:
            raise UsageError('threshold must be finite and nonnegative')
        _say(report.clean_answer(queue.status(paths, args.threshold).encode('utf-8')))
        return config.EXIT_OK
    if args.queue_command == 'add':
        if args.job_file != '-' and not os.path.lexists(args.job_file):
            raise UsageError('job file not found: ' + stage.printable(args.job_file))
        raw = sys.stdin.read(queue.MAX_JOB_BYTES + 1) if args.job_file == '-' else grading.read_input(args.job_file)
        if len(raw.encode('utf-8')) > queue.MAX_JOB_BYTES:
            raise UsageError('job is over 128 KiB')
        job = json.loads(raw)
        if args.dry_run:
            head, result = queue.preview(paths, job)
            for line in head + list(result.preview):
                _say(line)
            sys.stderr.writelines(result.diagnostics)
            if result.not_sent:
                info = result.metadata
                _say(report.render('', info['model'], len(info['files']), info['total_bytes'], info['seconds'],
                                   info['truncated'], None, result.not_sent, None))
            return result.exit_code
        _say(paths.add(job))
        try:
            runner = paths.running()
        except (OSError, stage.Refused):
            runner = None
        if runner is not None:
            # An add is a send when a watcher drains the queue: tell the caller it is not just a queued job.
            if runner.get('mode') == 'run':
                _say('NOTE runner pid %s (queue run) is running; it may send this job' % runner.get('pid'))
            else:
                _say('NOTE watcher pid %s is running; this job will be sent now' % runner.get('pid'))
        stale = queue.watcher_warning(runner)
        if stale:
            _fail(stale)
        return config.EXIT_OK
    poll = getattr(args, 'poll', 5)
    if not math.isfinite(poll) or not .05 <= poll <= 3600:
        raise UsageError('poll must be between .05 and 3600 seconds')
    if getattr(args, 'detach', False):
        return _detach_watcher(paths, poll)
    return queue.run(paths, args.queue_command == 'watch', poll)


WATCHER_LOG = 'watcher.log'
DETACH_WAIT_S = 3.0


def _detach_watcher(paths, poll):
    """Start `queue watch` in a new session with its output appended to <queue>/watcher.log.

    Refuses (R6) when a runner already holds the queue lock. Succeeds only once the new watcher holds the lock.
    """
    import time
    from pathlib import Path
    from . import safety

    if paths.running() is not None:
        raise stage.Refused([stage.Refusal('R6', 'queue already has an active runner; see status for its PID',
                                           'watcher.pid')])
    with safety.directory(os.path.join(paths.root, paths.queue), create=True) as parent:
        log = os.open(WATCHER_LOG, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600,
                      dir_fd=parent)
    log_path = os.path.join(paths.root, paths.queue, WATCHER_LOG)
    try:
        safety.regular(log)
        package_root = Path(__file__).resolve().parents[1]
        bootstrap = ("import runpy,sys; sys.path.insert(0,sys.argv.pop(1)); "
                     "runpy.run_module('agy_bridge', run_name='__main__')")
        argv = [sys.executable, '-I', '-c', bootstrap, str(package_root), 'queue', 'watch', '--repo=' + paths.root,
                '--queue-dir=' + paths.queue, '--results-dir=' + paths.results, '--poll=' + repr(float(poll))]
        if paths.allow:
            argv.append('--allow-non-git-root')
        # An ignored SIGHUP survives exec, so the watcher keeps it ignored (nohup convention).
        previous = signal.signal(signal.SIGHUP, signal.SIG_IGN)
        try:
            proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=log, cwd=paths.root,
                                    start_new_session=True)
        finally:
            signal.signal(signal.SIGHUP, previous)
    finally:
        os.close(log)
    deadline = time.monotonic() + DETACH_WAIT_S
    while time.monotonic() < deadline:
        active = paths.running()
        if active is not None and active.get('pid') == proc.pid:
            _say('watcher started pid %d; log %s; stop with: kill %d'
                 % (proc.pid, stage.printable(log_path), proc.pid))
            return config.EXIT_OK
        if proc.poll() is not None:
            break
        time.sleep(.05)
    code = proc.poll()
    if code is None:
        active = paths.running()
        if active is not None and active.get('pid') == proc.pid:
            _say('watcher started pid %d; log %s; stop with: kill %d'
                 % (proc.pid, stage.printable(log_path), proc.pid))
            return config.EXIT_OK
        # A start reported as failed must not keep running and drain the queue unattended.
        _stop_detached(proc)
        reason = 'did not take the queue lock within %gs and was stopped' % DETACH_WAIT_S
    else:
        reason = 'exited with code %d' % code
    _fail('watcher pid %d %s; read the end of %s and check: agy-bridge status -C %s'
          % (proc.pid, reason, stage.printable(log_path), stage.shell_arg(paths.root)))
    return config.EXIT_AGY_FAILED


def _stop_detached(proc, grace=2.0):
    """SIGTERM the detached watcher's session (its own process group), then SIGKILL if it lingers."""
    for sig in (signal.SIGTERM, getattr(signal, 'SIGKILL', signal.SIGTERM)):
        try:
            os.killpg(proc.pid, sig)
        except (OSError, AttributeError):
            try:
                proc.send_signal(sig)
            except OSError:
                pass
        try:
            proc.wait(timeout=grace)
            return
        except subprocess.TimeoutExpired:
            continue


def cmd_verdict(args, root):
    from . import verdicts

    try:
        warning = verdicts.record(root, args.job, args.verdict, args.by, args.evidence, args.note, force=args.force,
                                  allow=args.allow_non_git_root, queue_dir=args.queue_dir,
                                  results_dir=args.results_dir)
    except verdicts.UnknownJob as exc:
        _fail('verdict: ' + report.clean_answer(str(exc).encode('utf-8', 'replace')))
        return config.EXIT_USAGE
    if warning:
        _fail('warning: ' + report.clean_answer(warning.encode('utf-8', 'replace')))
    _say('verdict recorded for ' + args.job)
    return config.EXIT_OK


def cmd_verdicts(args, root):
    from . import verdicts

    if not 1 <= args.tail <= 10000:
        raise UsageError('--tail must be between 1 and 10000')
    for row in verdicts.rows(root, args.tail):
        _say(json.dumps(row, ensure_ascii=True, sort_keys=True))
    return config.EXIT_OK


def cmd_stats(args, root):
    from . import safety, stats

    if not math.isfinite(args.target) or args.target <= 0:
        raise UsageError('--target must be finite and positive')
    rel = safety.local(root, args.results_dir or '.agy-bridge/results', metadata=True)
    _say(report.clean_answer(stats.render(root, os.path.join(root, rel), args.target).encode('utf-8')))
    return config.EXIT_OK


def cmd_grading(args, root):
    from . import grading, verdicts

    if args.command == 'apply-diff' and args.input == 'table':
        if not args.records:
            raise UsageError('table needs at least one JSON/JSONL record file')
        rows = []
        for path in args.records:
            rows.extend(json.loads(line) for line in grading.read_input(path).splitlines() if line.strip())
        _say(report.clean_answer(grading.diff_table(rows).encode('utf-8')))
        return config.EXIT_OK
    if not args.case:
        raise UsageError('--case is required')
    case = json.loads(grading.read_input(args.case))
    grading.validate_case(root, case)
    if args.unsafe_no_sandbox:
        _fail('WARNING: --unsafe-no-sandbox runs model-written code with your privileges and network access')
    if args.command == 'grade-mutants':
        if args.record and (not args.by or not case.get('job')):
            raise UsageError('--record requires --by and case.job')
        mutants = grading.extract_mutants(grading.read_input(args.result))
        rows, table, records = grading.grade(root, case, mutants, unsafe_no_sandbox=args.unsafe_no_sandbox)
        _say(report.clean_answer(table.read_bytes()))
        _say('Table: %s\nJSONL: %s' % (table, records))
        if args.record:
            verdict = 'pass' if any(r['status'] == 'SURVIVES' for r in rows) else 'fixes' if any(r['status'] == 'TRIVIAL' for r in rows) else 'reject'
            verdicts.record(root, case['job'], verdict, args.by, str(table))
        return config.EXIT_OK
    if args.records:
        raise UsageError('unexpected record arguments outside table')
    raw = grading.read_input(args.input)
    diff = raw if args.input.endswith(('.diff', '.patch')) else grading.extract_diff(raw)
    _say(json.dumps(grading.apply_diff(root, case, diff, unsafe_no_sandbox=args.unsafe_no_sandbox), ensure_ascii=True))
    return config.EXIT_OK


def cmd_workflow(args):
    try:
        root = stage.resolve_repo(args.repo)
        stage.check_root(root, args.allow_non_git_root)
        return args.workflow(args, root)
    except (UsageError,ValueError,TypeError,UnicodeError) as exc:
        # JSON decoder errors can include untrusted material; never echo that input.
        if isinstance(exc, json.JSONDecodeError):
            detail = 'invalid JSON'
        elif isinstance(exc, (UsageError, ValueError)) and not isinstance(exc, UnicodeError):
            detail = report.clean_answer(str(exc).encode('utf-8', 'replace'))
            from .scan import scan_bytes
            if scan_bytes(detail.encode('utf-8')):
                detail = 'invalid input (detail redacted)'
        else:
            detail = type(exc).__name__
        _fail('invalid workflow input: '+detail)
        return config.EXIT_USAGE
    except (stage.Refused,events.EventError,OSError,report.OutPathRefused) as exc:
        operation = args.command + (' ' + args.queue_command if args.command == 'queue' else '')
        _refused(exc, operation, getattr(args, 'out', None))
        return config.EXIT_REFUSED
