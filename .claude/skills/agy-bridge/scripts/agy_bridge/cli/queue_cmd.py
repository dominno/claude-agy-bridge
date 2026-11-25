"""Handler for queue add/run/watch/status/cancel and status; `add` and `watch --detach` run in app.queue_commands."""
from __future__ import annotations

import math

from ..app import report
from ..core import config
from ..core.config import UsageError
from .output import fail, say


def cmd_queue(args, root):
    from ..app import queue

    if args.queue_command == 'cancel':
        try:
            action = queue.cancel(root, args.name, args.reason, args.queue_dir, args.results_dir,
                                  args.allow_non_git_root)
        except queue.CancelRefused as exc:
            fail('queue cancel: ' + report.clean_answer(str(exc).encode('utf-8', 'replace')))
            return config.EXIT_USAGE if isinstance(exc, queue.CancelUnknown) else config.EXIT_REFUSED
        say('cancelled %s; %s; nothing was sent' % (args.name, action))
        return config.EXIT_OK
    paths = queue.Paths(root, args.queue_dir, args.results_dir, args.allow_non_git_root)
    if args.queue_command == 'status':
        if not math.isfinite(args.threshold) or args.threshold < 0:
            raise UsageError('threshold must be finite and nonnegative')
        say(report.clean_answer(queue.status(paths, args.threshold).encode('utf-8')))
        return config.EXIT_OK
    if args.queue_command == 'add':
        from ..app import queue_commands

        return queue_commands.add(paths, args.job_file, args.dry_run)
    poll = getattr(args, 'poll', 5)
    if not math.isfinite(poll) or not .05 <= poll <= 3600:
        raise UsageError('poll must be between .05 and 3600 seconds')
    if getattr(args, 'detach', False):
        from ..app import queue_commands

        return queue_commands.detach_watcher(paths, poll)
    return queue.run(paths, args.queue_command == 'watch', poll)
