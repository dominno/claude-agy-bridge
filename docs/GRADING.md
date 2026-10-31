# Checking results

Queue answers are unverified model output. Citation checks prove only quote presence. Check the actual
sources, then record the check, reader and evidence:

```sh
bin/agy-bridge verdict --repo /path/to/repo --job review-parser-01 --verdict pass --by reader --evidence "Inspected parser and ran its checks" --note "Boundary cases covered"
bin/agy-bridge verdicts --repo /path/to/repo --tail 10
```

Verdicts are append-only JSONL in `.agy-bridge/verdicts.jsonl`, using the same anchored no-follow, owned
single-link, 0600, locked journal implementation as events. They contain time, job, verdict (`pass`, `fixes`,
`reject`), reader, evidence and optional note. Do not put task or answer text in evidence/notes. No model response
is automatically marked verified. Status lists result names with no verdict; a later verdict is another record.

## Trusted case format

Both graders use this generic JSON schema:

```json
{
  "name": "parser-checks",
  "job": "review-parser-01",
  "files": {"impl.py": "src/parser.py", "check.py": "checks/check_parser.py"},
  "solution_files": ["impl.py"],
  "test_cmd": ["python", "check.py"],
  "env": {"PYTHONPATH": "{scratch}"},
  "expect": "pass"
}
```

`files` maps safe scratch-relative destinations to existing source files inside the validated repository.
Absolute source paths are allowed only inside that repo. Symlinks, hard links, denylisted paths, traversal and
nonregular files refuse. Files are read once into buffers and each attempt gets a fresh copy. `name`, `files`
and `test_cmd` are required. `solution_files` is required for mutants and names only destinations in `files`.
`job` is required with `--record`. `env` defaults to `{}`; `expect` (`pass` or `fail`, default pass) documents the
starting tree expectation for diff cases. Diff output reports measured tests, regardless of expect. Mutant
reference checks always require a passing starting tree. Unknown fields refuse.

`test_cmd` is an argv list; the exact token `python` selects the bridge's Python interpreter. `{repo}` and
`{scratch}` substitute in argv and env values. Case arguments are quoted as data, never interpreted as shell
text. The Linux unshare backend uses a fixed mount/chroot setup script before launching that argv.
Each check runs serially with a 300-second limit, a new session, and process-group cleanup on timeout,
interruption and completion. Every post-kill wait is limited to one second; a child that still cannot exit
is abandoned and reported explicitly, without waiting indefinitely or draining pipes.
The environment starts with clean PATH/LANG/SYSTEMROOT, scratch HOME and temp
directories, PYTHONDONTWRITEBYTECODE, plus the case's explicit env entries. No provider keys are inherited.

**The case, interpreter and checks must be trusted; the proposed solution code is untrusted.** Both graders
execute model-written code when the checks import/run the changed files. Execution requires OS confinement:

- macOS: `sandbox-exec -p <generated profile>`, default-deny, no network, file-content reads limited to the scratch tree,
  selected interpreter/runtime and OS libraries; writes limited to that tree (and `/dev/null`). User home
  `.ssh`, `.config`, `.aws`, `.gnupg`, `.claude`, `.codex`, and `.git`/`.env*` paths are explicitly denied.
  General path metadata is readable; the explicit credential denials include metadata.
  The root directory itself has a literal `/` read allowance required on macOS 13 (not a recursive root
  allowance); secret denials follow the allowances and take precedence. The selected executable and its
  resolved framework `Resources/Python.app/Contents/MacOS/Python` (or `Python3.app`) re-exec binary are
  allowed, including venv links and installed Command Line Tools Python frameworks.
  Forking and execution of other binaries are denied, so tests requiring subprocesses fail within the sandbox. A custom
  interpreter may live outside the repository; its executable, virtualenv lib/config and base runtime are
  read-only inputs. Copy other required inputs through `files`.
- Linux: `bwrap` uses private filesystem/network/PID namespaces, a minimal read-only runtime and writable
  scratch tree. When absent, `unshare` uses user/mount/network/PID namespaces and a minimal chroot with the
  same bindings; `unshare -n` alone is insufficient. It requires `mount`, `chroot`, `setpriv` and permitted
  user namespaces; capabilities are dropped before checks run.
  Credential folders and `.git`/`.env*` within runtime bindings are hidden. Namespace teardown kills detached
  children, including children that call `setsid()`.
- Before any checks or patch strategies, a sandbox preflight runs the case's Python interpreter with
  `-I -S -c 'print(1)'` (for non-Python check commands it uses the bridge's Python). It uses a new session
  and a hard 10-second execution timeout, followed by at most one second of cleanup after killing the group.
  No model-written module is imported by this probe. A missing backend, startup refusal/crash, wrong probe
  output or timeout reports **"sandbox unavailable on this machine"** and refuses execution. Sandboxed
  patch/git startup failures, signals and timeouts also abort measurement; they are never an unapplied diff.
  After preflight passes, signals or printed sandbox diagnostic strings during ordinary checks are
  measured check failures (failed reference, KILLED mutant or failed diff tests), including denied hostile operations.
  An explicit `--unsafe-no-sandbox` on either grader skips preflight and
  **disables confinement and runs model-written code with your privileges and network access**. It emits a
  warning. The clean environment and process-group cleanup remain, but a `setsid()`/double-fork child can
  escape that cleanup. Never use the flag to grade an untrusted proposal.

These constraints are separate from agy's headless permissions. Jobs, config and NEED requests cannot launch
graders or select their test commands. Review a case before invoking it; model output cannot authorize a run.
The bridge's copying/patching never writes into the source repository. Unavailable confinement aborts
measurement and suggests the explicit unsafe flag; it never silently falls back.

Scratch and evidence stay under the system temp directory, or an explicit `AGY_BRIDGE_SCRATCH` directory
outside the repository. A per-run random directory avoids collisions. Evidence is retained for review:
`reference.log`, per-mutant logs, `table.md`, `grades.jsonl`, and separate diff strategy trees. No repo caches
are generated by the bridge. The agy environment remains the original exact five names.

## Structured mutants

````text
```mutants
{"file":"impl.py","old":"return value","new":"return None","why":"Missing nonempty output assertion"}
```
````

Return one JSON object per line in a fenced `mutants` block, with exactly the four string fields above.
Multiple blocks are accepted; citation annotations are removed with a linear line scanner. Only completed
fences are parsed. The replacement text is model-written code executed by checks under the boundary above.
Use the case's scratch destination in `file`. Targets must belong to `solution_files`. Each replacement must
be nonempty, change the text and match exactly once in the unmodified file. Each mutant starts from a fresh
reference tree. Reference tests must pass before any mutant is measured, otherwise grading aborts.

```sh
bin/agy-bridge grade-mutants result.md --repo /path/to/repo --case case.json
bin/agy-bridge grade-mutants result.md --repo /path/to/repo --case case.json --record --by reader
```

Outputs are a Markdown table and JSONL evidence. SURVIVES means checks passed, KILLED means checks failed
(including timeout), NOT-APPLICABLE means unsafe/disallowed target or invalid replacement, TRIVIAL means a
passing whitespace/comment/docstring-only Python change. The trivial classifier compares syntax and tokens;
parentheses and number-spelling changes are not automatically trivial. Non-Python syntax is conservatively
not classified as trivial. A surviving change may be semantically equivalent and still needs a reader.

`--record` requires `case.job` and `--by`; it appends pass if any mutant survives, fixes if only trivial passing
changes remain, otherwise reject. This verdict records measured mutant claims, not approval of any wider report.

## Diff application

```sh
bin/agy-bridge apply-diff result.md --repo /path/to/repo --case case.json > measurement.json
bin/agy-bridge apply-diff proposed.diff --repo /path/to/repo --case case.json
bin/agy-bridge apply-diff table measurement.json other.jsonl --repo /path/to/repo
```

Markdown uses the first `diff`/`patch` fence (or an unlabelled unified diff fence); `.diff` and `.patch` inputs
are raw. Only unified text patches are accepted. Traversal, absolute targets other than `/dev/null` file
creation/deletion markers, denylisted paths (including repository extra `deny` rules), symlink, rename/copy
and binary patches refuse before application. Every header must resolve to a `solution_files` destination;
when that list is absent/empty, only declared `files` destinations are eligible. Test files and undeclared new
files cannot be changed when `solution_files` is provided. Prefix stripping is validated for each strategy
before its resulting tree can run checks.
Strategies run serially inside the sandbox on fresh trees: `patch -p0`, `patch -p1`, `git apply -p0`,
`git apply -p1`. Patch stdin is closed; git has no inherited config, credentials or repo-local configuration. It runs only in scratch,
without reading the source repository's `.git`. No network-fetching git command is used.

One JSON record reports whether a diff was found, applied, which strategy worked, fuzz/offset reported by the
patch tool, tests_pass, and failed-test detail/timeout. A missing or unapplied diff is a measured record, not a
passing check. `table` summarizes JSON/JSONL records. Exit 0 means measurement completed, not that tests passed;
invalid input/reference failure exits 1, unsafe filesystem operations exit 4, interruption exits 130.

## Timing stats

`bin/agy-bridge stats --repo /path/to/repo --target 180` reports per-model sample counts, median/p90 seconds,
and least-squares `seconds = intercept + slope * bytes` with a recommended maximum byte count for the target.
A fit requires at least five points, distinct byte sizes and a useful positive slope; otherwise the report uses
median/p90 and makes no byte recommendation. `--results-dir` follows queue directory safety rules. Events and
result headers are reported separately to avoid double-counting or mixing attempt and retained-round metrics.
Only headers before attempts/rounds are parsed; model prose cannot invent timing samples. Estimates are not deadlines.
