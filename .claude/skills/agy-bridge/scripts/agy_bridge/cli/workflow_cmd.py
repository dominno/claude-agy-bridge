"""cmd_workflow (checks the repository, then maps errors to exit codes) and the verdict, stats and grading handlers."""
from __future__ import annotations

import json
import math

from ..app import report
from ..core import config, events, stage
from ..core.config import UsageError
from .output import fail, refused, say


def cmd_verdict(args, root):
    from ..app import verdicts

    try:
        warning = verdicts.record(root, args.job, args.verdict, args.by, args.evidence, args.note, force=args.force,
                                  allow=args.allow_non_git_root, queue_dir=args.queue_dir,
                                  results_dir=args.results_dir)
    except verdicts.UnknownJob as exc:
        fail('verdict: ' + report.clean_answer(str(exc).encode('utf-8', 'replace')))
        return config.EXIT_USAGE
    if warning:
        fail('warning: ' + report.clean_answer(warning.encode('utf-8', 'replace')))
    say('verdict recorded for ' + args.job)
    return config.EXIT_OK


def cmd_verdicts(args, root):
    from ..app import verdicts

    if not 1 <= args.tail <= 10000:
        raise UsageError('--tail must be between 1 and 10000')
    for row in verdicts.rows(root, args.tail):
        say(json.dumps(row, ensure_ascii=True, sort_keys=True))
    return config.EXIT_OK


def cmd_stats(args, root):
    from ..app import stats

    if not math.isfinite(args.target) or args.target <= 0:
        raise UsageError('--target must be finite and positive')
    say(report.clean_answer(stats.render_results(root, args.results_dir, args.target).encode('utf-8')))
    return config.EXIT_OK


def cmd_grading(args, root):
    from ..app import grading, verdicts
    from ..core import grading_rules

    if args.command == 'apply-diff' and args.input == 'table':
        if not args.records:
            raise UsageError('table needs at least one JSON/JSONL record file')
        rows = []
        for path in args.records:
            rows.extend(json.loads(line) for line in grading.read_input(path).splitlines() if line.strip())
        say(report.clean_answer(grading_rules.diff_table(rows).encode('utf-8')))
        return config.EXIT_OK
    if not args.case:
        raise UsageError('--case is required')
    case = json.loads(grading.read_input(args.case))
    grading_rules.validate_case(root, case)
    if args.unsafe_no_sandbox:
        fail('WARNING: --unsafe-no-sandbox runs model-written code with your privileges and network access')
    if args.command == 'grade-mutants':
        if args.record and (not args.by or not case.get('job')):
            raise UsageError('--record requires --by and case.job')
        mutants = grading_rules.extract_mutants(grading.read_input(args.result))
        rows, table, records = grading.grade(root, case, mutants, unsafe_no_sandbox=args.unsafe_no_sandbox)
        say(report.clean_answer(table.read_bytes()))
        say('Table: %s\nJSONL: %s' % (table, records))
        if args.record:
            verdict = 'pass' if any(r['status'] == 'SURVIVES' for r in rows) else 'fixes' if any(r['status'] == 'TRIVIAL' for r in rows) else 'reject'
            verdicts.record(root, case['job'], verdict, args.by, str(table))
        return config.EXIT_OK
    if args.records:
        raise UsageError('unexpected record arguments outside table')
    raw = grading.read_input(args.input)
    diff = raw if args.input.endswith(('.diff', '.patch')) else grading_rules.extract_diff(raw)
    say(json.dumps(grading.apply_diff(root, case, diff, unsafe_no_sandbox=args.unsafe_no_sandbox), ensure_ascii=True))
    return config.EXIT_OK


def cmd_workflow(args):
    try:
        root = stage.resolve_repo(args.repo)
        stage.check_root(root, args.allow_non_git_root)
        return args.workflow(args, root)
    except (UsageError,ValueError,TypeError,UnicodeError) as exc:
        # JSON decoder errors can include untrusted material; never echo that input.
        if isinstance(exc, json.JSONDecodeError):
            detail = 'invalid JSON'
        elif isinstance(exc, (UsageError, ValueError)) and not isinstance(exc, UnicodeError):
            detail = report.clean_answer(str(exc).encode('utf-8', 'replace'))
            from ..core.scan import scan_bytes
            if scan_bytes(detail.encode('utf-8')):
                detail = 'invalid input (detail redacted)'
        else:
            detail = type(exc).__name__
        fail('invalid workflow input: '+detail)
        return config.EXIT_USAGE
    except (stage.Refused,events.EventError,OSError,report.OutPathRefused) as exc:
        operation = args.command + (' ' + args.queue_command if args.command == 'queue' else '')
        refused(exc, operation, getattr(args, 'out', None))
        return config.EXIT_REFUSED
