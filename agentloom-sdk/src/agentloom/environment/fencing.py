"""Cooperative workspace authorization; unreadable fences never authorize access."""

import fcntl
import json
import os
from contextlib import contextmanager
from pathlib import Path

from ..errors import AgentLoomError, InfrastructureError, LeaseLost
from ..filesystem.posix import open_directory, open_nofollow, read_regular

MAX_EPOCH_BYTES = 4096


class LocalFencing:
    @contextmanager
    def _lock(self, scope):
        parent = open_directory(Path(scope["lock"]).parent)
        try:
            fd = open_nofollow(Path(scope["lock"]).name, os.O_CREAT | os.O_RDWR, dir_fd=parent)
        finally:
            os.close(parent)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _read_epoch(self, scope, *, allow_missing=False):
        try:
            parent = open_directory(Path(scope["lock"]).parent)
            try:
                data = read_regular(
                    parent, Path(scope["lock"]).name + ".epoch", limit=MAX_EPOCH_BYTES
                )
            finally:
                os.close(parent)
            value = json.loads(data)
            if (
                not isinstance(value, dict)
                or set(value) != {"run_id", "epoch", "active"}
                or not isinstance(value["run_id"], str)
                or not value["run_id"]
                or type(value["epoch"]) is not int
                or value["epoch"] < 1
                or type(value["active"]) is not bool
            ):
                raise ValueError()
            return value
        except FileNotFoundError as exc:
            if allow_missing:
                return None
            raise LeaseLost("Workspace authorization is missing") from exc
        except (ValueError, UnicodeError, AgentLoomError) as exc:
            raise LeaseLost("Workspace authorization is invalid") from exc
        except OSError as exc:
            raise InfrastructureError(
                "WORKSPACE_FENCE_UNAVAILABLE", external_outcome="unknown"
            ) from exc

    def _write_epoch(self, scope, value):
        target = Path(scope["lock"] + ".epoch")
        directory = open_directory(target.parent)
        staging = ".epoch-write-" + os.urandom(12).hex()
        try:
            fd = open_nofollow(staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL, dir_fd=directory)
            with os.fdopen(fd, "w") as stream:
                json.dump(value, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(staging, target.name, src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
        finally:
            try:
                os.unlink(staging, dir_fd=directory)
            except FileNotFoundError:
                pass
            finally:
                os.close(directory)

    def _check(self, scope, guard):
        guard()
        epoch = self._read_epoch(scope)
        if epoch != {"run_id": scope["run_id"], "epoch": scope["epoch"], "active": True}:
            raise LeaseLost()

    def revoke(self, scope):
        with self._lock(scope):
            value = self._read_epoch(scope)
            if value["run_id"] == scope["run_id"] and value["epoch"] == scope["epoch"]:
                self._write_epoch(scope, {**value, "active": False})
