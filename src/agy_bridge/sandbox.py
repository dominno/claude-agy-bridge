"""OS confinement for explicitly requested checks of untrusted proposals.

No backend silently falls back to an ordinary user process. The case (including
its interpreter and environment) is trusted input, but the solution is not.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import shlex

from . import config


class Unavailable(config.UsageError):
    pass


def unavailable(detail):
    return Unavailable('sandbox unavailable on this machine: %s; refusing execution. '
                       'Use --unsafe-no-sandbox explicitly to run with your privileges and network access' % detail)


def interpreter_binaries(command):
    """Framework Python stubs re-exec Python.app, including through venv links."""
    resolved = Path(os.path.realpath(command))
    binaries = {str(resolved)}
    if resolved.name.lower().startswith('python'):
        roots = [resolved.parent.parent]
        # Apple's /usr/bin/python3 shim is resolved to the installed CLT above.
        framework = resolved.parent.parent.parent/'Library/Frameworks/Python3.framework/Versions/Current'
        roots.append(framework)
        for root in roots:
            for name in ('Python', 'Python3'):
                app = root/('Resources/%s.app/Contents/MacOS/%s' % (name, name))
                if app.is_file():
                    binaries.add(os.path.realpath(app))
    return sorted(binaries)


def executable(name, path):
    candidates = [name] if os.path.isabs(name) else [os.path.join(p, name) for p in path.split(os.pathsep)]
    for candidate in candidates:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            candidate = os.path.abspath(candidate)
            # Apple's developer shims need a child to select the real tool.
            # Select the installed binary without granting child permission.
            developer = '/Library/Developer/CommandLineTools/usr/bin/'+os.path.basename(candidate)
            if sys.platform == 'darwin' and os.path.dirname(candidate) == '/usr/bin' and os.path.isfile(developer):
                return developer
            return candidate
    return None


def runtime_paths(command):
    """Read-only OS libraries and the chosen interpreter installation, never HOME."""
    paths = ['/System/Library', '/usr/lib', '/usr/lib64', '/usr/share', '/lib', '/lib64',
             '/dev/null', '/dev/urandom', '/dev/random', '/dev/zero', '/etc/ld.so.cache',
             '/etc/localtime', '/opt/homebrew/lib', '/opt/homebrew/opt', '/opt/homebrew/Cellar',
             '/usr/local/opt', '/usr/local/Cellar', sys.base_prefix]
    paths += interpreter_binaries(command)
    # A virtualenv's lib + pyvenv.cfg are needed even when bin/python is a symlink.
    for exe in (command, sys.executable):
        for candidate in (exe, os.path.realpath(exe)):
            parent = Path(candidate).parent
            if parent.name == 'bin':
                paths += [str(parent.parent/name) for name in
                          ('lib', 'lib64', 'libexec', 'pyvenv.cfg', 'Python', 'Resources')]
        paths += [exe, os.path.realpath(exe)]
    # A different selected framework (e.g. CLT Python) needs its own stdlib.
    for binary in interpreter_binaries(command):
        for parent in Path(binary).parents:
            if parent.parent.name == 'Versions' and parent.parent.parent.name.endswith('.framework'):
                paths.append(str(parent))
                break
    if sys.platform.startswith('linux'):
        paths.remove(sys.base_prefix)
        paths += ['/bin/sh', os.path.join(sys.base_prefix, 'lib'), os.path.join(sys.base_prefix, 'lib64')]
    result = sorted({value for p in paths if os.path.exists(p)
                     for value in (os.path.abspath(p), os.path.realpath(p))})
    private = {'.ssh', '.config', '.aws', '.gnupg', '.claude', '.codex', '.git'}
    if any(part in private or part.startswith('.env') for path in result for part in Path(path).parts):
        raise unavailable('interpreter runtime is inside a private credential directory')
    return result


def mac_profile(tree, command, home=None):
    def quote(value):
        return json.dumps(value, ensure_ascii=False)
    rules = ['(version 1)', '(deny default)', '(allow sysctl-read)',
             '(allow process-info* (target self))', '(allow signal (target self))',
             '(allow mach-lookup (global-name "com.apple.system.logger"))', '(deny network*)',
             '(deny file-read*)', '(allow file-read-metadata)', '(deny file-write*)',
             # Prevent fork/setsid double-fork escapes as well as shell execution.
             '(deny process-fork)', '(deny process-exec)',
             # macOS 13 needs content access to the root directory itself. This
             # is literal, not subpath: unrelated files remain unreadable.
             '(allow file-read* (literal "/"))']
    for binary in interpreter_binaries(command):
        rules.append('(allow process-exec (literal %s))' % quote(binary))
    for path in runtime_paths(command) + [os.path.realpath(tree)]:
        kind = 'subpath' if os.path.isdir(path) else 'literal'
        rules.append('(allow file-read* (%s %s))' % (kind, quote(path)))
        rules.append('(allow file-map-executable (%s %s))' % (kind, quote(path)))
    rules += ['(allow file-write* (subpath %s) (literal "/dev/null"))' % quote(os.path.realpath(tree))]
    homes = {os.path.expanduser('~'), home or os.path.expanduser('~')}
    homes |= {os.path.realpath(value) for value in homes}
    for home in homes:
        for folder in ('.ssh', '.config', '.aws', '.gnupg', '.claude', '.codex'):
            rules.append('(deny file-read* (subpath %s))' % quote(os.path.join(home,folder)))
    # Explicit denials also cover runtime installations containing these names.
    rules.append('(deny file-read* (regex #"(^|/)([.]git|[.]env[^/]*)(/|$)"))')
    return '\n'.join(rules)


def linux_bindings(command):
    """Expose runtime roots only, hiding sensitive names even within an install."""
    paths = runtime_paths(command)
    setpriv = executable('setpriv', '/usr/bin:/bin:/usr/local/bin')
    if setpriv:
        paths = sorted(set(paths + [setpriv, os.path.realpath(setpriv)]))
    blocked = set()
    private = {'.ssh', '.config', '.aws', '.gnupg', '.claude', '.codex'}
    for path in paths:
        if not os.path.isdir(path):
            continue
        for parent, dirs, files in os.walk(path, followlinks=False):
            for name in dirs + files:
                if name in private or name == '.git' or name.startswith('.env'):
                    blocked.add(os.path.join(parent, name))
            dirs[:] = [name for name in dirs if os.path.join(parent, name) not in blocked]
    return paths, sorted(blocked)


def unshare_command(tool, argv, tree, jail, paths, blocked):
    # unshare -n alone cannot confine files. Pair it with a private mount/PID/user
    # namespace and a minimal chroot; if namespaces/mounts are forbidden, fail closed.
    mount = executable('mount', '/usr/bin:/bin:/usr/sbin:/sbin')
    chroot = executable('chroot', '/usr/bin:/bin:/usr/sbin:/sbin')
    setpriv = executable('setpriv', '/usr/bin:/bin:/usr/local/bin')
    if not mount or not chroot or not setpriv or not jail:
        raise unavailable('unshare requires mount, chroot, setpriv and a private filesystem root')
    q = shlex.quote
    lines = ['set -eu', q(mount)+' --make-rprivate /',
             q(mount)+' -t tmpfs -o mode=0755 tmpfs '+q(jail)]
    for path in paths + [tree]:
        dest = jail + path
        if os.path.isdir(path):
            lines.append('[ -d '+q(dest)+' ] || mkdir -p '+q(dest))
        else:
            lines += ['mkdir -p '+q(os.path.dirname(dest)), '[ -e '+q(dest)+' ] || : > '+q(dest)]
        lines.append(q(mount)+' --bind '+q(path)+' '+q(dest))
        if path != tree:
            lines.append(q(mount)+' -o remount,bind,ro '+q(dest))
    for path in blocked:
        dest = jail + path
        if os.path.isdir(path):
            lines.append(q(mount)+' -t tmpfs -o ro,mode=000 tmpfs '+q(dest))
        else:
            lines.append(q(mount)+' --bind /dev/null '+q(dest))
    # Drop mount/chroot capabilities before untrusted code can unmask bindings.
    lines.append(q(mount)+' -o remount,ro '+q(jail))
    launch = 'cd '+q(tree)+'; exec '+shlex.join([setpriv, '--bounding-set=-all',
              '--inh-caps=-all', '--ambient-caps=-all', '--no-new-privs', '--'] + argv)
    lines.append('exec '+q(chroot)+' '+q(jail)+' /bin/sh -c '+q(launch))
    return [tool, '--user', '--map-root-user', '--mount', '--net', '--pid',
            '--fork', '--kill-child', '--', '/bin/sh', '-c', '\n'.join(lines)]


def wrap(argv, tree, env, unsafe_no_sandbox=False, jail=None):
    command = executable(argv[0], env['PATH'])
    if command is None:
        raise OSError('check executable unavailable')
    argv = [command] + argv[1:]
    if unsafe_no_sandbox:
        return argv
    if sys.platform == 'darwin':
        tool = '/usr/bin/sandbox-exec'
        if os.path.isfile(tool):
            return [tool, '-p', mac_profile(tree, command, env.get('HOME'))] + argv
    elif sys.platform.startswith('linux'):
        tool = executable('bwrap', '/usr/bin:/bin:/usr/local/bin')
        if tool:
            paths, blocked = linux_bindings(command)
            # PID namespace teardown kills even setsid/double-fork children.
            result = [tool, '--unshare-all', '--die-with-parent', '--new-session', '--cap-drop', 'ALL', '--dev', '/dev']
            # A read-only bind of a device cannot be written and may be unopenable; --dev supplies fresh ones.
            for path in paths:
                if path.startswith('/dev/'):
                    continue
                result += ['--ro-bind', path, path]
            for path in blocked:
                result += ['--tmpfs', path, '--remount-ro', path] if os.path.isdir(path) else ['--ro-bind', '/dev/null', path]
            result += ['--bind', tree, tree, '--chdir', tree, '--remount-ro', '/', '--'] + argv
            return result
        tool = executable('unshare', '/usr/bin:/bin:/usr/local/bin')
        if tool:
            paths, blocked = linux_bindings(command)
            return unshare_command(tool, argv, tree, jail, paths, blocked)
    raise unavailable('no execution sandbox backend available')
