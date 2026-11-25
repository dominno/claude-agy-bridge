# Job queue

The queue is built into agy-bridge; no repository scripts or external queue service are needed.
All execution is serial. The existing delegate implementation handles every attempt, including its
read-once scan/staging, five-name agy environment, output cap, version/argv checks and metadata events.
For work that edits files, submit an implementation job through `implement` or `/agy-bridge implement`. It has its own job ID,
status, cancellation, resume and patch review commands. The read-only queue accepts delegation jobs. [Implementation
setup and commands](IMPLEMENT.md).

```sh
scripts/agy-bridge queue add --dry-run job.json --repo /path/to/repo
scripts/agy-bridge queue add job.json --repo /path/to/repo
scripts/agy-bridge queue run --repo /path/to/repo
scripts/agy-bridge queue watch --repo /path/to/repo --poll 5
scripts/agy-bridge queue watch --detach --repo /path/to/repo --poll 5
scripts/agy-bridge queue status --repo /path/to/repo --threshold 300
scripts/agy-bridge queue cancel <name> --repo /path/to/repo [--reason "task dropped"]
scripts/agy-bridge status --repo /path/to/repo
scripts/agy-bridge jobs list --repo /path/to/repo --active
```

`jobs list --repo ROOT` shows job IDs and phases for the default queue under `ROOT` alongside implementation jobs for
that project. Pass `--queue-dir PATH` to inspect a different queue or `--json` for the machine-readable inventory.
`--active` shows the delegation currently being processed. A watcher waiting for work has no active job.
`queue status` and `status` still show the watcher, queue counts, and results awaiting a verdict.
[Job inventory and JSON format](CLI.md#job-inventory).

`add --dry-run` validates the job and prints its real first-round manifest without adding or sending anything:
the `.agy-bridge.json` `context_files` the job would include (all, none with `"context": false`, the first with
`"short"`), the job files, whether a NEED round is allowed, whether the name is free, and the same file list,
byte counts, hashes and `FILES NOT SENT` notices as `dry-run` after the denylist and content scan. A refusal there
(exit 4) predicts a refusal or an R5 file drop when the job runs.

`queue cancel <name>` withdraws a pending read-only job before any runner sends it. It holds the same queue lock a
runner needs to read a job, archives `<queue>/<name>.job` to `<queue>/failed/` and publishes a refusal result
`<results>/<name>.md` holding `cancelled by reader: <reason>` (atomically, as for invalid jobs), so `status`,
`verdict` and external readers that import the `failed/` archive (for example the xq tool) keep working. `--reason` is optional (default `cancelled`), printable, at
most 200 characters and redacted when key-shaped. It refuses (exit 4) a job that a runner is processing
(`job "<name>" is running (pid N); wait for it or stop the runner`) and a job already in `done/` or `failed/`; an
unknown name exits 1 (the usage code, as `verdict --job` does). A runner that listed the job before the cancel skips it and sends nothing. Pass the same
`--queue-dir`/`--results-dir` as `queue add`.

`add -` accepts JSON on stdin. `add` validates a job and writes it exclusively; existing names in pending,
archive or results are refused. Producers can also place complete `.job` JSON files into the queue, named
`<name>.job`; write elsewhere then rename into place to avoid readers seeing an incomplete file. The runner
validates these again. Invalid jobs are archived as failed when they are safe regular files; unsafe filesystem
objects are skipped with a refusal result. The run prints `<name>.job: invalid job (<reason>); archived as failed`
and the refusal result holds the same reason (for example `file not found: f.txt`, a rule line, or `invalid JSON`;
escaped, bounded, and replaced by `detail redacted` when key-shaped) followed by
`next: fix the job and re-add it under a new name: agy-bridge queue add -C <repo> <job.json>`. Deeply
nested/invalid JSON also cannot stop later jobs.
Archive collisions use numeric suffixes; refusal results use `<name>.1.md`, `<name>.2.md`, etc. as needed.

Refusal results (an invalid or refused job and `queue cancel`) keep `# Queue refusal` on line 1 and carry a
machine-readable marker on line 2:

```
# Queue refusal
<!-- agy-bridge-refusal-v1 sent: no -->

<message>
```

`sent: no` means nothing of the job was sent to agy; external readers (for example the xq tool) can treat the job as undelivered. The rare
refusal raised after an agy attempt had started (for example when the result itself could not be published) says
`sent: maybe`. A `verdict --verdict pass` on a job whose only result is a refusal stays refused, since the job is
archived in `failed/`.
The runner reports an archive only after moving the file successfully. Unsafe entries left pending are
reported once per unchanged entry per watcher session. `run` drains jobs in filename order; `watch` polls
until interrupted. Neither starts a service on installation.

### Long runs and unattended watchers

A job can run up to its `timeout`, and `run` sends every pending job, including jobs another session added; check
`queue status` first. From an agent tool such as Claude Code's Bash tool, run `delegate` and `queue run` with a tool
timeout above the job timeout (the tool allows at most 10 minutes) or in the background. A tool that kills the
command interrupts the job; check `status` and `events --tail 10` before retrying, never re-run blindly.

A plain `watch` stops with the terminal or tool that started it. Start an unattended watcher only on purpose, with
`queue watch --detach`: it starts the watcher in a new session with SIGHUP ignored, appends its output to
`<queue>/watcher.log` (0600, opened without following symlinks), waits up to 3 seconds until the watcher holds the
queue lock, prints `watcher started pid N; log <path>; stop with: kill N` and exits 0. It refuses with R6 when a
runner already holds the lock, and exits 2 naming the log if the watcher exits or does not take the lock in time; in the second case it stops the
watcher (SIGTERM to its session, then SIGKILL) first, so a reported failure never leaves a runner behind.
`--repo`, `--queue-dir`, `--results-dir`, `--allow-non-git-root` and `--poll` are passed on. Confirm the watcher
with `status` (it shows the PID) and stop it with `kill N`. Every runner also keeps an inherited SIGHUP ignore
(the `nohup` convention). The root checks are the same as delegate; `--allow-non-git-root` has the same limited meaning.

### Update or uninstall

Every runner records its agy-bridge `version` and `code_root` (the folder holding the `agy_bridge` package) in
`watcher.pid`. A detached watcher keeps running the code it started with, so stop detached watchers before
`npx skills add` (an update), `claude plugin update` or an uninstall: read the PID from `status`, `kill <pid>`,
update the skill or plugin, then restart
with `queue watch --detach`. When the live runner's version or code root differs from the invoking command's (or the
row predates these fields), `queue status`/`status`, `ps`/`jobs list --repo`, `queue add` and `doctor --repo` print
`WARN watcher pid N runs agy-bridge X from <path>; this is Y; stop it (kill N) and restart with queue watch --detach`.
A watcher whose code root no longer exists prints a line to its log and exits between jobs. Each watch iteration
also sweeps leftover publish temp files from the queue folder, so a temp hard link left by an interrupted
`queue add` never makes the next job look foreign.

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
applies. Duplicate files are sent once. Unknown fields refuse, including any command field; the message names each
unknown field and lists the allowed ones, and a refused path is echoed (escaped). A missing job file refuses with
`job file not found: <path>` (exit 1). `model` defaults
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
Use a distinct `--results-dir` for each parallel queue in a repository, and pass the same `--queue-dir` and
`--results-dir` to `status`, `verdict` and `queue watch --detach`. The default results directory is shared
and result filenames use the job name.

`verdict --verdict pass` accepts a job with a result in `<results>/<job>.md`, an archive in `<queue>/done/` or
a direct delegate answer in `.agy-bridge/delegate/<job>.md` (regular file, no symlink). A pass on a job that is
only pending, or archived in `<queue>/failed/` without a `done/` archive (a failed run, an invalid job or a
`queue cancel`, even though each writes a failure report to `<results>/<job>.md`), is refused with the next step; `fixes` and `reject` are recorded (with a
warning for a pending or unknown job). See [GRADING.md](GRADING.md).

SIGINT, SIGTERM and SIGHUP unwind delegate's existing cleanup, kill the agy process group and leave the
unfinished job pending (exit 130). If a complete result was saved before interruption of the archive move,
the next run archives that existing result only when its `job_sha256` matches the exact pending job bytes
and no archive already uses that name. Reused names or incomplete/unrecognizable results are refused with
a new, numerically suffixed result; old answers are preserved and never used to close a new job. A hard crash
during sending cannot establish whether a remote response was produced. The lock prevents simultaneous sends, not exactly-once remote delivery.

Results are published atomically: the runner writes a private `.agy-tmp-*` file in the results folder, fsyncs
it, links it to `<name>.md` (never replacing an existing file), removes the temp name and fsyncs the folder, so
readers see either no result or a complete one. A result left by an older or crashed writer that starts with this
exact job's `job_sha256` but lacks a valid checksum footer is an interrupted write: the run prints
`interrupted result write; re-running`, keeps it as `<name>.md.partial` (or `.partial.N`) and sends the job again.
A different job reusing the name is still refused as a conflict. Each run first removes leftover temp files that
were already published or are older than one hour.

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
