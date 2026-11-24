---
description: Delegate code edits to Antigravity in an isolated workspace and review the resulting patch
argument-hint: <task and approved input files, or status/resume/cancel followed by a job ID>
---

Request: $ARGUMENTS

Use `${CLAUDE_PLUGIN_ROOT}/bin/agy-bridge implement --dry-run --repo <absolute project path> --file <path> -- "<task>"` to inspect the input manifest. Add explicit `--file` or `--glob` arguments for the code, tests and build configuration this task needs. Files appearing in the manifest may be processed by Google's model.

Submit with `implement --repo ... --profile <dedicated Antigravity home> --file ... -- "<task>"`. Use `--write-dir` to allow edits and new files under a specified directory, and `--check` only with a test argv the user authorizes. Read `docs/IMPLEMENT.md` for the host and authentication requirements. The command returns a job ID. Use `implement-status <id>`, `implement-resume <id>`, and `implement-cancel <id>` as requested. Read `implement-patch <id>` and report the actual changes and check outcomes.

Apply the patch only if the user authorizes it after review. Use `implement-apply <id> --sha256 <reviewed hash>` to check that the reviewed patch still applies to the original files.
