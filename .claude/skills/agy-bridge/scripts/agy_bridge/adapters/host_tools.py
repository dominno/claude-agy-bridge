"""Trusted host executables and the Bubblewrap version policy shared by both sandboxes."""
from __future__ import annotations

import os
import re
import sys

MIN_BWRAP = (0, 8, 0)  # --disable-userns, which every bwrap sandbox here passes, first shipped in Bubblewrap 0.8.0
BWRAP_RELEASES = 'Ubuntu 22.04 ships 0.6.x; Debian 12+/Ubuntu 24.04+ qualify'
_BWRAP_VERSION = re.compile(r'bubblewrap ([0-9]{1,4})\.([0-9]{1,4})(?:\.([0-9]{1,4}))?')


def parse_bwrap_version(output):
    """(major, minor, patch) from `bwrap --version` output, or None."""
    match = _BWRAP_VERSION.search(output)
    return None if match is None else tuple(int(part or 0) for part in match.groups())


def bwrap_too_old(version, what):
    """The refusal text for a Bubblewrap older than MIN_BWRAP, or None (an unknown version proceeds)."""
    if version is None or version >= MIN_BWRAP:
        return None
    return ('%s requires Bubblewrap %d.%d.%d+ (found %d.%d.%d; --disable-userns is unsupported; %s)'
            % ((what,) + MIN_BWRAP + version + (BWRAP_RELEASES,)))


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
