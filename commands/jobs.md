---
description: List implementation jobs and the selected repository's delegation queue
argument-hint: [--active] [--repo PATH] [--queue-dir PATH] [--json]
allowed-tools: Bash(${CLAUDE_PLUGIN_ROOT}/bin/agy-bridge jobs list *)
---

Run `"${CLAUDE_PLUGIN_ROOT}/bin/agy-bridge" jobs list` with the requested options. Without `--repo`, it lists the
current user's implementation jobs across repositories. With `--repo PATH`, it filters those jobs and includes the
selected delegation queue. Use `--active` for live jobs and `--json` for machine-readable output.

Report the job IDs, phases and any warnings. Do not treat a queued delegation or an interrupted worker as active.
