# Contributing

## Setup

```sh
python3 -m venv .venv
.venv/bin/pip install pytest
.venv/bin/python -m pytest -q
```

The full suite takes several minutes and runs offline against a fake `agy` (`tests/fake_agy/agy`). No account is needed.
Use `.venv/bin/pip install .` to test the installed `agy-bridge` command and `python -m agy_bridge` entry point in
the virtual environment. Use [the Linux implementation acceptance test](.claude/skills/agy-bridge/references/MANUAL_TEST.md#14-check-implementation-on-linux)
to verify a real authenticated edit and resume.
`AGY_BRIDGE_HOME` moves the log directory only while the test suite runs (it creates a marker file
`.claude/skills/agy-bridge/scripts/agy_bridge/_test_mode`); in normal use the variable is ignored.

## Project rules

- Python standard library only at run time; `pytest` only for tests.
- Everything the skill needs stays in `.claude/skills/agy-bridge/`. No hooks, no network calls by the bridge, no permission-widening flag. `tests/test_hygiene.py` checks these.
- A new safety rule gets a test and a row in `references/THREAT_MODEL.md`.
- Build AGY arguments in `scripts/agy_bridge/adapters/agy.py` and parse its stream protocol in
  `adapters/agy_stream.py`. Record AGY compatibility findings in
  `references/COMPAT.md` and reproducible manual checks in `references/MANUAL_TEST.md`.
- Use only fake files and invented keys in tests, examples and bug reports.

## Code layout

The `agy_bridge` package in `.claude/skills/agy-bridge/scripts/` is organised into layers:

| Folder | Holds | May import |
|---|---|---|
| `core/` | Pure policy and models: limits and config (`config.py`), denylist, secret scan, refusals, text and invisible-Unicode checks, `--repo` root checks, input selection (`stage.py`), staging folder and task file (`staging.py`), journal (`events.py`), citations, grading and diff rules (`grading_rules.py`), repository path rules, job names and refusal markers (`verdict.py`), the queue job and result formats (`queue_job.py`), implementation job phases and record (`job_state.py`) | `core` only |
| `port/` | The seams to the outside: `process.py`, the one place that starts (and group-kills) a program; `safety.py`, the pinned no-follow file helpers; one module per adapter role with its interface, its errors and its slot: `model_runner.py` (agy), `grading_sandbox.py`, `edit_sandbox.py`, `job_store.py`, `workspace.py`, `patch_apply.py`, `launcher.py` | `core`, `port` |
| `adapters/` | `agy.py` and `agy_stream.py` (the agy CLI and its stream-json protocol), `sandbox.py` (grading: `sandbox-exec`, Bubblewrap or `unshare`, and the scratch runner), `edit_sandbox.py` (implementation: Bubblewrap), `host_tools.py` (trusted executables, Bubblewrap version gate), `edit_runtime.py`, `jobstore.py` (the job store on disk), `workspace.py` (snapshot and diff, Git in scratch only), `edit_apply.py` (publishing a reviewed patch), `launcher.py` (detached workers) | `core`, `port`, `adapters` |
| `app/` | Use cases: `delegation.py`, `queue.py` with `queue_store.py` (the on-disk queue) and `queue_commands.py` (`queue add`, `queue watch --detach`), `doctor.py` (health check and sandbox probe), `implement.py` (job commands) with `implement_work.py` (the worker), `jobs.py`, `verdicts.py`, `grading.py`, `stats.py`, `report.py`, `console.py` (stdout/stderr writers) | `core`, `port`, `app` (and `agy_bridge.__version__`) |
| `cli/` | `__init__.py` (exports `main` and runs `wiring.wire()` once), `wiring.py` (the composition root), `entry.py` (`main`), `signals.py` (stop handlers), `arguments.py` (the parser class and shared repository options), `parser.py` with one parser module per command group (`parser_delegate.py`, `parser_workflow.py`, `parser_implement.py`, `parser_jobs.py`, each with `add_arguments`), and thin handlers (`delegate_cmd.py`, `doctor_cmd.py`, `queue_cmd.py`, `workflow_cmd.py`) that check flags, call one use case and print through `output.py` | `core`, `app`, `cli`; only `cli/wiring.py` also imports `port` and `adapters` |

Only `__init__.py` (the version) and `__main__.py` stay at the package root. Rules for every change:

- Dependencies point one way: `cli -> app -> (core, port)`; adapters implement ports and may use `core`; `core`
  imports nothing outside `core` and never names `agy`, `bwrap` or `sandbox-exec`.
- One exception: `cli/wiring.py` is the composition root. It is the only module that imports `adapters` (and the
  only `cli` module that imports `port`), and binds each port to its adapter (`model_runner.bind(agy)`, ...).
  Something has to choose the concrete adapters, and `app` and `port` may not import them, so the choice lives in one
  named place. Every other `cli` module goes through `app` and never touches a port.
- Only `cli` imports `argparse`: a new command adds its options in the parser module of its group and a thin handler
  that calls a use case; the use case takes plain values (or the parsed namespace) and never builds a parser.
- A use case reaches an adapter through its port at call time (`model_runner.use().run_agy(...)`), and catches the
  errors the port defines (`model_runner.AgyMissing`, `edit_sandbox.DeadlineExceeded`, `patch_apply.ApplyError`).
  Tests patch the adapter module (`agy.find_agy`, `sandbox.run_scratch`, `jobstore.save`); `tests/conftest.py`
  imports the CLI so the ports are bound.
- Only `port/process.py` imports `subprocess`; no module calls `os.exec*`, `os.fork`, `os.spawn*` or `pty.fork`.
  Start a program with `process.spawn(argv, ...)` or `process.run(argv, ...)` and pass an explicit argv list.
- Only the launch sites listed in `SEAM_CALLERS` in `tests/test_hygiene.py` import the seam, and always as
  `from <..>port import process` with no alias (never `from ...process import spawn`), so every launch reads
  `process.spawn(...)` or `process.run(...)` and the argv check sees it. The list may only shrink; when a launch site
  moves into a layer, rename its entry.
- Modules stay at or under 600 lines (aim for 400) and functions at or under 60 lines; no import cycles.
- Patch a name in a test where it is looked up at call time (for example `rootcheck.real_homes`, not
  `stage.real_homes`; `process.subprocess.Popen` for program starts).

`tests/test_architecture.py` checks the import direction, cycles, sizes, that every module sits in a layer, that only
`cli` parses arguments, and the process seam.

Security problems: see [`SECURITY.md`](SECURITY.md), not a public issue.
