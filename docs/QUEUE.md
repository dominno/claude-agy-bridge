# Job queue

The queue is built into agy-bridge; no repository scripts or external queue service are needed.
All execution is serial. The existing delegate implementation handles every attempt, including its
read-once scan/staging, five-name agy environment, output cap, version/argv checks and metadata events.

```sh
bin/agy-bridge queue add job.json --repo /path/to/repo
bin/agy-bridge queue run --repo /path/to/repo
bin/agy-bridge queue watch --repo /path/to/repo --poll 5
bin/agy-bridge queue status --repo /path/to/repo --threshold 300
bin/agy-bridge status --repo /path/to/repo
```

`add -` accepts JSON on stdin. `add` validates a job and writes it exclusively; existing names in pending,
archive or results are refused. Producers can also place complete `.job` JSON files into the queue, named
`<name>.job`; write elsewhere then rename into place to avoid readers seeing an incomplete file. The runner
validates these again. Invalid jobs are archived as failed when they are safe regular files; unsafe filesystem
objects are skipped with a refusal result. Deeply nested/invalid JSON also cannot stop later jobs.
Archive collisions use numeric suffixes; refusal results use `<name>.1.md`, `<name>.2.md`, etc. as needed.
The runner reports an archive only after moving the file successfully. Unsafe entries left pending are
reported once per unchanged entry per watcher session. `run` drains jobs in filename order; `watch` polls
until interrupted. Neither starts a service on installation. The root checks are the same as delegate; `--allow-non-git-root` has the same limited meaning.

## Job format

```json
{
  "name": "review-parser-01",
  "model": "gemini-3.8-flash-low",
  "task": "Review the parser against its tests. If a source is missing, write NEED: path/to/file on its own line.",
  "files": ["src/parser.py", "tests/test_parser.py"],
  "timeout": "5m",
  "cite": false,
  "context": true,
  "need": false
}
```

`name`, `task`, `files` are required. Names start with an ASCII letter/digit and contain only letters, digits,
dots, underscores and hyphens (maximum 120 characters). Files are explicit repository-relative paths, no globs,
no traversal, existing regular single-link files with no symlink components; the built-in and extra denylist
applies. Duplicate files are sent once. Unknown fields refuse, including any command field. `model` defaults
to agy's default; `timeout` defaults to `5m` (integer seconds or delegate duration syntax, 1–3600 seconds).
`cite` defaults to false; all queue attempts request citation checks, while true also requires all lines to be
verified (exit 17). Structured reports can use false: their non-citation lines are visibly annotated.
`context` defaults to true and can also be false or `"short"`. `need` is a boolean, default false; only
`"need": true` authorizes the model to request additional files for a second round.

## Folders and config

Defaults under the validated repository:

```text
.agy-bridge/queue/*.job
.agy-bridge/queue/done/*.job
.agy-bridge/queue/failed/*.job
.agy-bridge/queue/watcher.pid
.agy-bridge/results/<name>.md
.agy-bridge/events.jsonl
.agy-bridge/verdicts.jsonl
```

Add `.agy-bridge/` to your ignore file. `--queue-dir` and `--results-dir` accept repository-relative paths
or absolute paths inside that repository. They must be separate, nonoverlapping directories with no symlinks
and no denylisted components. The reserved `.agy-bridge/` prefix is allowed for workflow storage only; its
contents remain denied as delegation inputs. Files are created exclusively, mode 0600, directories mode 0700.
No result is silently overwritten. Parent traversal is anchored with no-follow directory descriptors.
Built-in add/read operations also briefly lock the queue directory so a running watcher cannot observe an
incomplete job being written by `add`.

`.agy-bridge.json` supports only additive `deny` and optional `context_files`:

```json
{"deny": ["private-notes"], "context_files": ["docs/project-facts.md", "docs/review-rules.md"]}
```

Context files must be repository-relative and satisfy the same file safety rules. They are prepended and
deduplicated before each job; `context: false` sends none and `context: "short"` selects only the first.
The repository maintains these files itself. Configuration never launches a generator or other command.

## Locking and stop behavior

Both run and watch hold an exclusive advisory lock on `watcher.pid` (nonblocking probes retried for up to
200 ms). A second runner for that queue refuses with exit 4. The persistent file contains PID, mode and active job/start time; the lock, rather
than PID existence, determines liveness. A stale file from a crash does not block a new runner. Status does
not create directories or lock files; it briefly probes an existing lock. Runner retries tolerate that probe.
Different configured queues can run independently.

SIGINT, SIGTERM and SIGHUP unwind delegate's existing cleanup, kill the agy process group and leave the
unfinished job pending (exit 130). If a complete result was saved before interruption of the archive move,
the next run archives that existing result only when its `job_sha256` matches the exact pending job bytes
and no archive already uses that name. Reused names or incomplete/unrecognizable results are refused with
a new, numerically suffixed result; old answers are preserved and never used to close a new job. A hard crash
during sending cannot establish whether a remote response was produced. The lock prevents simultaneous sends, not exactly-once remote delivery.

## Rounds, retries and result evidence

Quota exit 10 gets at most three retries per round after 5, 10 and 20 seconds. A timeout (12) is recorded and
never retried. R5 can drop only the exact input files named by the delegate scan and retry at most twice per
round, retaining a nonempty file set. Task/generated-task refusals cannot be bypassed by dropping files.
Every attempt remains visible, including omitted paths and citation counts. No scan rule is disabled.

With explicit `"need": true`, a successful or citation-unverified first answer can request a second round
with `NEED: <repo path>` lines.
At most three distinct additional files are accepted: existing regular single-link files inside the repository,
no traversal, absolute paths, symlinks or denylisted components. Lines over 1024 characters (including
citation annotations) are ignored before parsing. At most 100 NEED candidates and 64 KiB of candidate
text (including annotations) are examined for filesystem reads; parsing stops when either limit is reached.
Parsing is linear even at the 1 MiB answer cap.
There is no source-directory allowlist: opt-in permits any three files passing these checks, including
confidential non-key files unrelated to the task. Leave `need` false unless you authorize that scope.
The actual delegate scans them again before sending. There are at most two rounds, with independent bounded
retry counters. A successful first answer is retained and archived as done if round two fails; both rounds
and the second failure remain in the result. Exit 17 by itself is not a successful verified answer.

The result says **unverified model output**. Its header records model, distinct sent-file bytes, retained
answer seconds, job SHA-256, final attempt exit, retained exit, per-round measurements, attempts, sent paths, FILES NOT SENT,
and VERIFIED/UNVERIFIED counts per attempt. Facts come directly from that delegate invocation, even if journal
appending failed; they are not guessed from the last event or model prose. The queue stores model output
as text. Separately invoked `grade-mutants`/`apply-diff` require a sandbox preflight before checks or patching:
the case's Python runs isolated `print(1)` with a 10-second execution timeout in a new session (non-Python
cases use the bridge's Python). Cleanup waits at most one second after killing the process group, then
abandons and reports a child that cannot exit. Missing, refused, crashing or hung confinement reports
"sandbox unavailable on this machine" and refuses execution unless the caller explicitly passes
`--unsafe-no-sandbox`, which skips preflight and runs with the caller's privileges and network access.
Sandboxed patch/git startup failures, signals and timeouts abort measurement too. See [GRADING.md](GRADING.md).

## Exits

Existing delegate codes are unchanged: 0 ok, 1 usage/invalid job, 2 agy failure, 3 empty, 4 rule/path/lock refusal,
10 quota, 11 auth, 12 timeout, 13 missing agy, 14 unsupported version, 15 soft denial, 16 output cap, 17 unverified
citation. A one-shot run returns the last nonzero job code, or 0 if all jobs succeeded (including a retained
successful first round). An interrupted runner returns 130. The watcher reports each job's code and continues.
