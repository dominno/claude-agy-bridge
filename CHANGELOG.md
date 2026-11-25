# Changelog

## Unreleased

### Added

- `delegate --remote-agent [--remote-project NAME]` (and the same on `dry-run`) runs a read-only delegation on an
  agy remote agent in Google's cloud. agy 1.5.17 supports remote agents only in its interactive terminal UI, so the
  bridge runs `agy --remote-agent` on a pseudo-terminal in a new empty folder, accepts the folder-trust question for
  that folder, pastes the prompt with bracketed paste (never on the command line) and reads the reply off the screen
  with a VT emulator that keeps the scrollback. The files go through the usual checks and are inlined between
  tagged delimiters; files or tasks with terminal control or invisible characters are refused. The reply is framed
  by random markers (named in the prompt only as halves) and requested as a fenced code block so Markdown rendering
  cannot change quotes; `--cite`/`--require-verified` check it against the local bytes. Sign-in screens, disabled
  cloud conversations, unavailable remote agents or models, an unknown project (agy would fall back to the default
  one), the running-environment limit, timeouts and a missing end marker fail fast with their own messages and
  the usual exit codes. A tool call shown before the reply is counted in the event record and printed as a warning.
  Needs agy 1.5.17+; `doctor` reports whether agy offers remote agents, without network or sign-in.
- `port/process.py` gains `spawn_pty`, which starts a program on a new pseudo-terminal as its controlling terminal.

### Fixed

- The launcher and detached workers no longer write `__pycache__` into the installed skill directory.

### Security

- `apply-diff` counts each hunk's declared lines and refuses, beyond those counts, any line that `patch` (which
  strips leading whitespace) or `git apply --recount` could read as a file header, hunk header or ed command. An
  indented header block after a valid hunk can no longer add `conftest.py` or rewrite the check in the scratch tree.
  Plain miscounted context lines are still accepted.
- `stats` reads at most the first 64 KiB of each result file and its fallback model pattern is line-local and
  bounded, so a crafted repository result file cannot hang it with a quadratic regex.
- `status` escapes result file names in `last result` and `results without verdict`, like pending job names, so a
  repository-supplied name with a newline cannot add forged bridge lines to the output.
- Invisible-character refusal is category based: `stage.is_invisible` covers every format (Cf), line and paragraph
  separator (Zl/Zp), private use (Co) and surrogate (Cs) code point plus U+034F, the Hangul fillers, U+17B4/U+17B5,
  U+1D173-U+1D17A, variation selectors and tag characters. Implement export and apply now also refuse added lines
  with U+2028, U+2029, U+00AD, U+206A-U+206F, U+FFF9-U+FFFB, U+0600-U+0605 and similar (a BOM at offset 0 is still
  allowed). `jobs patch` marks them as `\u{XXXX}` on a terminal and in a pipe (a pipe now also marks bidi
  controls), and `clean_answer` strips and counts them (LS/PS still become newlines).
- The grading sandbox runs `bwrap --unshare-all --unshare-user --disable-userns`, so checked code cannot create
  nested user namespaces, and refuses Bubblewrap older than 0.8.0 with the same gate as `implement`. Both messages
  add `Ubuntu 22.04 ships 0.6.x; Debian 12+/Ubuntu 24.04+ qualify`. The `unshare` fallback relies on its chroot,
  which makes a nested `unshare(CLONE_NEWUSER)` fail with EPERM (checked by a Linux-only test).
- `/agy-bridge delegate` no longer pre-approves `delegate`, and `/agy-bridge queue` no longer pre-approves a real `queue add`
  (only `queue add --dry-run`, plus `jobs list -C`), so every send goes through the permission prompt. `/agy-bridge queue`
  checks `status` before a real add and asks for confirmation when a watcher is running, and lists pending jobs
  with `jobs list -C` (showing files/context/need of jobs the user did not write) before `queue run`.

- Implement export and apply refuse new lines with invisible Unicode: tag characters (U+E0000-U+E007F), variation
  selectors, zero-width and format characters (U+200B-U+200F, U+2060-U+2064, U+FEFF except at offset 0, U+180E,
  U+061C), Hangul fillers and private use characters (one shared `stage.INVISIBLE_CLASS`). The refusal names the
  file, line and code point (`U+E0041`), never the raw character; identical pre-existing lines stay tolerated.
  `jobs patch` shows the class as `\u{XXXX}` markers on a terminal and in a pipe (`--raw` keeps exact bytes), and
  `clean_answer` strips it from delegate answers, queue results and the implement check-output tail, adding
  `note: N invisible Unicode characters removed` (a stderr diagnostic in `--cite` mode).
- `/agy-bridge delegate`, `/agy-bridge queue` and `/agy-bridge implement` set `disable-model-invocation: true`, and each shows the
  dry-run manifest and sends only after the user confirms that file set. Every action quotes the executable
  as `"<skill>/scripts/agy-bridge"`; `allowed-tools` lists the quoted pattern next to the unquoted one.
- A `verified` implement job records `check_inputs_changed` (changed `--check` argv paths and files under an
  argv directory such as `pytest tests`, test and build
  configuration such as `conftest.py`/`pyproject.toml`/`Makefile`, `*.pth` files and `.github/workflows/`), and
  `jobs show`/`jobs list` print `WARN check inputs changed by the agent: <paths>; review them before trusting
  verified`. The phase stays `verified`.
- R5 detects keys assigned to names ending in `private`, `signing`, `encryption`, `master`, `access`, `app`,
  `session`, `client`, `jwt`, `hmac` or `crypt` + `key` (`DB_ENCRYPTION_KEY=...`, `privateKey: ...`,
  `"signingKey": "..."`, `:app_key => "..."`) when the value has 16+ token characters including a digit and a
  letter, plus Azure `SharedAccessKey=`, age `AGE-SECRET-KEY-1`, Atlassian `ATATT` and Databricks `dapi` tokens.
  `KEY_NAME = "primary"`, `public_key: ssh-ed25519 ...` and all-letter or short values stay clean.
- R1 denies `.secret*` (`.secret`, `.secrets`), `*.tfvars`, `*.tfvars.json`, `kubeconfig` (and `kubeconfig.yaml`/
  `.yml`), `id_dsa*` and `.yarnrc.yml`.
- Answers, queue results and cite rows also drop C1 controls (including the 8-bit CSI `\x9b`), LRM/RLM and bidi
  embedding, override and isolate characters; NEL, LINE SEPARATOR and PARAGRAPH SEPARATOR become newlines.
- `--model` and the job `model` field refuse a trailing newline (the pattern is now a full match).
- R1 denies more secret-bearing files: `*.ppk`, `.dev.vars`, `.vault-token`, `*.tfstate`, `*.tfstate.*`,
  `.terraform/`, `*.ovpn`, `.s3cfg`, `.boto`, shell histories (`.*_history`), `.envrc` and `wp-config.php`.
- R5 detects PuTTY private keys, Azure `AccountKey=`, Vault `hvs.`, DigitalOcean, PyPI, Shopify, Linear, Groq and
  xAI tokens, and short passwords assigned to `*PASSWORD`/`*PASSWD`/`*PWD`/`*_PASS` names (including PHP `define`).
  The unquoted form must end at whitespace, `;`, `,` or end of line and must not be a dotted or snake_case
  identifier, so code such as `password = argon2.hash(raw)` or `new_password = old_password2` is not flagged.
- Implementation jobs also refuse edits to agent-instruction and auto-run files: `CLAUDE.md`, `CLAUDE.local.md`,
  `.claude/`, `.cursorrules`, `.cursor/`, `.windsurfrules`, `.github/copilot-instructions.md`, `.vscode/`,
  `.idea/`, `.githooks/`, `.husky/`, `.pre-commit-config.yaml` and `.devcontainer/`, at export and at apply time.
- Implementation exports and applies refuse files that gain terminal control, bare carriage return, C1 or bidi
  override characters. A line with such characters passes only when the identical line is in the original
  file, so swapping an existing escape for another (for example an OSC 8 hyperlink) is refused. `jobs patch` escapes non-printable characters when writing to a terminal; `--raw` writes the
  exact bytes.
- Queue result headers escape file names (`files sent`, `FILES NOT SENT`, `files added`), and `queue add` refuses
  job file paths with control characters, so a file name cannot inject header lines.

- Implementation jobs also refuse edits to AGY's other workspace customization roots (`.agent/`, `_agents/`,
  `_agent/`, at any depth like `.agents/`) and to other agents' instruction files: `.windsurf/`, `.clinerules`,
  `.roo/`, `.kiro/`, `.github/instructions/`, `.github/prompts/` and `AGENTS.override.md`, at export and at apply
  time.
- `/agy-bridge queue` no longer pre-approves `queue watch` or any `verdict`; they go through the permission prompt (see
  the later entries: a real `queue add` and `queue run` are not pre-approved either).

### Added

- Queue refusal results (invalid job, refused run, `queue cancel`) carry `<!-- agy-bridge-refusal-v1 sent: no -->` on
  line 2 (`# Queue refusal` stays on line 1), so readers such as xq can treat the job as undelivered; a refusal
  raised after an agy attempt had started says `sent: maybe`.
- `queue add` prints `NOTE watcher pid N is running; this job will be sent now` when a watcher holds the queue lock
  (`NOTE runner pid N (queue run) is running; it may send this job` for a `queue run`).
- `queue status`/`status` list up to 20 pending job names after `pending: N`.
- `jobs list`/`ps` without `-C` say that delegation queues were not searched (`No implementation jobs. Delegation
  queues are listed per repository: agy-bridge jobs list -C <repo>`, also in JSON `warnings`) and add
  `NOTE <n> queue job(s) pending in <repo>; use -C` when the current Git checkout has pending queue jobs. A malformed
  implement ID that looks like a queue job name adds `queue job names are shown by agy-bridge jobs list -C <repo>`.

- `queue cancel NAME [--reason R]` withdraws a pending read-only job under the queue lock: it is archived to
  `failed/` with a `cancelled by reader: <reason>` refusal result and never sent. A running, finished or unknown job
  is refused with a specific message; a runner that listed the job before the cancel skips it.
- `verdict --verdict pass` is refused (without `--force`) for a job archived in `queue/failed/` without a `done/`
  archive, even though a failed agy run, an invalid job and `queue cancel` all publish a failure report to
  `<results>/<job>.md`.
- `queue watch --detach` starts the watcher in a new session with SIGHUP ignored and output appended to
  `<queue>/watcher.log` (0600, no symlink), waits until it holds the queue lock, prints
  `watcher started pid N; log <path>; stop with: kill N` and exits 0. It refuses with R6 when a runner already holds
  the lock and passes `--repo`, `--queue-dir`, `--results-dir`, `--allow-non-git-root` and `--poll` on. A watcher
  that does not take the lock within 3 seconds is stopped before the command exits 2.
- `queue add --dry-run <job.json>` validates a job and prints the real per-job manifest (repository context files,
  job files, NEED round, name availability, and the dry-run file list after R1/R5), adding and sending nothing.

- `jobs remove JOB_ID [--force] [--dry-run]` and `jobs prune [--older-than 7d] [--force] [--dry-run]` delete
  implementation job directories under the job lock and print the reclaimed bytes. A job with an active worker is
  refused; unapplied `ready`/`verified` and unfinished jobs need `--force`. Deletion uses directory-handle-relative
  traversal, unlinks symlinks without following them and checks ownership. `prune` also removes `.prepare-*` and
  `.pending-*` leftovers older than 1 hour.

### Changed

- Internal: agy_bridge reorganised into core/port/adapters/app/cli
- agy-bridge is now one agent skill, `agy-bridge`, in `.claude/skills/agy-bridge/` (with
  `.agents/skills/agy-bridge` linking to it for Codex). The folder holds `SKILL.md`, the launcher
  `scripts/agy-bridge`, the `agy_bridge` package (moved from `src/agy_bridge`), the documents in `references/`
  (moved from `docs/`), example files and the infographic in `assets/`, its own `README.md` and `LICENSE`. Install it
  with `npx skills add dominno/claude-agy-bridge --skill agy-bridge -a claude-code -a codex` (add `-g` for all
  projects). The plugin marketplace still works: `/plugin install agy@agy-bridge` installs plugin `agy`, whose
  `skills` entry points at the same folder; it has no `commands/` and no `bin/` any more. The `agy-bridge` CLI,
  its options, exit codes and the pip console script are unchanged.
- The seven slash commands are merged into the skill's action table. The skill sets
  `disable-model-invocation: true` (Codex: `agents/openai.yaml` with `allow_implicit_invocation: false`), so
  `doctor`, `status`, `jobs` and `dry-run` are no longer started by the model on its own either. `allowed-tools`
  pre-approves the union of the old read-only rules (`dry-run`, `events`, `doctor`, `jobs list`,
  `queue add --dry-run`, `queue status`, `status`) through `${CLAUDE_SKILL_DIR}/scripts/agy-bridge`; every send and
  every state change still goes through the permission prompt, after the manifest and an explicit confirmation.
- Migration: `/agy:delegate` → `/agy-bridge delegate`, `/agy:dry-run` → `/agy-bridge dry-run`,
  `/agy:doctor` → `/agy-bridge doctor`, `/agy:queue` → `/agy-bridge queue`, `/agy:implement` →
  `/agy-bridge implement`, `/agy:jobs` → `/agy-bridge jobs`, `/agy:status` → `/agy-bridge status`. With the
  plugin, the namespaced form is `/agy:agy-bridge <action>`; in Codex type `$agy-bridge <action>`. Scripts that
  called `<checkout>/bin/agy-bridge` should call `<checkout>/.claude/skills/agy-bridge/scripts/agy-bridge` (or the
  pip-installed `agy-bridge`). Stop a detached watcher (`status`, then `kill <pid>`) before updating: its recorded
  code root moves from `src/` to `.claude/skills/agy-bridge/scripts/`, so a watcher left running prints the
  existing code-root warning.

- Suggested commands quote paths for the shell (`stage.shell_arg`): `status -C`, `queue run -C`, `queue add -C`,
  `--queue-dir`/`--results-dir` in `verdict` refusals, `queue cancel` and the detached-watcher failure, so a repository
  path with spaces can be copied as is. `queue cancel` of an unknown job now exits 1 (the usage code, like
  `verdict --job`); a running, finished or already cancelled job still exits 4.

- Every runner keeps a SIGINT/SIGTERM/SIGHUP that was already ignored at startup (the `nohup` convention).
- `/agy-bridge queue` no longer pre-approves `queue run`, so a drain goes through the permission prompt; it runs `queue run`
  only when the request says run, after showing `queue status` and every pending job (including other sessions'
  jobs). `/agy-bridge queue`, `/agy-bridge delegate` and `references/QUEUE.md` say to give the Bash tool a timeout above the job
  timeout (at most 10 minutes) or run in the background, to check `status`/`events` before retrying a killed
  command, and to start an unattended watcher only on request with `queue watch --detach`.
- `queue run` names the specific reason for an invalid job (for example `file not found: f.txt`, the rule line, or
  `invalid JSON`), escaped, bounded and redacted when key-shaped, on stdout
  (`<job>: invalid job (<reason>); archived as failed`) and in the refusal result, followed by
  `next: fix the job and re-add it under a new name: agy-bridge queue add -C <repo> <job.json>`.
- R6 for a system folder names the matched folder and the remedy, for example
  `the repository folder is inside system folder /usr; move the checkout under $HOME or a temporary folder`.
- Refusals from `queue`, `status`, `verdict`, `verdicts`, `stats`, `events`, `jobs`, `ps` and
  `doctor --implementation` name each rule (for example R6 for a folder without `.git`), or the failing operation,
  path and system reason, instead of a generic message. Exit codes are unchanged.
- `verdict --verdict pass` refuses with the usage exit code when no result or queue job file exists for `--job`,
  lists recent jobs and creates nothing; `--force` records anyway. `fixes` and `reject` on such a job record with a
  warning, so a failed or refused delegate (no result file) can still be rejected.
- `/agy-bridge queue` states that runs also send `.agy-bridge.json` `context_files` unless `"context": false`, and a NEED
  round with `"need": true`; it asks for both to be false unless authorized and to preview with
  `queue add --dry-run`. The skill references `references/...` and the launcher form used by its pre-approval
  rule.
- `jobs -C ROOT` (no subcommand) lists jobs like `jobs list`; `queue`, `status`, `verdict` and `verdicts` have help
  text.

- `jobs show/patch/cancel/resume/apply` with an unknown job ID print `no implementation job <id>; list jobs with:
  agy-bridge jobs list` and exit 1; a malformed ID names the accepted pattern. Other refusals name the rule or the
  failing path and system reason instead of `implementation path or operation refused`.
- A scope violation lists the out-of-scope paths (escaped, at most 10) and the `--write-dir` values. `jobs apply`
  says separately whether the phase is wrong or `--sha256` does not match (with the expected hash and how to hash the
  reviewed file).
- `queue add` names unknown job fields and lists the allowed ones, says when the job is not a JSON object, echoes a
  refused file path (escaped), and reports `job file not found: <path>` for a missing job file. `--repo is not a
  directory` echoes the value. `queue add --help` lists the job fields.
- `/agy-bridge delegate` and `/agy-bridge dry-run` run the dry run with exactly the same `--file`/`--glob`/`--cite`/
  `--allow-fragment-file` arguments as the delegate. `/agy-bridge implement` shows the `--check` JSON argv form, states the
  Linux/bubblewrap requirement, and saves `implement-patch --raw` to a file whose hash (`sha256sum` or
  `shasum -a 256`) is passed to `implement-apply`; `references/IMPLEMENT.md` adds the macOS `shasum` form.

### Fixed

- `agy-bridge jobs -C <repo> list` (and `--active`, `--json`, `--queue-dir`, `--allow-non-git-root` given before
  `list`) keeps those options; the `list` subparser no longer resets them to its own defaults.
- Suggested commands (`agy-bridge status -C ...`, `queue run -C ...`) quote a printable repository path as is, so
  `shlex.split` gives back a non-ASCII path such as `zażółć repo`; only a path with a control or other
  non-printable character is escaped first.
- `verdict pass` without `--force` is refused when `results/<job>.md` is a queue refusal (`# Queue refusal` on line
  1 or the `agy-bridge-refusal-v1` marker on line 2) even if the job could not be archived to `failed/`.
- `watcher.pid` is rewritten in place (`pwrite` then `ftruncate` to the new length) instead of truncate-then-write,
  and is no longer truncated to empty on exit (the last row gets `"stopping": true`), so a reader never sees an empty
  record while the lock is held; `running()` retries one empty or unparseable read after 50 ms before reporting pid
  `unknown`.

- An inherited SIGINT/SIGTERM ignore no longer disables cleanup; only an inherited SIGHUP ignore is kept.
- `verdict --verdict pass` accepts a direct delegate answer in `.agy-bridge/delegate/<job>.md` (regular, no-follow,
  single-link file) without `--force`, as in 0.3.0. A pass on a job that is only pending (`job "<job>" is still
  pending (no result yet); run agy-bridge queue run ...`) or only failed is refused; `fixes`/`reject` on a pending
  job record with a warning. The refusal lists the delegate folder and its recent names.
- The runner row in `watcher.pid` records `version` and `code_root`. `status`, `ps`, `queue add` and
  `doctor --repo` warn when a live watcher runs another agy-bridge version or plugin folder (or predates the
  fields); a watcher whose code root is gone stops between jobs. Each watch iteration sweeps leftover temp links
  from the queue folder, so an interrupted `queue add` no longer makes the job look foreign.
- `implement` and `doctor --implementation` check `bwrap --version` and refuse Bubblewrap older than 0.8.0
  (`implement requires Bubblewrap 0.8.0+ (found X.Y.Z; --disable-userns is unsupported)`) instead of a generic probe
  error; an unparseable version proceeds to the probe. Linux only.
- `verdict` accepts `--queue-dir` and `--results-dir`, so a pass on a job run with custom folders no longer needs
  `--force`; the unknown-job message names the searched folders.
- A project strictly below the home folder is accepted when that home is inside a system tree (`/root/<proj>` for
  uid 0 in containers). The home itself, its ancestors and its hidden top-level children stay refused, and a home
  at a system root (`/usr`, `/etc`, `/var`, ...) never lifts the refusal. Only the password-database home lifts
  it: `$HOME` and `~` can add refusals but never remove one (`HOME=/usr/share` leaves `/usr/share/man` refused).
- `jobs prune` ages a job with an unreadable state by its state file instead of its directory, whose mtime the
  prune itself changes when it creates the lock file; such jobs were never old enough to prune.
- A record torn by a crashed writer no longer breaks `events.jsonl` or `verdicts.jsonl`: the next append starts on
  a fresh line, each append is fsynced, and readers skip unparseable lines with one warning
  (`skipped N corrupt journal line(s)`). Oversized lines and unsafe files are still refused.
- Queue results are published atomically (private temp file, fsync, link to the final name, directory fsync). A
  result of the same job cut off before its checksum footer is set aside as `<name>.md.partial` and the job re-runs
  instead of failing as a reused name conflict; leftover temp files are swept at the start of each run.

- `queue add` publishes the job file atomically under the queue lock (private temp file, fsync, no-overwrite link,
  directory fsync), so an interrupted add leaves no partial `.job` and the name stays free. Refusal results are
  published the same way, and `queue run`/`watch` also sweep leftover temp files from the queue folder.

### Docs

- README and `references/QUEUE.md` add "Update or uninstall" (stop detached watchers, update, restart);
  README, `references/IMPLEMENT.md` and `references/COMPAT.md` state the Bubblewrap 0.8.0 minimum.
- `references/THREAT_MODEL.md`, `references/IMPLEMENT.md` and `/agy-bridge implement` state that the AGY agent sandbox uses bwrap
  `--share-net`: it can reach host loopback services, the LAN, cloud instance metadata (`169.254.169.254`) and
  abstract-namespace Unix sockets, held back only by AGY's tool policy. Run implement in a VM or container without
  sensitive loopback services and enforce IMDSv2 with hop limit 1 on cloud VMs.

### Tests

- `test_timeout_preserves_changed_files_and_resumes_conversation` no longer depends on 1 s of wall clock: the fake
  agent writes a ready file after its edit and sleeps until killed, under a 5 s turn limit.

- `tests/conftest.py` resets an inherited SIGINT/SIGTERM ignore, so the signal tests pass however the suite is
  launched (for example `(pytest ...) & wait`).
- The implementation transport timeout and cancel cases skip the version probe and use a 2 s budget; a separate
  test covers a version probe that uses up the budget. The long-line scan test uses `MAX_TOTAL_BYTES` with a time
  bound. CI also runs macOS with Python 3.9.
- The sandbox-crash regression tests kill the check with SIGKILL instead of `os.abort()` (which could stall in the
  macOS crash reporter and leave processes behind); SIGABRT coverage is opt-in with `AGY_TEST_ABORT=1`.

## 0.3.0

- `ps` shows live jobs, `jobs` lists history, and `jobs start/show/patch/cancel/resume/apply` group the implementation
  workflow under one command. The existing command spellings remain callable.
- Job inventory accepts the selected legacy queue's location and the explicit `--allow-non-git-root` option.
- Patch application uses canonical scratch paths and records recovery information for an interrupted apply.
- Offline sandbox startup failures stop implementation instead of becoming code feedback. Provider timeouts are
  reported as timeouts by read-only delegation.

## 0.2.0

- `implement` and `/agy:implement` run AGY edits in a private Linux Bubblewrap workspace. Jobs support offline checks,
  cancellation, resuming the saved conversation, patch review and explicit application.
- `jobs list` and `/agy:jobs` show local implementation jobs and a selected delegation queue. The CLI supports
  filtering to live workers and returning versioned JSON for integrations.
- `doctor --implementation --profile PATH` probes the isolated Linux worker and its AGY version.
- Install the standalone `agy-bridge` command from a checkout with `pip`. Detached workers also run from the
  installed package.
- Read-only delegation pins permissions on AGY versions with verified session settings. Claude Code pre-approval is
  limited to the subcommands used by each read-only slash command.

## 0.1.0 (2026-10-31)

First public release.

- `delegate`, `dry-run`, `doctor`: path denylist, content scan, read-once staging, five-name environment, guarded pty
  run, version-keyed argv, distinct exit codes.
- Result headers list every skipped or refused input. `--allow-fragment-file` accepts two fixed example literals only.
- `--cite` and `--require-verified` check quoted text against the exact buffers that were sent.
- Metadata-only event journal in `.agy-bridge/events.jsonl`, with `events --tail`.
- Serial queue (`queue add/run/watch/status`, `status`) with bounded quota retries and an opt-in NEED followup round.
- Reader verdict journal, per-model timing stats.
- `grade-mutants` and `apply-diff`: scratch-only checks of model-written code inside an OS sandbox, refusing to run
  when the sandbox is unavailable unless `--unsafe-no-sandbox` is passed.
- Slash commands: `/agy:delegate`, `/agy:dry-run`, `/agy:doctor`, `/agy:queue`, `/agy:status`.
