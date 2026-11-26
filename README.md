# agy-bridge

agy-bridge is an agent skill that lets Claude Code or Codex hand work to Google's Antigravity CLI (`agy`) without
giving it your repository. Use `/agy-bridge delegate` to get a second model's read-only review or summary of files you
choose, and `/agy-bridge implement` (Linux or macOS) to let Antigravity edit an isolated copy and return a patch you review
before anything touches your code.

**Why use it**

- Offload large, read-heavy reviews, searches and summaries to another model and keep Claude's context small.
- Get a second opinion, with optional quote checks against the exact files that were sent.
- Run coding tasks in the background without exposing secrets or your working tree.

Not affiliated with or endorsed by Google or Anthropic. MIT licensed; a local script using only the Python standard
library enforces the rules.

![agy-bridge workflow: inspect, confirm, delegate or implement, then review](.claude/skills/agy-bridge/assets/how-it-works.png)

## What leaves your machine

Only the files you select and your task text are sent to Google through `agy`. Before anything is sent you see the
exact file list (the *manifest*) and must confirm it: Claude cannot start the skill on its own, and the real send
also shows a Claude Code permission prompt. The bridge refuses `.env` files, keys and tokens, credential
folders, symlinks, binaries and hidden-Unicode tricks. It cannot catch secrets that are encoded, split across lines
or have no known shape, or confidential text that is not a secret. Answers come back as **untrusted text** and are
never run. The confirmation step is an instruction to Claude; the permission prompt is the hard stop.
[What is checked and what is not](.claude/skills/agy-bridge/references/THREAT_MODEL.md).

With `delegate --remote-agent` the same checked files are pasted, in full, into the prompt of an agy **remote
agent** that runs in a Google-managed cloud environment. That environment runs tools without asking and can read the
remote project's attached sources; the bridge tells the model to use no tools and only the given files, but cannot
enforce it, and the conversation stays in the remote project.

## Requirements

- macOS or Linux, Python 3.9 or newer. [Claude Code](https://claude.com/claude-code) or Codex for the skill; the
  script also works on its own. Node.js for the `npx skills` installer.
- Antigravity CLI major version 1, installed and signed in
  ([Google's instructions](https://antigravity.google/docs/cli-using)). Read-only delegation supports 1.x through
  1.5.6 and 1.5.16 onwards.
- Only for `implement`: Git, AGY 1.5.16 or later, a dedicated AGY profile folder signed in once
  (`HOME="$HOME/agy-implementation-profile" agy`) or `GEMINI_API_KEY`, and either Linux with Bubblewrap 0.8.0 or
  later and working unprivileged user namespaces, or macOS with `/usr/bin/sandbox-exec`.
  [Setup](.claude/skills/agy-bridge/references/IMPLEMENT.md).
- Only for `grade-mutants` and `apply-diff`: `sandbox-exec` on macOS, or Bubblewrap 0.8.0+ (or `unshare`) on Linux.

## Install

Run this in the project where you want to use the skill (add `-g` to install it for all your projects):

```sh
npx skills add dominno/claude-agy-bridge --skill agy-bridge -a claude-code -a codex
```

Keep just the `-a` option for the agent you use. The installer copies or links the whole skill folder
([`.claude/skills/agy-bridge`](.claude/skills/agy-bridge/README.md)): `SKILL.md`, the bridge script in `scripts/` and
the references. Update with `npx skills update agy-bridge` (stop a detached watcher first, see below).

| Agent | Invocation |
|---|---|
| Claude Code | `/agy-bridge delegate explain what a.py does, files: src/a.py` |
| Codex | `$agy-bridge delegate explain what a.py does, files: src/a.py` |

<details>
<summary>Alternative: the Claude Code plugin marketplace</summary>

```text
/plugin marketplace add dominno/claude-agy-bridge
/plugin install agy@agy-bridge
```

The plugin `agy` exposes the same skill folder, invoked as `/agy:agy-bridge <action> ...` (or `/agy-bridge` when no
other skill uses that name). Update later with `claude plugin update agy@agy-bridge`, then reload plugins or start a
new session. To try it for one session without installing: `git clone https://github.com/dominno/claude-agy-bridge.git`
and `claude --plugin-dir ./claude-agy-bridge`.

</details>

For the standalone `agy-bridge` command, run `python3 -m pip install /path/to/claude-agy-bridge` in a virtual
environment, or call `.claude/skills/agy-bridge/scripts/agy-bridge` directly.

## Quickstart (5 minutes)

0. Install `agy` and sign in, then check it with `agy --version` and `agy models`.
1. Install the skill (above).
2. In a Git repository of your own, add `.agy-bridge/` to `.gitignore` (the bridge keeps metadata there).
3. `/agy-bridge doctor --offline` checks the setup.
4. `/agy-bridge dry-run src/a.py -- explain what a.py does` shows exactly what would be sent, and sends nothing.
5. `/agy-bridge delegate explain what a.py does, files: src/a.py`: Claude shows the manifest and waits for your
   "yes"; then a permission prompt appears, and the answer comes back labelled as untrusted.

In Codex, type `$agy-bridge` instead of `/agy-bridge`.

From a terminal: `agy-bridge dry-run -f src/a.py -- "Explain what a.py does"`, then
`agy-bridge delegate -f src/a.py --timeout 5m -- "Explain what a.py does"`.

## Glossary

- **delegate**: a one-off, read-only question to `agy` about files you name.
- **manifest**: the list of files, sizes and hashes that would be sent, plus `FILES NOT SENT` notices.
- **staging folder**: the temporary copy `agy` works in; it never runs in your repository.
- **watcher**: a background process (`queue watch`) that sends queued jobs as they arrive.
- **verdict**: your recorded `pass`, `fixes` or `reject` after checking an answer yourself.

## Skill actions

The first word after `/agy-bridge` (or `$agy-bridge`) picks the action.

| Action | What it does | Sends files? |
|---|---|---|
| `doctor` | Read-only health check | no |
| `dry-run` | Shows what a delegation would send | no |
| `delegate` | Hands one task to `agy` (locally, or to a cloud remote agent with `--remote-agent`) and returns the answer | yes, after you confirm |
| `queue` | Adds, runs, watches or cancels queued read-only jobs | yes, after you confirm |
| `implement` | Starts an edit job on an isolated copy, returns a job ID | yes, after you confirm |
| `jobs` | Lists implementation jobs and a repository's queue; cleans up old jobs on request | no |
| `status` | Shows the watcher, queue counts and results awaiting a verdict | no |

The skill sets `disable-model-invocation: true` (and `allow_implicit_invocation: false` for Codex), so the
agent never starts it on its own. In Claude Code its
`allowed-tools` pre-approves the listed bridge subcommands, none of which sends; it is not a restriction. Details
and every option: [`references/CLI.md`](.claude/skills/agy-bridge/references/CLI.md).

## Coding jobs (Linux and macOS)

`/agy-bridge implement`, or `agy-bridge jobs start --dry-run -f src/parser.py -f tests/test_parser.py --write-dir src
--check '["python3", "-m", "unittest"]' -- 'Fix the parser bug'`, previews the job. Submit without `--dry-run` and
with `--profile`. Follow it with `jobs show`, `jobs patch`, `jobs cancel` or `jobs resume`; `agy-bridge ps` finds
live jobs. Apply only after review with `jobs apply <id> --sha256 <hash of the reviewed patch>`. `jobs prune` and
`jobs remove` delete old job folders. The agent shares the host network; run it in a VM or container.
[Full workflow](.claude/skills/agy-bridge/references/IMPLEMENT.md).

## Batch queue

Save a job as JSON (`{"name": "review-parser-01", "task": "Review the parser.", "files": ["src/parser.py"]}`), then:

```sh
agy-bridge queue add --dry-run job.json --repo /path/to/repo   # preview; adds and sends nothing
agy-bridge queue add job.json --repo /path/to/repo
agy-bridge queue run --repo /path/to/repo                      # or: queue watch --detach
agy-bridge queue cancel review-parser-01 --repo /path/to/repo  # withdraw a pending job
```

Results land in `.agy-bridge/results/`; a refused or cancelled job gets a result marked
`<!-- agy-bridge-refusal-v1 sent: no -->`. While a watcher runs, `queue add` sends at once.
[Queue reference](.claude/skills/agy-bridge/references/QUEUE.md).

## Checking results

Every answer is **unverified model output** until you check it. Record what you checked:

```sh
agy-bridge verdict --repo /path/to/repo --job review-parser-01 --verdict pass --by me --evidence "Checked the tests"
agy-bridge verdicts --repo /path/to/repo --tail 10
```

`grade-mutants` and `apply-diff` run a trusted test case against model-written code, only in a scratch copy inside
an OS sandbox; `--unsafe-no-sandbox` removes that protection. `stats` reports timing per model.
[Grading](.claude/skills/agy-bridge/references/GRADING.md).

## Updating with a detached watcher

A detached watcher keeps running the code it started with. Before updating or uninstalling, find its PID with
`agy-bridge status --repo <repo>`, `kill` it, update, then restart it with `queue watch --detach`.
[Details](.claude/skills/agy-bridge/references/QUEUE.md#update-or-uninstall).

## Project status

Version 0.3.0. The test suite uses a fake `agy`; the bridge has been run once against the real `agy` 1.4.3 with
fake files only, and a basic delegation also worked on 1.4.12. Authenticated end-to-end implementation jobs (edit,
offline check, resume, apply) were run on macOS 13 with AGY 1.5.17 and a signed-in dedicated profile (synthetic
files), and on Linux (Debian 12, Bubblewrap 0.8.0) with AGY 1.5.18, including cancel and resume. [What was measured](.claude/skills/agy-bridge/references/COMPAT.md).

## Documentation

The skill's references live in [`.claude/skills/agy-bridge/references/`](.claude/skills/agy-bridge/references/CLI.md),
so an installed skill carries them too.

| File | Contents |
|---|---|
| [`SKILL.md`](.claude/skills/agy-bridge/SKILL.md) | The skill: actions, confirmation steps, the exact bridge commands |
| [`references/CLI.md`](.claude/skills/agy-bridge/references/CLI.md) | All options, exit codes, repo config, input notices, `--cite`, the event journal |
| [`references/IMPLEMENT.md`](.claude/skills/agy-bridge/references/IMPLEMENT.md) | Edit jobs, authentication, checks, resume and patch application |
| [`references/QUEUE.md`](.claude/skills/agy-bridge/references/QUEUE.md) | Job format, folders, locking, retries, watchers |
| [`references/GRADING.md`](.claude/skills/agy-bridge/references/GRADING.md) | Verdicts, trusted cases, mutants, diffs, the sandbox, timing stats |
| [`references/THREAT_MODEL.md`](.claude/skills/agy-bridge/references/THREAT_MODEL.md) | Rules, the tests that prove them, residual risks |
| [`references/COMPAT.md`](.claude/skills/agy-bridge/references/COMPAT.md) | What is measured and what is unverified about `agy` |
| [`references/MANUAL_TEST.md`](.claude/skills/agy-bridge/references/MANUAL_TEST.md) | How to verify the bridge against a new `agy` version |
| [`SECURITY.md`](SECURITY.md) | Known limits and how to report a bypass |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | Development setup and project rules |

## Credit

The idea of a version-keyed argument builder, distinct exit codes and a wall-clock guard was inspired by
`yuting0624/antigravity-for-claude-code` (MIT). No code was copied from it.

## License

MIT, see [`LICENSE`](LICENSE).
