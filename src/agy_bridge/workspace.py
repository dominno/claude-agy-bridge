"""Capture an approved code snapshot and export the actual edits from its working copy."""
from __future__ import annotations

import hashlib
import os
import re
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import config, denylist, safety, stage
from .scan import scan_bytes


EDIT_DENY = ('.agents', 'AGENTS.md', 'GEMINI.md', '.mcp.json', '.agy-bridge.json',
             '.gitmodules', '.envrc', '.direnv')
MAX_FILE_BYTES = 1024 * 1024
MAX_TREE_BYTES = 8 * 1024 * 1024
MAX_DIFF_BYTES = 20 * 1024 * 1024
DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK


class Error(config.UsageError):
    pass


@dataclass(frozen=True)
class WorkspaceFile:
    data: bytes
    executable: bool

    def manifest(self, rel):
        return dict(path=rel, bytes=len(self.data), sha256=hashlib.sha256(self.data).hexdigest(),
                    executable=self.executable)


@dataclass(frozen=True)
class Snapshot:
    root: str
    task: str
    base_commit: str | None
    extra_deny: tuple
    not_sent: tuple
    _files: dict[str, WorkspaceFile]

    @property
    def manifest(self):
        return tuple(entry.manifest(rel) for rel, entry in sorted(self._files.items()))


@dataclass(frozen=True)
class Workspace:
    baseline: Path
    tree: Path
    manifest: tuple
    base_commit: str | None
    extra_deny: tuple


@dataclass(frozen=True)
class Changes:
    paths: tuple[tuple[str, str], ...]
    diff: bytes
    _files: dict[str, WorkspaceFile]


def _valid_path(rel, extra):
    safety.relative(rel)
    if not rel.isprintable() or len(rel.split('/')) > 64:
        raise Error('workspace path has control characters or exceeds the maximum depth')
    if scan_bytes(rel.encode('utf-8', 'replace')):
        raise Error('workspace contains a key-shaped name')
    if denylist.is_denied(rel, extra):
        raise Error('workspace path is denied: ' + stage.printable(rel))


def read_workspace_file(parent, name, rel):
    before = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise Error('workspace path is not a single-link regular file: ' + stage.printable(rel))
    fd = os.open(name, _FILE_FLAGS, dir_fd=parent)
    with os.fdopen(fd, 'rb') as handle:
        opened = os.fstat(handle.fileno())
        if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1 or
                (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)):
            raise Error('workspace file changed while being opened: ' + stage.printable(rel))
        data = handle.read(MAX_FILE_BYTES + 1)
        after = os.fstat(handle.fileno())
    if len(data) > MAX_FILE_BYTES:
        raise Error('workspace file exceeds its size limit: ' + stage.printable(rel))
    if (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
        raise Error('workspace file changed while being read: ' + stage.printable(rel))
    return WorkspaceFile(data, bool(opened.st_mode & stat.S_IXUSR))


def git_env(home):
    home = os.fspath(home)
    return {
        'HOME': home, 'PATH': '/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin', 'LC_ALL': 'C',
        'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_ATTR_NOSYSTEM': '1',
        'GIT_TERMINAL_PROMPT': '0', 'GIT_OPTIONAL_LOCKS': '0',
        'GIT_CEILING_DIRECTORIES': os.path.dirname(home),
    }


def git_executable():
    for base in ('/usr/bin', '/bin', '/opt/homebrew/bin', '/usr/local/bin'):
        exe = os.path.join(base, 'git')
        if os.path.isfile(exe) and os.access(exe, os.X_OK):
            return exe
    raise Error('Git is required for an implementation workspace')


def run_git(args, cwd, env, data=b'', check=True):
    argv = [git_executable(), *args]
    try:
        result = subprocess.run(argv, cwd=cwd, env=env, input=data, capture_output=True, timeout=15)
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise Error('Git could not complete a workspace operation') from exc
    if check and result.returncode:
        raise Error('Git rejected a workspace operation')
    return result


def source_commit(root):
    with tempfile.TemporaryDirectory(prefix='agy-edit-git-home-') as home:
        env = dict(git_env(home), GIT_CEILING_DIRECTORIES=os.path.dirname(root))
        result = run_git(['rev-parse', '--verify', 'HEAD'], root, env, check=False)
    value = result.stdout.strip()
    if result.returncode == 0 and re.fullmatch(rb'[0-9a-f]{40}|[0-9a-f]{64}', value):
        return value.decode('ascii')
    return None


def prepare(repo, files, globs, task):
    if not isinstance(task, str) or not task.strip():
        raise Error('implementation needs a nonempty task')
    root = stage.resolve_repo(repo)
    stage.check_root(root)
    extra = tuple(config.load_extra_deny(root)) + EDIT_DENY
    commit = source_commit(root)
    root, prepared = stage.prepare(repo, files, globs, task, MAX_FILE_BYTES, MAX_TREE_BYTES, extra,
                                   include_modes=True)
    captured = {}
    normalized = {}
    for entry in prepared.entries:
        rel = record_path(entry.rel, extra, normalized)
        captured[rel] = WorkspaceFile(entry.data, entry.executable)
    if source_commit(root) != commit:
        raise Error('source Git HEAD changed during workspace preparation')
    return Snapshot(root, task, commit, extra, tuple(prepared.not_sent), captured)


def record_path(rel, extra, seen):
    _valid_path(rel, extra)
    raw = rel.split('/')
    folded = tuple(denylist._components(rel))
    for depth in range(1, len(raw) + 1):
        key = folded[:depth]
        if seen.setdefault(key, raw[depth - 1]) != raw[depth - 1]:
            raise Error('workspace contains paths that collide after case and Unicode normalization')
    return rel


def _write_tree(base, files, readonly=False):
    with safety.directory(os.fspath(base.parent)) as parent:
        os.mkdir(base.name, 0o700, dir_fd=parent)
    for rel, entry in sorted(files.items()):
        folder, name = os.path.split(rel)
        executable = 0o100 if entry.executable else 0
        mode = (0o400 if readonly else 0o600) | executable
        with safety.directory(os.fspath(base / folder), create=True) as parent:
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
            with os.fdopen(fd, 'wb') as handle:
                handle.write(entry.data)
                handle.flush()
                os.fchmod(handle.fileno(), mode)


def load(job_dir, manifest, base_commit=None, extra_deny=()):
    base = Path(os.path.abspath(job_dir))
    return Workspace(base / 'baseline', base / 'tree', tuple(manifest), base_commit,
                     tuple(dict.fromkeys((*EDIT_DENY, *extra_deny))))


def materialize(snapshot, job_dir):
    workspace = load(job_dir, snapshot.manifest, snapshot.base_commit, snapshot.extra_deny)
    _write_tree(workspace.baseline, snapshot._files, readonly=True)
    _write_tree(workspace.tree, snapshot._files)
    return workspace


def _capture_tree(base, extra):
    files = {}
    total = count = 0

    def walk(parent, prefix, depth):
        nonlocal total, count
        if depth > 64:
            raise Error('workspace exceeds the maximum directory depth')
        with os.scandir(parent) as entries:
            seen = {}
            for entry in entries:
                count += 1
                if count > stage._WALK_LIMIT:
                    raise Error('workspace has too many directory entries')
                rel = '/'.join((*prefix, entry.name))
                record_path(rel, extra, seen)
                if entry.is_dir(follow_symlinks=False):
                    child = os.open(entry.name, DIRECTORY_FLAGS, dir_fd=parent)
                    try:
                        walk(child, (*prefix, entry.name), depth + 1)
                    finally:
                        os.close(child)
                    continue
                value = read_workspace_file(parent, entry.name, rel)
                refusal = stage._check_text(rel, value.data)
                if refusal:
                    raise Error(stage.describe(refusal[0]))
                total += len(value.data)
                if total > MAX_TREE_BYTES or len(files) >= config.MAX_FILES:
                    raise Error('workspace exceeds the file count or total size limit')
                files[rel] = value

    try:
        with safety.directory(os.fspath(base)) as fd:
            walk(fd, (), 1)
    except OSError as exc:
        raise Error('workspace tree could not be read safely') from exc
    return files


def validate_candidate(workspace):
    """Check paths and bytes before AGY opens a previous run's workspace."""
    _capture_tree(workspace.tree, workspace.extra_deny)


def copy_candidate(workspace, dest, changes=None):
    """Write independent editable copies for checks, optionally from the exact exported bytes."""
    dest = Path(os.path.abspath(dest))
    files = changes._files if changes is not None else _capture_tree(workspace.tree, workspace.extra_deny)
    _write_tree(dest, files)
    return dest


def git_tree(scratch, label, files, env):
    indexed_env = dict(env, GIT_INDEX_FILE=os.fspath(scratch / (label + '.index')))
    if files:
        _write_tree(scratch / label, files, readonly=True)
        names = sorted(files)
        paths = ''.join(label + '/' + name + '\n' for name in names).encode('utf-8')
        hashes = run_git(['hash-object', '-w', '--no-filters', '--stdin-paths'],
                      scratch, indexed_env, paths).stdout.splitlines()
        if len(hashes) != len(names) or any(not re.fullmatch(rb'[0-9a-f]{40}|[0-9a-f]{64}', h) for h in hashes):
            raise Error('Git returned an invalid workspace object list')
        records = bytearray()
        for name, object_id in zip(names, hashes):
            mode = b'100755' if files[name].executable else b'100644'
            records.extend(mode + b' ' + object_id + b'\t' + name.encode('utf-8') + b'\0')
        run_git(['update-index', '-z', '--index-info'], scratch, indexed_env, bytes(records))
    tree = run_git(['write-tree'], scratch, indexed_env).stdout.strip()
    if not re.fullmatch(rb'[0-9a-f]{40}|[0-9a-f]{64}', tree):
        raise Error('Git returned an invalid workspace tree ID')
    return tree.decode('ascii')


def change_inventory(before, after):
    changes = {}
    for rel in sorted(before.keys() | after.keys()):
        if rel not in before:
            changes[rel] = 'added'
        elif rel not in after:
            changes[rel] = 'deleted'
        elif before[rel] != after[rel]:
            changes[rel] = 'modified'
    return changes


def export_diff(workspace):
    baseline = _capture_tree(workspace.baseline, workspace.extra_deny)
    manifest = tuple(value.manifest(name) for name, value in sorted(baseline.items()))
    if manifest != workspace.manifest:
        raise Error('workspace baseline no longer matches the approved snapshot')
    candidate = _capture_tree(workspace.tree, workspace.extra_deny)
    paths = tuple((kind, rel) for rel, kind in change_inventory(baseline, candidate).items())
    if not paths:
        return Changes((), b'', candidate)

    with tempfile.TemporaryDirectory(prefix='agy-edit-diff-', dir=workspace.baseline.parent) as temporary:
        scratch = Path(temporary)
        env = git_env(scratch)
        run_git(['init', '--quiet', '--template='], scratch, env)
        old = git_tree(scratch, 'before', baseline, env)
        new = git_tree(scratch, 'after', candidate, env)
        patch = run_git(['diff-tree', '-r', '-p', '--no-color', '--no-commit-id', '--no-ext-diff', '--no-textconv',
                      '--no-renames', '--no-relative', '--full-index', '--src-prefix=a/', '--dst-prefix=b/', old, new],
                     scratch, env).stdout
    if not patch or len(patch) > MAX_DIFF_BYTES:
        raise Error('workspace diff is missing or exceeds its size limit')
    return Changes(paths, patch, candidate)
