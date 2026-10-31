"""Delegation and built-in workflows. Existing exit codes are listed in config.py."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import signal
import stat
import subprocess
import sys
import tempfile
import time
from typing import List, Optional

from . import __version__, citations, config, events, report, runner, stage
from .config import UsageError


class _Parser(argparse.ArgumentParser):
    def error(self, message):  # argparse exits 2 by default; 2 means "agy failed" here
        self.print_usage(sys.stderr)
        sys.stderr.write("agy-bridge: usage error: %s\n" % message)
        raise SystemExit(config.EXIT_USAGE)


def _parser() -> argparse.ArgumentParser:
    p = _Parser(prog="agy-bridge", description="Hand one bounded, read-only task to agy.")
    p.add_argument("--version", action="version", version="agy-bridge " + __version__)
    sub = p.add_subparsers(dest="command", required=True, parser_class=_Parser)
    for name in ("delegate", "dry-run"):
        d = sub.add_parser(name)
        d.add_argument("--repo", default=".", help="repository root (default: current folder)")
        d.add_argument("--file", action="append", default=[], help="file to include (repeatable)")
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
    ev.add_argument("--repo", default=".")
    ev.add_argument("--tail", type=int, default=10)
    ev.add_argument("--allow-non-git-root", action="store_true")
    doc = sub.add_parser("doctor")
    doc.add_argument("--repo", help="repository folder; agy must not resolve inside it (the slash command passes it)")
    doc.add_argument("--offline", action="store_true", help="skip the check that needs network and sign-in")
    def repo_options(parser, folders=False):
        parser.add_argument('--repo', default='.')
        parser.add_argument('--allow-non-git-root', action='store_true')
        if folders:
            parser.add_argument('--queue-dir')
            parser.add_argument('--results-dir')
    q = sub.add_parser('queue')
    qs = q.add_subparsers(dest='queue_command', required=True, parser_class=_Parser)
    for name in ('run', 'watch', 'status', 'add'):
        cmd = qs.add_parser(name)
        repo_options(cmd, folders=True)
        if name == 'add':
            cmd.add_argument('job_file', help='JSON job file, or - for stdin')
        if name == 'watch':
            cmd.add_argument('--poll', type=float, default=5)
        if name == 'status':
            cmd.add_argument('--threshold', type=float, default=300)
    st = sub.add_parser('status')
    repo_options(st, folders=True)
    st.add_argument('--threshold', type=float, default=300)
    v = sub.add_parser('verdict')
    repo_options(v)
    v.add_argument('--job', required=True)
    v.add_argument('--verdict', choices=('pass','fixes','reject'), required=True)
    v.add_argument('--by', required=True)
    v.add_argument('--evidence', required=True)
    v.add_argument('--note', nargs='?', const='')
    vs = sub.add_parser('verdicts')
    repo_options(vs)
    vs.add_argument('--tail', type=int, default=10)
    g = sub.add_parser('grade-mutants')
    repo_options(g)
    g.add_argument('result')
    g.add_argument('--case', required=True)
    g.add_argument('--record', action='store_true')
    g.add_argument('--by')
    g.add_argument('--unsafe-no-sandbox', action='store_true', help='run model-written code WITHOUT a sandbox, with your privileges and network access')
    d = sub.add_parser('apply-diff')
    repo_options(d)
    d.add_argument('input', help='result.md, file.diff, or table')
    d.add_argument('records', nargs='*')
    d.add_argument('--case')
    d.add_argument('--unsafe-no-sandbox', action='store_true', help='run model-written code WITHOUT a sandbox, with your privileges and network access')
    t = sub.add_parser('stats')
    repo_options(t)
    t.add_argument('--results-dir')
    t.add_argument('--target', type=float, default=180)
    return p


def _say(text: str) -> None:
    sys.stdout.write(text + ("" if text.endswith("\n") else "\n"))


def _fail(text: str) -> None:
    sys.stderr.write("agy-bridge: " + text + "\n")


def _argv_preview(model: Optional[str], root: str, env: dict) -> str:
    """The agy command line, then a line saying exactly which program (if any) was run to get it."""
    ran = "Program run: only 'agy --version' (five-name environment, empty folder). The bridge runs no git."
    none = "No program was run. The bridge runs no git."
    try:
        exe = runner.find_agy(root)
        argv = runner.build_argv(runner.detect_version(exe, env), exe, model)
    except runner.AgyMissing as exc:
        return "agy command: unavailable (%s)\n%s" % (exc, none)
    except runner.AgyNotRun as exc:
        return "agy command: unavailable (%s)\n%s" % (exc, none)
    except runner.UnsupportedAgy as exc:
        return "agy command: unavailable (%s)\n%s" % (exc, ran)
    return "agy command (cwd = staging folder): " + " ".join(_quote(a) for a in argv) + "\n" + ran


def _quote(arg: str) -> str:
    return arg if arg.isalnum() or arg.startswith("/") and " " not in arg else "'%s'" % arg.replace("'", "'\\''")


def _manifest_text(st: stage.Staging, prepared: stage.Prepared) -> str:
    lines = ["Would send %d file(s), %d bytes, to agy (Google). No file contents or task text have been sent."
             % (prepared.file_count, prepared.total_bytes)]
    for rel, size, sha in st.manifest:
        lines.append("  %8d  %s  %s" % (size, sha[:12], stage.printable(rel)))
    if prepared.skipped_by_denylist:
        lines.append("  (%d glob match(es) skipped by the denylist)" % prepared.skipped_by_denylist)
    return "\n".join(lines)


def _log(code: int, args, model, prepared, seconds: float, task: str) -> None:
    warn = report.write_log({
        "command": "delegate", "exit": code, "model": model or "default",
        "files": prepared.file_count if prepared else 0, "bytes": prepared.total_bytes if prepared else 0,
        "seconds": round(seconds, 2), "task_sha256": hashlib.sha256(task.encode("utf-8", "replace")).hexdigest()[:16]})
    if warn:
        _fail(warn)


def _complete_not_sent(prepared, files, sent, dry):
    if sent or dry:
        return
    known = {item.where for item in prepared.not_sent}
    requested = prepared.requested or files
    for path in requested:
        if path not in known:
            prepared.not_sent.append(stage.Refusal("TASK", "whole delegation was not sent", path))
            known.add(path)


def _event_path(path, root):
    """Keep outside-root locations out of repository metadata, even on early errors."""
    try:
        rel, bad = stage._relative(path, [root])
    except UsageError:
        rel, bad = path, None
    if bad:
        return '<outside repo>'
    from .scan import scan_bytes
    return '[redacted key-shaped filename]' if scan_bytes(rel.encode('utf-8', 'replace')) else rel


def cmd_delegate(args, dry: bool, capture=None) -> int:
    started = time.monotonic()
    task = " ".join(args.task).strip()
    prepared = stage.Prepared()
    model = None
    code = config.EXIT_USAGE
    event_fd = None
    sent = False
    result = None
    answer = ""
    checked = citations.Check([]) if args.cite else None
    out_path = None
    written_path = None
    record = None
    try:
        # Validate the root before making any repo-local metadata directory.
        root = stage.resolve_repo(args.repo)
        for raw in args.file:
            try:
                rel, bad = stage._relative(raw, [root, os.path.abspath(args.repo)])
                path = raw if bad else rel
            except UsageError:
                path = raw
            if path not in prepared.requested:
                prepared.requested.append(path)
        stage.check_root(root, args.allow_non_git_root)
        try:
            event_fd = events.open_log(root)
        except events.EventIOError as exc:
            _fail('warning: %s; event not recorded' % exc)
        try:
            task.encode("utf-8")
        except UnicodeEncodeError:
            raise UsageError("the task text is not valid UTF-8")
        if not task:
            raise UsageError('give the task after "--", for example: -- "summarise these files"')
        if args.require_verified and not args.cite:
            raise UsageError("--require-verified requires --cite")
        model = config.validate_model(args.model)
        timeout = config.parse_duration(args.timeout)
        if min(args.max_bytes, args.max_file_bytes, args.max_out_bytes) < 1:
            raise UsageError("size limits must be positive")
        extra = config.load_extra_deny(root)
        root, prepared = stage.prepare(root, args.file, args.glob,
                                       task + (citations.INSTRUCTION if args.cite else ""),
                                       args.max_file_bytes, args.max_bytes, extra,
                                       allow_non_git=args.allow_non_git_root,
                                       allow_fragment_file=args.allow_fragment_file, audit=prepared)
        out_path = report.resolve_out_path(args.out, root) if args.out else None
        env = config.build_agy_env(root)
        if not dry:
            stage.sweep_old_staging()
        with stage.materialize(prepared) as st:
            if dry:
                _say(_manifest_text(st, prepared))
                _say(_argv_preview(model, root, env))
                code = config.EXIT_OK
            else:
                exe = runner.find_agy(root)
                version = runner.detect_version(exe, env)
                argv = runner.build_argv(version, exe, model)
                def mark_sent():
                    nonlocal sent
                    sent = True
                result = runner.run_agy(argv, st.path, env, timeout, args.max_out_bytes, on_started=mark_sent)
        if not dry:
            answer = report.clean_answer(result.out, preserve_lines=args.cite)
            code = report.classify(result, answer.strip())
            if args.cite:
                checked = citations.check(answer, prepared.entries)
            if code != config.EXIT_OK:
                _fail(report.FAILURE_TEXT[code])
            else:
                if args.require_verified and checked.unverified:
                    code = config.EXIT_UNVERIFIED
                    _fail("citation check contains unverified lines")
                saved = checked.render() if checked is not None else answer
                if out_path:
                    try:
                        report.write_new_file(out_path, saved)
                        written_path = out_path
                    except FileExistsError:
                        raise UsageError("--out already exists; it is never overwritten")
                elif capture is None and len(answer) > config.INLINE_ANSWER_CHARS:
                    written_path = report.write_temp_answer(saved)
    except UsageError as exc:
        _fail("usage error: %s" % exc)
        code = config.EXIT_USAGE
    except report.OutPathRefused as exc:
        _fail("Rule R8: %s" % exc)
        code = config.EXIT_REFUSED
    except runner.AgyMissing as exc:
        _fail(str(exc))
        code = config.EXIT_AGY_MISSING
    except runner.UnsupportedAgy as exc:
        _fail(str(exc))
        code = config.EXIT_UNSUPPORTED
    except KeyboardInterrupt:
        code = 130
        raise
    except events.EventError as exc:
        _fail(str(exc) + "; delegation refused")
        code = config.EXIT_REFUSED
    except OSError:
        _fail("operation could not be completed safely")
        code = config.EXIT_REFUSED
    except stage.Refused as exc:
        _fail("refused by a rule; nothing was staged or sent (%d problem(s)):" % len(exc.refusals))
        for item in exc.refusals:
            sys.stderr.write("  - %s\n" % stage.describe(item))
        code = config.EXIT_REFUSED
    finally:
        _complete_not_sent(prepared, args.file + args.glob, sent, dry and code == config.EXIT_OK)
        if event_fd is not None or capture is not None:
            record = {
                "model": model or "default",
                "files": [{"path": e.rel, "sha256": e.sha256, "bytes": len(e.data)}
                          for e in prepared.entries] if sent else [],
                "not_sent": [{"path": _event_path(item.where, root),
                              "rule": item.rule} for item in prepared.not_sent],
                "total_bytes": prepared.total_bytes if sent else 0,
                "seconds": round(time.monotonic() - started, 3), "exit_code": code,
                "truncated": result.truncated if result else False, "dry_run": dry,
            }
            if checked is not None:
                record["cite"] = {"verified": checked.verified, "unverified": checked.unverified}
        if capture is not None:
            capture.update(record or {})
            capture.update(answer=answer, exit_code=code, rendered_cite=checked.render() if checked is not None else None)
        if event_fd is not None:
            try:
                events.append(event_fd, record)
            except events.EventError as exc:
                _fail('warning: %s; event not recorded' % exc)
            finally:
                try:
                    os.close(event_fd)
                except OSError:
                    _fail('warning: event log could not be closed')
        else:
            _fail("event not recorded: repository or event path could not be validated safely")
        if not dry:
            _log(code, args, model, prepared if sent else None, time.monotonic() - started, task)
        if answer or prepared.not_sent or checked is not None:
            _say(report.render(answer, model, prepared.file_count if sent else 0,
                               prepared.total_bytes if sent else 0, time.monotonic() - started,
                               result.truncated if result else False, written_path,
                               prepared.not_sent, checked))
    return code


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
    args = _parser().parse_args(argv)

    install_stop_handlers()
    try:
        if args.command in ('queue','status','verdict','verdicts','grade-mutants','apply-diff','stats'):
            return cmd_workflow(args)
        if args.command == "doctor":
            return cmd_doctor(args)
        if args.command == "events":
            return cmd_events(args)
        return cmd_delegate(args, dry=args.command == "dry-run" or getattr(args, "dry_run", False))
    except KeyboardInterrupt:
        _fail("interrupted; the staging folder was removed")
        return 130


def cmd_workflow(args):
    from . import grading, queue, safety, stats, verdicts
    try:
        root = stage.resolve_repo(args.repo)
        stage.check_root(root, args.allow_non_git_root)
        if args.command in ('queue','status'):
            paths = queue.Paths(root,args.queue_dir,args.results_dir,args.allow_non_git_root)
            command = args.queue_command if args.command == 'queue' else 'status'
            if command == 'status':
                if not math.isfinite(args.threshold) or args.threshold < 0:
                    raise UsageError('threshold must be finite and nonnegative')
                _say(report.clean_answer(queue.status(paths,args.threshold).encode('utf-8')))
                return 0
            if command == 'add':
                raw = sys.stdin.read(queue.MAX_JOB_BYTES+1) if args.job_file == '-' else grading.read_input(args.job_file)
                if len(raw.encode('utf-8')) > queue.MAX_JOB_BYTES:
                    raise UsageError('job is over 128 KiB')
                _say(paths.add(json.loads(raw)))
                return 0
            poll = getattr(args,'poll',5)
            if not math.isfinite(poll) or poll < .05 or poll > 3600:
                raise UsageError('poll must be between .05 and 3600 seconds')
            return queue.run(paths,command == 'watch',poll)
        if args.command == 'verdict':
            verdicts.record(root,args.job,args.verdict,args.by,args.evidence,args.note)
            _say('verdict recorded for '+args.job)
            return 0
        if args.command == 'verdicts':
            if not 1 <= args.tail <= 10000:
                raise UsageError('--tail must be between 1 and 10000')
            for row in verdicts.rows(root,args.tail):
                _say(json.dumps(row,ensure_ascii=True,sort_keys=True))
            return 0
        if args.command == 'stats':
            if not math.isfinite(args.target) or args.target <= 0:
                raise UsageError('--target must be finite and positive')
            rel = safety.local(root,args.results_dir or '.agy-bridge/results',metadata=True)
            _say(report.clean_answer(stats.render(root,os.path.join(root,rel),args.target).encode('utf-8')))
            return 0
        if args.command == 'apply-diff' and args.input == 'table':
            if not args.records:
                raise UsageError('table needs at least one JSON/JSONL record file')
            rows=[]
            for path in args.records:
                rows.extend(json.loads(line) for line in grading.read_input(path).splitlines() if line.strip())
            _say(report.clean_answer(grading.diff_table(rows).encode('utf-8')))
            return 0
        if not args.case:
            raise UsageError('--case is required')
        case = json.loads(grading.read_input(args.case))
        grading.validate_case(root,case)
        if args.unsafe_no_sandbox:
            _fail('WARNING: --unsafe-no-sandbox runs model-written code with your privileges and network access')
        if args.command == 'grade-mutants':
            if args.record and (not args.by or not case.get('job')):
                raise UsageError('--record requires --by and case.job')
            rows,table,records = grading.grade(root,case,grading.extract_mutants(grading.read_input(args.result)),unsafe_no_sandbox=args.unsafe_no_sandbox)
            _say(report.clean_answer(table.read_bytes()))
            _say('Table: %s\nJSONL: %s' % (table,records))
            if args.record:
                verdict = 'pass' if any(r['status']=='SURVIVES' for r in rows) else 'fixes' if any(r['status']=='TRIVIAL' for r in rows) else 'reject'
                verdicts.record(root,case['job'],verdict,args.by,str(table))
            return 0
        if args.records:
            raise UsageError('unexpected record arguments outside table')
        raw = grading.read_input(args.input)
        diff = raw if args.input.endswith(('.diff','.patch')) else grading.extract_diff(raw)
        _say(json.dumps(grading.apply_diff(root,case,diff,unsafe_no_sandbox=args.unsafe_no_sandbox),ensure_ascii=True))
        return 0
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
