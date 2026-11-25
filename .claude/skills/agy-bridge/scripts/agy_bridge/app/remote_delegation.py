"""The remote-agent variant of a read-only delegation: extra input checks, the inlined prompt, the send and its
failure texts. The staging pipeline (denylist, secret scan, size limits) is the shared one in delegation.py."""
from __future__ import annotations

import re
from typing import List, Optional

from ..core import remote_prompt, stage
from ..core.config import UsageError
from ..port import model_runner

NOT_ENFORCED = ("The 'use no tools, use only these files' rule is an instruction to the model, not something the "
                "bridge can enforce in that environment.")
NOTICE = ("remote agent: the staged files were inlined into the prompt and sent to a Google-managed cloud "
          "environment. " + NOT_ENFORCED)
TOOLS_WARNING = ("warning: the remote agent showed %d tool call(s) before its reply despite the no-tools "
                 "instruction; treat the answer with extra care.")
FAILURE_TEXT = {
    "auth": "agy is not signed in with Google (remote agents need a Google sign-in; an API key is not "
            "supported). Sign in by running agy yourself, then retry.",
    "cloud_disabled": "agy says cloud conversations are not enabled for this account.",
    "remote_unavailable": "agy says remote agents are not available for this account.",
    "model_unavailable": "the chosen model is not available for remote conversations; drop --model or pick another.",
    "quota": "agy reported a quota or rate limit. Try later.",
    "environment_limit": "the account reached its limit of running remote environments; each remote delegation "
                         "keeps one running for a while after it ends. Wait, or delete old remote conversations "
                         "in agy (/resume), then retry.",
    "send_failed": "agy could not send the message to the remote agent.",
    "timeout": "the remote agent did not finish within the time limit and was stopped.",
    "not_ready": "agy's terminal UI did not become ready for input; nothing was pasted.",
    "not_submitted": "agy did not start working on the pasted prompt.",
    "exited": "agy exited before the remote agent replied.",
    "no_end_marker": "the reply's end marker was not found; the answer may be incomplete and was not used.",
    "output_limit": "the remote reply is larger than --max-out-bytes; it was not used.",
    "no_answer": "the remote agent finished without a framed reply.",
    "project_mismatch": "agy reported the requested --remote-project as not found and would use the default "
                        "remote project instead; nothing was pasted. Check the project name or ID.",
}
# Remote agents in the terminal UI were verified on agy 1.5.17; other versions fail closed.
MIN_VERSION = (1, 5, 17)
_PROJECT = re.compile(r"^[^\s-][^\x00-\x1f\x7f]{0,199}$")


def validate_project(name: Optional[str]) -> Optional[str]:
    if name is None:
        return None
    if not _PROJECT.match(name) or not name.isprintable():
        raise UsageError("--remote-project must be a printable project name or ID (no leading '-')")
    return name


def check_pasteable(prepared) -> None:
    """The prompt is pasted into a terminal: refuse text that carries terminal control or invisible characters
    (it could end the paste early and type keystrokes into agy's UI)."""
    refusals: List[stage.Refusal] = []
    for entry in prepared.entries:
        refusals.extend(stage.check_terminal_text(entry.rel, entry.data))
    refusals.extend(stage.check_terminal_text("the task text", prepared.task.encode("utf-8")))
    if refusals:
        raise stage.Refused(refusals)


def argv_preview(exe: str, project: Optional[str], model: Optional[str], prompt_chars: int, quote) -> str:
    argv = model_runner.use().build_remote_argv(exe, project, model)
    return ("agy command (cwd = a new empty temporary folder, on a pseudo-terminal): %s\n"
            "The prompt (%d characters: rules, every file above inlined between delimiter lines, the task and the "
            "reply markers) would be pasted into agy's terminal UI, never put on the command line, and run in a "
            "Google-managed cloud environment. %s" % (" ".join(quote(a) for a in argv), prompt_chars, NOT_ENFORCED))


def require_version(version) -> None:
    if version[0] != MIN_VERSION[0] or version < MIN_VERSION:
        raise model_runner.UnsupportedAgy("--remote-agent needs agy %s or later in major version %d"
                                          % (".".join(map(str, MIN_VERSION)), MIN_VERSION[0]))


def send(exe: str, prepared, request, model: Optional[str], env, timeout, on_started):
    """Build the inlined prompt and run one remote turn; returns the RemoteRunResult."""
    agy = model_runner.use()
    require_version(agy.detect_version(exe, env))
    markers = remote_prompt.new_markers()
    prompt = remote_prompt.build_prompt(prepared.task, prepared.entries, markers)
    argv = agy.build_remote_argv(exe, request.remote_project, model)
    return agy.run_remote(argv, prompt, markers, env, timeout, request.max_out_bytes, on_started=on_started)


def capability(exe: str, env, cwd: str, version) -> str:
    """One doctor line about remote agents from 'agy --help' (local only: no sign-in, network or prompt)."""
    try:
        proc = model_runner.use().run_probe([exe, "--help"], env, cwd, timeout=20)
        listed = b"--remote-agent" in proc.stdout + proc.stderr
    except (OSError, model_runner.TimeoutExpired, model_runner.UnsupportedAgy):
        return "info remote agent: 'agy --help' could not be run"
    if not listed:
        return "info remote agent: this agy does not list --remote-agent; delegate --remote-agent is unavailable"
    try:
        require_version(version)
    except model_runner.UnsupportedAgy as exc:
        return "info remote agent: listed by agy, but %s" % exc
    return ("info remote agent: available (agy lists --remote-agent; the bridge drives its terminal UI because "
            "--print is not supported for remote agents yet). Sign-in and account access are checked only on use.")


def preview_prompt_chars(prepared) -> int:
    return len(remote_prompt.build_prompt(prepared.task, prepared.entries, remote_prompt.new_markers()))
