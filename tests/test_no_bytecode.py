"""An installed skill must stay clean: running the CLI or a detached worker writes no __pycache__ into it."""
import os
import shutil
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / '.claude' / 'skills' / 'agy-bridge' / 'scripts'


def test_launcher_writes_no_bytecode(tmp_path):
    copy = tmp_path / 'scripts'
    shutil.copytree(SCRIPTS, copy, ignore=shutil.ignore_patterns('__pycache__', '_test_mode'))
    env = {k: v for k, v in os.environ.items() if k != 'PYTHONDONTWRITEBYTECODE'}
    result = subprocess.run([sys.executable, str(copy / 'agy-bridge'), '--help'], env=env,
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert not list(copy.rglob('__pycache__'))


def test_detached_worker_runs_without_bytecode():
    source = (SCRIPTS / 'agy_bridge' / 'adapters' / 'launcher.py').read_text(encoding='utf-8')
    assert "[sys.executable, '-I', '-B', '-c'," in source
