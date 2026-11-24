"""Verify a reviewed patch and publish its exact bytes to the source tree."""
from __future__ import annotations

import contextlib
import fcntl
import os
import re
import secrets
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import config, safety, stage, workspace


class ApplyError(ValueError):
    pass


@dataclass
class _Update:
    rel: str
    kind: str
    parent: int
    name: str
    backup: str | None = None
    output: str | None = None


def _inventory(manifest, changes, extra):
    snapshot = {}
    normalized = {}
    for entry in manifest:
        rel = workspace.record_path(entry['path'], extra, normalized)
        if (rel in snapshot or type(entry['bytes']) is not int or
                not 0 <= entry['bytes'] <= workspace.MAX_FILE_BYTES or
                not isinstance(entry['sha256'], str) or
                not re.fullmatch(r'[0-9a-f]{64}', entry['sha256']) or
                type(entry['executable']) is not bool):
            raise ApplyError('invalid approved file manifest')
        snapshot[rel] = entry

    expected = {}
    for kind, rel in changes:
        workspace.record_path(rel, extra, normalized)
        if (rel in expected or kind not in ('added', 'modified', 'deleted') or
                (rel in snapshot) == (kind == 'added')):
            raise ApplyError('invalid reviewed change inventory')
        expected[rel] = kind
    if not expected or len(expected) > 2 * config.MAX_FILES:
        raise ApplyError('invalid reviewed change count')
    return snapshot, expected


@contextlib.contextmanager
def _parent(root_fd, rel, created=None):
    fd = os.dup(root_fd)
    opened = []
    try:
        for part in rel.split('/')[:-1]:
            opened.append(part)
            if created is not None:
                try:
                    os.mkdir(part, 0o755, dir_fd=fd)
                except FileExistsError:
                    pass
                else:
                    created.append('/'.join(opened))
            child = os.open(part, workspace.DIRECTORY_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def _preimage(root_fd, rel, approved):
    name = rel.rsplit('/', 1)[-1]
    try:
        with _parent(root_fd, rel) as fd:
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if approved is None:
                raise ApplyError('added path already exists: ' + stage.printable(rel))
            file = workspace.read_workspace_file(fd, name, rel)
    except FileNotFoundError as exc:
        if approved is None:
            return None
        raise ApplyError('approved file no longer exists: ' + stage.printable(rel)) from exc
    if file.manifest(rel) != approved:
        raise ApplyError('approved file changed: ' + stage.printable(rel))
    return file, stat.S_IMODE(info.st_mode) & 0o777


def _blobs(scratch, env, entries):
    if not entries:
        return {}
    request = b''.join(oid + b'\n' for _, oid in entries.values())
    sizes = workspace.run_git(['cat-file', '--batch-check=%(objectsize)'], scratch, env, request).stdout.splitlines()
    if len(sizes) != len(entries) or any(not value.isdigit() for value in sizes):
        raise ApplyError('Git returned invalid blob sizes')
    lengths = [int(value) for value in sizes]
    if any(n > workspace.MAX_FILE_BYTES for n in lengths) or sum(lengths) > workspace.MAX_TREE_BYTES:
        raise ApplyError('patch output exceeds the approved workspace limits')

    data = workspace.run_git(['cat-file', '--batch'], scratch, env, request).stdout
    files = {}
    cursor = 0
    for (rel, (executable, oid)), length in zip(entries.items(), lengths):
        header = oid + b' blob ' + str(length).encode('ascii') + b'\n'
        if not data.startswith(header, cursor):
            raise ApplyError('Git returned an unexpected blob')
        start = cursor + len(header)
        end = start + length
        if data[end:end + 1] != b'\n':
            raise ApplyError('Git returned a truncated blob')
        refusal = stage._check_text(rel, data[start:end])
        if refusal:
            raise ApplyError(stage.describe(refusal[0]))
        files[rel] = workspace.WorkspaceFile(data[start:end], executable)
        cursor = end + 1
    if cursor != len(data):
        raise ApplyError('Git returned unexpected data after the patch blobs')
    return files


def _review_patch(scratch, env, before, expected, patch):
    workspace.run_git(['init', '-q', '--template='], scratch, env)
    workspace.git_tree(scratch, 'approved', before, env)
    index_env = dict(env, GIT_INDEX_FILE=os.fspath(scratch / 'approved.index'))
    args = ['apply', '--cached', '-p1', '--whitespace=nowarn']
    workspace.run_git([*args, '--check'], scratch, index_env, patch)
    workspace.run_git(args, scratch, index_env, patch)
    next_tree = workspace.run_git(['write-tree'], scratch, index_env).stdout.strip().decode('ascii')
    listing = workspace.run_git(['ls-tree', '-rz', next_tree], scratch, index_env).stdout
    entries = {}
    for row in listing.split(b'\0')[:-1]:
        metadata, _, encoded_name = row.partition(b'\t')
        mode, type_, oid = metadata.split()
        rel = encoded_name.decode('utf-8')
        if (rel not in expected or type_ != b'blob' or mode not in (b'100644', b'100755') or
                not re.fullmatch(rb'[0-9a-f]{40}|[0-9a-f]{64}', oid)):
            raise ApplyError('patch attempts to update an unapproved path or file type')
        entries[rel] = (mode == b'100755', oid)
    after = _blobs(scratch, env, entries)
    if workspace.change_inventory(before, after) != expected:
        raise ApplyError('patch does not match the reviewed change inventory')
    return after


def _stage_file(parent, data, mode):
    name = '.agy-apply-' + secrets.token_hex(16)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    fd = os.open(name, flags, 0o600, dir_fd=parent)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(data)
            handle.flush()
            os.fchmod(handle.fileno(), mode)
            os.fsync(handle.fileno())
    except BaseException:
        os.unlink(name, dir_fd=parent)
        raise
    return name


def _publish(root_fd, snapshot, expected, originals, after):
    created = []
    updates = []
    published = []
    preserve = set()
    try:
        with contextlib.ExitStack() as parents:
            try:
                directories = {}
                for rel, kind in expected.items():
                    folder, _, name = rel.rpartition('/')
                    if folder not in directories:
                        directories[folder] = parents.enter_context(
                            _parent(root_fd, rel, created if kind == 'added' else None))
                    update = _Update(rel, kind, directories[folder], name)
                    updates.append(update)
                    if kind == 'added':
                        mode = 0o644
                    else:
                        original, mode = originals[rel]
                        update.backup = _stage_file(update.parent, original.data, mode)
                    if kind != 'deleted':
                        result = after[rel]
                        new_mode = mode | stat.S_IXUSR if result.executable else mode & ~0o111
                        update.output = _stage_file(update.parent, result.data, new_mode)

                for rel in expected:
                    if _preimage(root_fd, rel, snapshot.get(rel)) != originals[rel]:
                        raise ApplyError('approved file changed during apply: ' + stage.printable(rel))

                for update in updates:
                    rel = update.rel
                    with _parent(root_fd, rel) as current:
                        if (not os.path.samestat(os.fstat(current), os.fstat(update.parent))
                                or _preimage(root_fd, rel, snapshot.get(rel)) != originals[rel]):
                            raise ApplyError('approved file changed during apply: ' + stage.printable(rel))
                    if update.kind == 'added':
                        os.link(update.output, update.name, src_dir_fd=update.parent,
                                dst_dir_fd=update.parent, follow_symlinks=False)
                    elif update.kind == 'deleted':
                        os.unlink(update.name, dir_fd=update.parent)
                    else:
                        os.replace(update.output, update.name, src_dir_fd=update.parent, dst_dir_fd=update.parent)
                    published.append(update)
            except BaseException:
                for update in reversed(published):
                    try:
                        if update.kind == 'added':
                            os.unlink(update.name, dir_fd=update.parent)
                        elif update.kind == 'deleted':
                            os.link(update.backup, update.name, src_dir_fd=update.parent,
                                    dst_dir_fd=update.parent, follow_symlinks=False)
                        else:
                            os.replace(update.backup, update.name, src_dir_fd=update.parent, dst_dir_fd=update.parent)
                    except OSError:
                        preserve.add(update.rel)
                if preserve:
                    retained = [os.path.join(os.path.dirname(update.rel), update.backup)
                                for update in updates if update.backup and update.rel in preserve]
                    errors = 'apply and rollback failed for: ' + ', '.join(sorted(preserve))
                    if retained:
                        errors += '. Retained backups: ' + ', '.join(retained)
                    raise ApplyError(errors)
                raise
            finally:
                for update in updates:
                    backup = None if update.rel in preserve else update.backup
                    for temporary in (backup, update.output):
                        if temporary is not None:
                            with contextlib.suppress(FileNotFoundError):
                                os.unlink(temporary, dir_fd=update.parent)
    except BaseException:
        for rel in reversed(created):
            with contextlib.suppress(OSError), _parent(root_fd, rel) as parent:
                os.rmdir(rel.rsplit('/', 1)[-1], dir_fd=parent)
        raise


def apply(root, base_commit, manifest, changes, patch):
    """Publish only reviewed changes whose source preimages still match."""
    if not isinstance(patch, bytes) or not patch or len(patch) > workspace.MAX_DIFF_BYTES:
        raise ApplyError('invalid reviewed patch size')
    if b'\nGIT binary patch\n' in patch:
        raise ApplyError('binary patches are not supported in implementation workspaces')
    root = os.path.abspath(root)
    try:
        stage.check_root(root)
        with safety.directory(root) as root_fd:
            try:
                fcntl.flock(root_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ApplyError('another bridge apply is already in progress') from exc
            extra = (*workspace.EDIT_DENY, *config.load_extra_deny(root))
            snapshot, expected = _inventory(manifest, changes, extra)
            if workspace.source_commit(root) != base_commit:
                raise ApplyError('source Git HEAD changed after the review')
            originals = {rel: _preimage(root_fd, rel, snapshot.get(rel)) for rel in expected}
            before = {rel: item[0] for rel, item in originals.items() if item is not None}
            with tempfile.TemporaryDirectory(prefix='agy-approved-patch-') as directory:
                scratch = Path(directory)
                after = _review_patch(scratch, workspace.git_env(scratch), before, expected, patch)
            if workspace.source_commit(root) != base_commit:
                raise ApplyError('source Git HEAD changed during apply')
            _publish(root_fd, snapshot, expected, originals, after)
    except ApplyError:
        raise
    except stage.Refused as exc:
        raise ApplyError(stage.describe(exc.refusals[0])) from exc
    except config.UsageError as exc:
        raise ApplyError(str(exc)) from exc
    except (KeyError, OSError, TypeError, ValueError) as exc:
        raise ApplyError('could not safely verify and publish the reviewed patch') from exc
