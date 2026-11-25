"""Parsers for the job inventory: `jobs` (with no subcommand, or `jobs list`) and `ps`. add_arguments returns the
`jobs` subcommand group so parser_implement.add_job_actions can add the job actions to it."""
from __future__ import annotations

import argparse

from ..app import jobs


def add_arguments(subparsers):
    group = subparsers.add_parser('jobs', help='inspect local background jobs', allow_abbrev=False,
                                  description='inspect local background jobs; with no subcommand, lists jobs '
                                              '(the same as jobs list)')
    group.set_defaults(handler=jobs.dispatch, active=False, repo=None, queue_dir=None, json=False,
                       allow_non_git_root=False)
    _inventory_arguments(group)
    commands = group.add_subparsers(dest='jobs_command')
    listing = commands.add_parser('list', help='list implementation jobs and a selected delegation queue',
                                  allow_abbrev=False)
    # SUPPRESS: `jobs -C X list` must keep the options parsed before `list`; argparse otherwise lets the
    # subparser's own defaults overwrite them.
    _inventory_arguments(listing, suppress=True)
    active = subparsers.add_parser('ps', help='show active local jobs', allow_abbrev=False)
    _inventory_arguments(active, active_default=True)
    active.set_defaults(inventory_command='ps')
    return commands


def _inventory_arguments(parser, *, active_default=False, suppress=False):
    parser.set_defaults(handler=jobs.dispatch)
    unset = {'default': argparse.SUPPRESS} if suppress else {}
    if active_default:
        parser.add_argument('--all', dest='active', action='store_false', default=True,
                            help='also show jobs without a live worker')
    else:
        parser.add_argument('--active', action='store_true', help='show only work with a live worker', **unset)
    parser.add_argument('-C', '--repo', metavar='ROOT',
                        help='filter implementation jobs and include the delegation queue for this repository',
                        **unset)
    parser.add_argument('--queue-dir', metavar='PATH', help='select a delegation queue under --repo', **unset)
    parser.add_argument('--allow-non-git-root', action='store_true',
                        help='allow --repo to select a delegation queue without a .git entry', **unset)
    parser.add_argument('--json', action='store_true', help='emit a versioned JSON inventory', **unset)
