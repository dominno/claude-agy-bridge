---
description: Delegate code edits to Antigravity in an isolated workspace and review the resulting patch
argument-hint: <task and approved files, or status/patch/resume/cancel/apply with a job ID>
---

The user's request follows.

$ARGUMENTS

For an existing job, use the command that matches the request.

- `"${CLAUDE_PLUGIN_ROOT}/bin/agy-bridge" implement-status <id>`
- `"${CLAUDE_PLUGIN_ROOT}/bin/agy-bridge" implement-patch <id>`
- `"${CLAUDE_PLUGIN_ROOT}/bin/agy-bridge" implement-resume <id>`
- `"${CLAUDE_PLUGIN_ROOT}/bin/agy-bridge" implement-cancel <id>`

Report the phase and recorded check outcomes. If the job ID is missing, locate it with
`"${CLAUDE_PLUGIN_ROOT}/bin/agy-bridge" jobs list --repo "<absolute project path>"`.

For a new task, preview the input manifest with
`"${CLAUDE_PLUGIN_ROOT}/bin/agy-bridge" implement --dry-run --repo "<absolute project path>" --file "<path>" -- "<task>"`.
Add explicit `--file` or `--glob` arguments for the code, tests, and build configuration the task needs. Google can
process the files that appear in the manifest. Read `"${CLAUDE_PLUGIN_ROOT}/docs/IMPLEMENT.md"` for host and
authentication requirements.

Submit with `"${CLAUDE_PLUGIN_ROOT}/bin/agy-bridge" implement --repo "<absolute project path>" --profile "<dedicated Antigravity home>" --file "<path>" -- "<task>"`.
Use `--write-dir` to allow edits and new files under a specific directory, and use `--check` with a test argv the user
authorized. Shell-quote each supplied argument as a literal value. Return the job ID and use
`"${CLAUDE_PLUGIN_ROOT}/bin/agy-bridge" implement-patch <id>` to review the changes after the worker saves a patch.

Apply after the user reviews and authorizes the patch. Run
`"${CLAUDE_PLUGIN_ROOT}/bin/agy-bridge" implement-apply <id> --sha256 <reviewed hash>` to verify the hash and original
files and write the reviewed changes to the working tree.
