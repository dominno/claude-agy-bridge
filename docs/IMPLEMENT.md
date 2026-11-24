# Implement code with AGY

`implement` sends an explicit snapshot of project files to Antigravity. AGY can edit a private copy, and the bridge calculates a Git patch from the resulting files. An optional local test command runs offline on a separate copy of those exact bytes. Failed checks can trigger two more AGY turns in the same conversation.

## Set up the worker

Use Linux with Bubblewrap in `/usr/bin` or `/bin` and working unprivileged user namespaces, Git, and AGY CLI 1.5.16 or later in major version 1. The worker probes the sandbox before sending a task and refuses to proceed if isolation is unavailable. This often requires a regular Linux host or VM when containers disable user namespaces.

Create a dedicated, empty profile, outside the project and the bridge's job directory. For API-key authentication, set `GEMINI_API_KEY` in the invoking environment and pass `--gemini-api-key`. The bridge selects the Gemini model provider in that session. A dedicated AGY profile with usable cached credentials can also be passed without that flag. The worker does not make the host keyring available.

```sh
mkdir -m 700 "$HOME/agy-implementation-profile"
bin/agy-bridge implement --dry-run --repo "$PWD" \
  --glob 'src/**/*.py' --glob 'tests/**/*.py' --file pyproject.toml \
  --write-dir src --write-dir tests \
  --check '["python3", "-m", "unittest", "discover", "-s", "tests"]' \
  -- 'Fix the parser bug and cover it with a regression test.'
```

Review the manifest and `files_not_sent`. Submit the same command without `--dry-run` and add `--profile "$HOME/agy-implementation-profile"` and, if appropriate, `--gemini-api-key` before `--`. The task text and approved snapshot can be processed by Google. The snapshot uses the current bytes of the selected files, including explicit dirty or untracked inputs, and records the current Git `HEAD`. Include the source, test, and configuration files needed by the check.

A job accepts up to 500 regular text files, 1 MiB per file and 8 MiB total. The snapshot preserves executable bits. Automatic AGY customizations, secret-shaped input, links and special files are filtered or refused. With no `--write-dir`, the bridge exports changes to existing staged files only. Each `--write-dir` authorizes the subtree, including new files. Use `--write-dir .` for the full isolated tree. The bridge validates the authorization against the actual change inventory.

## Inspect and resume

```sh
bin/agy-bridge implement-status "$job_id"
bin/agy-bridge implement-patch "$job_id"
bin/agy-bridge implement-cancel "$job_id"
bin/agy-bridge implement-resume "$job_id"
```

Submission starts a detached local worker and returns a job ID. Use `--foreground` for a synchronous exit code. The Linux host and its worker must keep running. The default budget is 25 minutes per turn and two hours per worker invocation. Set `--turn-timeout` or `--job-timeout` at submission. The bridge can finish processing a patch after stopping AGY. A new, explicit resume starts a fresh invocation and continues the same working copy and saved AGY conversation when available. Cancellation, timeout, quota exhaustion, and worker interruption retain the copy. A partial patch is available once the worker has validated and saved it.

`verified` means the configured check passed against the captured patch. `ready` means the job produced a patch without a configured check. `checks_failed` includes the last nonzero check result. The check uses the supplied JSON argv, a system executable or workspace-relative executable, and a separate offline Bubblewrap sandbox. Put required tools on the host software image and include the project inputs explicitly. CPU, memory and disk limits depend on the host. The AGY workspace has model network access and an isolated writable profile. Its file edits and check output are private job state under `~/.agy-bridge/implement/`. Keep that directory private and remove completed job directories when their artifacts are no longer needed.

## Apply after review

```sh
bin/agy-bridge implement-apply "$job_id" --sha256 "$reviewed_patch_sha256"
```

Use the hash from `implement-status` that corresponds to the patch you inspected. The apply command checks the base commit and original content and mode of each affected file. It rejects existing paths for new files and prepares exact result bytes before publishing them. Finish any concurrent local editing before applying. Applying from the bridge does not stage or commit the files. The job state keeps the patch available for manual review and recovery.
