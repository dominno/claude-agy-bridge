import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
TEST_MARKER = os.path.join(ROOT, "src", "agy_bridge", "_test_mode")


import atexit  # noqa: E402

atexit.register(lambda: os.path.exists(TEST_MARKER) and os.remove(TEST_MARKER))


def pytest_configure(config):
    os.environ["AGY_BRIDGE_TEST_MODE"] = "1"
    # Lets AGY_BRIDGE_HOME work while tests run; the file is removed afterwards (see config.TEST_MARKER).
    open(TEST_MARKER, "w").close()


def pytest_unconfigure(config):
    os.environ.pop("AGY_BRIDGE_TEST_MODE", None)
    try:
        os.remove(TEST_MARKER)
    except OSError:
        pass

import json
import shutil
import subprocess

import pytest


@pytest.fixture
def guarded_sandbox(request, monkeypatch):
    """Every real v3 sandbox call has an independent subprocess timeout.

    Probe failure skips explicitly; later sandbox failures remain failures.
    Mock lifecycle tests and the explicit unsafe-flag test keep their local calls.
    """
    if request.node.name in ('test_test_timeout_kills_process_group',
                             'test_missing_sandbox_refuses_unless_explicit'):
        return
    from agy_bridge import runner, sandbox
    from sandbox_worker import scratch_call
    def guarded(argv, tree, repo, extra_env=None, timeout=300, **kwargs):
        result = scratch_call(argv, tree, repo, extra_env, timeout=timeout, **kwargs)
        if 'unavailable' in result:
            if argv[-1] == 'print(1)':
                pytest.skip('real sandbox preflight: ' + result['unavailable'])
            raise sandbox.Unavailable(result['unavailable'])
        return tuple(result['result'])
    monkeypatch.setattr(runner, 'run_scratch', guarded)

BIN = os.path.join(ROOT, "bin", "agy-bridge")
FAKE = os.path.join(ROOT, "tests", "fake_agy", "agy")
SECRETS = {
    "EXAMPLE_API_KEY": "fake-example-not-a-real-key",
    "SERVICE_API_KEY": "fake-service-not-a-real-key",
    "AWS_SECRET_ACCESS_KEY": "fake-aws-not-a-real-key",
    "GITHUB_TOKEN": "fake-github-not-a-real-key",
}


class Harness:
    """A temp repo, a temp HOME/TMPDIR, and a fake agy that records what it was given."""

    def __init__(self, tmp_path):
        self.root = tmp_path
        self.repo = tmp_path / "repo"
        self.home = tmp_path / "home"
        self.tmpdir = tmp_path / "tmp"
        self.bindir = tmp_path / "fakebin"
        self.record_path = tmp_path / "record.json"
        for d in (self.repo / "src", self.repo / ".git", self.home, self.tmpdir, self.bindir):
            d.mkdir(parents=True)
        shutil.copy(FAKE, self.bindir / "agy")
        os.chmod(self.bindir / "agy", 0o755)
        (self.repo / "src" / "a.py").write_text("print('a')\n")
        (self.repo / "notes.md").write_text("some notes\n")
        self.configure()

    def configure(self, **cfg):
        cfg.setdefault("record_path", str(self.record_path))
        (self.bindir / "fake_agy.json").write_text(json.dumps(cfg))

    @property
    def log_path(self):
        return self.home / ".agy-bridge" / "log.jsonl"

    def env(self, extra=None, with_fake=True):
        path = os.environ.get("PATH", "/usr/bin:/bin")
        env = {
            "PATH": (str(self.bindir) + os.pathsep + path) if with_fake else path,
            "HOME": str(self.home),
            "AGY_BRIDGE_HOME": str(self.home / ".agy-bridge"),
            "AGY_BRIDGE_TEST_MODE": "1",
            "TMPDIR": str(self.tmpdir),
            "LANG": "en_US.UTF-8",
            "TERM": "xterm",
        }
        env.update(extra or {})
        return env

    def args(self, *rest, task="summarise these files", repo=True):
        base = ["--repo", str(self.repo)] if repo else []
        return base + list(rest) + ["--", task]

    def run(self, command, *rest, extra_env=None, timeout=60, with_fake=True, cwd=None, **kw):
        return subprocess.run([sys.executable, BIN, command] + self.args(*rest, **kw)
                              if command != "doctor" else [sys.executable, BIN, command, *rest],
                              capture_output=True, text=True, env=self.env(extra_env, with_fake),
                              timeout=timeout, cwd=str(cwd or self.root))

    def raw(self, *argv, extra_env=None, cwd=None, with_fake=True):
        """Call bin/agy-bridge with exactly these arguments (no --repo added)."""
        return subprocess.run([sys.executable, BIN, *argv], capture_output=True, text=True,
                              env=self.env(extra_env, with_fake), timeout=60, cwd=str(cwd or self.root))

    def delegate(self, *rest, **kw):
        return self.run("delegate", "--file", "src/a.py", *rest, **kw)

    def record(self):
        return json.loads(self.record_path.read_text())

    def side_record(self, kind):
        return json.loads(open(str(self.record_path) + "." + kind).read())

    def leftovers(self):
        return sorted(p.name for p in self.tmpdir.iterdir())


@pytest.fixture
def h(tmp_path):
    return Harness(tmp_path)
