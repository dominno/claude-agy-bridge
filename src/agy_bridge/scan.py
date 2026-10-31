"""R5: the content scan (plan section 5). Rules are data; the scan works on a buffer.

The scan sees exactly the bytes that will be written to staging, so there is no
window between "checked" and "copied".
"""
from __future__ import annotations

import re
from bisect import bisect_right
from dataclasses import dataclass
from typing import Dict, Sequence


@dataclass(frozen=True)
class SecretRule:
    id: str
    pattern: str
    positive: str  # fake text that must match
    negative: str  # harmless text that must not match any rule
    ignorecase: bool = False


# All sample "secrets" below are invented strings.
# Every repetition below is bounded and no group is repeated, so the worst case per start position is
# constant and the scan is linear in the input. tests/test_scan.py checks this structurally and by timing.
_KEY = r"(?:api[_-]?key|secret|token|password|passwd|credential)"
_SUFFIX = r"(?:[_\-][A-Za-z0-9_\-]{0,30})?"  # tokenizer_name, tokens, passwordHash are not secrets' names
_AFTER = r"['\"\]]{0,3}[ \t]{0,20}"

SECRET_RULES: Sequence[SecretRule] = (
    # Variable mentions are sensitive even without a value; no provider-specific names.
    SecretRule("api-key-variable", r"[A-Za-z0-9_]{1,64}_API_KEY",
               "SERVICE_API_KEY=abc", "service documentation", True),
    SecretRule("credential-variable", r"(?<![A-Za-z0-9_])[A-Z0-9_]{1,64}_(?:SECRET|TOKEN)(?![A-Za-z0-9_])",
               "SERVICE_SECRET", "max_tokens and client_secret"),
    SecretRule("private-key-block", r"-----BEGIN (?:[A-Z]{1,20} ){0,4}PRIVATE KEY(?: BLOCK)?-----",
               "-----BEGIN RSA PRIVATE KEY-----", "-----BEGIN PUBLIC KEY-----"),
    SecretRule("sk-token", r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_\-]{20,512}",
               "key = 'sk-proj-abcdefghij_abcdefghijklmnop'", "risk-averse-behaviour-in-markets and sk-short"),
    SecretRule("sk-underscore-token", r"(?<![A-Za-z0-9])sk_[A-Za-z0-9]{32,512}",
               "sk_0123456789abcdef0123456789abcdef", "task_0123456789abcdef0123456789abcdef and sk_short"),
    SecretRule("stripe-key", r"(?<![A-Za-z0-9])[rs]k_(?:live|test)_[A-Za-z0-9]{10,512}",
               "rk_live_ABCDEFGHIJKLMN", "rk_live_x"),
    SecretRule("aws-access-key", r"AKIA[0-9A-Z]{16}", "AKIAABCDEFGHIJKLMNOP", "AKIA is a prefix"),
    SecretRule("aws-temporary-key", r"(?<![A-Za-z0-9])ASIA[0-9A-Z]{16}", "ASIAABCDEFGHIJKLMNOP",
               "ASIA is a continent, ASIAN markets"),
    SecretRule("google-api-key", r"AIza[0-9A-Za-z_\-]{30,512}", "AIza" + "a" * 35, "AIza" + "a" * 10),
    SecretRule("google-oauth-secret", r"GOCSPX-[A-Za-z0-9_\-]{20,512}", "GOCSPX-" + "a" * 28, "GOCSPX-short"),
    SecretRule("google-access-token", r"(?<![A-Za-z0-9])ya29\.[A-Za-z0-9_\-]{20,512}", "ya29." + "a" * 30,
               "ya29.short"),
    SecretRule("gitlab-token", r"glpat-[A-Za-z0-9_\-]{20,512}", "glpat-" + "a" * 24, "glpat-short"),
    SecretRule("sendgrid-key", r"(?<![A-Za-z0-9])SG\.[A-Za-z0-9_\-]{16,128}\.[A-Za-z0-9_\-]{16,128}",
               "SG." + "a" * 22 + "." + "b" * 43, "SG.short.short and SG.a"),
    SecretRule("slack-token", r"xox[abprs]-[A-Za-z0-9-]{10,512}", "xoxb-1234567890-abc", "xoxb-1"),
    SecretRule("github-token", r"gh[pousr]_[A-Za-z0-9]{20,512}", "gho_abcdefghijklmnopqrstuv", "ghp_short"),
    SecretRule("github-pat", r"github_pat_[A-Za-z0-9_]{22,512}", "github_pat_11ABCDEFG0abcdefghijklmn",
               "github_pat_short"),
    SecretRule("url-credentials",
               r"[A-Za-z][A-Za-z0-9+.\-]{1,15}://[^\s:/@]{0,100}:[^\s/@]{3,100}@",
               "redis://:hunter2pw@cache.example:6379", "https://example.com:8080/a and http://user@host/x"),
    SecretRule("slack-webhook", r"hooks\.slack\.com/services/T[A-Za-z0-9]{1,64}/B[A-Za-z0-9]{1,64}/[A-Za-z0-9]{8,64}",
               "hooks.slack.com/services/T00000000/B00000000/XXXXXXXXXXXXXXXXXXXXXXXX",
               "hooks.slack.com/services/docs"),
    SecretRule("huggingface-token", r"(?<![A-Za-z0-9])hf_[A-Za-z0-9]{30,512}", "hf_" + "a" * 34,
               "hf_short and shelf_" + "a" * 34),
    SecretRule("npm-token", r"(?<![A-Za-z0-9])npm_[A-Za-z0-9]{30,512}", "npm_" + "a" * 36,
               "npm_install and pnpm_" + "a" * 36),
    # Linear JWT: a segment can only start after a non-token character, so a long run is scanned once.
    SecretRule("jwt", r"(?<![A-Za-z0-9_\-])eyJ[A-Za-z0-9_\-]{8,2048}\.[A-Za-z0-9_\-]{8,2048}\.[A-Za-z0-9_\-]{8,2048}",
               "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcdefghijk", "eyJ.a.b and eyJabc.def.ghi"),
    SecretRule("bearer-token", r"Bearer[ \t]{1,5}[A-Za-z0-9_\-.=+/]{20,512}",
               "authorization: bearer abcdefghij0123456789xyz", "Bearer token and bearer abc", ignorecase=True),
    SecretRule("basic-auth-header",
               r"authorization[\"']?[ \t]{0,5}[:=][ \t]{0,5}[\"']?basic[ \t]{1,5}[A-Za-z0-9+/]{8,512}",
               "Authorization: Basic dXNlcjpwYXNzd29yZA==", "basic information about authorization",
               ignorecase=True),
    SecretRule("netrc-entry",
               r"machine[ \t]{1,5}[^\s]{1,100}[ \t\r\n]{1,10}login[ \t]{1,5}[^\s]{1,100}[ \t\r\n]{1,10}"
               r"password[ \t]{1,5}[^\s]{3,100}",
               "machine example.com login bob password s3cret", "machine learning models and login pages",
               ignorecase=True),
    SecretRule("assigned-secret",
               _KEY + _SUFFIX + _AFTER + r"[:=][ \t]{0,20}['\"]?[A-Za-z0-9_\-+/=]{16,512}",
               "password = 'abcdefghij0123456789'", "token = short and secret: str", ignorecase=True),
    SecretRule("assigned-secret-json",
               r"\"[A-Za-z0-9_\-]{0,30}(?:api[_-]?key|secret|token|password|passwd|private_key)[A-Za-z0-9_\-]{0,30}\""
               r"[ \t\r\n]{0,20}:[ \t\r\n]{0,20}\"[^\"]{12,4096}\"",
               '{"token": "ya29.a0AfH6SMBxxxxxxxx"}', '{"client_secret": "", "token": "short"}', ignorecase=True),
    SecretRule("assigned-quoted-secret",
               _KEY + _SUFFIX + _AFTER + r"[:=][ \t]{0,20}(['\"])(?![$\{])[^'\"\s]{8,128}\1",
               'password = "Tr0ub4dor&3!horse"', 'password = "${DB_PASSWORD}" and token = "two words here"',
               ignorecase=True),
    SecretRule("assigned-unquoted-special",
               _KEY + _SUFFIX + _AFTER + r"[:=][ \t]{1,20}(?=[A-Za-z0-9_\-+/=]{0,64}[&!@#%^*~?;,])"
               r"[A-Za-z0-9_\-+/=&!@#%^*~?;,]{10,128}",
               "password: Tr0ub4dor&3horse!!", "password: Optional[str] and token_count = 1234567890",
               ignorecase=True),
)

_COMPILED = [(r.id, re.compile(r.pattern.encode("ascii"), re.I if r.ignorecase else 0)) for r in SECRET_RULES]


# Only these exact backtick-wrapped variable-name examples may be exempted.
# Provider tokens, private-key headers and assigned values can never be exempted.
EXAMPLE_FRAGMENTS = frozenset(("`EXAMPLE_API_KEY`", "`SERVICE_API_KEY`"))
_EXAMPLE_ASSIGNMENT = re.compile(rb"[\"']?\s*[:=]")
_VALUE_RUN = re.compile(rb"[A-Za-z0-9_\-+/=]{16}")


def _example_context(buf):
    """Index value signals once, including each line's next nonblank line.

    Fixed-width token matches avoid rescanning long suffixes for every example.
    Store the rightmost signal: any such signal after a mention defeats exemption.
    """
    starts, last_tokens, nonblank = [], [], []
    offset = 0
    for line in buf.splitlines(keepends=True):
        starts.append(offset)
        last_tokens.append(max((offset + m.start() for m in _VALUE_RUN.finditer(line)), default=-1))
        nonblank.append(bool(line.decode('utf-8', 'replace').strip()))
        offset += len(line)
    next_tokens = [-1] * len(starts)
    following = -1
    for index in range(len(starts) - 1, -1, -1):
        next_tokens[index] = following
        if nonblank[index]:
            following = last_tokens[index]
    return starts, last_tokens, next_tokens


def _matches(buf: bytes, allow_fragments=()):
    allowed = {fragment.encode("ascii") for fragment in allow_fragments
               if fragment in EXAMPLE_FRAGMENTS}
    starts, last_tokens, next_tokens = _example_context(buf) if allowed else ([], [], [])
    for rule_id, regex in _COMPILED:
        for match in regex.finditer(buf):
            if rule_id == "api-key-variable" and match.start():
                envelope = buf[match.start()-1:match.end()+1]
                before = buf[match.start()-2:match.start()-1] if match.start() > 1 else b""
                after = buf[match.end()+1:match.end()+2]
                if (envelope in allowed and not re.search(rb"[A-Za-z0-9_]", before + after)
                        and not _EXAMPLE_ASSIGNMENT.match(buf, match.end()+1)):
                    line = bisect_right(starts, match.start()) - 1
                    if last_tokens[line] < match.end()+1 and next_tokens[line] < 0:
                        continue
            yield rule_id, match.start()


def first_hit_line(buf: bytes, allow_fragments=()) -> int:
    start = min((start for _, start in _matches(buf, allow_fragments)), default=None)
    return len(re.findall(rb"\r\n|\r|\n", buf[:start])) + 1 if start is not None else 0


def scan_bytes(buf: bytes, allow_fragments=()) -> Dict[str, int]:
    """Return {rule id: match count} for every rule that matches. Never returns the match."""
    hits: Dict[str, int] = {}
    for rule_id, _ in _matches(buf, allow_fragments):
        hits[rule_id] = hits.get(rule_id, 0) + 1
    return hits


def rule_regexes():
    """(id, compiled bytes regex) for each rule; used by the timing tests."""
    return list(_COMPILED)
