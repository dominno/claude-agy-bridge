"""The top-level `agy-bridge` parser. Each command group adds its own subcommands, in --help order."""
from __future__ import annotations

import argparse

from .. import __version__
from . import parser_delegate, parser_workflow
from .arguments import Parser


def build_parser() -> argparse.ArgumentParser:
    p = Parser(prog="agy-bridge", description="Delegate read-only tasks or isolated implementation jobs to AGY.")
    p.add_argument("--version", action="version", version="agy-bridge " + __version__)
    sub = p.add_subparsers(dest="command", required=True, parser_class=Parser)
    parser_delegate.add_arguments(sub)
    parser_workflow.add_arguments(sub)
    from . import parser_implement, parser_jobs
    parser_implement.add_arguments(sub)
    parser_implement.add_job_actions(parser_jobs.add_arguments(sub))
    return p
