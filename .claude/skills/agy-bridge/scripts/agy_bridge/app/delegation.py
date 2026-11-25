"""Read-only delegation shared by the CLI and the repository queue."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
import time
from typing import Optional, Sequence

from ..core import citations, config, events, stage
from ..core.config import UsageError
from ..core.scan import scan_bytes
from ..port import model_runner
from . import report


@dataclass(frozen=True)
class DelegationRequest:
    repo: str
    task: str
    files: Sequence[str] = ()
    globs: Sequence[str] = ()
    model: Optional[str] = None
    cite: bool = False
    require_verified: bool = False
    allow_fragment_file: Optional[str] = None
    timeout: str = '300s'
    allow_non_git_root: bool = False
    max_bytes: int = config.MAX_TOTAL_BYTES
    max_file_bytes: int = config.MAX_FILE_BYTES
    max_out_bytes: int = config.MAX_OUT_BYTES
    out: Optional[str] = None
    dry: bool = False
    save_large_answer: bool = False


@dataclass(frozen=True)
class DelegationResult:
    exit_code: int
    answer: str
    checked: Optional[citations.Check]
    not_sent: Sequence[stage.Refusal]
    written_path: Optional[str]
    preview: tuple[str, ...]
    diagnostics: tuple[str, ...]
    metadata: dict

    def as_queue_record(self) -> dict:
        return dict(self.metadata, answer=self.answer,
                    rendered_cite=self.checked.render() if self.checked is not None else None,
                    diagnostics=''.join(self.diagnostics))


def _argv_preview(model: Optional[str], root: str, env: dict) -> str:
    """The agy command line, then a line saying exactly which program (if any) was run to get it."""
    ran = "Program run: only 'agy --version' (five-name environment, empty folder). The bridge runs no git."
    none = "No program was run. The bridge runs no git."
    agy = model_runner.use()
    try:
        exe = agy.find_agy(root)
        argv = agy.build_argv(agy.detect_version(exe, env), exe, model)
    except model_runner.AgyMissing as exc:
        return "agy command: unavailable (%s)\n%s" % (exc, none)
    except model_runner.AgyNotRun as exc:
        return "agy command: unavailable (%s)\n%s" % (exc, none)
    except model_runner.UnsupportedAgy as exc:
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


def _log(code: int, model, prepared, seconds: float, task: str):
    return report.write_log({
        "command": "delegate", "exit": code, "model": model or "default",
        "files": prepared.file_count if prepared else 0, "bytes": prepared.total_bytes if prepared else 0,
        "seconds": round(seconds, 2), "task_sha256": hashlib.sha256(task.encode("utf-8", "replace")).hexdigest()[:16]})


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
    return '[redacted key-shaped filename]' if scan_bytes(rel.encode('utf-8', 'replace')) else rel


class _Run:
    """The state of one delegation, kept for its event record, its log line and its result."""

    def __init__(self, request: DelegationRequest):
        self.request = request
        self.started = time.monotonic()
        self.task = request.task.strip()
        self.dry = request.dry
        self.diagnostics: list[str] = []
        self.preview: list[str] = []
        self.prepared = stage.Prepared()
        self.model = None
        self.code = config.EXIT_USAGE
        self.event_fd = None
        self.sent = False
        self.result = None
        self.answer = ""
        self.checked = citations.Check([]) if request.cite else None
        self.out_path = None
        self.written_path = None
        self.root = request.repo

    def fail(self, text):
        self.diagnostics.append("agy-bridge: " + text + "\n")

    def mark_sent(self):
        self.sent = True


def _open_journal(run: _Run) -> None:
    """Resolve and check the root (before any repo-local metadata folder exists), then open the event log."""
    request = run.request
    run.root = stage.resolve_repo(request.repo)
    for raw in request.files:
        try:
            rel, bad = stage._relative(raw, [run.root, os.path.abspath(request.repo)])
            path = raw if bad else rel
        except UsageError:
            path = raw
        if path not in run.prepared.requested:
            run.prepared.requested.append(path)
    stage.check_root(run.root, request.allow_non_git_root)
    try:
        run.event_fd = events.open_log(run.root)
    except events.EventIOError as exc:
        run.fail('warning: %s; event not recorded' % exc)


def _prepare(run: _Run):
    """Validate the request and select, read and check the files; returns the timeout and the agy environment."""
    request, task = run.request, run.task
    try:
        task.encode("utf-8")
    except UnicodeEncodeError:
        raise UsageError("the task text is not valid UTF-8")
    if not task:
        raise UsageError('give the task after "--", for example: -- "summarise these files"')
    if request.require_verified and not request.cite:
        raise UsageError("--require-verified requires --cite")
    run.model = config.validate_model(request.model)
    timeout = config.parse_duration(request.timeout)
    if min(request.max_bytes, request.max_file_bytes, request.max_out_bytes) < 1:
        raise UsageError("size limits must be positive")
    extra = config.load_extra_deny(run.root)
    run.root, run.prepared = stage.prepare(run.root, request.files, request.globs,
                                           task + (citations.INSTRUCTION if request.cite else ""),
                                           request.max_file_bytes, request.max_bytes, extra,
                                           allow_non_git=request.allow_non_git_root,
                                           allow_fragment_file=request.allow_fragment_file, audit=run.prepared)
    run.out_path = report.resolve_out_path(request.out, run.root) if request.out else None
    env = config.build_agy_env(run.root)
    if not run.dry:
        stage.sweep_old_staging()
    return timeout, env


def _stage_and_send(run: _Run, timeout, env) -> None:
    """Materialise the staging folder, then preview the send (dry run) or run agy in it."""
    with stage.materialize(run.prepared) as st:
        if run.dry:
            run.preview.extend((_manifest_text(st, run.prepared), _argv_preview(run.model, run.root, env)))
            run.code = config.EXIT_OK
        else:
            agy = model_runner.use()
            exe = agy.find_agy(run.root)
            version = agy.detect_version(exe, env)
            argv = agy.build_argv(version, exe, run.model)
            run.result = agy.run_agy(argv, st.path, env, timeout, run.request.max_out_bytes,
                                     on_started=run.mark_sent)


def _save_answer(run: _Run) -> None:
    request = run.request
    if request.require_verified and run.checked.unverified:
        run.code = config.EXIT_UNVERIFIED
        run.fail("citation check contains unverified lines")
    saved = run.checked.render() if run.checked is not None else run.answer
    if run.out_path:
        try:
            report.write_new_file(run.out_path, saved)
            run.written_path = run.out_path
        except FileExistsError:
            raise UsageError("--out already exists; it is never overwritten")
    elif request.save_large_answer and len(run.answer) > config.INLINE_ANSWER_CHARS:
        run.written_path = report.write_temp_answer(saved)


def _read_answer(run: _Run) -> None:
    """Clean and classify agy's output, check citations, then save the answer when it succeeded."""
    request = run.request
    run.answer, removed = report.clean_counted(run.result.out, preserve_lines=request.cite)
    run.code = report.classify(run.result, run.answer.strip())
    if request.cite:
        run.checked = citations.check(run.answer, run.prepared.entries)
        if removed:
            # A note line would count as an unverified citation line; report it beside the answer.
            run.diagnostics.append("agy-bridge: " + report.INVISIBLE_NOTE % removed + "\n")
    else:
        run.answer = report.with_note(run.answer, removed)
    if run.code != config.EXIT_OK:
        run.fail(report.FAILURE_TEXT[run.code])
    else:
        _save_answer(run)


def _refuse(run: _Run, exc: BaseException) -> None:
    """The diagnostic and exit code for a refused delegation, checked in the original handler order."""
    if isinstance(exc, UsageError):
        run.fail("usage error: %s" % exc)
        run.code = config.EXIT_USAGE
    elif isinstance(exc, report.OutPathRefused):
        run.fail("Rule R8: %s" % exc)
        run.code = config.EXIT_REFUSED
    elif isinstance(exc, model_runner.AgyMissing):
        run.fail(str(exc))
        run.code = config.EXIT_AGY_MISSING
    elif isinstance(exc, model_runner.UnsupportedAgy):
        run.fail(str(exc))
        run.code = config.EXIT_UNSUPPORTED
    elif isinstance(exc, events.EventError):
        run.fail(str(exc) + "; delegation refused")
        run.code = config.EXIT_REFUSED
    elif isinstance(exc, OSError):
        run.fail("operation could not be completed safely")
        run.code = config.EXIT_REFUSED
    else:
        run.fail("refused by a rule; nothing was staged or sent (%d problem(s)):" % len(exc.refusals))
        for item in exc.refusals:
            run.diagnostics.append("  - %s\n" % stage.describe(item))
        run.code = config.EXIT_REFUSED


def _event_record(run: _Run) -> dict:
    prepared, sent = run.prepared, run.sent
    record = {
        "model": run.model or "default",
        "files": [{"path": e.rel, "sha256": e.sha256, "bytes": len(e.data)}
                  for e in prepared.entries] if sent else [],
        "not_sent": [{"path": _event_path(item.where, run.root), "rule": item.rule}
                     for item in prepared.not_sent],
        "total_bytes": prepared.total_bytes if sent else 0,
        "seconds": round(time.monotonic() - run.started, 3), "exit_code": run.code,
        "truncated": run.result.truncated if run.result else False, "dry_run": run.dry,
    }
    if run.checked is not None:
        record["cite"] = {"verified": run.checked.verified, "unverified": run.checked.unverified}
    return record


def _close_journal(run: _Run, record: dict) -> None:
    if run.event_fd is not None:
        try:
            events.append(run.event_fd, record)
        except events.EventError as exc:
            run.fail('warning: %s; event not recorded' % exc)
        finally:
            try:
                os.close(run.event_fd)
            except OSError:
                run.fail('warning: event log could not be closed')
    else:
        run.fail("event not recorded: repository or event path could not be validated safely")


def _finish(run: _Run) -> dict:
    """Complete the not-sent list, append the event record, close the log and write the metadata log line."""
    _complete_not_sent(run.prepared, [*run.request.files, *run.request.globs], run.sent,
                       run.dry and run.code == config.EXIT_OK)
    record = _event_record(run)
    _close_journal(run, record)
    if not run.dry:
        warning = _log(run.code, run.model, run.prepared if run.sent else None, time.monotonic() - run.started,
                       run.task)
        if warning:
            run.fail(warning)
    return record


def execute(request: DelegationRequest) -> DelegationResult:
    """Run a delegation and return its result without touching process output streams."""
    run = _Run(request)
    try:
        _open_journal(run)
        timeout, env = _prepare(run)
        _stage_and_send(run, timeout, env)
        if not run.dry:
            _read_answer(run)
    except (UsageError, report.OutPathRefused, model_runner.AgyMissing, model_runner.UnsupportedAgy,
            events.EventError, OSError, stage.Refused) as exc:
        _refuse(run, exc)
    except KeyboardInterrupt:
        run.code = 130
        raise
    finally:
        record = _finish(run)
    return DelegationResult(run.code, run.answer, run.checked, tuple(run.prepared.not_sent), run.written_path,
                            tuple(run.preview), tuple(run.diagnostics), record)
