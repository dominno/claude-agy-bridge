"""Isolated Linux processes for implementation jobs and their test commands."""
from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import selectors
import signal
import stat
import subprocess
import sys
import time

from . import config, sandbox, stage


GUEST_TREE = '/workspace'
GUEST_PROFILE = '/home/agy'
GUEST_AGY = '/opt/agy/bin/agy'
SYSTEM_PATH = '/usr/bin:/bin:/usr/sbin:/sbin'
TEST_OUTPUT_TAIL = 8192


class Unavailable(config.UsageError):
    """A required OS isolation feature is unavailable."""


def _unavailable(detail):
    return Unavailable('implementation sandbox unavailable: ' + detail)


def _private_directory(path, label):
    path = os.path.abspath(path)
    resolved = os.path.realpath(path)
    try:
        info = os.lstat(path)
    except OSError:
        raise config.UsageError('%s must be an existing private directory' % label) from None
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise config.UsageError('%s must be an owned, non-symlink directory with mode 0700' % label)
    return resolved


def _nested(left, right):
    return os.path.commonpath((left, right)) == left


def validate_profile(path, repo=None):
    """Accept an explicitly selected standalone AGY profile, outside the source repo."""
    profile = _private_directory(path, 'AGY profile')
    for home in stage.real_homes():
        if _nested(profile, home):
            raise config.UsageError('AGY profile must be separate from the login home')
        if any(_nested(os.path.join(home, name), profile) for name in ('.gemini', '.ssh', '.aws', '.config', '.codex', '.claude')):
            raise config.UsageError('AGY profile must be separate from personal tool and credential directories')
    if repo is not None:
        repo = os.path.realpath(repo)
        if _nested(repo, profile) or _nested(profile, repo):
            raise config.UsageError('AGY profile must be outside the source repository')
    for name in ('.agents', 'AGENTS.md', 'GEMINI.md', '.gemini/AGENTS.md', '.gemini/GEMINI.md',
                 '.gemini/config', '.gemini/antigravity-cli/skills', '.gemini/antigravity-cli/plugins',
                 '.gemini/antigravity-cli/hooks.json', '.gemini/antigravity-cli/mcp_config.json',
                 '.gemini/antigravity-cli/mcp_servers.json'):
        if os.path.lexists(os.path.join(profile, name)):
            raise config.UsageError('AGY profile must be dedicated to implementation and free of customizations (%s)' % name)
    for name in ('.gemini', '.gemini/antigravity-cli', '.gemini/antigravity-cli/settings.json'):
        if os.path.islink(os.path.join(profile, name)):
            raise config.UsageError('AGY profile contains a symlink at %s' % name)
    _check_settings(profile)
    return profile


def _check_settings(profile):
    path = os.path.join(profile, '.gemini/antigravity-cli/settings.json')
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return
    except OSError:
        raise config.UsageError('could not safely open dedicated AGY profile settings') from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 65536:
            raise config.UsageError('dedicated AGY profile settings must be a regular file up to 64 KiB')
        with os.fdopen(fd, 'rb', closefd=False) as file:
            settings = json.loads(file.read(65537))
    except (ValueError, RecursionError):
        raise config.UsageError('dedicated AGY profile settings must contain a JSON object') from None
    finally:
        os.close(fd)
    if not isinstance(settings, dict):
        raise config.UsageError('dedicated AGY profile settings must contain a JSON object')
    active_extensions = ('hooks', 'mcpServers', 'customModelsConfig', 'sandboxProxy', 'statusLine', 'title')
    if any(settings.get(key) for key in active_extensions):
        raise config.UsageError('dedicated AGY profile settings may not enable hooks, external tools or custom endpoints')


def agent_environment(*, use_api_key=False):
    """Build the exact Popen environment for agent_command. Secrets stay out of argv."""
    if not use_api_key:
        return {}
    api_key = os.environ.get('GEMINI_API_KEY')
    if not api_key:
        raise config.UsageError('API key authentication requires GEMINI_API_KEY in the environment')
    return {'GEMINI_API_KEY': api_key}


def _bwrap_args(network):
    if not sys.platform.startswith('linux'):
        raise _unavailable('implement requires Linux and Bubblewrap')
    tool = sandbox.executable('bwrap', '/usr/bin:/bin')
    if tool is None:
        raise _unavailable('Bubblewrap was not found in /usr/bin or /bin')
    argv = [tool, '--unshare-all', '--unshare-user', '--disable-userns', '--die-with-parent',
            '--new-session', '--cap-drop', 'ALL', '--proc', '/proc', '--dev', '/dev', '--tmpfs', '/tmp']
    if network:
        argv.insert(2, '--share-net')
    else:
        argv += ['--clearenv']
    for name, value in {'HOME': GUEST_PROFILE, 'PATH': SYSTEM_PATH, 'LANG': 'C.UTF-8', 'TERM': 'dumb',
                        'USER': 'agy', 'LOGNAME': 'agy', 'TMPDIR': '/tmp'}.items():
        argv += ['--setenv', name, value]
    for directory in ('/lib', '/lib64', '/lib32', '/usr/lib', '/usr/lib64', '/usr/lib32', '/usr/libexec'):
        if os.path.isdir(directory):
            argv += ['--ro-bind', os.path.realpath(directory), directory]
    if network:
        for path in ('/etc/ld.so.cache', '/etc/resolv.conf', '/etc/nsswitch.conf',
                     '/etc/ssl/certs/ca-certificates.crt', '/etc/pki/tls/certs/ca-bundle.crt',
                     '/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem'):
            if os.path.isfile(path):
                argv += ['--ro-bind', os.path.realpath(path), path]
    else:
        for directory in ('/bin', '/sbin', '/usr/bin', '/usr/sbin', '/usr/share/nodejs', '/usr/share/java',
                          '/usr/share/python-wheels', '/usr/share/perl', '/usr/share/perl5', '/usr/share/git-core',
                          '/usr/share/zoneinfo'):
            if os.path.isdir(directory):
                argv += ['--ro-bind', os.path.realpath(directory), directory]
    return argv


def _workspace_args(tree):
    return ['--bind', tree, GUEST_TREE, '--chdir', GUEST_TREE, '--remount-ro', '/']


def agent_command(argv, *, tree, profile):
    """Wrap a real AGY argv. Call Popen with agent_environment and close_fds=True."""
    tree = _private_directory(tree, 'implementation workspace')
    profile = validate_profile(profile)
    if _nested(tree, profile) or _nested(profile, tree):
        raise config.UsageError('AGY profile and implementation workspace must be separate directories')
    source = os.path.realpath(argv[0])
    if not os.path.isfile(source) or not os.access(source, os.X_OK):
        raise config.UsageError('AGY binary must be an executable file')
    command = _bwrap_args(network=True)
    command += ['--ro-bind', source, GUEST_AGY, '--bind', profile, GUEST_PROFILE,
                '--setenv', 'PATH', '/opt/agy/bin']
    return command + _workspace_args(tree) + ['--', GUEST_AGY, *argv[1:]]


def _test_command(argv, tree):
    command = _bwrap_args(network=False) + ['--tmpfs', GUEST_PROFILE] + _workspace_args(tree)
    return command + ['--', *argv]


def _terminate(proc):
    if proc.poll() is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGKILL)
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        raise _unavailable('Bubblewrap did not exit after cancellation') from None


def _run(argv, tree, env, timeout_s, should_cancel):
    with subprocess.Popen(argv, cwd=tree, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, start_new_session=True, close_fds=True) as proc:
        deadline = time.monotonic() + timeout_s
        tail = bytearray()
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(proc.stdout, selectors.EVENT_READ)
                while selector.get_map() or proc.poll() is None:
                    if should_cancel is not None and should_cancel():
                        return 130, 'cancelled'
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return 124, 'timeout after %ss' % timeout_s
                    for key, _ in selector.select(min(remaining, 0.1)):
                        data = os.read(key.fd, 65536)
                        if not data:
                            selector.unregister(key.fileobj)
                            continue
                        tail.extend(data)
                        if len(tail) > TEST_OUTPUT_TAIL:
                            del tail[:-TEST_OUTPUT_TAIL]
            return proc.returncode, tail.decode('utf-8', 'replace')
        finally:
            _terminate(proc)


def preflight(*, tree, profile, exe, should_cancel=None):
    """Probe the exact AGY mount and namespace before a prompt can be sent."""
    command = agent_command([exe, '--version'], tree=tree, profile=profile)
    try:
        code, detail = _run(command, tree, {}, 15, should_cancel)
    except OSError as exc:
        raise _unavailable('could not start Bubblewrap: %s' % exc.strerror) from None
    if code:
        raise _unavailable('AGY version probe returned %s (%s)' % (code, detail.strip()[-300:]))
    return detail


def run_test(argv, *, tree, timeout_s, should_cancel=None):
    """Run one trusted argv in a writable disposable test tree, offline."""
    tree = _private_directory(tree, 'test workspace')
    if not argv:
        raise config.UsageError('test command is empty')
    if argv[0].startswith('/') and Path(argv[0]).parent not in map(Path, SYSTEM_PATH.split(':')):
        raise config.UsageError('use a system test executable or a workspace-relative path')
    try:
        return _run(_test_command(argv, tree), tree, {}, timeout_s, should_cancel)
    except OSError as exc:
        raise _unavailable('could not start Bubblewrap: %s' % exc.strerror) from None
