"""Cooperative POSIX execution domains, fenced with cross-process flock + epochs.

Only SDK file operations use this domain. Arbitrary host code and subprocesses
are not sandboxed by it; script execution requires a separate SandboxGateway.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

from ..configuration import digest
from ..errors import AgentLoomError, LeaseLost
from ..workspace.paths import path_parts as _parts
from .limits import DEFAULT_FILE_READ_BYTES, MAX_FILE_BYTES, MAX_FILE_READ_BYTES
from .mutations import FileMutations


def _key(value):
    return hashlib.sha256(str(value).encode()).hexdigest()


def _directory(path, *, create=False):
    """Walk with dirfd and O_NOFOLLOW, including every ancestor of the root."""
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in Path(path).absolute().parts[1:]:
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nxt
        return fd
    except BaseException:
        os.close(fd)
        raise


class LocalEnvironment(FileMutations):
    """A single-machine file backend. No NFS fencing or OS process sandbox claims."""

    def prepare(
        self, binding, session_id, run_id, epoch, namespace="default", *, guard=lambda: None
    ):
        guard()
        base = binding.root.resolve() / _key(namespace) / _key(session_id or run_id)
        path = base / ("persistent" if binding.persistent_sandbox else "runs/" + _key(run_id))
        fd = _directory(base / ".agentloom", create=True)
        os.close(fd)
        scope = {
            "path": str(path),
            "base": str(base),
            "persistent": binding.persistent_sandbox,
            "run_id": str(run_id),
            "epoch": epoch,
            "namespace": namespace,
            "lock": str(
                base
                / ".agentloom"
                / ("persistent.lock" if binding.persistent_sandbox else _key(run_id) + ".lock")
            ),
        }
        with self._lock(scope):
            guard()
            try:
                with open(scope["lock"] + ".epoch") as fp:
                    previous = json.load(fp)
                if previous["run_id"] == str(run_id) and (
                    previous["epoch"] > epoch
                    or previous["epoch"] == epoch
                    and not previous["active"]
                ):
                    raise LeaseLost()
            except FileNotFoundError:
                pass
            fd = _directory(path, create=True)
            os.close(fd)
            self._write_epoch(scope, {"run_id": str(run_id), "epoch": epoch, "active": True})
        return scope

    @contextmanager
    def _lock(self, scope):
        fd = os.open(scope["lock"], os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _write_epoch(self, scope, value):
        target = Path(scope["lock"] + ".epoch")
        fd, name = tempfile.mkstemp(dir=target.parent)
        try:
            with os.fdopen(fd, "w") as fp:
                json.dump(value, fp)
                fp.flush()
                os.fsync(fp.fileno())
            os.replace(name, target)
            dfd = _directory(target.parent)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def _check(self, scope, guard):
        guard()
        with open(scope["lock"] + ".epoch") as fp:
            epoch = json.load(fp)
        if epoch != {"run_id": scope["run_id"], "epoch": scope["epoch"], "active": True}:
            raise LeaseLost()

    def revoke(self, scope):
        with self._lock(scope):
            with open(scope["lock"] + ".epoch") as fp:
                value = json.load(fp)
            if value["run_id"] == scope["run_id"] and value["epoch"] == scope["epoch"]:
                self._write_epoch(scope, {**value, "active": False})

    @contextmanager
    def _parent(self, scope, path, *, parents=False):
        parts = _parts(path)
        if not parts:
            raise AgentLoomError("WORKSPACE_ROOT_PROTECTED", category="permission")
        fd = _directory(scope["path"])
        try:
            for part in parts[:-1]:
                if parents:
                    try:
                        os.mkdir(part, mode=0o700, dir_fd=fd)
                    except FileExistsError:
                        pass
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
            try:
                if stat.S_ISLNK(os.stat(parts[-1], dir_fd=fd, follow_symlinks=False).st_mode):
                    raise AgentLoomError("SYMLINK_FORBIDDEN", category="permission")
            except FileNotFoundError:
                pass
            yield fd, parts[-1]
        except OSError as exc:
            if exc.errno in (20, 40):
                raise AgentLoomError("SYMLINK_FORBIDDEN", category="permission") from exc
            raise
        finally:
            os.close(fd)

    def resolve(self, scope, path):
        # Diagnostic path resolution. Mutations still re-open using dirfd.
        parts = _parts(path)
        target = Path(scope["path"])
        for part in parts:
            target = target / part
            if target.is_symlink():
                raise AgentLoomError("SYMLINK_FORBIDDEN", category="permission")
        return target

    @staticmethod
    def _read_bytes(fd, name):
        file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        with os.fdopen(file_fd, "rb") as fp:
            before = os.fstat(fp.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise AgentLoomError("FILE_TYPE_FORBIDDEN")
            data = fp.read(MAX_FILE_BYTES + 1)
            if len(data) > MAX_FILE_BYTES:
                raise AgentLoomError("FILE_TOO_LARGE")
            after = os.fstat(fp.fileno())

            def stamp(s):
                return (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)

            if stamp(before) != stamp(after) or len(data) != after.st_size:
                raise AgentLoomError("FILE_CHANGED", category="conflict")
            return data

    @staticmethod
    def _version(path, data):
        sha = hashlib.sha256(data).hexdigest()
        return {
            "path": path,
            "kind": "file",
            "size": len(data),
            "sha256": sha,
            "etag": digest({"kind": "file", "size": len(data), "sha256": sha}),
        }

    def read(self, scope, path, guard=lambda: None, *, offset=0, limit=DEFAULT_FILE_READ_BYTES):
        if (
            type(offset) is not int
            or offset < 0
            or type(limit) is not int
            or not 1 <= limit <= MAX_FILE_READ_BYTES
        ):
            raise AgentLoomError("FILE_SLICE_INVALID")
        with self._lock(scope):
            self._check(scope, guard)
            with self._parent(scope, path) as (fd, name):
                data = self._read_bytes(fd, name)
            if offset > len(data):
                raise AgentLoomError("FILE_SLICE_INVALID")
            try:
                if b"\0" in data:
                    raise UnicodeError()
                data.decode("utf-8")
                data[:offset].decode("utf-8")
            except UnicodeError as exc:
                raise AgentLoomError("BINARY_FILE_OR_INVALID_OFFSET") from exc
            end = min(len(data), offset + limit)
            while end > offset:
                try:
                    text = data[offset:end].decode("utf-8")
                    break
                except UnicodeDecodeError:
                    end -= 1
            else:
                text = ""
            return {
                **self._version(path, data),
                "text": text,
                "offset": offset,
                "end_offset": end,
                "next_offset": end if end < len(data) else None,
                "total_bytes": len(data),
            }

    def write(self, scope, path, content, guard=lambda: None, *, mode="create_only", if_match=None):
        if mode not in ("create_only", "replace") or (mode == "replace") != (if_match is not None):
            raise AgentLoomError("INVALID_WRITE_MODE")
        if not isinstance(content, str) or len(content.encode()) > MAX_FILE_BYTES:
            raise AgentLoomError("FILE_TOO_LARGE")
        data = content.encode()
        with self._lock(scope):
            self._check(scope, guard)
            with self._parent(scope, path) as (fd, name):
                try:
                    previous = self._read_bytes(fd, name)
                except FileNotFoundError:
                    previous = None
                if mode == "create_only" and previous is not None:
                    raise AgentLoomError("FILE_EXISTS", category="conflict")
                if mode == "replace" and (
                    previous is None or self._version(path, previous)["etag"] != if_match
                ):
                    raise AgentLoomError("FILE_CHANGED", category="conflict")
                staging = ".agentloom-write-" + os.urandom(12).hex()
                temp_fd = os.open(
                    staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd
                )
                try:
                    with os.fdopen(temp_fd, "wb") as fp:
                        fp.write(data)
                        fp.flush()
                        os.fsync(fp.fileno())
                    self._check(scope, guard)
                    if mode == "create_only":
                        try:
                            os.link(
                                staging, name, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False
                            )
                        except FileExistsError as exc:
                            raise AgentLoomError("FILE_EXISTS", category="conflict") from exc
                    else:
                        os.replace(staging, name, src_dir_fd=fd, dst_dir_fd=fd)
                    os.fsync(fd)
                finally:
                    try:
                        os.unlink(staging, dir_fd=fd)
                    except FileNotFoundError:
                        pass
        return self._version(path, data)

    def list(self, scope, path=".", guard=lambda: None, *, versions=False):
        with self._lock(scope):
            self._check(scope, guard)
            target = self.resolve(scope, path)
            fd = _directory(target)
            try:
                result = []
                for name in sorted(os.listdir(fd), key=lambda s: s.encode()):
                    info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                    if name.startswith(".agentloom") or not (
                        stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)
                    ):
                        continue
                    relative = str(PurePosixPath(path) / name)
                    result.append(
                        {
                            "name": name,
                            "path": relative,
                            "kind": "directory" if stat.S_ISDIR(info.st_mode) else "file",
                            "size": info.st_size,
                            **(self._entry_version(fd, name, relative) if versions else {}),
                        }
                    )
                return result
            finally:
                os.close(fd)

    def list_page(self, scope, path=".", guard=lambda: None, *, cursor=None, limit=100):
        import base64

        if type(limit) is not int or not 1 <= limit <= 1000:
            raise AgentLoomError("INVALID_CURSOR")
        entries = self.list(scope, path, guard, versions=True)
        version = digest([[e["name"], e["kind"], e["etag"]] for e in entries])
        offset = 0
        if cursor is not None:
            try:
                state = json.loads(base64.urlsafe_b64decode(cursor.encode()))
                if (
                    set(state) != {"version", "path", "offset"}
                    or state["path"] != path
                    or type(state["offset"]) is not int
                    or state["offset"] < 0
                ):
                    raise ValueError()
                if state["version"] != version:
                    raise AgentLoomError("DIRECTORY_CHANGED", category="conflict")
                if state["offset"] > len(entries):
                    raise ValueError()
                offset = state["offset"]
            except (ValueError, TypeError, AttributeError) as exc:
                raise AgentLoomError("INVALID_CURSOR") from exc
        end = min(len(entries), offset + limit)
        next_cursor = (
            base64.urlsafe_b64encode(
                json.dumps({"version": version, "path": path, "offset": end}).encode()
            ).decode()
            if end < len(entries)
            else None
        )
        return {"entries": entries[offset:end], "etag": version, "next_cursor": next_cursor}

    def mkdir(self, scope, path, guard=lambda: None, *, parents=False):
        with self._lock(scope):
            self._check(scope, guard)
            with self._parent(scope, path, parents=parents) as (fd, name):
                try:
                    os.mkdir(name, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    if not stat.S_ISDIR(os.stat(name, dir_fd=fd, follow_symlinks=False).st_mode):
                        raise AgentLoomError("FILE_EXISTS")
                os.fsync(fd)
        return {"path": path, "kind": "directory"}

    def grep(self, scope, pattern, path=".", guard=lambda: None, *, limit=1000, literal=True):
        import heapq

        if not isinstance(pattern, str) or type(limit) is not int or not 1 <= limit <= 1001:
            raise AgentLoomError("SEARCH_ARGUMENTS_INVALID")
        if not literal:
            raise AgentLoomError(
                "REGEX_PROFILE_UNSUPPORTED", "This backend supports literal searches only"
            )
        result = []
        pending = [path]
        while pending and len(result) < limit:
            current = heapq.heappop(pending)
            with self._lock(scope):
                self._check(scope, guard)
                target = self.resolve(scope, current)
                is_directory = target.is_dir()
            if is_directory:
                for entry in self.list(scope, current, guard):
                    heapq.heappush(pending, entry["path"])
                continue
            try:
                text = self._whole_text(scope, current, guard)
            except AgentLoomError as exc:
                if exc.code == "BINARY_FILE":
                    continue
                raise
            for line, value in enumerate(text.splitlines(), 1):
                if pattern in value:
                    result.append(
                        {
                            "path": current,
                            "line": line,
                            "column": value.index(pattern) + 1,
                            "text": value,
                        }
                    )
                    if len(result) >= limit:
                        break
        return sorted(result, key=lambda r: (r["path"], r["line"], r["column"]))[:limit]

    def cleanup(self, scope):
        if scope["persistent"]:
            return
        # A run directory is never reused. Retried cleanup cannot delete a new run.
        target = Path(scope["path"])
        expected = Path(scope["base"]) / "runs" / _key(scope["run_id"])
        if target != expected or target.is_symlink():
            raise AgentLoomError("CLEANUP_SCOPE_INVALID")
        with self._lock(scope):
            if target.exists():
                shutil.rmtree(target)
