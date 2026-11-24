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
    for name in ("delegate", "dry-run"):
        d = sub.add_parser(name)
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
    ev = sub.add_parser("events")
    ev.set_defaults(handler=cmd_events)
    ev.add_argument("-C", "--repo", default=".")
    ev.add_argument("--tail", type=int, default=10)
    ev.add_argument("--allow-non-git-root", action="store_true")
    doc = sub.add_parser("doctor")
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
    q = sub.add_parser('queue')
    qs = q.add_subparsers(dest='queue_command', required=True, parser_class=_Parser)
    for name in ('run', 'watch', 'status', 'add'):
        cmd = qs.add_parser(name)
        cmd.set_defaults(handler=cmd_workflow, workflow=cmd_queue)
        repo_options(cmd, folders=True)
        if name == 'add':
            cmd.add_argument('job_file', help='JSON job file, or - for stdin')
        if name == 'watch':
            cmd.add_argument('--poll', type=float, default=5)
        if name == 'status':
            cmd.add_argument('--threshold', type=float, default=300)
    st = sub.add_parser('status')
    st.set_defaults(handler=cmd_workflow, workflow=cmd_queue, queue_command='status')
    repo_options(st, folders=True)
    st.add_argument('--threshold', type=float, default=300)
    v = sub.add_parser('verdict')
    v.set_defaults(handler=cmd_workflow, workflow=cmd_verdict)
    repo_options(v)
    v.add_argument('--job', required=True)
    v.add_argument('--verdict', choices=('pass','fixes','reject'), required=True)
    v.add_argument('--by', required=True)
    v.add_argument('--evidence', required=True)
    v.add_argument('--note', nargs='?', const='')
    vs = sub.add_parser('verdicts')
    vs.set_defaults(handler=cmd_workflow, workflow=cmd_verdicts)
    repo_options(vs)
    vs.add_argument('--tail', type=int, default=10)
    g = sub.add_parser('grade-mutants')
    g.set_defaults(handler=cmd_workflow, workflow=cmd_grading)
    repo_options(g)
    g.add_argument('result')
    g.add_argument('--case', required=True)
    g.add_argument('--record', action='store_true')
    g.add_argument('--by')
    g.add_argument('--unsafe-no-sandbox', action='store_true', help='run model-written code WITHOUT a sandbox, with your privileges and network access')
    d = sub.add_parser('apply-diff')
    d.set_defaults(handler=cmd_workflow, workflow=cmd_grading)
    repo_options(d)
    d.add_argument('input', help='result.md, file.diff, or table')
    d.add_argument('records', nargs='*')
    d.add_argument('--case')
    d.add_argument('--unsafe-no-sandbox', action='store_true', help='run model-written code WITHOUT a sandbox, with your privileges and network access')
    t = sub.add_parser('stats')
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
    except (stage.Refused, events.EventError):
        _fail("events could not be read safely")
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
    if not failures:
        return config.EXIT_OK
    return next((c for c in failures if c in (config.EXIT_AGY_MISSING, config.EXIT_UNSUPPORTED)), failures[0])


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
    except (UsageError, stage.Refused, OSError):
        _fail('implementation profile, repository or temporary directory could not be validated')
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
    """The first stop signal unwinds through the cleanup code; later ones are ignored."""
    def on_stop(signum, _frame):
        for sig in STOP_SIGNALS:
            signal.signal(sig, signal.SIG_IGN)
        raise KeyboardInterrupt

    for sig in STOP_SIGNALS:
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

    paths = queue.Paths(root, args.queue_dir, args.results_dir, args.allow_non_git_root)
    if args.queue_command == 'status':
        if not math.isfinite(args.threshold) or args.threshold < 0:
            raise UsageError('threshold must be finite and nonnegative')
        _say(report.clean_answer(queue.status(paths, args.threshold).encode('utf-8')))
        return config.EXIT_OK
    if args.queue_command == 'add':
        raw = sys.stdin.read(queue.MAX_JOB_BYTES + 1) if args.job_file == '-' else grading.read_input(args.job_file)
        if len(raw.encode('utf-8')) > queue.MAX_JOB_BYTES:
            raise UsageError('job is over 128 KiB')
        _say(paths.add(json.loads(raw)))
        return config.EXIT_OK
    poll = getattr(args, 'poll', 5)
    if not math.isfinite(poll) or not .05 <= poll <= 3600:
        raise UsageError('poll must be between .05 and 3600 seconds')
    return queue.run(paths, args.queue_command == 'watch', poll)


def cmd_verdict(args, root):
    from . import verdicts

    verdicts.record(root, args.job, args.verdict, args.by, args.evidence, args.note)
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
    except (stage.Refused,events.EventError,OSError,report.OutPathRefused):
        _fail('workflow path or operation refused; inspect paths and permissions')
        return config.EXIT_REFUSED
