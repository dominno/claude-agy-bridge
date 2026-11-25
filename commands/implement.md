---
description: Delegate code edits to Antigravity in an isolated workspace and review the resulting patch
argument-hint: <task and approved files, or status/patch/resume/cancel/apply with a job ID>
disable-model-invocation: true
---

The user's request follows.

$ARGUMENTS

For an existing job, use the command that matches the request.

- `"${CLAUDE_PLUGIN_ROOT}/bin/agy-bridge" implement-status <id>`
- `"${CLAUDE_PLUGIN_ROOT}/bin/agy-bridge" implement-patch <id>`
- `"${CLAUDE_PLUGIN_ROOT}/bin/agy-bridge" implement-resume <id>`
- `"${CLAUDE_PLUGIN_ROOT}/bin/agy-bridge" implement-cancel <id>`

Report the phase and recorded check outcomes. If `jobs show`/`implement-status` prints
`WARN check inputs changed by the agent: <paths>`, tell the user that `verified` is not trustworthy until those
files (the check script, test configuration or CI workflow the agent edited) are reviewed. If the job ID is missing, locate it with
`"${CLAUDE_PLUGIN_ROOT}/bin/agy-bridge" jobs list --repo "<absolute project path>"`.

`implement` requires Linux with Bubblewrap 0.8.0 or later and working unprivileged user namespaces; it does not run
on macOS. On macOS use the read-only `/agy:delegate` or `/agy:queue` instead. On Linux, check the host first with
`/agy:doctor --implementation --profile PATH`.

For a new task, preview the input manifest with
`"${CLAUDE_PLUGIN_ROOT}/bin/agy-bridge" implement --dry-run --repo "<absolute project path>" --file "<path>" -- "<task>"`.
Add explicit `--file` or `--glob` arguments for the code, tests, and build configuration the task needs. Google can
process the files that appear in the manifest. Read `"${CLAUDE_PLUGIN_ROOT}/docs/IMPLEMENT.md"` for host and
authentication requirements.

Before launch, tell the user that the AGY agent shares the host network (bwrap `--share-net`): it can reach host
loopback services, the LAN and cloud instance metadata, held back only by AGY's tool policy. Recommend a VM or
container without sensitive local services (IMDSv2 with hop limit 1 on cloud VMs).

Confirm before launch. Show the dry-run manifest and the network note above; unless the user's invoking message
already approved exactly this file set, `--write-dir` and `--check`, STOP, ask the user to confirm and launch only
after the user confirms.

Submit with `"${CLAUDE_PLUGIN_ROOT}/bin/agy-bridge" implement --repo "<absolute project path>" --profile "<dedicated Antigravity home>" --file "<path>" -- "<task>"`.
Use `--write-dir` to allow edits and new files under a specific directory, and use `--check` with a test argv the user
authorized. `--check` takes one JSON array argv, e.g. `--check '["python3","-m","pytest","-q"]'`. Shell-quote each supplied argument as a literal value. Return the job ID and use
`"${CLAUDE_PLUGIN_ROOT}/bin/agy-bridge" implement-patch <id>` to review the changes after the worker saves a patch.

Apply after the user reviews and authorizes the patch. Save
`"${CLAUDE_PLUGIN_ROOT}/bin/agy-bridge" implement-patch <id> --raw > <file>` to a file, review that file, hash it
(`sha256sum <file>`, or `shasum -a 256 <file>` on macOS), and compare the hash with `patch_sha256` from
`implement-status <id>` (or `jobs show <id>`). Then pass that hash to
`"${CLAUDE_PLUGIN_ROOT}/bin/agy-bridge" implement-apply <id> --sha256 <reviewed hash>`, which verifies the hash and
original files and writes the reviewed changes to the working tree.
