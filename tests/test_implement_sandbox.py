import os
import sys

import pytest

from agy_bridge import config, edit_sandbox, sandbox


@pytest.fixture
def areas(tmp_path):
    profile, tree = tmp_path / 'profile', tmp_path / 'candidate'
    for directory in (profile, tree):
        directory.mkdir(mode=0o700)
    return tree, profile


@pytest.fixture
def bubblewrap(monkeypatch):
    monkeypatch.setattr(edit_sandbox.sys, 'platform', 'linux')
    monkeypatch.setattr(sandbox, 'executable', lambda _name, _path: '/usr/bin/bwrap')


def test_agent_mounts_only_candidate_and_dedicated_profile(bubblewrap, areas, monkeypatch):
    tree, profile = areas
    monkeypatch.setenv('GEMINI_API_KEY', 'test-key')
    monkeypatch.setenv('OTHER_SECRET', 'never-pass')
    cmd = edit_sandbox.agent_command([sys.executable, '--print', 'change'], tree=tree, profile=profile)
    mounts = [cmd[index + 1:index + 3] for index, value in enumerate(cmd) if value == '--bind']
    assert mounts == [[str(profile.resolve()), '/home/agy'], [str(tree.resolve()), '/workspace']]
    assert cmd[-4:] == ['--', '/opt/agy/bin/agy', '--print', 'change']
    assert '--share-net' in cmd and '--disable-userns' in cmd and '--die-with-parent' in cmd
    assert 'test-key' not in cmd
    assert edit_sandbox.agent_environment() == {}
    assert edit_sandbox.agent_environment(use_api_key=True) == {'GEMINI_API_KEY': 'test-key'}
    assert os.environ['HOME'] not in cmd


def test_test_process_uses_private_networkless_home(bubblewrap, areas):
    tree, profile = areas
    cmd = edit_sandbox._test_command(['python3', '-m', 'pytest'], str(tree))
    assert '--unshare-all' in cmd and '--share-net' not in cmd and '--clearenv' in cmd
    assert cmd[cmd.index('--tmpfs', cmd.index('--tmpfs') + 1) + 1] == '/home/agy'
    assert str(profile) not in cmd
    assert cmd[-4:] == ['--', 'python3', '-m', 'pytest']


def test_profile_refuses_loose_permissions_and_extension_settings(areas):
    tree, profile = areas
    with pytest.raises(config.UsageError, match='outside the source repository'):
        edit_sandbox.validate_profile(profile, repo=profile.parent)
    cli = profile / '.gemini' / 'antigravity-cli'
    cli.mkdir(parents=True)
    settings = cli / 'settings.json'
    settings.write_text('{"modelProvider":"gemini"}')
    assert edit_sandbox.validate_profile(profile, repo=tree) == str(profile.resolve())
    settings.write_text('{"hooks":{"enabled":true}}')
    with pytest.raises(config.UsageError, match='may not enable hooks'):
        edit_sandbox.validate_profile(profile)
    profile.chmod(0o755)
    with pytest.raises(config.UsageError, match='mode 0700'):
        edit_sandbox.validate_profile(profile)


def test_failed_bubblewrap_probe_stops_implementation(areas, monkeypatch, bubblewrap):
    tree, profile = areas
    monkeypatch.setattr(edit_sandbox, '_run', lambda *_: (1, 'bwrap: user namespaces forbidden'))
    with pytest.raises(edit_sandbox.Unavailable, match='user namespaces forbidden'):
        edit_sandbox.preflight(exe=sys.executable, profile=profile, tree=tree)


def test_verification_stream_has_bounded_tail_and_cancels(tmp_path):
    argv = [sys.executable, '-c', "print('x' * 40000); print('final')"]
    code, tail = edit_sandbox._run(argv, tmp_path, {}, 3, None)
    assert code == 0
    assert len(tail) == edit_sandbox.TEST_OUTPUT_TAIL and tail.endswith('final\n')
    argv = [sys.executable, '-c', 'import time; time.sleep(180)']
    assert edit_sandbox._run(argv, tmp_path, {}, 3, lambda: True) == (130, 'cancelled')
    assert edit_sandbox._run(argv, tmp_path, {}, 0.1, None) == (124, 'timeout after 0.1s')


@pytest.mark.skipif(not sys.platform.startswith('linux'), reason='implementation sandbox uses Linux namespaces')
def test_linux_namespace_mounts_and_network(areas, tmp_path):
    tree, profile = areas
    if not sandbox.executable('bwrap', '/usr/bin:/bin'):
        pytest.skip('Bubblewrap is unavailable')
    code, detail = edit_sandbox.run_test(['true'], tree=tree, timeout_s=5)
    if code and any(message in detail for message in ('uid map', 'creating new namespace',
                                                     'No permissions to create a new namespace')):
        pytest.skip(detail.strip())
    assert code == 0, detail

    host_file = tmp_path / 'host-file'
    host_file.write_text('external')
    (profile / 'profile-file').write_text('dedicated')
    command = edit_sandbox.agent_command(
        ['/bin/sh', '-c', 'test "$PWD" = /workspace && test ! -e "$1" && '
         'test -f /home/agy/profile-file && printf result > edited.txt', 'agy', str(host_file)],
        tree=tree, profile=profile,
    )
    assert edit_sandbox._run(command, tree, edit_sandbox.agent_environment(), 5, None) == (0, '')
    assert (tree / 'edited.txt').read_text() == 'result'

    network_namespace = os.readlink('/proc/self/ns/net')
    code, detail = edit_sandbox.run_test(
        ['python3', '-c', 'import os, sys; assert os.readlink("/proc/self/ns/net") != sys.argv[1]; '
         'assert not os.path.exists("/home/agy/profile-file"); assert not os.path.exists(sys.argv[2])',
         network_namespace, str(host_file)],
        tree=tree, timeout_s=5,
    )
    assert code == 0, detail
