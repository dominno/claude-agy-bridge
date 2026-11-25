"""The queue job model: job fields, result and refusal formats, and the sanitised reasons a job is refused."""
from __future__ import annotations

import hashlib
import json

from . import config, stage
from .scan import scan_bytes
from .verdict import REFUSAL_HEADING

MAX_JOB_BYTES = 128 * 1024
MAX_RESULT_BYTES = 8 * 1024 * 1024
MAX_NEED_LINE = 1024
MAX_NEED_CANDIDATES = 100
MAX_NEED_TEXT = 64 * 1024
RESULT_WARNING = 'unverified model output: verify claims against original sources before use.'
RESULT_FORMAT = '<!-- agy-bridge-result-v2 -->'
# Line 2 of a refusal result (an invalid job, a refused run or `queue cancel`). 'sent: no' means nothing of the job
# was sent to agy, so a reader such as xq can treat the job as undelivered. 'sent: maybe' marks the rare refusal
# after an agy attempt had started (for example the result could not be published).
REFUSAL_FORMAT = '<!-- agy-bridge-refusal-v1 sent: %s -->'
RESULT_CHECKSUM = '\n<!-- agy-bridge-result-sha256: '

JOB_FIELDS = ('name', 'model', 'task', 'files', 'timeout', 'cite', 'context', 'need')
MAX_SHOWN_FIELDS = 10


def refusal_text(message, sent=False):
    """A refusal result: the heading (kept on line 1 for older readers), the machine marker and the message."""
    return '%s\n%s\n\n%s\n' % (REFUSAL_HEADING, REFUSAL_FORMAT % ('maybe' if sent else 'no'), message)


def _interrupted(text, name, digest):
    """A result this exact job began to write but whose checksum footer is missing or wrong."""
    start = '# '+name+'\n\n'+RESULT_WARNING+'\n\n'+RESULT_FORMAT+'\njob_sha256: '+digest+'\n'
    if not text.startswith(start):
        return False
    body, separator, checksum = text.rpartition(RESULT_CHECKSUM)
    return not separator or checksum != hashlib.sha256(body.encode('utf-8')).hexdigest() + ' -->\n'


def result_head(job, digest, rounds, attempts, code):
    """The result header: job, model, rounds, exit codes and the totals of what was sent."""
    success = [r for r in rounds if r['item']['exit_code']==0]
    retained = (success or rounds)[-1]['item']
    sent = {f['path']:f['bytes'] for a in attempts for f in a.get('files', [])}
    head = ['# '+job['name'], '', RESULT_WARNING, '', RESULT_FORMAT,
            'job_sha256: '+digest, 'model: '+(job.get('model') or 'default'), 'round: '+str(len(rounds)),
            'exit_code: '+str(code), 'retained_exit_code: '+str(retained['exit_code']),
            'bytes: '+str(sum(sent.values())), 'seconds: '+str(retained['seconds']),
            'quota exits: '+str(sum(a['exit_code']==10 for a in attempts)),
            'timeout exits: '+str(sum(a['exit_code']==12 for a in attempts))]
    if len(rounds)==2 and code != 0:
        head.append('status: round 2 failed; earlier answer retained when successful')
    for r in rounds:
        head += ['round %d bytes: %s' % (r['number'],r['item'].get('total_bytes',0)),
                 'round %d seconds: %s' % (r['number'],r['item']['seconds'])]
    return head


def result_text(job, digest, rounds, attempts, code):
    """The full result file body (before its checksum footer): header, one block per attempt, one per round."""
    head = result_head(job, digest, rounds, attempts, code)
    for n,a in enumerate(attempts,1):
        cite = a.get('cite', {})
        head += ['', '### Attempt %d (round %d)' % (n,a['round']),
                 'exit_code: '+str(a['exit_code']), 'bytes: '+str(a.get('total_bytes',0)),
                 'seconds: '+str(a['seconds']), 'files sent: '+', '.join(stage.printable(f['path']) for f in a.get('files',[])),
                 'VERIFIED: '+str(cite.get('verified',0)), 'UNVERIFIED: '+str(cite.get('unverified',0))]
        head += ['FILES NOT SENT: %s (%s)' % (stage.printable(r['path']), stage.printable(r['rule']))
                 for r in a.get('not_sent',[])]
        if a.get('diagnostics'):
            head += ['Bridge diagnostics:', a['diagnostics']]
    for r in rounds:
        head += ['', '## Round '+str(r['number']), 'files added: '+', '.join(stage.printable(f) for f in r['added']), '',
                 r['item'].get('rendered_cite') or r['item']['answer']]
    return '\n'.join(head)+'\n'


MAX_CANCEL_REASON = 200
MAX_REASON_CHARS = 600


def invalid_reason(exc):
    """The specific, sanitized reason a queued job was refused: escaped, bounded and never key-shaped.

    JSON decoder messages can quote job bytes, so they become 'invalid JSON'.
    """
    if isinstance(exc, json.JSONDecodeError):
        return 'invalid JSON'
    if isinstance(exc, RecursionError):
        return 'job JSON is nested too deeply'
    if isinstance(exc, config.UsageError) and 'conflict' in str(exc):
        return 'reused name conflict'
    prefix = 'queue run refused: '
    lines = [line.strip() for line in stage.explain(exc, 'queue run')]
    if isinstance(exc, stage.Refused):
        lines = lines[1:]  # drop the "refused by a rule (N problem(s)):" heading; keep each rule line
    elif lines[0].startswith(prefix):
        lines[0] = lines[0][len(prefix):]
    # stage.explain already escaped every name and detail with stage.printable().
    reason = '; '.join(line[2:] if line.startswith('- ') else line for line in lines if line)
    if len(reason) > MAX_REASON_CHARS:
        reason = reason[:MAX_REASON_CHARS] + '...'
    # explain() replaces an empty or key-shaped detail with the exception type name.
    if not reason or reason == type(exc).__name__ or scan_bytes(reason.encode('utf-8', 'replace')):
        return 'invalid input (detail redacted)'
    return reason


def _display_name(value):
    return '[redacted key-shaped filename]' if scan_bytes(value.encode('utf-8', 'replace')) else stage.printable(value)
