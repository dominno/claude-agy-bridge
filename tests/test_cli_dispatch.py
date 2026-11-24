import os

from agy_bridge import config, edit_sandbox, runner
from agy_bridge.cli import main


def test_status_does_not_accept_all_as_an_abbreviation(h):
    result = h.raw('status', '--all', '--repo', str(h.repo))
    assert result.returncode == config.EXIT_USAGE
    assert 'unrecognized arguments: --all' in result.stderr
    assert '_implement-worker' not in h.raw('--help').stdout


def test_implementation_doctor_checks_version_inside_sandbox(h, monkeypatch, capsys):
    profile = h.root / 'profile'
    profile.mkdir(mode=0o700)
    executable = str(h.bindir / 'agy')
    monkeypatch.setattr(runner, 'find_agy', lambda _: executable)
    monkeypatch.setattr('agy_bridge.cli.install_stop_handlers', lambda: None)
    calls = []

    def preflight(*, tree, profile, exe):
        assert os.path.isdir(tree) and not os.listdir(tree)
        calls.append((profile, exe))
        return '1.5.16'

    monkeypatch.setattr(edit_sandbox, 'preflight', preflight)
    flags = ['doctor', '--implementation', '--repo', str(h.repo), '--profile', str(profile)]
    assert main(flags) == config.EXIT_OK
    assert calls == [(str(profile.resolve()), executable)]
    assert 'agy 1.5.16' in capsys.readouterr().out
    assert not (h.repo / '.agy-bridge').exists()
