"""Create-only local moves with identity-checked rollback of a failed source removal."""

import errno
import os

from ..errors import AgentLoomError


def _identity(info):
    return info.st_dev, info.st_ino


def move_file(source_fd, source, destination_fd, destination):
    identity = _identity(os.stat(source, dir_fd=source_fd, follow_symlinks=False))
    try:
        os.link(
            source,
            destination,
            src_dir_fd=source_fd,
            dst_dir_fd=destination_fd,
            follow_symlinks=False,
        )
    except OSError as exc:
        if exc.errno == errno.EXDEV:
            raise AgentLoomError(
                "FILE_MOVE_CROSS_DEVICE", category="validation", external_outcome="not_started"
            ) from exc
        raise
    try:
        os.unlink(source, dir_fd=source_fd)
    except OSError as original:
        try:
            # Never delete an unrelated replacement, or the only surviving copy.
            if (
                _identity(os.stat(source, dir_fd=source_fd, follow_symlinks=False)) != identity
                or _identity(os.stat(destination, dir_fd=destination_fd, follow_symlinks=False))
                != identity
            ):
                raise OSError("Move rollback identity changed")
            os.unlink(destination, dir_fd=destination_fd)
            os.fsync(destination_fd)
        except OSError as rollback:
            raise AgentLoomError(
                "FILE_MOVE_OUTCOME_UNKNOWN", category="unknown", external_outcome="unknown"
            ) from rollback
        raise original
    os.fsync(source_fd)
    os.fsync(destination_fd)
