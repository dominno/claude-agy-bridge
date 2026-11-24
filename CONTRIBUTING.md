# Contributing

## Setup

```sh
python3 -m venv .venv
.venv/bin/pip install pytest
.venv/bin/python -m pytest -q
```

The full suite takes several minutes and runs offline against a fake `agy` (`tests/fake_agy/agy`). No account is needed.
Use `.venv/bin/pip install .` to test the installed `agy-bridge` command and `python -m agy_bridge` entry point in
the virtual environment. Use [the Linux implementation acceptance test](docs/MANUAL_TEST.md#14-check-implementation-on-linux)
to verify a real authenticated edit and resume.
`AGY_BRIDGE_HOME` moves the log directory only while the test suite runs (it creates a marker file
`src/agy_bridge/_test_mode`); in normal use the variable is ignored.

## Project rules

- Python standard library only at run time; `pytest` only for tests.
- No hooks, no network calls by the plugin, no permission-widening flag. `tests/test_hygiene.py` checks these.
- A new safety rule gets a test and a row in `docs/THREAT_MODEL.md`.
- Build AGY arguments and parse its protocol in `src/agy_bridge/runner.py`. Record AGY compatibility findings in
  `docs/COMPAT.md` and reproducible manual checks in `docs/MANUAL_TEST.md`.
- Use only fake files and invented keys in tests, examples and bug reports.

Security problems: see [`SECURITY.md`](SECURITY.md), not a public issue.
