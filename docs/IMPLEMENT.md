# Implement code with AGY

`implement` gives Antigravity an explicit snapshot of project files. AGY can edit the private copy, and the bridge
calculates a Git patch from the resulting files. The AGY session permits file edits and blocks shell tools. An optional
check runs offline on a separate copy of the exact candidate bytes. Failed checks can trigger two more AGY turns in
the same conversation.

## Set up the worker

Use Linux with Bubblewrap in `/usr/bin` or `/bin` and working unprivileged user namespaces, Git, and AGY CLI 1.5.16
or later in major version 1. The worker runs `agy --version` inside Bubblewrap before sending a task. Use a Linux host
or VM with working user namespaces if your container disables them. [Recorded compatibility and evidence](COMPAT.md).

Create a dedicated profile outside the project and the bridge's job directory. For API-key authentication, set
`GEMINI_API_KEY` in the invoking environment and pass `--gemini-api-key`. The bridge selects the Gemini model provider
in that session. A dedicated profile with cached credentials usable inside Bubblewrap can also work. The sandbox
exposes the dedicated profile without the host keyring. [AGY authentication](https://antigravity.google/docs/install?tab=cli).

From your Git project, check the isolated worker startup. This probe only runs `agy --version` inside Bubblewrap.
It does not submit a job or check the profile's authentication.

```sh
mkdir -m 700 "$HOME/agy-implementation-profile"
bin/agy-bridge doctor --implementation --repo "$PWD" --profile "$HOME/agy-implementation-profile"
```

## Preview and submit

```sh
bin/agy-bridge implement --dry-run --repo "$PWD" \
  --glob 'src/**/*.py' --glob 'tests/**/*.py' --file pyproject.toml \
  --write-dir src --write-dir tests \
  --check '["python3", "-m", "unittest", "discover", "-s", "tests"]' \
  -- 'Fix the parser bug and cover it with a regression test.'
```

Review the manifest and `files_not_sent`. The snapshot uses the current bytes of the selected files, including dirty
or untracked inputs that match your selection, and records the current Git `HEAD`. Include the source, test, and
configuration files needed by the check. Submission can send the task and selected files to Google for processing.

Submit the same command without `--dry-run` and include the profile. This example assumes `GEMINI_API_KEY` is already
set in your environment:

```sh
job_id=$(bin/agy-bridge implement --repo "$PWD" \
  --profile "$HOME/agy-implementation-profile" --gemini-api-key \
  --glob 'src/**/*.py' --glob 'tests/**/*.py' --file pyproject.toml \
  --write-dir src --write-dir tests \
  --check '["python3", "-m", "unittest", "discover", "-s", "tests"]' \
  -- 'Fix the parser bug and cover it with a regression test.' \
  | python3 -c 'import json, sys; print(json.load(sys.stdin)["job_id"])')
```

A job accepts up to 500 regular text files, 1 MiB per file, and 8 MiB total. The snapshot preserves executable bits.
Automatic AGY customizations, secret-shaped input, links, and special files are filtered or refused. With no
`--write-dir`, the bridge exports changes to existing staged files only. Each `--write-dir` authorizes the subtree,
including new files. Use `--write-dir .` for the full isolated tree. The bridge validates the authorization against
the actual change inventory.

## Inspect and resume

List jobs to find the ID if you closed the submitting session. Add `--active` for currently running work and `--json`
for a versioned machine-readable result. With no `--repo`, the listing covers implementation jobs for your user.

```sh
bin/agy-bridge jobs list --active
bin/agy-bridge jobs list --repo "$PWD" --json
bin/agy-bridge implement-status "$job_id"
bin/agy-bridge implement-patch "$job_id"
bin/agy-bridge implement-cancel "$job_id"
bin/agy-bridge implement-resume "$job_id"
```

Submission starts a detached local worker and returns a job ID. Use `--foreground` for a synchronous exit code.
The Linux host and worker must keep running. The defaults are 25 minutes per AGY turn and two hours per invocation.
Set `--turn-timeout` or `--job-timeout` at submission. The bridge can finish processing a patch after stopping AGY.
A resume starts a fresh invocation and continues the same working copy and saved AGY conversation when available.
Cancellation, timeout, quota exhaustion, and worker interruption retain the copy. A partial patch is available once
the worker has validated and saved it. `worker_active` and `cancel_requested` in `implement-status` distinguish a
running job from an interrupted worker or a pending cancellation. For API-key jobs, supply `GEMINI_API_KEY` in the
resume environment as well.

`verified` means the configured check passed against the captured patch. `ready` means the job produced a patch without
a configured check. `checks_failed` includes the last nonzero check result. The check uses the supplied JSON argv, a
system or workspace-relative executable, and a separate offline Bubblewrap sandbox. Put required tools on the host
software image and include the project inputs explicitly. CPU, memory, and disk limits depend on the host. The AGY
sandbox allows provider network access and mounts the isolated writable profile. The private job directory under
`~/.agy-bridge/implement/` keeps the files, patch, and state. A failed check's status may include a filtered excerpt of
its output. Remove completed job directories when you no longer need their artifacts.
[Full command and status reference](CLI.md#implementation-jobs).

## Apply after review

```sh
patch_path=$(mktemp)
bin/agy-bridge implement-patch "$job_id" > "$patch_path"
cat "$patch_path"
```

For a `ready` or `verified` job, apply only after reviewing the displayed diff. Compare the SHA-256 of the reviewed
file with `patch_sha256` from `implement-status`:

```sh
reviewed_patch_sha256=$(sha256sum "$patch_path" | cut -d ' ' -f 1)
bin/agy-bridge implement-apply "$job_id" --sha256 "$reviewed_patch_sha256"
git diff
```

The apply command checks the base commit and original content and mode of each affected file. It rejects existing
paths where the patch adds files and prepares exact result bytes before publishing them. Finish concurrent local
editing before applying. Use Git to inspect, stage, and commit the resulting working-tree changes.
