# Contributing

## Setup

```sh
python3 -m venv .venv
.venv/bin/pip install pytest
.venv/bin/python -m pytest -q
```

The full suite takes several minutes and runs offline against a fake `agy` (`tests/fake_agy/agy`). No account is needed.
`AGY_BRIDGE_HOME` moves the log directory only while the test suite runs (it creates a marker file
`src/agy_bridge/_test_mode`); in normal use the variable is ignored.

## Project rules

- Python standard library only at run time; `pytest` only for tests.
- No hooks, no network calls by the plugin, no permission-widening flag. `tests/test_hygiene.py` checks these.
- A new safety rule gets a test and a row in `docs/THREAT_MODEL.md`.
- All `agy` flags live in `ARGV_SPECS` in `src/agy_bridge/runner.py`. Anything learned about `agy` goes in
  `docs/COMPAT.md`; `docs/MANUAL_TEST.md` shows how to measure it.
- Use only fake files and invented keys in tests, examples and bug reports.

Security problems: see [`SECURITY.md`](SECURITY.md), not a public issue.
