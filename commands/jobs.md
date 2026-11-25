---
description: List implementation jobs and the selected repository's delegation queue
argument-hint: [--active] [-C PATH] [--queue-dir PATH] [--allow-non-git-root] [--json]
allowed-tools: Bash(${CLAUDE_PLUGIN_ROOT}/bin/agy-bridge jobs list *) Bash("${CLAUDE_PLUGIN_ROOT}/bin/agy-bridge" jobs list *)
---

Run `"${CLAUDE_PLUGIN_ROOT}/bin/agy-bridge" jobs list` with the requested options. Without `--repo`, it lists the
current user's implementation jobs across repositories. With `-C PATH` (`--repo PATH`), it filters those jobs and
includes the selected delegation queue. Use `--active` for live jobs and `--json` for machine-readable output.

Report the job IDs, phases and any warnings. Do not treat a queued delegation or an interrupted worker as active.

Only when the user asks to clean up, use `"${CLAUDE_PLUGIN_ROOT}/bin/agy-bridge" jobs prune --dry-run` (or
`jobs remove <id> --dry-run`) first, show the list, and delete only after they confirm. These are not pre-approved
and need `--force` for unapplied or unfinished jobs; do not add `--force` unless the user asked for it.
