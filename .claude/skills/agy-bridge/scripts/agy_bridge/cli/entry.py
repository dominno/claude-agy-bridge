"""main(): parse the command line, install the stop handlers and run the chosen handler."""
from __future__ import annotations

import sys
from typing import List, Optional

from . import signals
from .arguments import Parser
from .output import fail
from .parser import build_parser


def main(argv: Optional[List[str]] = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if arguments and arguments[0] == '_implement-worker':
        from ..app import implement
        parser = Parser(prog='agy-bridge _implement-worker', add_help=False)
        parser.add_argument('job')
        parser.add_argument('lock_fd', type=int)
        parser.set_defaults(command='_implement-worker', handler=implement.dispatch)
        args = parser.parse_args(arguments[1:])
    else:
        args = build_parser().parse_args(arguments)
    signals.install_stop_handlers()
    try:
        return args.handler(args)
    except KeyboardInterrupt:
        fail("interrupted; the staging folder was removed")
        return 130
