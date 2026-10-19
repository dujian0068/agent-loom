"""Descriptor-based no-follow traversal and bounded, version-consistent reads."""

import errno
import os
import stat
from pathlib import Path

from ..errors import AgentLoomError

MAX_FILE_BYTES = 20 * 1024 * 1024


def open_nofollow(name, flags, mode=0o600, *, dir_fd=None):
    try:
        return os.open(name, flags | os.O_NOFOLLOW, mode, dir_fd=dir_fd)
    except OSError as exc:
        symlink = exc.errno == errno.ELOOP
        if exc.errno == errno.ENOTDIR:
            try:
                symlink = stat.S_ISLNK(os.stat(name, dir_fd=dir_fd, follow_symlinks=False).st_mode)
            except OSError:
                pass
        if symlink:
            raise AgentLoomError("SYMLINK_FORBIDDEN", category="permission") from exc
        raise


def open_directory(path, *, create=False):
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in Path(path).absolute().parts[1:]:
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            child = open_nofollow(part, os.O_RDONLY | os.O_DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def read_regular(directory, name, *, limit=MAX_FILE_BYTES):
    fd = open_nofollow(name, os.O_RDONLY | os.O_NONBLOCK, dir_fd=directory)
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise AgentLoomError("FILE_TYPE_FORBIDDEN", category="permission")
        if before.st_size > limit:
            raise AgentLoomError("FILE_TOO_LARGE", category="validation")
        data = stream.read(limit + 1)
        if len(data) > limit:
            raise AgentLoomError("FILE_TOO_LARGE", category="validation")
        after = os.fstat(stream.fileno())

        def stamp(info):
            return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)

        if stamp(before) != stamp(after) or len(data) != after.st_size:
            raise AgentLoomError("FILE_CHANGED", category="conflict")
        return data


def walk_files(root, *, guard=lambda: None):
    """Yield (parent descriptor, relative path, lstat) without following links.

    Descriptors remain valid only until the iterator advances. Depth-first
    traversal bounds open handles by depth instead of the number of directories.
    """
    stack = []

    def push(fd, prefix):
        try:
            entries = iter(sorted(os.listdir(fd)))
        except BaseException:
            os.close(fd)
            raise
        stack.append((fd, prefix, entries))

    try:
        push(open_directory(root), "")
        while stack:
            guard()
            fd, prefix, entries = stack[-1]
            name = next(entries, None)
            if name is None:
                os.close(fd)
                stack.pop()
                continue
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            relative = prefix + name
            if stat.S_ISDIR(info.st_mode):
                push(open_nofollow(name, os.O_RDONLY | os.O_DIRECTORY, dir_fd=fd), relative + "/")
            else:
                yield fd, relative, info
    finally:
        for fd, _, _ in stack:
            os.close(fd)
