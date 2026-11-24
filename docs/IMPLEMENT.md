# Implement code with AGY

`implement` gives Antigravity an explicit snapshot of project files. AGY can edit the private copy, and the bridge
calculates a Git patch from the resulting files. The AGY session permits file edits and blocks shell tools. An optional
check runs offline on a separate copy of the exact candidate bytes. Failed checks can trigger two more AGY turns in
the same conversation.

## Set up the worker

Use Linux with Bubblewrap in `/usr/bin` or `/bin` and working unprivileged user namespaces, Git, and an AGY CLI native
binary at version 1.5.16 or later in major version 1. Use the root of a Git checkout whose Git directory is inside
the checkout. The worker runs `agy --version` inside Bubblewrap before sending a task. Use a Linux host
or VM with working user namespaces if your container disables them. [Recorded compatibility and evidence](COMPAT.md).

Create a dedicated profile outside the project and the bridge's job directory. For API-key authentication, set
`GEMINI_API_KEY` in the invoking environment and pass `--gemini-api-key`. The bridge selects the Gemini model provider
in that session. A dedicated profile with cached credentials usable inside Bubblewrap can also work. The sandbox
exposes the dedicated profile without the host keyring. [AGY authentication](https://antigravity.google/docs/install?tab=cli).

Install `agy-bridge` with `python3 -m pip install /path/to/claude-agy-bridge` in your virtual environment. From your
Git project, check the isolated worker startup. This probe only runs `agy --version` inside Bubblewrap.
It does not submit a job or check the profile's authentication.

```sh
mkdir -m 700 "$HOME/agy-implementation-profile"
agy-bridge doctor --implementation --repo "$PWD" --profile "$HOME/agy-implementation-profile"
```

## Preview and submit

```sh
agy-bridge jobs start --dry-run \
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
job_id=$(agy-bridge jobs start \
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
agy-bridge ps
agy-bridge jobs list -C "$PWD" --json
agy-bridge jobs show "$job_id"
agy-bridge jobs patch "$job_id"
agy-bridge jobs cancel "$job_id"
agy-bridge jobs resume "$job_id"
```

Submission starts a detached local worker and returns a job ID. Use `--foreground` for a synchronous exit code.
The Linux host and worker must keep running. The defaults are 25 minutes per AGY turn and two hours per invocation.
Set `--turn-timeout` or `--job-timeout` at submission. The bridge can finish processing a patch after stopping AGY.
A resume starts a fresh invocation and continues the same working copy and saved AGY conversation when available.
Cancellation, timeout, quota exhaustion, and worker interruption retain the copy. A partial patch is available once
the worker has validated and saved it. `worker_active` and `cancel_requested` in `jobs show` distinguish a
running job from an interrupted worker or a pending cancellation. For API-key jobs, supply `GEMINI_API_KEY` in the
resume environment as well. If subprocess cleanup could not be confirmed, verify that the old process has stopped
and start a new job.

`verified` means the configured check passed against the captured patch. `ready` means the job produced a patch without
a configured check. `checks_failed` includes the last nonzero check result. The check uses the supplied JSON argv, a
system or workspace-relative executable, and a separate offline Bubblewrap sandbox. Review changes to any files that
define the check along with the rest of the patch. Put required tools on the host
software image and include the project inputs explicitly. CPU, memory, and disk limits depend on the host. The AGY
sandbox allows provider network access and mounts the isolated writable profile. The private job directory under
`~/.agy-bridge/implement/` keeps the files, patch, and state. A failed check's status may include a filtered excerpt of
its output. Remove completed job directories when you no longer need their artifacts.
[Full command and status reference](CLI.md#implementation-jobs).

## Apply after review

```sh
patch_path=$(mktemp)
agy-bridge jobs patch "$job_id" > "$patch_path"
cat "$patch_path"
```

For a `ready` or `verified` job, apply only after reviewing the displayed diff. Compare the SHA-256 of the reviewed
file with `patch_sha256` from `jobs show`:

```sh
reviewed_patch_sha256=$(sha256sum "$patch_path" | cut -d ' ' -f 1)
agy-bridge jobs apply "$job_id" --sha256 "$reviewed_patch_sha256"
git diff
```

The apply command checks the base commit and original content and mode of each affected file. It rejects existing
paths where the patch adds files and prepares exact result bytes before publishing them. If application is interrupted,
repeat the same explicit `jobs apply` with the reviewed hash. Recovery checks the recorded original and resulting
files and refuses a path that has diverged from both. Finish concurrent local editing before applying. Use Git to
inspect, stage, and commit the resulting working-tree changes.

## Remote execution options

Run `agy-bridge` from a Linux host you can reach via SSH if you need a job to continue after disconnecting your
laptop. Keep the checkout and the dedicated AGY profile on that host. Use `jobs show` and `jobs patch` there to
inspect the result. Applying with `jobs apply` changes the checkout on that host.

Google's [Remote Control](https://antigravity.google/docs/remote-control/?tab=cli) exposes an interactive AGY
session on a running host through a browser. [Remote Agents](https://antigravity.google/docs/remote-agents?tab=cli)
runs in the cloud, using the sources attached to a remote project. An unavailable project identifier can cause
the CLI to open the default project. Check the selected project before entering a task. Remote Agents does not
support `--print`. Both modes require Google Account sign-in. The bridge's current integration uses the local Linux
worker. [AGY sign-in change](https://github.com/google-antigravity/antigravity-cli/releases/tag/1.5.12).
