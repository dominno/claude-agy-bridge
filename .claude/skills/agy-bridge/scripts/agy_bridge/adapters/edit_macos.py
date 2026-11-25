"""macOS Seatbelt (sandbox-exec) commands for implementation jobs: the AGY agent and its offline check.

Pure argv and policy builders; adapters/edit_sandbox.py validates the inputs, selects this backend on darwin and
supervises the processes. The agent policy is default-deny: AGY may read the OS libraries, its own binary, the job
tree and the dedicated profile, write only the tree and the profile, execute only itself, and use the network.
"""
from __future__ import annotations

import json
import os
import stat
import sys

from ..core import config, rootcheck
from . import host_tools, sbpl


SANDBOX_EXEC = '/usr/bin/sandbox-exec'
ENV = '/usr/bin/env'
SYSTEM_PATH = '/usr/bin:/bin:/usr/sbin:/sbin'
PROFILE_TEMP = 'tmp'
# Personal tool and credential folders under each real home. Default-deny already hides them; these rules come
# last so that no allowance (a profile or job tree that somehow nested there) can expose them.
PRIVATE_HOME_FOLDERS = ('.ssh', '.aws', '.gnupg', '.gemini', '.config', '.claude', '.codex', '.docker', '.kube',
                        '.netrc', '.git-credentials', 'Library/Keychains', 'Library/Cookies',
                        'Library/Application Support/Google', 'Library/Application Support/antigravity')
# Read-only operating system content AGY's Go runtime and TLS stack load. Never a home folder.
SYSTEM_READ = ('/System', '/usr/lib', '/usr/share', '/private/etc', '/private/var/db/timezone',
               '/Library/Preferences/Logging', '/private/var/run/resolv.conf')
SYSTEM_LITERALS = ('/', '/etc', '/var', '/tmp', '/dev/null', '/dev/random', '/dev/urandom', '/dev/zero')


def available():
    """Refusal text when this host cannot run the macOS implementation sandbox, else None."""
    if sys.platform != 'darwin':
        return 'the sandbox-exec backend requires macOS'
    if not (os.path.isfile(SANDBOX_EXEC) and os.access(SANDBOX_EXEC, os.X_OK)):
        return 'sandbox-exec was not found at /usr/bin/sandbox-exec'
    return None


def quote(path):
    """An SBPL string literal for an absolute path; refuse characters a policy string should never carry."""
    if not os.path.isabs(path) or any(ord(char) < 32 or char in '"\\' for char in path):
        raise config.UsageError('sandbox path must be absolute and free of quotes, backslashes and controls')
    return json.dumps(path, ensure_ascii=False)


def _home_denials(homes):
    rules = []
    for home in sorted(set(homes) | {os.path.realpath(home) for home in homes}):
        for folder in PRIVATE_HOME_FOLDERS:
            target = quote(os.path.join(home, folder))
            rules.append('(deny file-read* file-write* (subpath %s))' % target)
    return rules


def _ancestors(paths):
    """Every parent folder of the given paths: path lookup and getcwd need their metadata, never their listing."""
    found = set()
    for path in paths:
        parent = os.path.dirname(path)
        while parent and parent not in found:
            found.add(parent)
            if parent == '/':
                break
            parent = os.path.dirname(parent)
    return sorted(found)


def agent_policy(agy, *, tree, profile, homes):
    """The default-deny SBPL for one AGY process. All paths must already be validated real paths."""
    rules = ['(version 1)', '(deny default)',
             '(allow process-info* (target self))', '(allow signal (target self))', '(allow sysctl-read)',
             # AGY talks to its provider and to its own language server on localhost; like Bubblewrap
             # --share-net this is the host network (see THREAT_MODEL.md, network exposure).
             '(allow network*)',
             # Certificate verification only (measured: TLS fails without trustd.agent). No securityd and no
             # Keychain: AGY's credential manager falls back to the file-based token in the dedicated profile.
             '(allow mach-lookup (global-name "com.apple.system.logger") (global-name "com.apple.trustd.agent"))',
             '(allow process-exec (literal %s))' % quote(agy),
             '(allow file-read* file-map-executable (literal %s))' % quote(agy)]
    for path in SYSTEM_READ:
        rules.append('(allow file-read* file-map-executable (subpath %s))' % quote(path))
    rules.append('(allow file-read* %s)' % ' '.join('(literal %s)' % quote(path) for path in SYSTEM_LITERALS))
    rules.append('(allow file-read-metadata %s)' % ' '.join('(literal %s)' % quote(path)
                                                           for path in _ancestors((agy, tree, profile))))
    rules.append('(allow file-read* file-write* (subpath %s) (subpath %s))' % (quote(tree), quote(profile)))
    rules.append('(allow file-write* (literal "/dev/null"))')
    return '\n'.join(rules + _home_denials(homes))


def _private_temp(profile):
    """A 0700 temp folder inside the dedicated profile (the only other place AGY may write)."""
    path = os.path.join(profile, PROFILE_TEMP)
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise config.UsageError('AGY profile temp folder must be an owned, non-symlink directory with mode 0700')
    return path


def _environment(home, temp):
    """NAME=VALUE words for /usr/bin/env, which runs before sandbox-exec. Secrets stay in the Popen environment."""
    values = {'HOME': home, 'PATH': SYSTEM_PATH, 'LANG': 'C.UTF-8', 'TERM': 'dumb', 'USER': 'agy',
              'LOGNAME': 'agy', 'TMPDIR': temp + '/'}
    return ['%s=%s' % item for item in values.items()]


def agent_command(agy, args, *, tree, profile):
    """Wrap the real AGY binary; tree and profile are validated real paths, agy a resolved executable."""
    temp = _private_temp(profile)
    policy = agent_policy(agy, tree=tree, profile=profile, homes=rootcheck.real_homes())
    return [ENV, *_environment(profile, temp), SANDBOX_EXEC, '-p', policy, agy, *args]


def agent_private_paths(profile, tree):
    """Directory prefixes for AGY's own read/write tool deny list (defense in depth beside the OS policy).

    A tool call on a path named here fails visibly for the model. Any other path outside the workspace makes AGY
    ask for approval, which print mode soft-denies by ending the turn (exit 15, `permission`). Measured on macOS:
    AGY's research subagent probed `<job>/.git/HEAD` and `<job>/meta.json` next to the tree, so the job folder's
    other entries and a `.git/` under every ancestor of the tree are named too.
    """
    homes = rootcheck.real_homes()
    paths = [profile, *(os.path.join(home, folder) for home in homes for folder in PRIVATE_HOME_FOLDERS),
             '/etc', '/private/etc', '/dev', *(os.path.join(parent, '.git') for parent in _ancestors((tree,)))]
    job = os.path.dirname(tree)
    siblings = [os.path.join(job, name) for name in sorted(os.listdir(job)) if name != os.path.basename(tree)]
    return tuple(path.rstrip('/') + '/' for path in paths) + tuple(
        path + '/' if os.path.isdir(path) else path for path in siblings)


def check_executable(name, tree):
    """The real path the offline check starts: a system tool by name or a workspace-relative program."""
    if '/' in name and not name.startswith('/'):
        candidate = os.path.realpath(os.path.join(tree, name))
        inside = os.path.commonpath((tree, candidate)) == tree
        return candidate if inside and os.path.isfile(candidate) and os.access(candidate, os.X_OK) else None
    found = host_tools.executable(name, SYSTEM_PATH)
    if found is None and name.startswith('/'):
        # run_test admits only absolute names in a system folder; macOS keeps e.g. true in /usr/bin, not /bin.
        found = host_tools.executable(os.path.basename(name), SYSTEM_PATH)
    return found


def check_command(argv, *, tree, scratch):
    """Run a trusted check argv offline in the grading Seatbelt profile, with HOME and TMPDIR in `scratch`."""
    command = check_executable(argv[0], tree)
    if command is None:
        return None
    try:
        policy = sbpl.mac_profile(tree, command, scratch)
    except sbpl.Unavailable:
        return None  # the runtime lies in a private credential folder
    policy += '\n(allow file-read* file-write* (subpath %s))' % quote(scratch)
    policy += '\n' + '\n'.join(_home_denials(rootcheck.real_homes()))
    return [ENV, *_environment(scratch, scratch), SANDBOX_EXEC, '-p', policy, command, *argv[1:]]


def launch_failed(returncode, output):
    """True when sandbox-exec itself (policy or exec) failed rather than the check it started."""
    return returncode != 0 and output.startswith('sandbox-exec:')
