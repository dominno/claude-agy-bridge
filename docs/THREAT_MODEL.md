# Threat model

## Assets
1. Secrets on the machine: API keys in `.env` files and the environment, tokens, SSH and cloud credentials, the
   sign-in state of other tools (`~/.codex`, `~/.claude`).
2. Repository integrity: the working tree and its history.
3. Confidential repository content that should not leave the machine.
4. The user's attention: wrong text from a model that is applied without checking.

## Attackers and failure modes
- **A prompt-injected or careless Claude** asks the bridge to send secrets or to widen permissions.
- **Hostile repository content** (a symlink, a pipe, a huge file, a trick file name) aimed at the bridge itself.
- **The remote model or `agy`** returns text designed to be executed or applied, or reads more than intended.
- **A bug in the bridge**: a race between checking and copying, a leaked environment variable, an over-open log.
- Out of scope: a malicious `agy` binary, a compromised Google account, a hostile local user with the same uid.

## Rules, enforcement and tests
| Rule | What it guarantees | Where | Main tests |
|---|---|---|---|
| R1 | Denylisted names are never staged. The check covers every component of the path under `--repo` **and** of the `--repo` folder itself, reduced to its on-disk letter case (macOS `F_GETPATH`) and with symlinks resolved, then case-folded and NFKC-normalised. The root may not be `/`, HOME or above HOME (compared by device and inode, not by string), a system folder, or inside a hidden folder of HOME (`~/.config/...`); it must contain a real `.git` directory (a path check only; a symlink or stray `.git` file does not count; a `gitdir:` file must name a real directory inside the repo) unless `--allow-non-git-root`, which never lifts the other refusals. System folders are refused with the flag too: whole trees `/etc`, `/private`, `/var`, `/usr`, `/bin`, `/sbin`, `/dev`, `/System`, `/Library`, `/root`, `/proc`, `/sys`, `/boot` (except children of the scratch places `/tmp`, `/private/tmp`, `/var/tmp`, `/private/var/tmp` and the macOS temp folder `/var/folders`), and the folder itself for `/Volumes`, `/Users`, `/home`, `/opt`, `/mnt`, `/media`, `/tmp`; also `~/Library`. A project below `/Volumes/<disk>/` or in your home folder (not in a hidden folder) is fine. HOME is taken from `$HOME`, `~` and the password database. The denylist is name based: a secret in an innocent file name is caught only by R5 | `denylist.py`, `stage.check_root`, `stage.prepare` | `test_denylist.py` (`test_r1_*`), `test_stage.py` (`test_r1_*`), `test_integration.py` (`test_r1_*`) |
| R2 | The environment passed to `agy` (also for `--version` and `agy models`) is exactly HOME, PATH, LANG, TERM, TMPDIR, with empty and relative PATH entries removed; `agy` is looked up on that cleaned PATH and refused if it resolves inside the repo. It does **not** limit what `agy` can read through the real HOME | `config.build_agy_env`, `Popen(env=...)` | `test_r2_*` (the fake records its environment with four fake secrets exported; a plain `/usr/bin/env` run proves the exact set) |
| R3 | No permission-widening flag is ever built; the model name is validated | `runner.build_argv`, `config.validate_model` | `test_r3_*`, `test_r3_runtime_code_never_names_...` |
| R4 | `agy` runs with the staging folder as working directory, never the repo; staging is 0700 | `stage.materialize`, `runner.run_agy` | `test_r4_*` |
| R5 | A key-shaped string in a file or the task refuses the whole delegation; messages hold path, count and first hit line, never matched text. The opt-in, fixed example exception is specified below | `scan.py`, `stage.prepare` | `test_scan.py`, `test_stage.py` (`test_r5_*`), `test_integration.py` (`test_r5_*`) |
| R6 | Symlinks (also in parent folders), hard links, `..`, outside absolute paths, pipes, devices, folders, binary and oversized files, and names differing only in case are refused; files are opened without following links and read once | `stage._read_regular`, `stage._relative` | `test_stage.py` (`test_r6_*`), `test_integration.py` (`test_r6_*`) |
| R7 | The legacy home log holds only time, exit code, model, counts, seconds and a short task hash. The repo event journal adds sent paths/full hashes/byte counts, not-sent paths/rules, dry-run/truncation and citation counts; neither stores contents, task text or answer text. Both use 0600 files in 0700 folders | `report.write_log`, `cli` | `test_r7_*` |
| R8 | Delegated answers are text, never executed by delegate or queue (explicit graders execute proposals under the boundary below); control codes stripped; delegate writes only to temp or `<repo>/.agy-bridge/`; queue overrides stay inside safe repo folders; `--out` never overwrites and never follows a symlink | `report.py` | `test_r8_*` |
| R9 | (`allowed-tools` pre-approves the script with any arguments; it is not a restriction, the code-side checks are.) No hooks, no `userConfig`, no skills or agents, no network module, no `shell=True`, `eval`, `exec` or `os.system` in the plugin | repository checks | `test_r9_*` in `test_hygiene.py` |

## Race handling
The bridge reads each file once into memory (descriptor opened with `O_NOFOLLOW`, every parent folder opened the same
way), scans that buffer, and later writes that same buffer. A file changed after reading cannot change what is sent
(`test_staged_bytes_are_the_scanned_bytes`).

## Residual risks (not mitigated)
- `allowed-tools: Bash(<script>:*)` pre-approves the script with any arguments (any `--repo`, any `--out` within the
  script's own limits). It adds no restriction; the checks live in the script.
- A grandchild process that keeps the pty open and ignores the kill is reported as a timeout (exit 12).
- `agy --version` (also run by `dry-run`) executes the real `agy` binary found on the cleaned PATH, in an empty folder. `doctor --repo` applies the same inside-the-repo refusal; without `--repo` it cannot.
- Delegate and queue never run git. Explicit local graders use the scratch process launcher in `runner.py`:
  a trusted case argv, or fixed patch/git-apply argv. Git runs only in fresh scratch, with global/system config
  disabled and a ceiling to avoid parent repositories. No source-repo git metadata is copied. Static checks
  preserve the delegate launch restrictions and isolate the new grading capability.
- Stale-folder sweep removes only real, user-owned, mode-0700 folders named exactly like `mkdtemp` output (`agy-bridge-xxxxxxxx`, `-ver-`, `-doctor-`) older than three hours (the longest allowed run is one hour, plus a two-hour margin).
- Secrets the scan cannot see: encoded, split across lines, or without a known shape. The scan is a net, not a proof.
- Confidential but not secret content is still sent to Google.
- `agy` runs with the real `HOME`; its own tools might read other files or use the network. Unverified, see `COMPAT.md`.
- A very long prompt-injection inside staged files can steer the remote model; the answer is untrusted for that reason.
- Delegate/agy cleanup targets the process group; a program that leaves that group is not stopped.
  Sandboxed graders prevent this escape with denied fork (macOS) or a PID namespace (Linux). Graders run
  with `--unsafe-no-sandbox` retain the same detached-child limit as delegate.
- The repo-local `.agy-bridge.json` can add deny rules and name context files; it cannot remove rules or execute commands.
- Glob expansion walks the repo (denylisted folders are pruned, at most 100,000 files seen).

## Fixed example fragments, never arbitrary scan bypasses
`--allow-fragment-file` is explicit and applies only to selected file contents. Its file must be inside the
validated repo, must pass the built-in and extra path denylist, and is read once using the same pinned root and
no-follow component traversal as other inputs. Symlinks, hard links, non-regular files and files above 16 KiB
are refused. It must be UTF-8 with one literal per line, each at least 12 characters. Blank lines, whitespace
padding, unsupported literals and empty files fail closed without echoing their contents. No regex, wildcard,
case folding or substring replacement is supported. It is not automatically staged.

The complete supported vocabulary is the literal text inside this fenced block, including backticks:

```text
`EXAMPLE_API_KEY`
`SERVICE_API_KEY`
```

Only a variable-name rule match immediately surrounded by those exact backticks, with no adjacent ASCII identifier characters outside them, can be exempted. An optional
quote followed by whitespace and `:` or `=` after the closing backtick prevents exemption (including across
lines). Exemption also requires no run of 16 or more `[A-Za-z0-9_\-+/=]` characters after the variable
on its line or anywhere on the next nonblank line, regardless of separator (including prose, arrows,
table cells, non-breaking spaces and full-width punctuation). Value signals override the allow file;
ambiguous mentions refuse. A bare variable name, a larger identifier, a different case or a value is not an allowed fragment.
Every other scanner rule still runs on the **original, unchanged buffer**, including overlapping assigned-secret
matches. Provider-key shapes, JWTs, auth headers, private-key headers and assigned values cannot be exempted,
even when surrounded by backticks or written in the allow file. Allowlisted mentions do not affect path checks,
task scanning or the scan of generated TASK.md. File buffers sent are still exactly the scanned buffers.
This is deliberately a two-literal vocabulary of non-credential examples, not a general false-positive override.
Tests in `test_v2_notice.py` cover actual-looking invented keys and the original refusals; no real key is used.

## Omissions and citation evidence
A skipped/refused path is visible in the result header and as `{path, rule}` in `not_sent`. R5 includes the
one-based first remaining hit line in the header. Key-shaped filenames are redacted instead of printed or
journaled. Denylisted directories are named as pruned; their contents are never enumerated merely to improve
a notice. A missing glob match is reported by its pattern. Other selected files blocked by a whole-task
failure carry informational rule `TASK`; this is not a permission exception. Original refusal exit codes stay.

`--cite` requests a strict line format. Every cleaned answer line is retained and checked, including malformed
and blank internal/leading lines; trailing blank lines are ignored. Fixed-delimiter parsing avoids nested
regex backtracking on hostile answers. Only whitespace is normalised; path matching is exact against the sent
manifest, with no path resolution, reopening, traversal or reads of unselected files. Quotes must contain at
least 8 characters after whitespace normalisation and at most 25 whitespace-separated words. Short nonempty
quotes are UNVERIFIED with reason `too short`. VERIFIED asserts substring presence in the staged file bytes decoded as UTF-8;
it says nothing about CLAIM's truth, completeness, context or whether the source itself is reliable. A false
claim with a real quote can be VERIFIED. Source prompt injection remains possible. Output control stripping
still applies before checking. Citation reports bypass the ordinary long-answer preview so lines are not lost;
the existing output cap still bounds the answer, reports truncation and keeps exit 16. `--require-verified`
requires `--cite`; it adds exit 17 only after an otherwise successful run with unverified lines.

## Repository event journal
`<repo>/.agy-bridge/events.jsonl` is separate from the legacy home log. It stores UTC `ts`, `model`, sent `files`
(`path`, full `sha256`, `bytes`), `not_sent` (`path`, `rule`), `total_bytes`, `seconds`, `exit_code`, `truncated`,
`dry_run`, and `cite: {verified, unverified}` only with `--cite`. It stores no task text or task hash, answer text,
quotes, claims, file contents or allow-file literals. Sent means handed to the started agy task process, not
proof of upload or remote reading. Dry runs, version-only checks and pre-send refusals record no sent files and
zero sent bytes. Outside-repo input paths are journaled as `<outside repo>`, including early argument failures.
Metadata about filenames can still be confidential; ignore `.agy-bridge/` in target repos.
The bridge's own `.gitignore` already does so. The journal directory is also denylisted against staging.

Event paths are opened component-by-component from the filesystem root with no symlink following, like
`--out` creation. The scratch folder is created/tightened to 0700; the regular, single-link, user-owned journal
is created/tightened to 0600. File ownership/type/link count are checked before chmod or append. FIFOs use
nonblocking opens and are rejected; directory/symlink/hard-link targets cannot be modified. Advisory file locks
serialize appends and protect tail reads. `events --tail N` (N from 1 to 10,000, default 10) streams the journal keeping
only its tail, parses/re-encodes JSON to escape terminal controls, and never runs agy, creates or appends records.
Reads are bounded to 64 KiB plus one byte per record; oversized records fail explicitly without being read whole.
A missing journal prints nothing. Invalid JSON reports an error instead of silently skipping records.

A journal open is attempted before sending. Symlinked paths and other unsafe targets (wrong owner, hard links,
non-regular files) still refuse before sending. Ordinary I/O failures such as permissions or full disks warn
on stderr and delegation continues with its real exit code. Append failures also warn and never replace that
code: exit 4 must not describe a successful task already sent. The open descriptor pins the destination if the scratch path is subsequently swapped.
Unsafe/invalid repo roots, unsafe/unwritable event paths and argument-parser failures cannot receive a repo
record; this limit is reported for dispatched delegations. SIGINT,
SIGTERM and SIGHUP unwind through the normal event/cleanup path. SIGKILL, process crashes and power loss cannot
be guaranteed a record; no fsync/durable transaction with Google's service is claimed.

## Unattended queue boundary

Job JSON and result Markdown are untrusted repository content. A user who starts `queue watch` authorizes
sending bounded tasks from that queue through the same delegate checks; installation never starts it.
A malicious job can choose a permitted model, task, timeout and safe files in that repository. It can incur
provider usage and exfiltrate confidential content that passes the named-path/key scan, so review producers and
inputs before enabling unattended work. It cannot select a different root, output path, shell command,
scan exemption, environment variable, permission flag or grader command. Unknown job/config fields refuse.
No commands run from jobs or configuration. The watcher runs the existing delegate library, not a second
staging or scan implementation. A key scan file drop omits the refused file; it never exempts its bytes.

Queue/results paths must remain within the validated repository. Every directory component is pinned with
no-follow descriptors; symlink/hard-link/nonregular inputs refuse. Only the reserved workflow-storage prefix
`.agy-bridge/` is excepted from the storage denylist; it remains denied as an input. Overrides cannot enter
other denied folders. Results/jobs use exclusive writes and no-overwrite archive moves. A persistent advisory
PID lock covers both run and watch; another runner refuses. Status uses lock state, not PID existence alone.
An interrupt cleans staging, kills the process group and retains a pending job. An existing result is reconciled only on an exact pending-job SHA-256 match and no archive-name conflict;
reused names are refused with new evidence instead of reusing an old answer; a hard crash cannot guarantee exactly-once provider delivery.

NEED lines are model requests, never authorization to run commands. A job must opt in with `"need": true`
(default false). Lines over 1024 characters are ignored; parsing is linear. Filesystem examination stops
after 100 NEED candidates or 64 KiB of candidate text (including annotations). At most three additional distinct
repo-relative existing files are considered for one followup round. The same denylist and no-follow regular
file rules apply, followed by the same delegate scan. No outside file, symlink, .env, .git or .ssh content can
be obtained this way. Opt-in has no source-directory allowlist: confidential non-key files anywhere in
the repository can still be requested, even if unrelated to the task. Result text is cleaned and retained as unverified output; citation annotations do not
endorse claims. Facts in attempt headers come from the exact in-memory delegate capture, not journal-tail
matching or answer text. Results remain untrusted even after a recorded verdict.

Verdicts reuse the events journal's pinned, locked, owned single-link file checks. The journal stores only
job, reader, verdict, evidence/note and timestamp; callers must keep answer/task content out of evidence.
It is local metadata, not a signature or access-control mechanism. A malicious same-uid process is outside
the threat model and can forge local files. Stats parse only result headers, never answer rounds.

## Explicit local graders

`grade-mutants` and `apply-diff` copy safe source buffers into fresh random scratch trees outside the repo.
The case and its interpreter/checks are trusted caller inputs; mutant replacements and diffs are untrusted
model-written code, executed when those checks import/run them. Mutants and diff headers must target named
`solution_files` (diffs fall back to declared `files` when no solution list is supplied). Extra repository
`deny` rules apply to destinations and diff paths as well as sources. Unified syntax only; traversal,
binary, symlink, rename/copy, test rewrites outside the solution list, and undeclared file creation refuse.
Every strategy gets a fresh tree. Git has no inherited/global/system config or parent repository discovery.

Both patch tools and test commands require an OS sandbox. macOS uses a generated default-deny `sandbox-exec`
profile: no network, no writes outside the current tree except `/dev/null`, file-content access limited to the chosen
interpreter/runtime, OS libraries and scratch, and explicit credential-folder and `.git`/`.env*` denials.
General path metadata is readable; credential denials include metadata. Process-fork and execution of other
binaries are denied, so subprocess-dependent checks fail rather than escape. A literal `/` read allowance
is required on macOS 13; it does not expose descendants. Secret denials follow all allowances. The selected
interpreter's framework Python.app/Python3.app re-exec binary and runtime are read/exec inputs, including
virtualenv links and the installed Command Line Tools framework. Linux uses
`bwrap`, or `unshare` plus user/mount/network/PID namespaces and a minimal chroot (never network-only
`unshare -n`); the runtime is read-only, credential folders are hidden, and only scratch is writable.
Capabilities are dropped before checks; namespace teardown kills even detached children. Linux namespace creation must be permitted by the host.
No backend silently falls back if missing, denied by the host, or unable to start.

Before checks or patch strategies, preflight runs only the case's Python interpreter with
`-I -S -c 'print(1)'` (bridge Python for non-Python check commands), in a new session with a hard 10-second
execution timeout. It imports no model-written code. Any failure, signal, wrong output or timeout reports
"sandbox unavailable on this machine" and refuses execution unless the caller explicitly selects
`--unsafe-no-sandbox`. Sandboxed patch/git startup failures, signals and timeouts also abort measurement;
after preflight passes, signals or printed sandbox diagnostic strings during ordinary checks are measured
failures: a failed reference, KILLED mutant or failed diff tests, including denied hostile operations.
The unsafe flag skips preflight; it is never an automatic fallback.

The environment remains scrubbed, with scratch HOME/temp directories plus explicit case env. Each run starts
a new session; its group is killed on timeout, interruption and completion. Every post-kill wait is bounded
to one second; an unkillable child is abandoned and explicitly reported. No unbounded wait/pipe drain follows
a kill. Scratch evidence is retained.
A caller may explicitly pass **`--unsafe-no-sandbox`**, which disables confinement and runs model-written
code with the caller's privileges and network access, with a warning. In that mode, a `setsid()` or double-fork
child can escape group cleanup. The flag is never inferred from model prose, jobs, NEED or config.

This execution surface belongs only to explicitly invoked graders. Delegate and queue store answers as text
and cannot launch graders. The trusted Linux namespace setup shell quotes argv data and executes no model
setup instructions. The bridge imports no network library; confinement is enforced by the operating system,
not by the prompt or the clean environment. See [GRADING.md](GRADING.md) for runtime paths and case limits.
