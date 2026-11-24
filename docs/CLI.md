# Command line reference

```
bin/agy-bridge delegate --repo <root> --file <path>... [--glob <pattern>...] [--model <name>]
                        [--timeout 5m] [--max-bytes N] [--out <file>]
                        [--cite [--require-verified]] [--allow-fragment-file <path>] -- "<task>"
bin/agy-bridge dry-run  (same arguments; prints the manifest and the agy command, sends nothing)
bin/agy-bridge doctor   [--repo <root>] [--offline]
bin/agy-bridge doctor --implementation --profile <absolute path> [--repo <root>]
bin/agy-bridge events   [--repo <root>] [--tail N]
```

Queue commands are in [QUEUE.md](QUEUE.md). See [GRADING.md](GRADING.md) for `verdict`, `verdicts`, `grade-mutants`,
`apply-diff` and `stats`. `bin/agy-bridge <command> --help` lists the options. If you installed the package, replace
`bin/agy-bridge` with `agy-bridge` or `python3 -m agy_bridge`.

## Job inventory

```text
agy-bridge jobs
agy-bridge jobs list [--active] [-C ROOT] [--queue-dir PATH] [--allow-non-git-root] [--json]
agy-bridge ps [--all] [-C ROOT] [--queue-dir PATH] [--allow-non-git-root] [--json]
```

`jobs` and `jobs list` read existing local metadata without starting AGY, creating storage, or opening queued
delegation prompts. `ps` shows live work by default and `ps --all` includes job history. `-C` and `--repo` select a
project and include its delegation queue. Use `--queue-dir` for a custom queue. Use the explicit
`--allow-non-git-root` option to inspect a legacy delegation queue rooted in a non-Git project.

`--active` selects jobs with a live worker. It includes implementation jobs in `queued`, `running`, `checking`, or
`cancel_requested` while their worker holds the lock, and the current job of a running delegation queue. Pending queue
entries and an idle watcher do not count as active jobs. For a worker that exits before recording a result, the phase
is `interrupted` and the job is inactive. Use `jobs show` for a job in the foreground `applying` phase, which is
outside the active-worker filter.

With `--json`, stdout contains a single JSON object with `schema_version`, `jobs`, and `warnings`. The current schema
version is `1`. Each job includes `kind` (`implement` or `delegate`), `job_id`, `repository`, `phase`, `active`, and
`cancel_requested`. Inventory displays `cancel_requested` as the phase while detailed status keeps the worker phase
and sets the boolean flag. The `queue_dir`, `created_at`, `updated_at`, `started_at`, `last_agy_activity_at`, `attempt`, and
`bridge_exit` fields are `null` when unavailable. Timestamps are Unix seconds. Jobs with live workers appear first,
then by most recently updated. Warnings describe entries the inventory could not read safely.

An empty inventory is successful. The text output shows `No jobs.` or `No active jobs.` and sends warnings to stderr.
Use `jobs show <job-id>` for detailed implementation state. `status` and `queue status` retain their queue
report format. [Queue commands](QUEUE.md).

Every command with `--repo` also accepts `-C`. File selection in `delegate`, `dry-run`, and `jobs start` accepts
`-f` as a shortcut for `--file`.

## Implementation jobs

```text
agy-bridge jobs start [-C ROOT] [-f PATH]... [--glob PATTERN]... [--write-dir DIR]...
                      [--profile PATH] [--gemini-api-key] [--check JSON_ARGV]
                      [--model NAME] [--turn-timeout 25m] [--job-timeout 2h]
                      [--dry-run] [--foreground] -- "<task>"
agy-bridge jobs show <job-id>
agy-bridge jobs patch <job-id>
agy-bridge jobs cancel <job-id>
agy-bridge jobs resume <job-id> [--foreground]
agy-bridge jobs apply <job-id> --sha256 <reviewed patch hash>
```

`implement` and the `implement-*` names remain CLI entry points for the same operations. `jobs status` is an alias
for `jobs show`. `jobs start --dry-run` prints a JSON manifest from the current bytes selected by `--file` and
`--glob`. It needs a Git project and at least one matching file. A dry run makes no AGY call and creates no job. A submitted job requires an
absolute `--profile` path to a dedicated directory with mode `0700`. `--gemini-api-key` opts into forwarding the
existing environment variable `GEMINI_API_KEY`. The option is saved with the job. Supply `GEMINI_API_KEY` in the
environment again when resuming that job.

Use `doctor --implementation --profile <absolute path>` to check whether Bubblewrap can run the required AGY
version with that profile. The probe runs `agy --version` in a temporary workspace and does not create a job or
check authentication. Pass `--repo <root>` to keep executable discovery outside the selected project.

By default, the permitted output paths are the files already in the snapshot. When you supply `--write-dir`, edits
and new files must be under one of its paths. Use `--write-dir .` for the whole snapshot and its new files. `--check`
accepts a JSON array such as `'["python3", "-m", "unittest", "discover", "-s", "tests"]'`. The worker runs this
fixed command offline on a copy of the actual candidate. This command may also run against the baseline to record a
before-and-after result. `--turn-timeout` accepts 1 second to 1 hour. `--job-timeout` accepts 1 second to 24 hours per
invocation. Resume starts a new invocation of the same job. [Setup and examples](IMPLEMENT.md).

| Command or phase | Output and meaning |
|---|---|
| `jobs start`, `jobs resume` | Background start returns a JSON job ID and `queued`. `--foreground` waits and returns the worker exit code. |
| `jobs show` | JSON phase, `worker_active`, `cancel_requested`, Git base, attempt, available timestamps, recorded check exit codes, change list, and patch hash. |
| `jobs cancel` | Requests cancellation for the current invocation and returns `cancel_requested`. Check status for the outcome. |
| `jobs patch` | Writes the saved unified diff to stdout after checking its hash. Partial patches can be inspected once saved. |
| `ready`, `verified` | A patch is available for review. `verified` also records a successful configured check. |
| `checks_failed`, `no_changes` | The check failed after retries, or there were no file changes. |
| `canceled`, `timed_out`, `quota`, `auth_required`, `interrupted`, `failed` | The worker stopped. Resume can continue from the retained workspace unless subprocess cleanup was unconfirmed. |
| `applying` | Apply started. Use the same job ID and reviewed SHA-256 to resume an interrupted apply. |
| `jobs apply`, `applied` | Checks the approved hash and file contents, then writes the reviewed patch to the working tree. |

Background commands report submission success independently of the eventual job outcome. For a finished job,
`jobs show` shows `bridge_exit` when the worker recorded one. `check_exit` is the check's own exit code.
Application writes the source working tree. Use Git to stage the resulting files. Job state and patch files live
under `~/.agy-bridge/implement/`.

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
| 16 | output cap reached, or an implementation produced no changes or exhausted its check retries |
| 17 | unverified citation (only with `--cite --require-verified`) |
| 130 | interrupted or implementation canceled |

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
