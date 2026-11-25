"""The macOS (sandbox-exec) backend of the implementation sandbox: policy text, platform selection, profile
defaults, and real negative probes on darwin."""
import json
import os
import stat
import sys

import pytest

from agy_bridge.adapters import agy, edit_macos, edit_sandbox, host_tools
from agy_bridge.adapters.edit_runtime import GUEST_PRIVATE_PATHS
from agy_bridge.core import config

darwin_only = pytest.mark.skipif(sys.platform != 'darwin' or edit_macos.available() is not None,
                                 reason='needs macOS with /usr/bin/sandbox-exec')


@pytest.fixture
def areas(tmp_path):
    profile, tree = tmp_path / 'profile', tmp_path / 'candidate'
    for directory in (profile, tree):
        directory.mkdir(mode=0o700)
    return tree.resolve(), profile.resolve()


@pytest.fixture
def macos(monkeypatch):
    monkeypatch.setattr(edit_sandbox.sys, 'platform', 'darwin')
    monkeypatch.setattr(edit_macos, 'available', lambda: None)


def _policy(areas, homes=('/Users/someone',)):
    tree, profile = areas
    return edit_macos.agent_policy('/opt/agy/agy', tree=str(tree), profile=str(profile), homes=list(homes))


def test_agent_policy_is_default_deny_with_only_tree_and_profile_writable(areas):
    tree, profile = areas
    rules = _policy(areas).splitlines()
    assert rules[:2] == ['(version 1)', '(deny default)']
    writes = [rule for rule in rules if 'file-write*' in rule and rule.startswith('(allow')]
    assert writes == ['(allow file-read* file-write* (subpath "%s") (subpath "%s"))' % (tree, profile),
                      '(allow file-write* (literal "/dev/null"))']
    assert [rule for rule in rules if 'process-exec' in rule] == ['(allow process-exec (literal "/opt/agy/agy"))']
    assert '(deny process-fork)' not in rules and not any('process-fork' in rule for rule in rules)
    assert '(allow network*)' in rules


def test_agent_policy_has_no_keychain_and_denies_private_home_folders_last(areas):
    policy = _policy(areas)
    lookups = [rule for rule in policy.splitlines() if 'mach-lookup' in rule]
    assert lookups == ['(allow mach-lookup (global-name "com.apple.system.logger") '
                       '(global-name "com.apple.trustd.agent"))']
    assert 'SecurityServer' not in policy and 'securityd' not in policy and 'Keychains"' in policy
    rules = policy.splitlines()
    last_allow = max(index for index, rule in enumerate(rules) if rule.startswith('(allow'))
    denials = [rule for rule in rules[last_allow + 1:]]
    for folder in ('.ssh', '.aws', '.gemini', '.config', '.claude', '.codex', 'Library/Keychains'):
        assert '(deny file-read* file-write* (subpath "/Users/someone/%s"))' % folder in denials
    # No allowance names the home itself; only metadata of the ancestors of agy, tree and profile.
    assert not any('"/Users/someone"' in rule for rule in rules if rule.startswith('(allow file-read* '))


def test_policy_paths_refuse_quotes_and_controls():
    for bad in ('relative/path', '/tmp/a"b', '/tmp/a\\b', '/tmp/a\nb'):
        with pytest.raises(config.UsageError):
            edit_macos.quote(bad)
    assert edit_macos.quote('/tmp/żółw') == '"/tmp/żółw"'


def test_agent_command_sets_home_and_private_temp_outside_argv_secrets(areas, macos, monkeypatch):
    tree, profile = areas
    monkeypatch.setenv('GEMINI_API_KEY', 'test-key')
    cmd = edit_sandbox.agent_command([sys.executable, '--print', 'change'], tree=tree, profile=profile)
    sandbox_at = cmd.index(edit_macos.SANDBOX_EXEC)
    assert cmd[0] == '/usr/bin/env' and cmd[sandbox_at + 1] == '-p'
    assert cmd[1:sandbox_at] == ['HOME=%s' % profile, 'PATH=/usr/bin:/bin:/usr/sbin:/sbin', 'LANG=C.UTF-8',
                                 'TERM=dumb', 'USER=agy', 'LOGNAME=agy', 'TMPDIR=%s/tmp/' % profile]
    assert cmd[sandbox_at + 3:] == [os.path.realpath(sys.executable), '--print', 'change']
    assert 'test-key' not in ' '.join(cmd)
    assert stat.S_IMODE(os.lstat(profile / 'tmp').st_mode) == 0o700
    assert edit_sandbox.agent_environment(use_api_key=True) == {'GEMINI_API_KEY': 'test-key'}


def test_agent_command_refuses_a_symlinked_profile_temp(areas, macos, tmp_path):
    tree, profile = areas
    (profile / 'tmp').symlink_to(tmp_path)
    with pytest.raises(config.UsageError, match='temp folder'):
        edit_sandbox.agent_command([sys.executable], tree=tree, profile=profile)


def test_platform_selects_the_backend(areas, monkeypatch):
    tree, profile = areas
    monkeypatch.setattr(edit_sandbox.sys, 'platform', 'sunos5')
    with pytest.raises(edit_sandbox.Unavailable, match='Linux with Bubblewrap or macOS with sandbox-exec'):
        edit_sandbox.agent_command([sys.executable], tree=tree, profile=profile)
    assert edit_sandbox.agent_private_paths(tree=tree, profile=profile) == GUEST_PRIVATE_PATHS
    monkeypatch.setattr(edit_sandbox.sys, 'platform', 'darwin')
    monkeypatch.setattr(edit_macos, 'available', lambda: 'sandbox-exec was not found at /usr/bin/sandbox-exec')
    with pytest.raises(edit_sandbox.Unavailable, match='sandbox-exec was not found'):
        edit_sandbox.agent_command([sys.executable], tree=tree, profile=profile)
    with pytest.raises(edit_sandbox.InfrastructureError, match='sandbox-exec was not found'):
        edit_sandbox.run_test(['/bin/true'], tree=tree, timeout_s=5)
    monkeypatch.setattr(edit_sandbox.sys, 'platform', 'linux')
    monkeypatch.setattr(host_tools, 'executable', lambda _name, _path: '/usr/bin/bwrap')
    assert edit_sandbox.agent_command([sys.executable], tree=tree, profile=profile)[0] == '/usr/bin/bwrap'


def test_macos_private_paths_cover_the_profile_and_credentials(areas, macos):
    _, profile = areas
    paths = edit_sandbox.agent_private_paths(tree=profile.parent / 'candidate', profile=profile)
    assert str(profile) + '/' in paths and os.path.expanduser('~/.ssh/') in paths
    assert os.path.expanduser('~/.gemini/') in paths and '/etc/' in paths
    # AGY asks (and print mode ends the turn) for any other path outside the workspace: name the likely probes.
    assert str(profile.parent / '.git') + '/' in paths and '/.git/' in paths
    assert not any(path.startswith(str(profile.parent / 'candidate')) for path in paths)
    settings = json.loads(agy._session_settings(edit=True, private_paths=paths))
    assert 'read_file(%s/)' % profile in settings['permissions']['deny']
    linux = json.loads(agy._session_settings(edit=True))
    assert 'read_file(/home/agy/)' in linux['permissions']['deny']


def test_check_command_is_offline_and_writes_only_tree_and_scratch(areas, tmp_path):
    tree, _ = areas
    scratch = tmp_path / 'scratch'
    scratch.mkdir()
    cmd = edit_macos.check_command(['/bin/true'], tree=str(tree), scratch=str(scratch))
    policy = cmd[cmd.index('-p') + 1]
    assert '(deny network*)' in policy and '(deny process-fork)' in policy and '(allow network' not in policy
    assert '(allow file-read* file-write* (subpath "%s"))' % scratch in policy
    assert cmd[-1] == os.path.realpath(edit_macos.check_executable('/bin/true', str(tree))) and 'HOME=%s' % scratch in cmd
    assert edit_macos.check_command(['./missing'], tree=str(tree), scratch=str(scratch)) is None
    assert edit_macos.check_command(['../escape'], tree=str(tree), scratch=str(scratch)) is None
    assert edit_macos.launch_failed(65, 'sandbox-exec: sandbox_apply: Operation not permitted\n')
    assert not edit_macos.launch_failed(1, 'FAILED (failures=1)\n')


def _default_config(profile):
    root = profile / '.gemini' / 'config'
    (root / 'projects').mkdir(parents=True)
    (root / '.migrated').write_text('')
    (root / 'mcp_config.json').write_text('')
    (root / 'config.json').write_text('{"plugins":{"swarm":{"enabled":true}}}')
    (root / 'projects' / 'default-cli-project.json').write_text(
        '{"id": "default-cli-project", "name": "CLI Project", "projectResources": {}}')
    return root


def test_profile_accepts_only_the_agy_default_config(areas):
    tree, profile = areas
    root = _default_config(profile)
    assert edit_sandbox.validate_profile(profile, repo=tree) == str(profile)
    changes = [('mcp_config.json', '{"mcpServers":{"x":{"command":"sh"}}}'),
               ('config.json', '{"plugins":{"swarm":{"enabled":true},"other":{"enabled":true}}}'),
               ('config.json', '{"plugins":{},"hooks":{}}'),
               ('extra.json', '{}'),
               ('projects/default-cli-project.json', '{"id":"x","projectResources":{"a":1}}')]
    for name, text in changes:
        original = (root / name).read_text() if (root / name).exists() else None
        (root / name).write_text(text)
        with pytest.raises(config.UsageError, match='differs from the AGY defaults'):
            edit_sandbox.validate_profile(profile)
        if original is None:
            (root / name).unlink()
        else:
            (root / name).write_text(original)
    assert edit_sandbox.validate_profile(profile) == str(profile)


@darwin_only
def test_darwin_agent_sandbox_blocks_host_reads_writes_and_exec(areas, tmp_path):
    tree, profile = areas
    secret = tmp_path / 'host-secret'
    secret.write_text('host')
    (profile / 'profile-file').write_text('dedicated\n')
    outside = tmp_path / 'outside-write'
    ssh = os.path.expanduser('~/.ssh')
    script = ('read x < "$1" && exit 10; echo x > "$2" && exit 11; '
              'shopt -s execfail; exec /bin/ls / && exit 12; [ -d "$4" ] && { read z < "$4/known_hosts" && exit 13; }; '
              'read p < "$3" || exit 14; echo edited > edited.txt || exit 15; exit 0')
    command = edit_sandbox.agent_command(
        ['/bin/bash', '-c', script, 'agy', str(secret), str(outside), str(profile / 'profile-file'), ssh],
        tree=tree, profile=profile)
    code, output = edit_sandbox._run(command, tree, {}, 20, None)
    assert code == 0, output
    assert (tree / 'edited.txt').read_text() == 'edited\n' and not outside.exists()


@darwin_only
def test_darwin_offline_check_runs_without_network_or_host_files(areas, tmp_path):
    tree, _ = areas
    assert edit_sandbox.run_test(['/bin/true'], tree=tree, timeout_s=10) == (0, '')
    assert edit_sandbox.preflight_test(tree=tree, timeout_s=10) == (0, '')
    with pytest.raises(edit_sandbox.InfrastructureError):
        edit_sandbox.run_test(['agy-bridge-missing-check-for-test'], tree=tree, timeout_s=5)
    if host_tools.executable('python3', edit_macos.SYSTEM_PATH) is None:
        pytest.skip('no system python3')
    secret = tmp_path / 'host-secret'
    secret.write_text('host')
    program = ('import os, socket, sys\n'
               'try:\n    open(sys.argv[1]).read(); sys.exit(20)\nexcept OSError: pass\n'
               'try:\n    socket.create_connection(("127.0.0.1", 9), 2); sys.exit(21)\n'
               'except OSError: pass\n'
               'open("written.txt", "w").write("ok"); print(os.environ["HOME"] != os.path.expanduser("~root"))')
    code, output = edit_sandbox.run_test(['python3', '-c', program, str(secret)], tree=tree, timeout_s=30)
    assert code == 0, output
    assert (tree / 'written.txt').read_text() == 'ok'
