# Changelog

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
