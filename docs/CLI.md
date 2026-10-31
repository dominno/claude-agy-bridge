# Command line reference

```
bin/agy-bridge delegate --repo <root> --file <path>... [--glob <pattern>...] [--model <name>]
                        [--timeout 5m] [--max-bytes N] [--out <file>]
                        [--cite [--require-verified]] [--allow-fragment-file <path>] -- "<task>"
bin/agy-bridge dry-run  (same arguments; prints the manifest and the agy command, sends nothing)
bin/agy-bridge doctor   [--offline]
bin/agy-bridge events   [--repo <root>] [--tail N]
```

Queue commands are in [QUEUE.md](QUEUE.md); `verdict`, `verdicts`, `grade-mutants`, `apply-diff` and `stats` are in
[GRADING.md](GRADING.md). `bin/agy-bridge <command> --help` lists every option.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | ok |
| 1 | usage |
| 2 | `agy` failed |
| 3 | empty answer |
| 4 | refused by a rule |
| 10 | quota |
| 11 | auth |
| 12 | timeout |
| 13 | `agy` missing |
| 14 | unsupported `agy` version |
| 15 | soft permission denial |
| 16 | output cap reached |
| 17 | unverified citation (only with `--cite --require-verified`) |

## Output and repo config

`--out` may point only into the temp folder or `<repo>/.agy-bridge/` (add that folder to your ignore file). A repo-local
`.agy-bridge.json` with `{"deny": ["pattern"], "context_files": ["docs/project-facts.md"]}` can add denylist rules and
select optional queue context files. It never launches commands or removes rules.

## Input notices

The result header lists `FILES NOT SENT: <path> (<rule>: <reason>)` for skipped or refused files. Rule R5 (key-shaped
content, see [THREAT_MODEL.md](THREAT_MODEL.md)) adds the one-based line of the first hit, never the match. A denylisted
glob directory is listed as pruned; its children are not inspected. Explicit refusals still stop the whole task, with
other selected files labelled `TASK: whole delegation was not sent`. Missing files keep exit 1; rule refusals keep exit 4.

## Example fragments

`--allow-fragment-file <path>` reads a small UTF-8 file inside the repo, one literal per line (minimum 12
characters). Only these two exact literals are supported, including their backticks:

```text
`EXAMPLE_API_KEY`
`SERVICE_API_KEY`
```

They exempt only those inline-code variable-name mentions in selected file contents. Assignments, actual key
shapes, denylisted paths, task text and generated task text still refuse. The allow file is not automatically
sent. Other literals are rejected without echoing them. See [the threat model](THREAT_MODEL.md).

## Citations

`--cite` appends an instruction to return lines in this form:

```text
FILE: src/example.py | QUOTE: "verbatim text, max 25 words" | CLAIM: the model's interpretation
```

Every returned line is labelled VERIFIED or UNVERIFIED, with counts and reasons. Verification checks only that
the whitespace-normalised quote occurs in that exact sent file buffer. It never verifies CLAIM. Malformed
lines, quotes shorter than 8 normalised characters or over 25 words, unavailable files and absent quotes are
unverified. Trailing blank lines are ignored; other lines remain visible even for long answers.
Add `--require-verified` to exit 17 for any unverified line; existing provider errors take
precedence. Without it, citation findings do not change the exit code.

## Event journal

Each delegate or dry run on a safely validated repo appends one JSON line to `<repo>/.agy-bridge/events.jsonl`:
UTC time, model, sent paths/full SHA-256/byte counts, `not_sent` paths/rules, total sent bytes, elapsed seconds,
exit code, truncation, dry-run flag, and citation counts when requested. No task, answer or file contents are
stored. Dry runs and pre-send refusals have empty sent-file lists and zero sent bytes. Key-shaped filenames are
redacted; outside-repo paths appear as `<outside repo>`. Add `.agy-bridge/` to the ignore file of every target repo.
The folder is 0700 and the journal 0600; unsafe links or file types refuse before calling agy.

`bin/agy-bridge events --repo <root> --tail 10` accepts 1–10,000 records (default 10) and prints the last records in
chronological order, without running agy or creating a journal. Invalid arguments before command dispatch, invalid/unsafe
repo roots and unsafe/unwritable event paths cannot be recorded there. Unsafe targets refuse before sending; ordinary
journal I/O failures warn and preserve the delegation's real exit code. Tail rejects records above 64 KiB.
Normal delegates also keep a metadata-only log at `~/.agy-bridge/log.jsonl`.
