# Changelog

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
