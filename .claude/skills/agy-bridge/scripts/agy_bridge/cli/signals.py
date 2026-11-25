"""Stop-signal handling for every command: the first SIGINT, SIGTERM or SIGHUP unwinds through the cleanup code."""
from __future__ import annotations

import signal

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
