---
description: Delegate code edits to Antigravity in an isolated workspace and review the resulting patch
argument-hint: <task and approved input files, or status/patch/resume/cancel followed by a job ID>
---

Request: $ARGUMENTS

For an existing job, use `${CLAUDE_PLUGIN_ROOT}/bin/agy-bridge implement-status <id>`, `implement-patch <id>`,
`implement-resume <id>`, or `implement-cancel <id>` as requested. Report the phase and recorded check outcomes.

For a new task, use `${CLAUDE_PLUGIN_ROOT}/bin/agy-bridge implement --dry-run --repo <absolute project path>
--file <path> -- "<task>"` to inspect the input manifest. Add explicit `--file` or `--glob` arguments for the code,
tests and build configuration the task needs. Google can process the files that appear in the manifest.

Submit with `implement --repo ... --profile <dedicated Antigravity home> --file ... -- "<task>"`. Use `--write-dir`
to allow edits and new files under a specific directory, and use `--check` with a test argv the user authorized.
Read `docs/IMPLEMENT.md` for host and authentication requirements. Return the job ID and use `implement-patch <id>`
to review the changes after the worker has saved a patch.

Apply after the user reviews and authorizes the patch. `implement-apply <id> --sha256 <reviewed hash>` verifies the
hash and original files, then writes the reviewed changes to the working tree.
