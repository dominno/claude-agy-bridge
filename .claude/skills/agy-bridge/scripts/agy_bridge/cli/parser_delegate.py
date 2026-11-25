"""Parsers for the read-only delegation group: delegate, dry-run, events and doctor."""
from __future__ import annotations

from ..core import config
from .delegate_cmd import cmd_delegate, cmd_events
from .doctor_cmd import cmd_doctor


def add_arguments(sub) -> None:
    _delegate(sub)
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


def _delegate(sub) -> None:
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
        d.add_argument("--remote-agent", action="store_true",
                       help="run the task on an agy remote agent (Google cloud); the files are inlined in the prompt")
        d.add_argument("--remote-project", help="with --remote-agent: remote project name or ID (default: agy's)")
        if name == "delegate":
            d.add_argument("--dry-run", action="store_true", help="show what would be sent and stop")
        d.add_argument("task", nargs="*", help='put the task after "--"')
