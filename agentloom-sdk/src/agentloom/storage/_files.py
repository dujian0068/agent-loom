"""POSIX file primitives for the runtime store; no runtime or record semantics."""

import errno
import fcntl
import hashlib
import os
import stat
import threading
import time
import uuid
from contextlib import ExitStack, contextmanager
from pathlib import Path

from ..errors import InfrastructureError

_held = threading.local()
_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def prepare_root(path):
    # The caller chooses a trusted root. All paths below it use dirfd + NOFOLLOW.
    root = Path(path).expanduser().resolve()
    missing = []
    current = root
    while not current.exists():
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            pass
        fd = os.open(directory.parent, _DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    fd = os.open(root, _DIRECTORY)
    os.close(fd)
    return root


def _directory(stack, name, parent=None):
    if parent is not None:
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent)
        except FileExistsError:
            pass
        # Also seals a directory created by a process that died before fsync.
        os.fsync(parent)
    fd = os.open(name, _DIRECTORY, dir_fd=parent)
    stack.callback(os.close, fd)
    return fd


@contextmanager
def namespace_files(root, namespace, timeout):
    with ExitStack() as stack:
        root_fd = _directory(stack, root)
        namespaces = _directory(stack, "namespaces", root_fd)
        name = hashlib.sha256(namespace.encode("utf-8")).hexdigest()
        directory = _directory(stack, name, namespaces)
        identity = (os.fstat(directory).st_dev, os.fstat(directory).st_ino)
        held = getattr(_held, "namespaces", set())
        if identity in held:
            raise InfrastructureError("STORE_TRANSACTION_NESTED")
        lock = os.open(
            "lock",
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=directory,
        )
        stack.callback(os.close, lock)
        if not stat.S_ISREG(os.fstat(lock).st_mode):
            raise InfrastructureError("STORE_CORRUPT", "Invalid namespace lock")
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise InfrastructureError("STORE_BUSY", "Namespace lock timed out") from exc
                time.sleep(min(remaining, 0.01))
        _held.namespaces = held
        held.add(identity)
        stack.callback(held.remove, identity)
        objects = _directory(stack, "objects", directory)
        # A prior writer may have died after publishing a manifest but before its
        # directory sync. Do not acknowledge that writer's receipt until sealed.
        os.fsync(objects)
        os.fsync(directory)
        yield directory, objects


def read_file(directory, name):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=directory)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise InfrastructureError("STORE_CORRUPT", "Expected a regular state file")
        return stream.read()


def write_file(directory, name, content, *, immutable=False):
    """Publish only fully written, synced files. Caller syncs the directory."""
    temporary = ".tmp-" + uuid.uuid4().hex
    fd = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
        dir_fd=directory,
    )
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if immutable:
            try:
                os.link(
                    temporary,
                    name,
                    src_dir_fd=directory,
                    dst_dir_fd=directory,
                    follow_symlinks=False,
                )
            except FileExistsError:
                if read_file(directory, name) != content:
                    raise InfrastructureError(
                        "STORE_CORRUPT", "Object digest collision or corruption"
                    )
        else:
            os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
    finally:
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass
