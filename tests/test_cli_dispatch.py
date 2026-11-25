import json
import os

from agy_bridge.adapters import agy, edit_sandbox
from agy_bridge.core import config
from agy_bridge.cli import main


def test_status_does_not_accept_all_as_an_abbreviation(h):
    result = h.raw('status', '--all', '--repo', str(h.repo))
    assert result.returncode == config.EXIT_USAGE
    assert 'unrecognized arguments: --all' in result.stderr
    assert '_implement-worker' not in h.raw('--help').stdout


def test_grouped_start_dry_run_matches_legacy_command(h):
    flags = ['--dry-run', '--', 'Fix the parser']
    grouped = h.raw('jobs', 'start', '-C', str(h.repo), '-f', 'src/a.py', *flags)
    legacy = h.raw('implement', '--repo', str(h.repo), '--file', 'src/a.py', *flags)

    assert grouped.returncode == legacy.returncode == config.EXIT_OK
    assert json.loads(grouped.stdout) == json.loads(legacy.stdout)


def test_grouped_apply_requires_reviewed_digest(h):
    result = h.raw('jobs', 'apply', 'a' * 32)
    assert result.returncode == config.EXIT_USAGE
    assert 'the following arguments are required: --sha256' in result.stderr


def test_implementation_doctor_checks_version_inside_sandbox(h, monkeypatch, capsys):
    profile = h.root / 'profile'
    profile.mkdir(mode=0o700)
    executable = str(h.bindir / 'agy')
    monkeypatch.setattr(agy, 'find_agy', lambda _: executable)
    monkeypatch.setattr('agy_bridge.cli.signals.install_stop_handlers', lambda: None)
    calls = []

    def preflight(*, tree, profile, exe):
        assert os.path.isdir(tree) and not os.listdir(tree)
        calls.append((profile, exe))
        return '1.5.16'

    monkeypatch.setattr(edit_sandbox, 'preflight', preflight)
    flags = ['doctor', '--implementation', '-C', str(h.repo), '--profile', str(profile)]
    assert main(flags) == config.EXIT_OK
    assert calls == [(str(profile.resolve()), executable)]
    assert 'agy 1.5.16' in capsys.readouterr().out
    assert not (h.repo / '.agy-bridge').exists()
