"""Read-only delegation shared by the CLI and the repository queue."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
import time
from typing import Optional, Sequence

from . import citations, config, events, report, runner, stage
from .config import UsageError


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
    from .scan import scan_bytes
    return '[redacted key-shaped filename]' if scan_bytes(rel.encode('utf-8', 'replace')) else rel


def execute(request: DelegationRequest) -> DelegationResult:
    """Run a delegation and return its result without touching process output streams."""
    started = time.monotonic()
    task = request.task.strip()
    dry = request.dry
    diagnostics: list[str] = []
    preview: list[str] = []

    def fail(text):
        diagnostics.append("agy-bridge: " + text + "\n")

    prepared = stage.Prepared()
    model = None
    code = config.EXIT_USAGE
    event_fd = None
    sent = False
    result = None
    answer = ""
    checked = citations.Check([]) if request.cite else None
    out_path = None
    written_path = None
    root = request.repo
    try:
        # Validate the root before making any repo-local metadata directory.
        root = stage.resolve_repo(request.repo)
        for raw in request.files:
            try:
                rel, bad = stage._relative(raw, [root, os.path.abspath(request.repo)])
                path = raw if bad else rel
            except UsageError:
                path = raw
            if path not in prepared.requested:
                prepared.requested.append(path)
        stage.check_root(root, request.allow_non_git_root)
        try:
            event_fd = events.open_log(root)
        except events.EventIOError as exc:
            fail('warning: %s; event not recorded' % exc)
        try:
            task.encode("utf-8")
        except UnicodeEncodeError:
            raise UsageError("the task text is not valid UTF-8")
        if not task:
            raise UsageError('give the task after "--", for example: -- "summarise these files"')
        if request.require_verified and not request.cite:
            raise UsageError("--require-verified requires --cite")
        model = config.validate_model(request.model)
        timeout = config.parse_duration(request.timeout)
        if min(request.max_bytes, request.max_file_bytes, request.max_out_bytes) < 1:
            raise UsageError("size limits must be positive")
        extra = config.load_extra_deny(root)
        root, prepared = stage.prepare(root, request.files, request.globs,
                                       task + (citations.INSTRUCTION if request.cite else ""),
                                       request.max_file_bytes, request.max_bytes, extra,
                                       allow_non_git=request.allow_non_git_root,
                                       allow_fragment_file=request.allow_fragment_file, audit=prepared)
        out_path = report.resolve_out_path(request.out, root) if request.out else None
        env = config.build_agy_env(root)
        if not dry:
            stage.sweep_old_staging()
        with stage.materialize(prepared) as st:
            if dry:
                preview.extend((_manifest_text(st, prepared), _argv_preview(model, root, env)))
                code = config.EXIT_OK
            else:
                exe = runner.find_agy(root)
                version = runner.detect_version(exe, env)
                argv = runner.build_argv(version, exe, model)
                def mark_sent():
                    nonlocal sent
                    sent = True
                result = runner.run_agy(argv, st.path, env, timeout, request.max_out_bytes, on_started=mark_sent)
        if not dry:
            answer, removed = report.clean_counted(result.out, preserve_lines=request.cite)
            code = report.classify(result, answer.strip())
            if request.cite:
                checked = citations.check(answer, prepared.entries)
                if removed:
                    # A note line would count as an unverified citation line; report it beside the answer.
                    diagnostics.append("agy-bridge: " + report.INVISIBLE_NOTE % removed + "\n")
            else:
                answer = report.with_note(answer, removed)
            if code != config.EXIT_OK:
                fail(report.FAILURE_TEXT[code])
            else:
                if request.require_verified and checked.unverified:
                    code = config.EXIT_UNVERIFIED
                    fail("citation check contains unverified lines")
                saved = checked.render() if checked is not None else answer
                if out_path:
                    try:
                        report.write_new_file(out_path, saved)
                        written_path = out_path
                    except FileExistsError:
                        raise UsageError("--out already exists; it is never overwritten")
                elif request.save_large_answer and len(answer) > config.INLINE_ANSWER_CHARS:
                    written_path = report.write_temp_answer(saved)
    except UsageError as exc:
        fail("usage error: %s" % exc)
        code = config.EXIT_USAGE
    except report.OutPathRefused as exc:
        fail("Rule R8: %s" % exc)
        code = config.EXIT_REFUSED
    except runner.AgyMissing as exc:
        fail(str(exc))
        code = config.EXIT_AGY_MISSING
    except runner.UnsupportedAgy as exc:
        fail(str(exc))
        code = config.EXIT_UNSUPPORTED
    except KeyboardInterrupt:
        code = 130
        raise
    except events.EventError as exc:
        fail(str(exc) + "; delegation refused")
        code = config.EXIT_REFUSED
    except OSError:
        fail("operation could not be completed safely")
        code = config.EXIT_REFUSED
    except stage.Refused as exc:
        fail("refused by a rule; nothing was staged or sent (%d problem(s)):" % len(exc.refusals))
        for item in exc.refusals:
            diagnostics.append("  - %s\n" % stage.describe(item))
        code = config.EXIT_REFUSED
    finally:
        _complete_not_sent(prepared, [*request.files, *request.globs], sent, dry and code == config.EXIT_OK)
        record = {
            "model": model or "default",
            "files": [{"path": e.rel, "sha256": e.sha256, "bytes": len(e.data)}
                      for e in prepared.entries] if sent else [],
            "not_sent": [{"path": _event_path(item.where, root), "rule": item.rule}
                         for item in prepared.not_sent],
            "total_bytes": prepared.total_bytes if sent else 0,
            "seconds": round(time.monotonic() - started, 3), "exit_code": code,
            "truncated": result.truncated if result else False, "dry_run": dry,
        }
        if checked is not None:
            record["cite"] = {"verified": checked.verified, "unverified": checked.unverified}
        if event_fd is not None:
            try:
                events.append(event_fd, record)
            except events.EventError as exc:
                fail('warning: %s; event not recorded' % exc)
            finally:
                try:
                    os.close(event_fd)
                except OSError:
                    fail('warning: event log could not be closed')
        else:
            fail("event not recorded: repository or event path could not be validated safely")
        if not dry:
            warning = _log(code, model, prepared if sent else None, time.monotonic() - started, task)
            if warning:
                fail(warning)
    return DelegationResult(code, answer, checked, tuple(prepared.not_sent), written_path,
                            tuple(preview), tuple(diagnostics), record)
