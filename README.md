# agy-bridge

A small Claude Code plugin for Google Antigravity CLI (`agy`). Use `delegate` for bounded, read-only work. Use
`implement` for editing an isolated snapshot, running a configured check, and reviewing a patch. A local script
using the Python standard library enforces the bridge rules.

Not affiliated with or endorsed by Google or Anthropic.

**Version 0.3.0.** The test suite uses a fake `agy`. The bridge has been run once against the real `agy` 1.4.3
(2026-10-20, fake files only): doctor, dry-run and delegate work, and writes, commands, reads outside the folder and
network use were not possible headless without a permission. That is one version on one machine, not a guarantee; see
[`docs/COMPAT.md`](docs/COMPAT.md) for what was measured and what stays unverified. A basic delegation also worked on
1.4.12. The plugin has been loaded in real Claude Code sessions. Implementation has offline coverage for edit,
check, resume and patch application. An authenticated end-to-end implementation job still needs verification on a
Linux host with working Bubblewrap namespaces.

![agy-bridge workflow for read-only reviews and resumable implementation jobs](docs/img/how-it-works.png)

## When to use it

Use `delegate` for a first-pass review, broad search or summary of large inputs. Use `implement` for changes to approved
project files. Review the files that can be sent, the resulting patch, and the check outcome before acting on the result.

## Requirements

- macOS or Linux for read-only delegation. Python 3.9 or newer. The bridge uses only the standard library.
- [Claude Code](https://claude.com/claude-code), for the slash commands. The script also works on its own.
- Antigravity CLI major version 1 and credentials for your selected model. Use `agy --version` and `agy models` to
  check the installation. Read-only delegation supports 1.x through 1.5.6 and 1.5.16 onwards. See
  [Google's installation instructions](https://antigravity.google/docs/cli-using).
- `implement` requires AGY 1.5.16 or later in major version 1, Git, Linux, Bubblewrap with working unprivileged user
  namespaces, and a dedicated AGY profile.
- `grade-mutants` and `apply-diff` use `sandbox-exec` on macOS or `bwrap`/`unshare` on Linux.

## Quick start

Install the plugin in Claude Code:

```text
/plugin marketplace add dominno/claude-agy-bridge
/plugin install agy@agy-bridge
```

If you already have the plugin, update it with `claude plugin update agy@agy-bridge`, then reload plugins or start a
new Claude Code session.

Or try it for one session only, without installing anything:

```sh
git clone https://github.com/dominno/claude-agy-bridge.git
claude --plugin-dir ./claude-agy-bridge
```

Then, inside a Git repository of your own:

```text
/agy:doctor --offline
/agy:dry-run src/a.py src/b.py -- review these files for missing tests
/agy:delegate review these files for missing tests, files: src/a.py src/b.py
```

`doctor` checks the setup. `dry-run` shows exactly which files would be sent and sends nothing. `delegate` runs the task
and shows the answer, labelled as untrusted. Add `.agy-bridge/` to the ignore file of every repository you use it in.

For the standalone CLI, install from a checkout in your virtual environment with
`python3 -m pip install /path/to/claude-agy-bridge`. From the project you want to review, run:

```sh
agy-bridge dry-run -f src/a.py -- "Explain what a.py does"
agy-bridge delegate -f src/a.py --timeout 5m -- "Explain what a.py does"
```

You can also use `python3 -m agy_bridge`, or run the checkout's `bin/agy-bridge` with its absolute path.

| Slash command | What it does |
|---|---|
| `/agy:doctor` | Read-only health check |
| `/agy:dry-run` | Shows what a delegation would send; sends nothing |
| `/agy:delegate` | Hands one task to `agy` and returns the answer |
| `/agy:implement` | Starts an edit job in an isolated snapshot and returns its job ID |
| `/agy:jobs` | Lists local implementation jobs and a selected delegation queue |
| `/agy:queue` | Adds a job to the serial queue or drains it |
| `/agy:status` | Shows the watcher, queue counts and results awaiting a verdict |

## Data flow warning

AGY can send staged files and task text to Google for processing. The scan and denylist catch some secret formats and
file names. Encoded, split, or confidential content can pass. Review the manifest from `dry-run` for delegation or
`implement --dry-run` for edits before submitting.

## What read-only delegation does

- Copies only the files you name (or glob) into a throw-away staging folder, with a short `TASK.md`.
- Runs `agy` there, in a terminal pair, with a time limit and an output cap.
- Returns the answer as text, labelled as untrusted.

## Read-only delegation boundaries

- The bridge starts `agy` without permission-widening flags. From 1.5.16 onward it also pins read-only session settings.
- The process environment contains only `HOME`, `PATH` (absolute entries), `LANG`, `TERM`, and `TMPDIR`. AGY receives the
  real `HOME`, so environment filtering alone cannot limit what it reads there.
- Staging rejects denylisted paths, symlinks, hard links, devices, pipes, binary or oversized files, and key-shaped
  content. For a content match the report names the file, count and first hit line.
- The selected root must be an ordinary project directory. System directories, the home directory and its ancestors,
  denylisted paths, and hidden folders under the home directory are refused. A `.git` directory or local `gitdir:` marker
  is required unless you pass `--allow-non-git-root`.
- AGY runs in the staged copy. Logs contain metadata. Answers live in temporary storage or the selected repository
  workflow directory. The queue starts only when you invoke it.

## What the slash commands can and cannot do

`allowed-tools` pre-approves the listed bridge subcommands. The implementation slash command uses normal Claude Code
permissions. The bridge validates repository roots, output locations, paths and file contents.

What `dry-run` does: it reads and checks the files exactly like `delegate`, **writes them to a temporary folder (mode
0700, removed before it exits)**, prints the manifest and the `agy` command line, and runs `agy --version` with the
five-name environment in an empty folder. That is the only program a dry run starts; delegation never runs git. No file contents and no task text go to `agy`.

To have a diff reviewed, pass the changed source files with `--file`.

## Implement code with AGY

Use `/agy:implement` in Claude Code, or preview a job from the terminal inside your Git project:

```sh
agy-bridge jobs start --dry-run -f src/parser.py -f tests/test_parser.py --write-dir src \
  --check '["python3", "-m", "unittest", "discover", "-s", "tests"]' \
  -- 'Fix the parser bug and run the regression tests.'
```

Review the manifest, then submit without `--dry-run` and supply `--profile` as shown in the setup guide. Use the job ID
with `jobs show`, `jobs patch`, `jobs cancel`, or `jobs resume`. The worker can continue after you
close Claude Code while the Linux host stays running. After reviewing the patch, `jobs apply` requires its exact
SHA-256. [Set up credentials and run the full workflow](docs/IMPLEMENT.md).

## Find a background job

Use `/agy:jobs` in Claude Code or run one of these commands locally.

```sh
agy-bridge ps
agy-bridge ps -C "$PWD" --json
agy-bridge jobs
```

`ps` shows live jobs. `jobs` and `ps --all` include history. Use `-C` or `--repo` to filter implementation jobs and
include one delegation queue for that project. Pass `--queue-dir` to select a custom queue.
[Job inventory and JSON format](docs/CLI.md#job-inventory).

## Job queue

A serial queue, for several tasks in a row. Nothing runs until you start it. Save a job as `job.json`:

```json
{
  "name": "review-parser-01",
  "task": "Review the parser against its tests.",
  "files": ["src/parser.py", "tests/test_parser.py"],
  "timeout": "5m"
}
```

```sh
bin/agy-bridge queue add job.json --repo /path/to/repo
bin/agy-bridge queue run --repo /path/to/repo     # drain once; `queue watch` keeps polling until interrupted
bin/agy-bridge status --repo /path/to/repo
```

Results land in `.agy-bridge/results/`. Format, configuration and exits: [`docs/QUEUE.md`](docs/QUEUE.md).

## Checking results

Every result is **unverified model output** until a reader checks its claims:

```sh
bin/agy-bridge verdict --repo /path/to/repo --job review-parser-01 --verdict pass --by reader --evidence "Checked sources and tests"
bin/agy-bridge verdicts --repo /path/to/repo --tail 10
bin/agy-bridge grade-mutants result.md --repo /path/to/repo --case case.json
bin/agy-bridge apply-diff result.md --repo /path/to/repo --case case.json
bin/agy-bridge stats --repo /path/to/repo --target 180
```

`grade-mutants` and `apply-diff` **execute model-written code**, only in a scratch copy inside an OS sandbox. They never
change your working tree. If the sandbox is unavailable they refuse to run; `--unsafe-no-sandbox` explicitly removes
that protection and runs the code with your privileges and network access. See [`docs/GRADING.md`](docs/GRADING.md).

## Documentation

| File | Contents |
|---|---|
| [`docs/CLI.md`](docs/CLI.md) | All options, exit codes, repo config, input notices, `--cite` and the event journal |
| [`docs/IMPLEMENT.md`](docs/IMPLEMENT.md) | Edit jobs, authentication, checks, resume and patch application |
| [`docs/QUEUE.md`](docs/QUEUE.md) | Job format, folders, locking, retries |
| [`docs/GRADING.md`](docs/GRADING.md) | Trusted case format, mutants, diffs, the sandbox, timing stats |
| [`docs/THREAT_MODEL.md`](docs/THREAT_MODEL.md) | Rules R1 to R9, the tests that prove them, residual risks |
| [`docs/COMPAT.md`](docs/COMPAT.md) | What is measured and what is unverified about `agy` |
| [`docs/MANUAL_TEST.md`](docs/MANUAL_TEST.md) | How to verify the bridge against a new `agy` version |
| [`SECURITY.md`](SECURITY.md) | Known limits and how to report a bypass |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | Development setup and project rules |

## Credit

The idea of a version-keyed argument builder, distinct exit codes and a wall-clock guard was inspired by
`yuting0624/antigravity-for-claude-code` (MIT). No code was copied from it.

## License

MIT, see [`LICENSE`](LICENSE).
