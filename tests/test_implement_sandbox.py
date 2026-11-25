import os
import sys

import pytest

from agy_bridge.adapters import edit_sandbox, host_tools
from agy_bridge.core import config


@pytest.fixture
def areas(tmp_path):
    profile, tree = tmp_path / 'profile', tmp_path / 'candidate'
    for directory in (profile, tree):
        directory.mkdir(mode=0o700)
    return tree, profile


@pytest.fixture
def bubblewrap(monkeypatch):
    monkeypatch.setattr(edit_sandbox.sys, 'platform', 'linux')
    monkeypatch.setattr(host_tools, 'executable', lambda _name, _path: '/usr/bin/bwrap')


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
    cmd = edit_sandbox._test_command(['python3', '-m', 'pytest'], str(tree), status_fd=9)
    assert '--unshare-all' in cmd and '--share-net' not in cmd and '--clearenv' in cmd
    assert cmd[cmd.index('--tmpfs', cmd.index('--tmpfs') + 1) + 1] == '/home/agy'
    assert str(profile) not in cmd
    assert cmd[-6:] == ['--json-status-fd', '9', '--', 'python3', '-m', 'pytest']


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


@pytest.mark.parametrize('exit_code', [0, 1, 124, 126, 127])
def test_offline_check_classifies_a_real_exit_by_the_private_status_channel(areas, monkeypatch, exit_code):
    tree, _ = areas

    def wrap(argv, wrapped_tree, status_fd):
        assert argv == ['example-check'] and wrapped_tree == str(tree.resolve())
        program = (
            'import os, sys; '
            'os.write(int(sys.argv[1]), b\'{"child-pid": 123}\\n{"exit-code": %d}\\n\'); '
            'print("bwrap: synthetic test text"); '
            'sys.exit(%d)' % (exit_code, exit_code)
        )
        return [sys.executable, '-I', '-S', '-c', program, str(status_fd)]

    monkeypatch.setattr(edit_sandbox, '_test_command', wrap)
    assert edit_sandbox.run_test(['example-check'], tree=tree, timeout_s=5) == (
        exit_code, 'bwrap: synthetic test text\n',
    )


@pytest.mark.parametrize('status', [b'', b'{"child-pid": 123}\n',
                                   b'{"child-pid": 123}\n{"exit-code": 0}\n'])
def test_unconfirmed_check_launch_is_infrastructure_failure(areas, monkeypatch, status):
    tree, _ = areas

    def wrap(_argv, _tree, status_fd):
        program = (
            'import os, sys; '
            'os.write(int(sys.argv[1]), %r); '
            'print(\'{"exit-code": 1}\'); '
            'sys.exit(1)' % status
        )
        return [sys.executable, '-I', '-S', '-c', program, str(status_fd)]

    monkeypatch.setattr(edit_sandbox, '_test_command', wrap)
    with pytest.raises(edit_sandbox.InfrastructureError, match='could not confirm'):
        edit_sandbox.run_test(['example-check'], tree=tree, timeout_s=5)


def test_offline_check_supervisor_timeout_and_cancel_keep_existing_results(areas, monkeypatch):
    tree, _ = areas
    monkeypatch.setattr(edit_sandbox, '_test_command',
                        lambda *_: [sys.executable, '-c', 'import time; time.sleep(30)'])
    assert edit_sandbox.run_test(['example-check'], tree=tree, timeout_s=5, should_cancel=lambda: True)[0] == 130
    assert edit_sandbox.run_test(['example-check'], tree=tree, timeout_s=0.1)[0] == 124


def test_offline_preflight_only_runs_trusted_probe(areas, monkeypatch):
    tree, _ = areas

    def probe(argv, **kwargs):
        assert argv == ['/bin/true']
        assert kwargs['tree'] == tree
        assert kwargs['timeout_s'] == edit_sandbox.PREFLIGHT_TIMEOUT_S
        return 0, ''

    monkeypatch.setattr(edit_sandbox, 'run_test', probe)
    assert edit_sandbox.preflight_test(tree=tree, timeout_s=30) == (0, '')


@pytest.mark.skipif(not sys.platform.startswith('linux'), reason='implementation sandbox uses Linux namespaces')
def test_linux_namespace_mounts_and_network(areas, tmp_path):
    tree, profile = areas
    if not host_tools.executable('bwrap', '/usr/bin:/bin'):
        pytest.skip('Bubblewrap is unavailable')
    # Determine host namespace support using the raw, trusted pre-test. The
    # production check never uses these diagnostic strings to classify exits.
    code, detail = edit_sandbox._run(edit_sandbox._test_command(['/bin/true'], str(tree)), tree, {}, 5, None)
    if code and any(message in detail for message in ('uid map', 'creating new namespace',
                                                     'No permissions to create a new namespace')):
        pytest.skip(detail.strip())
    assert code == 0, detail
    assert edit_sandbox.run_test(['/bin/true'], tree=tree, timeout_s=5) == (0, '')
    assert edit_sandbox.run_test(['/bin/sh', '-c', 'exit 127'], tree=tree, timeout_s=5) == (127, '')
    with pytest.raises(edit_sandbox.InfrastructureError):
        edit_sandbox.run_test(['agy-bridge-missing-check-for-test'], tree=tree, timeout_s=5)

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


def _fake_bwrap(tmp_path, monkeypatch, output):
    tool = tmp_path / 'bwrap'
    tool.write_text('#!/bin/sh\nprintf "%s\\n" "' + output + '"\n')
    tool.chmod(0o700)
    tree = tmp_path / 'tree'
    tree.mkdir(mode=0o700)
    monkeypatch.setattr(edit_sandbox.sys, 'platform', 'linux')
    monkeypatch.setattr(host_tools, 'executable', lambda name, path: str(tool))
    return tool, tree


def test_old_bubblewrap_is_refused_before_the_agy_probe(tmp_path, monkeypatch):
    _, tree = _fake_bwrap(tmp_path, monkeypatch, 'bubblewrap 0.6.1')
    monkeypatch.setattr(edit_sandbox, 'agent_command', lambda *a, **k: pytest.fail('AGY probe ran'))
    with pytest.raises(edit_sandbox.Unavailable) as info:
        edit_sandbox.preflight(tree=str(tree), profile=str(tmp_path / 'p'), exe='/trusted/agy')
    assert str(info.value) == ('implement requires Bubblewrap 0.8.0+ (found 0.6.1; --disable-userns is '
                               'unsupported; Ubuntu 22.04 ships 0.6.x; Debian 12+/Ubuntu 24.04+ qualify)')


@pytest.mark.parametrize('output,expected', [('bubblewrap 0.9.0', (0, 9, 0)), ('bubblewrap 0.8.0', (0, 8, 0)),
                                             ('something else', None)])
def test_new_or_unparseable_bubblewrap_proceeds(tmp_path, monkeypatch, output, expected):
    tool, tree = _fake_bwrap(tmp_path, monkeypatch, output)
    assert edit_sandbox.bwrap_version(str(tool), str(tree)) == expected
    edit_sandbox.check_bwrap_version(str(tree))  # no refusal


def test_bubblewrap_version_check_is_linux_only(tmp_path, monkeypatch):
    monkeypatch.setattr(edit_sandbox.sys, 'platform', 'darwin')
    monkeypatch.setattr(host_tools, 'executable', lambda *a: pytest.fail('bwrap looked up on macOS'))
    edit_sandbox.check_bwrap_version(str(tmp_path))
