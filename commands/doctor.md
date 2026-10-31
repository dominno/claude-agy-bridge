---
description: Read-only health check for the agy bridge (agy on PATH, version, log file mode)
argument-hint: [--offline]
allowed-tools: Bash(${CLAUDE_PLUGIN_ROOT}/bin/agy-bridge:*)
---

Run `${CLAUDE_PLUGIN_ROOT}/bin/agy-bridge doctor --repo <absolute path of the current project folder> $ARGUMENTS` and report each line as ok or fail with its reason.
Use `--offline` to skip the one check that needs network and sign-in. The check changes nothing outside
`~/.agy-bridge/` and the temp folder. Do not try to fix a failure by installing or signing in on the user's behalf;
tell them what failed.

Note: `allowed-tools` only pre-approves running the bridge script with any arguments, so no permission prompt appears. It is not a restriction. The limits (the `--repo` folder checks, the `--out` locations, the denylist and the content scan) are enforced inside the script.
