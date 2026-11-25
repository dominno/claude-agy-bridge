# Implement code with AGY

`implement` gives Antigravity an explicit snapshot of project files. AGY can edit the private copy, and the bridge
calculates a Git patch from the resulting files. The AGY session permits file edits and blocks shell tools. An optional
check runs offline on a separate copy of the exact candidate bytes. Failed checks can trigger two more AGY turns in
the same conversation.

## Set up the worker

Use Linux with Bubblewrap 0.8.0 or later (the sandbox passes `--disable-userns`; Ubuntu 22.04 ships 0.6.x, and
Debian 11 and RHEL 9 also ship older versions; Debian 12+/Ubuntu 24.04+ qualify) in `/usr/bin` or `/bin` and working unprivileged user namespaces, Git, and an AGY CLI native
binary at version 1.5.16 or later in major version 1. Use the root of a Git checkout whose Git directory is inside
the checkout. The worker runs `agy --version` inside Bubblewrap before sending a task. Use a Linux host
or VM with working user namespaces if your container disables them. [Recorded compatibility and evidence](COMPAT.md).

Create a dedicated profile outside the project and the bridge's job directory. For API-key authentication, set
`GEMINI_API_KEY` in the invoking environment and pass `--gemini-api-key`. The bridge selects the Gemini model provider
in that session. A dedicated profile with cached credentials usable inside Bubblewrap can also work. The sandbox
exposes the dedicated profile without the host keyring. [AGY authentication](https://antigravity.google/docs/install?tab=cli).

Install `agy-bridge` with `python3 -m pip install /path/to/claude-agy-bridge` in your virtual environment, or use
the skill's `scripts/agy-bridge` in place of `agy-bridge` below. From your
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

Some paths are never part of an implementation workspace, never exported as changes and refused again at apply time,
because an edit there would steer the next coding agent or run code on checkout, commit or open: the AGY
customization roots `.agents/`, `.agent/`, `_agents/` and `_agent/`, `AGENTS.md`, `AGENTS.override.md`, `GEMINI.md`,
`CLAUDE.md`, `CLAUDE.local.md`, `.claude/`, `.cursorrules`, `.cursor/`, `.windsurfrules`, `.windsurf/`, `.clinerules`,
`.roo/`, `.kiro/`, `.github/copilot-instructions.md`, `.github/instructions/`, `.github/prompts/`, `.mcp.json`,
`.agy-bridge.json`, `.gitmodules`, `.envrc`, `.direnv/`, `.vscode/`, `.idea/`, `.githooks/`, `.husky/`,
`.pre-commit-config.yaml` and `.devcontainer/`. Names match at any depth (a `sub/CLAUDE.md` or
`sub/_agents/rules/a.md` is refused too); the `.github/...` entries match from the repository root. CI files such as
`.github/workflows/` stay editable, so review them in the patch like any other change. Repository deny rules in
`.agy-bridge.json` add to this list.

Added or modified files may not gain terminal control or invisible characters:

- C0 controls other than tab and newline, a carriage return not followed by a newline, DEL and C1 controls;
- the bidirectional overrides and isolates U+202A-U+202E and U+2066-U+2069;
- invisible Unicode (`core.text.is_invisible`): every format (Unicode category Cf), line and paragraph separator (Zl/Zp,
  including U+2028 and U+2029), private use (Co) and surrogate (Cs) code point, which covers among others U+00AD,
  U+061C, U+0600-U+0605, U+180E, the zero-width and format characters U+200B-U+200F, U+2060-U+2064 and
  U+206A-U+206F, U+FFF9-U+FFFB and U+FEFF (allowed only at offset 0 of a file as a byte order mark); plus U+034F,
  the Hangul fillers U+115F, U+1160, U+3164 and U+FFA0, U+17B4/U+17B5, U+1D173-U+1D17A, variation selectors
  U+FE00-U+FE0F and U+E0100-U+E01EF, and tag characters U+E0000-U+E007F.

A line holding such characters is tolerated only when an identical line already exists in the
original file (each original line covers one copy); any other such line, including an existing escape swapped for a
different one on the same line, refuses the export (and the apply) and the refusal names the file, line and first
code point (for example `U+E0041`), never the raw character. When `jobs patch` writes to a terminal, any remaining
non-printable character is shown as an escape such as `\x1b` and invisible characters as `\u{200b}`; written to a
pipe or file it still shows the invisible class as `\u{XXXX}` markers and keeps every other byte. Pass `--raw` for
the exact bytes whose SHA-256 `jobs apply --sha256` expects.

Answers from AGY (delegate output, queue result files and the check-output tail of implement jobs) have the same
invisible characters removed; when any were removed a final line says `note: N invisible Unicode characters
removed` (in `--cite` mode the note is a diagnostic on stderr, so it never counts as an unverified line).

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
define the check along with the rest of the patch. A `verified` job records `check_inputs_changed`: the changed
paths that are an argv path of `--check` (argv[0] included, normalised) or lie under one (`pytest tests` covers
`tests/test_x.py`; a bare `.` does not count), a file named `conftest.py`, `pytest.ini`,
`tox.ini`, `setup.cfg`, `pyproject.toml`, `noxfile.py`, `setup.py`, `package.json`, `Makefile`, `GNUmakefile`,
`makefile`, `justfile`, `sitecustomize.py` or `usercustomize.py`, a `*.pth` file, or a file under
`.github/workflows/`. When the list is not empty, `jobs show` (JSON `warning` field and stderr) and `jobs list` print
`WARN check inputs changed by the agent: <paths>; review them before trusting verified`: the agent may have made
the check pass by changing the check itself, so review those files first. The phase stays `verified`. Put required tools on the host
software image and include the project inputs explicitly. CPU, memory, and disk limits depend on the host. The AGY
sandbox allows provider network access and mounts the isolated writable profile. It uses bwrap `--share-net`,
the host network namespace: the agent can reach host loopback services, the local network, cloud instance metadata
(`169.254.169.254`) and abstract-namespace Unix sockets (X11, some D-Bus). Only AGY's session policy, which denies
its command, URL and MCP tools, keeps it from using them. Run implementation jobs in a VM or container without
sensitive loopback services, and on cloud VMs enforce IMDSv2 with hop limit 1
([network exposure](THREAT_MODEL.md#network-exposure-of-the-agy-agent)). The private job directory under
`~/.agy-bridge/implement/` keeps the files, patch, and state. A failed check's status may include a filtered excerpt of
its output. Remove completed job directories when you no longer need their artifacts: `agy-bridge jobs remove
"$job_id"` for one job, or `agy-bridge jobs prune --older-than 7d --dry-run` to list old finished jobs before pruning.
An unapplied `ready`/`verified` job or an unfinished one needs `--force`; a running worker is never removed.
[Full command and status reference](CLI.md#implementation-jobs).

## Apply after review

```sh
patch_path=$(mktemp)
agy-bridge jobs patch "$job_id" --raw > "$patch_path"
cat "$patch_path"
```

For a `ready` or `verified` job, apply only after reviewing the displayed diff. Compare the SHA-256 of the reviewed
file with `patch_sha256` from `jobs show`:

```sh
reviewed_patch_sha256=$(sha256sum "$patch_path" | cut -d ' ' -f 1)
# macOS has no sha256sum by default: reviewed_patch_sha256=$(shasum -a 256 "$patch_path" | cut -d ' ' -f 1)
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
support `--print`. Both modes require Google Account sign-in. `implement` uses the local Linux worker only.

For **read-only** tasks the bridge can use Remote Agents: `delegate --remote-agent [--remote-project NAME]` (also on
`dry-run`) inlines the checked files into the prompt, drives agy's terminal UI on a pseudo-terminal from an empty
folder and reads the framed reply off the screen. It refuses to paste when agy reports the requested project as not
found. Editing on a remote agent is not supported: the remote environment runs tools without asking, and there is no
patch channel back that the bridge could check. See [CLI.md](CLI.md#remote-agent) and the remote-agent section of
[THREAT_MODEL.md](THREAT_MODEL.md). [AGY sign-in change](https://github.com/google-antigravity/antigravity-cli/releases/tag/1.5.12).
