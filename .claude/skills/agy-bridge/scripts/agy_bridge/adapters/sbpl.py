"""macOS Seatbelt (sandbox-exec) policy helpers shared by the grading and implementation check sandboxes."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys

from ..core import config


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
