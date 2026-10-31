---
description: Show exactly which files and which agy command line a delegation would use, and send nothing
argument-hint: <task> [files or globs to include]
allowed-tools: Bash(${CLAUDE_PLUGIN_ROOT}/bin/agy-bridge:*)
---

Request: $ARGUMENTS

Run `${CLAUDE_PLUGIN_ROOT}/bin/agy-bridge dry-run --repo <absolute path of the current project folder> --file <path> [--glob <pattern>] -- "<task>"`
and show the user the manifest (path, bytes, hash prefix) and the agy command line it prints. No file contents and no task text are sent to agy. It writes the checked files to a temporary 0700 folder that is removed before it exits, and runs `agy --version` with the five-name environment in an empty folder. That is the only program a dry run starts; delegate and dry-run run no git.
The same refusals apply as for a real delegation, so a refusal here (exit code 4) predicts a refusal there.
Run this before the first delegation of a session and whenever the set of input files changes.

Note: `allowed-tools` only pre-approves running the bridge script with any arguments, so no permission prompt appears. It is not a restriction. The limits (the `--repo` folder checks, the `--out` locations, the denylist and the content scan) are enforced inside the script.

`--cite`, `--require-verified` (with `--cite`) and `--allow-fragment-file` are also accepted here. No citation is
verified without an answer. Inspect every `FILES NOT SENT:` notice as well as the manifest: a glob can succeed
while denylisted matches are skipped. R5 notices show the first key-shaped hit's line, never its text.

Dry runs now append metadata to `<repo>/.agy-bridge/events.jsonl`, with `dry_run: true`, no sent files and zero
sent bytes. They still send no task or file contents and do not write the legacy home log. Add `.agy-bridge/` to
the target repo's `.gitignore`. Read recent metadata with
`${CLAUDE_PLUGIN_ROOT}/bin/agy-bridge events --repo <absolute project root> --tail 10`.
See `docs/THREAT_MODEL.md` for the two supported example fragments and the safe-journaling limits.
