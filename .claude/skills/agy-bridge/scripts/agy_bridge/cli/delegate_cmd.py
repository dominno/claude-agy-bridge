"""Handlers for delegate, dry-run and events."""
from __future__ import annotations

import sys

from ..app import delegation, report
from ..core import config, events, stage
from ..core.config import UsageError
from .output import fail, refused, say


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
        say(line)
    sys.stderr.writelines(result.diagnostics)
    if result.answer or result.not_sent or result.checked is not None:
        info = result.metadata
        say(report.render(result.answer, info["model"], len(info["files"]), info["total_bytes"],
                          info["seconds"], info["truncated"], result.written_path, result.not_sent, result.checked))
    return result.exit_code


def cmd_events(args):
    try:
        if not 1 <= args.tail <= 10000:
            raise UsageError("--tail must be between 1 and 10000")
        root = stage.resolve_repo(args.repo)
        stage.check_root(root, args.allow_non_git_root)
        for line in events.tail(root, args.tail):
            say(line)
        return config.EXIT_OK
    except UsageError as exc:
        fail(str(exc))
        return config.EXIT_USAGE
    except (stage.Refused, events.EventError, OSError) as exc:
        refused(exc, "events")
        return config.EXIT_REFUSED
