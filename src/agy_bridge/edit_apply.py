"""Verify a reviewed patch and publish its exact bytes to the source tree."""
from __future__ import annotations

import base64
import binascii
import contextlib
import fcntl
import hashlib
import json
import os
import re
import secrets
import signal
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import config, jobstore, safety, stage, workspace


INTENT_NAME = 'apply-intent.json'
MAX_INTENT_BYTES = 24 * 1024 * 1024
_STOP_SIGNALS = {signal.SIGINT, signal.SIGTERM, signal.SIGHUP}


class ApplyError(ValueError):
    pass


@dataclass(frozen=True)
class _Image:
    file: workspace.WorkspaceFile
    mode: int


@dataclass(frozen=True)
class _Intent:
    token: str
    before: dict[str, _Image | None]
    after: dict[str, _Image | None]


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
def _parent(root_fd, rel, created=None, *, sync_ancestors=False):
    fd = os.dup(root_fd)
    opened = []
    try:
        for part in rel.split('/')[:-1]:
            opened.append(part)
            if sync_ancestors:
                os.fsync(fd)
            if created is not None:
                try:
                    os.mkdir(part, 0o755, dir_fd=fd)
                except FileExistsError:
                    pass
                else:
                    created.append('/'.join(opened))
                    os.fsync(fd)
            child = os.open(part, workspace.DIRECTORY_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def _read_at(parent, name, rel):
    try:
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return None
    file = workspace.read_workspace_file(parent, name, rel)
    checked = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if (not os.path.samestat(info, checked) or (info.st_mode, info.st_ctime_ns) != (checked.st_mode, checked.st_ctime_ns)
            or stat.S_IMODE(checked.st_mode) & ~0o777):
        raise ApplyError('approved file mode or identity could not be verified: ' + stage.printable(rel))
    return _Image(file, stat.S_IMODE(checked.st_mode))


def _current(root_fd, rel):
    try:
        with _parent(root_fd, rel) as parent:
            return _read_at(parent, rel.rsplit('/', 1)[-1], rel)
    except FileNotFoundError:
        return None


def _preimage(root_fd, rel, approved):
    image = _current(root_fd, rel)
    if approved is None:
        if image is not None:
            raise ApplyError('added path already exists: ' + stage.printable(rel))
    elif image is None:
        raise ApplyError('approved file no longer exists: ' + stage.printable(rel))
    elif image.file.manifest(rel) != approved:
        raise ApplyError('approved file changed: ' + stage.printable(rel))
    return image


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
    workspace.check_new_text(before, after, tuple((kind, rel) for rel, kind in expected.items()), ApplyError)
    return after


def _after_images(before, reviewed):
    result = {}
    for path, original in before.items():
        file = reviewed.get(path)
        mode = original.mode if original is not None else 0o644
        if file is None:
            result[path] = None
        else:
            mode = mode | stat.S_IXUSR if file.executable else mode & ~0o111
            result[path] = _Image(file, mode)
    return result


def _record_image(path, image, include_data=False):
    if image is None:
        return None
    entry = image.file.manifest(path)
    entry['mode'] = image.mode
    if include_data:
        entry['data'] = base64.b64encode(image.file.data).decode('ascii')
    return entry


def _record_intent(binding, intent):
    return dict(binding, token=intent.token, files={
        path: {'before': _record_image(path, intent.before[path], include_data=True),
               'after': _record_image(path, intent.after[path])}
        for path in intent.before
    })


def _intent_record(transaction_dir):
    if transaction_dir is None:
        return None
    with safety.directory(os.fspath(transaction_dir)) as parent:
        info = os.fstat(parent)
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ApplyError('apply intent requires a private job directory')
        try:
            fd = os.open(INTENT_NAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        except FileNotFoundError:
            return None
        with os.fdopen(fd, 'rb') as handle:
            safety.regular(handle.fileno())
            data = handle.read(MAX_INTENT_BYTES + 1)
    if len(data) > MAX_INTENT_BYTES:
        raise ApplyError('stored apply intent exceeds the size limit')
    try:
        record = json.loads(data)
    except (ValueError, RecursionError) as exc:
        raise ApplyError('stored apply intent is not valid JSON') from exc
    if not isinstance(record, dict):
        raise ApplyError('stored apply intent is not an object')
    return record


def _restore_preimages(record, binding, snapshot, expected):
    if (not isinstance(record, dict) or type(record.get('schema_version')) is not int
            or any(record.get(key) != value for key, value in binding.items())
            or not isinstance(record.get('token'), str) or not re.fullmatch(r'[0-9a-f]{32}', record['token'])
            or not isinstance(record.get('files'), dict) or set(record['files']) != set(expected)):
        raise ApplyError('stored apply intent does not match this reviewed patch and repository')
    before = {}
    total = 0
    for path, entry in record['files'].items():
        if not isinstance(entry, dict) or set(entry) != {'before', 'after'}:
            raise ApplyError('stored apply intent is missing a file image')
        saved = entry['before']
        approved = snapshot.get(path)
        if approved is None and saved is None:
            before[path] = None
            continue
        if (approved is None or not isinstance(saved, dict) or type(saved.get('mode')) is not int
                or not 0 <= saved['mode'] <= 0o777 or not isinstance(saved.get('data'), str)
                or len(saved['data']) > ((workspace.MAX_FILE_BYTES + 2) // 3) * 4):
            raise ApplyError('stored apply intent has an invalid preimage')
        try:
            data = base64.b64decode(saved['data'], validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ApplyError('stored apply intent has an invalid preimage') from exc
        file = workspace.WorkspaceFile(data, bool(saved['mode'] & stat.S_IXUSR))
        before[path] = _Image(file, saved['mode'])
        total += len(data)
        if (total > workspace.MAX_TREE_BYTES or file.manifest(path) != approved
                or _record_image(path, before[path], include_data=True) != saved):
            raise ApplyError('stored apply intent has a preimage that differs from the approved snapshot')
    return before


def _stage_file(parent, name, image):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    fd = os.open(name, flags, 0o600, dir_fd=parent)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(image.file.data)
            handle.flush()
            os.fchmod(handle.fileno(), image.mode)
            os.fsync(handle.fileno())
    except BaseException:
        os.unlink(name, dir_fd=parent)
        raise


@contextlib.contextmanager
def _defer_stop_signals():
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, _STOP_SIGNALS)
    try:
        yield
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)


def _stage_name(token, index):
    return f'.agy-apply-{token}-{index}'


def _discard_stage(parent, stage_name, destination, rel):
    """Clean this transaction's staged inode, including an interrupted no-clobber link."""
    try:
        info = os.stat(stage_name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink not in (1, 2):
        raise ApplyError('unexpected staging file during apply recovery: ' + stage.printable(rel))
    if info.st_nlink == 2:
        try:
            target = os.stat(destination, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError as exc:
            raise ApplyError('unexpected staging link during apply recovery: ' + stage.printable(rel)) from exc
        if not os.path.samestat(info, target):
            raise ApplyError('unexpected staging link during apply recovery: ' + stage.printable(rel))
    os.unlink(stage_name, dir_fd=parent)
    os.fsync(parent)


def _set_image(root_fd, parent, rel, temporary, before, after):
    name = rel.rsplit('/', 1)[-1]
    if after is not None:
        _stage_file(parent, temporary, after)
    # A slow staging write gives another editor time to change the original.
    with _parent(root_fd, rel) as current:
        if (not os.path.samestat(os.fstat(parent), os.fstat(current))
                or _read_at(current, name, rel) != before):
            raise ApplyError('approved file changed during apply: ' + stage.printable(rel))
    if after is None:
        os.unlink(name, dir_fd=parent)
    else:
        if before is None:
            os.link(temporary, name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
            os.unlink(temporary, dir_fd=parent)
        else:
            os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
    os.fsync(parent)


def _remaining(root_fd, intent, recovering):
    pending = []
    for index, rel in enumerate(sorted(intent.before)):
        temporary = _stage_name(intent.token, index)
        if recovering:
            try:
                with _parent(root_fd, rel, sync_ancestors=True) as parent:
                    _discard_stage(parent, temporary, rel.rsplit('/', 1)[-1], rel)
                    # The previous process can exit between publication and its directory sync.
                    os.fsync(parent)
            except FileNotFoundError:
                pass
        current = _current(root_fd, rel)
        if current == intent.before[rel]:
            pending.append((rel, temporary))
        elif not recovering or current != intent.after[rel]:
            raise ApplyError('file differs from both the approved preimage and recovery image: ' + stage.printable(rel))
    return pending


def _rollback(root_fd, attempted, intent):
    failed = []
    for rel, temporary in reversed(attempted):
        name = rel.rsplit('/', 1)[-1]
        try:
            with _parent(root_fd, rel) as parent:
                _discard_stage(parent, temporary, name, rel)
                current = _read_at(parent, name, rel)
                if current == intent.before[rel]:
                    continue
                if current != intent.after[rel]:
                    raise ApplyError('destination changed before rollback')
                _set_image(root_fd, parent, rel, temporary, current, intent.before[rel])
        except (OSError, ApplyError, config.UsageError):
            failed.append(rel)
    return failed


def _publish(root_fd, intent, pending):
    created, attempted = [], []
    with _defer_stop_signals():
        try:
            for rel, temporary in pending:
                before = intent.before[rel]
                with _parent(root_fd, rel, created if before is None else None) as parent:
                    # Record the attempted path before a mutating syscall. Rollback checks which image survived.
                    attempted.append((rel, temporary))
                    _set_image(root_fd, parent, rel, temporary, before, intent.after[rel])
            for rel, image in intent.after.items():
                if _current(root_fd, rel) != image:
                    raise ApplyError('reviewed file changed before apply completed: ' + stage.printable(rel))
        except BaseException:
            failed = _rollback(root_fd, attempted, intent)
            for rel in reversed(created):
                with contextlib.suppress(OSError), _parent(root_fd, rel) as parent:
                    os.rmdir(rel.rsplit('/', 1)[-1], dir_fd=parent)
                    os.fsync(parent)
            if failed:
                raise ApplyError('apply and rollback could not finish for: ' + ', '.join(stage.printable(path) for path in failed))
            raise


def apply(root, base_commit, manifest, changes, patch, *, transaction_dir=None):
    """Publish the reviewed patch, or finish its interrupted publication in the same job."""
    if not isinstance(patch, bytes) or not patch or len(patch) > workspace.MAX_DIFF_BYTES:
        raise ApplyError('invalid reviewed patch size')
    if b'\nGIT binary patch\n' in patch:
        raise ApplyError('binary patches are not supported in implementation workspaces')
    root = os.path.abspath(root)
    if transaction_dir is not None:
        transaction_dir = os.path.abspath(transaction_dir)
        if os.path.commonpath((root, transaction_dir)) in (root, transaction_dir):
            raise ApplyError('the private apply intent must be outside the source repository')
    try:
        stage.check_root(root)
        with safety.directory(root) as root_fd:
            try:
                fcntl.flock(root_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ApplyError('another bridge apply is already in progress') from exc
            info = os.fstat(root_fd)
            binding = {'schema_version': 1, 'repository': root, 'base_commit': base_commit,
                       'root_device': info.st_dev, 'root_inode': info.st_ino,
                       'patch_sha256': hashlib.sha256(patch).hexdigest()}
            extra = (*workspace.EDIT_DENY, *config.load_extra_deny(root))
            snapshot, expected = _inventory(manifest, changes, extra)
            if workspace.source_commit(root) != base_commit:
                raise ApplyError('source Git HEAD changed after the review')
            stored = _intent_record(transaction_dir)
            if stored is None:
                before = {rel: _preimage(root_fd, rel, snapshot.get(rel)) for rel in expected}
                token = secrets.token_hex(16)
            else:
                before = _restore_preimages(stored, binding, snapshot, expected)
                token = stored['token']
            original_files = {rel: image.file for rel, image in before.items() if image is not None}
            # On macOS the trusted system temp path can start at the /var symlink.
            # Canonicalize only its scratch parent, never the source repository path.
            temp_parent = os.path.realpath(tempfile.gettempdir())
            with tempfile.TemporaryDirectory(prefix='agy-approved-patch-', dir=temp_parent) as directory:
                scratch = Path(directory)
                reviewed = _review_patch(scratch, workspace.git_env(scratch), original_files, expected, patch)
            intent = _Intent(token, before, _after_images(before, reviewed))
            record = _record_intent(binding, intent)
            if stored is not None and record != stored:
                raise ApplyError('stored recovery images differ from the reviewed patch')
            if workspace.source_commit(root) != base_commit:
                raise ApplyError('source Git HEAD changed during apply')
            pending = _remaining(root_fd, intent, recovering=stored is not None)
            if stored is not None:
                # The private intent may have been renamed immediately before interruption.
                with safety.directory(transaction_dir) as parent:
                    os.fsync(parent)
            elif transaction_dir is not None:
                data = json.dumps(record, ensure_ascii=True, sort_keys=True).encode('ascii') + b'\n'
                if len(data) > MAX_INTENT_BYTES:
                    raise ApplyError('apply intent exceeds the size limit')
                jobstore.write(Path(transaction_dir), INTENT_NAME, data)
            if workspace.source_commit(root) != base_commit:
                raise ApplyError('source Git HEAD changed during apply')
            _publish(root_fd, intent, pending)
    except ApplyError:
        raise
    except stage.Refused as exc:
        raise ApplyError(stage.describe(exc.refusals[0])) from exc
    except config.UsageError as exc:
        raise ApplyError(str(exc)) from exc
    except (KeyError, OSError, TypeError, ValueError) as exc:
        raise ApplyError('could not safely verify and publish the reviewed patch') from exc
