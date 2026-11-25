# Compatibility with `agy`

The bridge's test suite runs against a fake `agy` (`tests/fake_agy/agy`) that implements only what the bridge expects.
The table of assumptions below was written before any real run; the measured sections after it record what real runs
showed. Anything not listed there stays unverified: treat it as a guess and settle it by running `agy` by hand (ideally
on a throw-away account, see [MANUAL_TEST.md](MANUAL_TEST.md)).

## How the bridge copes
- `adapters/agy.py` builds AGY arguments from the reported version and the selected workflow. From AGY 1.5.16 onward the
  read-only runner pins session permissions with `--settings`. The bridge refuses versions 1.5.7–1.5.15 because
  those releases can default to Auto and are outside the bridge's supported session-settings range.
- An unknown major version fails closed with exit code 14. The implementation workflow requires AGY 1.5.16 or above
  within major version 1 and either Linux Bubblewrap 0.8.0 or later (`--disable-userns`; `doctor --implementation`
  and the implement preflight refuse an older `bwrap --version`) or macOS `sandbox-exec`. Its authenticated edit,
  check, resume and apply were measured on macOS (below); on Linux with Bubblewrap they stay unverified.

## Unverified assumptions
| # | Assumption | Where it lives | How to settle it |
|---|---|---|---|
| C1 | `agy --version` prints a `major.minor.patch` number | `adapters.agy.parse_version` | run it |
| C2 | Print (non-interactive) mode is `--print <prompt>`; the alternative may be `-p` | `ARGV_SPECS[1].print_flag` | `agy --help` |
| C3 | The model flag is `--model <name>` and `agy models` lists model names, one per line | `ARGV_SPECS`, `doctor` | `agy --help`, `agy models` |
| C4 | No timeout flag is passed; the bridge enforces its own wall clock | `adapters.agy.run_agy` | check whether `agy` has `--timeout` or `--print-timeout` and whether it is wanted |
| C5 | Major version 1 is the supported major; versions 0 and 2+ are rejected | `ARGV_SPECS` | read the real version |
| C6 | Print mode may need a terminal on stdout, so the bridge uses a pty | `adapters.agy.run_agy` | try without a pty |
| C7 | A soft permission denial looks like exit 0, empty stdout and the words "auto-denied" or "permission denied by" on stderr | `runner._MARKERS`, exit 15 | provoke a denial |
| C8 | Quota and sign-in failures contain words such as "quota", "rate limit", "unauthenticated", "log in" on stderr | `runner._MARKERS`, exit 10 and 11 | provoke each |
| C9 | A prompt that says "read TASK.md" makes `agy` read the staged `TASK.md` | `runner.PROMPT` | try it |
| C10 | `agy` in print mode cannot write files, run commands or fetch URLs without an extra flag. If it can, rule R3 only means the bridge does not ask for more | whole design | read the permission docs; test with a prompt that asks it to write a file |
| C11 | Whether `agy` reads `.env` or `.gitignore`d files, follows symlinks, or reads outside its working directory (for example under `HOME`) | threat model | test on a throw-away `HOME` |
| C12 | `agy` finds its sign-in under the real `HOME` and needs nothing else from the environment | `core.config.build_agy_env` | run with the five variables only |
| C13 | The `~/.gemini` folder is where `agy` keeps settings (the doctor only reports whether it exists) | `app.doctor.health` | look at where `agy` writes |
| C14 | Output is plain text, possibly with colour codes and CRLF; the bridge strips both | `app.report.clean_answer` | capture a real answer |
| C15 | Whether `agy` reads stdin; the bridge gives it the pty | `adapters.agy.run_agy` | try `agy --print` with closed stdin |
| C16 | `agy --version` and `agy models` are harmless to run with an empty folder as the working directory and the five-name environment | `adapters.agy.detect_version`, `app.doctor.check_models` | run them |
| C17 | Delegate has no `--diff` and runs no git. Explicit local patch checks run git apply only in scratch; see GRADING.md | n/a | n/a |

## Still open
- Does the answer ever echo staged file contents on stderr (the bridge never prints stderr for that reason)?

## Measured on agy 1.4.3 (2026-10-20, one machine, one account)
Run on a main account with fake files only (a scratch repository, a fake `.env` and a fake
canary outside it). Results of [MANUAL_TEST.md](MANUAL_TEST.md) sections 4, 6 and 7, plus direct `agy` runs. Everything else in the
table above stays unverified.

| Item | Measured |
|---|---|
| C1 | `agy --version` prints `1.4.3`; the bridge accepts it as a known major |
| C2 | `--print` takes the NEXT argument as its prompt, so `agy --model <name> --print "<prompt>"` works and `agy --print --model <name> "<prompt>"` fails with a clear error; the bridge's argv order is correct |
| C3 | `--model <id>` works; `agy models` prints 13 lines `id<TAB>name` (Gemini 4 Argon, Gemini 3.8 Flash, Claude Opus 5.5, Claude Sonnet 5.5, GPT-OSS 120B). `gemini-3.8-flash-low` answered in 9 to 29 s; the default model took 152 s for a trivial task |
| C4 | `agy` has `--print-timeout`; the bridge still enforces its own wall clock |
| C6, C15 | print mode runs in the bridge's pty; a plain pipe is not needed |
| C7 | a soft denial prints on stderr: `no output produced — a tool required the "<tool>" permission that headless mode cannot prompt for, so it was auto-denied. Add an allow-rule under permissions.allow ...` (tools seen: `read_file`, `write_file`, `command`). The bridge's marker "auto-denied" matches it |
| C9 | the model read the staged `TASK.md` and followed it (every probe answered the task) |
| C10 | headless, without any flag: `write_file` and `command` are auto-denied, and so is `read_file` outside the working directory. **URL fetching: a first probe seemed to show a fetch, but it was not one.** Asked to fetch https://example.com, the model answered "Example Domain" from its own knowledge; its recorded tool calls were only `read_file` attempts in the staging folder. A second probe used a local server with a unique page: with neutral wording `agy` tried to fetch it through the `command` tool, which was auto-denied, and the server saw no request from `agy` (with and without a `read_url`/`execute_url` deny rule in `settings.json`). So no network use by `agy` was observed headless without a permission. This is one version, one model and a few prompts, not a proof |
| C11 | `read_file` of a path outside the working directory (a fake canary) was auto-denied when asked neutrally; with the bridge's read-only wording the model also refused in words. A fake absolute-path `.env` read was refused in words |
| C12 | `HOME`, `PATH`, `LANG`, `TERM`, `TMPDIR` is enough; sign-in is found |
| C13 | `~/.gemini` holds `antigravity-cli` and `config` |
| C16 | `agy --version` and `agy models` ran fine in an empty folder with the five-name environment |

**What this means.** The bridge's guarantees about what it stages, the environment `agy` gets and where `agy` runs are
unchanged, and in these runs `agy` could not write, run commands, read outside its working directory or reach the network
without a permission that headless mode cannot grant. Do not widen that: never add `permissions.allow` rules for
`command`, `write_file`, `read_url` or paths outside the staging folder to `~/.gemini/antigravity-cli/settings.json`.
A `permissions.deny` list (`["read_url(*)", "execute_url(*)"]`, the documented `action(target)` syntax) is optional extra
protection. It was tried here and changed nothing observable, because nothing was reachable anyway.

## Measured on agy 1.4.12 (2026-10-31, one machine, one account)
A smaller check, with one fake file in a scratch repository: `doctor --offline` passes and `delegate` with
`gemini-3.8-flash-low` returned a correct answer in 10 s. The permission probes above (C7, C10, C11) were **not** repeated
on this version.

## Summary for the README

The test suite uses a fake `agy`. The bridge has been run against the real `agy` 1.4.3 (2026-10-20, fake files
only): doctor, dry-run and delegate work, and writes, commands, reads outside the folder and network use were not
possible headless without a permission. That is one version on one machine, not a guarantee. A basic delegation
also worked on 1.4.12. The plugin (then with `/agy:*` slash commands) has been loaded in real Claude Code sessions.
Implementation has offline coverage
for edit, check, resume and patch application. Authenticated end-to-end implementation jobs ran on macOS 13 with
AGY 1.5.17 (sandbox-exec backend, synthetic files); on a Linux host with working Bubblewrap namespaces they still need
verification.

## Measured on agy 1.5.17, macOS 13 (2026-11-25, one Intel Mac, one account)

Implementation jobs under the macOS `sandbox-exec` backend, with a dedicated profile signed in once with
`HOME=~/agy-implementation-profile agy` (Google account, subscription, no API key) and a synthetic Git repository
(`calc.py`, `tests/test_calc.py`).

| Item | Measured |
|---|---|
| Sign-in storage | The profile sign-in is file based: `.gemini/antigravity-cli/antigravity-oauth-token` (0600) and `Library/Application Support/antigravity/credential-user-scope`. During the sign-in AGY's `Keyring SaveToken timed out after 5s` and it switched to file storage; no new Keychain item appeared. `HOME=<empty dir> agy` does not reuse the normal login (Keychain services `gemini`, `Antigravity Safe Storage`) |
| Keychain inside the sandbox | AGY runs `/usr/bin/security` for the OS credential manager; the policy denies that exec (`fork/exec /usr/bin/security: operation not permitted`) and AGY loads the file token (`ChainedAuth: authenticated via keyring (effective: keyring)` from the file fallback). No securityd/Keychain Mach service is needed |
| Mach services | Without `com.apple.trustd.agent` every HTTPS call fails (`x509: OSStatus -26276`); with it and `com.apple.system.logger` only, `agy models` and editing turns work. `com.apple.trustd`, configd and mDNSResponder were not needed |
| Process creation | `agy --version`, `agy models` and editing turns work with `process-fork` denied and `process-exec` limited to the AGY binary; AGY's language server runs in-process and listens on localhost (network allowed) |
| Temp | AGY wrote nothing to `TMPDIR` in these runs; it still gets `<profile>/tmp` |
| `.gemini/config` | AGY creates it on first run with `mcp_config.json` (empty), `config.json` `{"plugins":{"swarm":{"enabled":true}}}`, `.migrated` and `projects/default-cli-project.json`; the former blanket refusal of `.gemini/config` rejected every used profile |
| Negative probes | Inside the agent policy (with `/bin/bash` builtins standing in for AGY): reading `~/.ssh/known_hosts`, `~/.gemini/antigravity-cli/settings.json`, `~/.zshrc`, `~/Library/Keychains/login.keychain-db`, the source repository and another worktree, listing `~` or `~/.ssh` (0 entries), writing `~/x` or `/tmp/x`, and running `/bin/ls` all failed with `Operation not permitted`; reading `/etc/hosts` and writing the tree and the profile worked |
| Print-mode soft denial | A tool call outside the workspace that no deny rule names makes AGY ask; print mode soft-denies it and ends the whole turn (bridge exit 15, `permission`). Two jobs ended this way when AGY's research subagent read `<job>/meta.json` and `<job>/.git/HEAD`; `jobs resume` then finished the first job. Deny rules turn such calls into a visible error (a deny rule beats an allow rule, so the root cannot be denied); the bridge now names the job folder's entries and `.git/` above the tree, and the next fresh job verified on its first turn |
| End to end | `jobs start --dry-run`, `jobs start --profile ... --check '["python3","-m","unittest","discover","-s","tests"]'`, baseline check (exit 0), one AGY turn (`gemini-3.8-flash-medium`), offline check (exit 0), phase `verified` with the `check inputs changed` warning for the edited test, `jobs patch --raw`, `jobs apply --sha256` (phase `applied`), tests pass in the checkout |

## Implementation verification

Offline tests exercise manifest creation, file edits, check retries, job cancellation and resume, and exact patch
application. They simulate the AGY turn. `tests/test_implement_macos.py` also runs real negative probes against the
macOS policy on darwin. The job inventory is a local filesystem read and works without AGY. Run
`doctor --implementation --profile <absolute path>` to test AGY version and sandbox startup without a model call.
An authenticated edit and resume with real AGY on a Linux host with working Bubblewrap user namespaces remains to be
measured. To record the AGY version, actual file changes, check result, and resume behavior, use the
[implementation acceptance test](MANUAL_TEST.md#14-check-implementation-on-linux).
