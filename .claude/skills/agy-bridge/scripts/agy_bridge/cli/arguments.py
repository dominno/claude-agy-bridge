"""The argparse parser class every command uses, and the repository options the workflow commands share."""
from __future__ import annotations

import argparse
import sys

from ..core import config


class Parser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("allow_abbrev", False)
        super().__init__(*args, **kwargs)

    def error(self, message):  # argparse exits 2 by default; 2 means "agy failed" here
        self.print_usage(sys.stderr)
        sys.stderr.write("agy-bridge: usage error: %s\n" % message)
        raise SystemExit(config.EXIT_USAGE)


def repo_options(parser, folders=False):
    parser.add_argument('-C', '--repo', default='.')
    parser.add_argument('--allow-non-git-root', action='store_true')
    if folders:
        parser.add_argument('--queue-dir')
        parser.add_argument('--results-dir')
