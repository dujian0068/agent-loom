"""Collect abandoned local write files while holding the workspace's exclusive lock."""

import os
import re
import stat

from ..filesystem.posix import walk_files

WRITE_STAGING = re.compile(r"\.agentloom-write-[0-9a-f]{24}\Z")


def cleanup_staging(path, guard):
    from contextlib import closing

    with closing(walk_files(path, guard=guard)) as entries:
        for fd, relative, info in entries:
            name = relative.rsplit("/", 1)[-1]
            if stat.S_ISREG(info.st_mode) and WRITE_STAGING.fullmatch(name):
                guard()
                os.unlink(name, dir_fd=fd)
                os.fsync(fd)
