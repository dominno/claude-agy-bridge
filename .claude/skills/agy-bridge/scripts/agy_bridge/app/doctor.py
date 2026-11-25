"""The doctor use cases: the read-only health check (agy probes, log mode, queue watcher) and the
--implementation sandbox probe. Each prints its lines and returns the exit code."""
from __future__ import annotations

import os
import stat
import sys
import tempfile
from typing import List

from ..core import config, rootcheck, stage
from ..core.config import UsageError
from ..port import edit_sandbox, model_runner
from .console import fail, refused, say


def health(repo, offline) -> int:
    failures: List[int] = []

    def line(ok: bool, name: str, why: str, code: int = config.EXIT_AGY_FAILED) -> None:
        say("%-4s %s: %s" % ("ok" if ok else "fail", name, why))
        if not ok:
            failures.append(code)

    line(sys.version_info >= (3, 9), "python", "%d.%d" % sys.version_info[:2], config.EXIT_UNSUPPORTED)
    try:
        with tempfile.TemporaryDirectory(prefix="agy-bridge-doctor-") as tmp:
            line(True, "temp dir", "writable")
            env = config.build_agy_env(repo)
            agy = model_runner.use()
            try:
                exe = agy.find_agy(rootcheck.canonical_path(repo) if repo else None)
                line(True, "agy on PATH", exe)
                version = agy.detect_version(exe, env)
                try:
                    agy.build_argv(version, exe, None)
                    line(True, "agy version", "%d.%d.%d is a known major (still unverified)" % version)
                except model_runner.UnsupportedAgy as exc:
                    line(False, "agy version", str(exc), config.EXIT_UNSUPPORTED)
                if offline:
                    say("skip agy models: --offline")
                else:
                    check_models(exe, env, tmp, line)
            except model_runner.AgyMissing as exc:
                line(False, "agy on PATH", str(exc), config.EXIT_AGY_MISSING)
            except model_runner.UnsupportedAgy as exc:
                line(False, "agy version", str(exc), config.EXIT_UNSUPPORTED)
    except OSError:
        line(False, "temp dir", "not writable")
    log = os.path.join(config.bridge_home(), "log.jsonl")
    if os.path.lexists(log):
        mode = stat.S_IMODE(os.lstat(log).st_mode)
        line(mode == 0o600, "log file mode", oct(mode))
    else:
        line(True, "log file mode", "no log yet")
    say("info: ~/.gemini %s" % ("exists" if os.path.isdir(os.path.expanduser("~/.gemini")) else "not found"))
    if repo:
        _watcher(repo)
    if not failures:
        return config.EXIT_OK
    return next((c for c in failures if c in (config.EXIT_AGY_MISSING, config.EXIT_UNSUPPORTED)), failures[0])


def _watcher(repo) -> None:
    """Warn (not fail) when the repository's default queue runner runs another agy-bridge version or code root."""
    from . import queue

    try:
        location = queue.QueueLocation(stage.resolve_repo(repo), allow=True)
        active = location.running()
    except (UsageError, stage.Refused, OSError):
        return
    stale = queue.watcher_warning(active)
    if stale:
        say("warn queue watcher: " + stale)


def implementation(repo_arg, profile_arg) -> int:
    """Probe the implementation sandbox with the profile."""
    from . import implement_work

    try:
        repo = stage.resolve_repo(repo_arg or '.')
        profile = implement_work._profile(profile_arg, repo)
        agy = model_runner.use()
        exe = agy.find_agy(repo)
        with tempfile.TemporaryDirectory(prefix='agy-bridge-edit-doctor-') as tree:
            version = agy.parse_version(edit_sandbox.use().preflight(tree=tree, profile=profile, exe=exe))
        agy.require_edit_version(version)
    except model_runner.AgyMissing as exc:
        fail(str(exc))
        return config.EXIT_AGY_MISSING
    except model_runner.UnsupportedAgy as exc:
        fail(str(exc))
        return config.EXIT_UNSUPPORTED
    except edit_sandbox.Unavailable as exc:
        fail(str(exc))
        return config.EXIT_REFUSED
    except UsageError as exc:
        fail('implementation profile or repository could not be validated: ' + stage.printable(str(exc)))
        return config.EXIT_REFUSED
    except (stage.Refused, OSError) as exc:
        refused(exc, 'doctor --implementation')
        return config.EXIT_REFUSED
    say('ok   implementation sandbox: agy %d.%d.%d' % version)
    return config.EXIT_OK


def check_models(exe, env, tmp, line) -> None:
    try:
        proc = model_runner.use().run_probe([exe, "models"], env, tmp, timeout=30)  # same five-name env
        names = [ln for ln in proc.stdout.decode("utf-8", "replace").splitlines() if ln.strip()]
        line(proc.returncode == 0 and bool(names), "agy models", "%d line(s) listed" % len(names))
    except (OSError, model_runner.TimeoutExpired, model_runner.UnsupportedAgy) as exc:
        line(False, "agy models", "could not run: %s" % exc)
