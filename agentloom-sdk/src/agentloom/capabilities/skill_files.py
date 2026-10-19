"""Skill-specific errors over the same bounded, no-follow import/read boundary."""

import errno
import hashlib
import os
import stat
from contextlib import closing
from pathlib import PurePosixPath

from ..errors import AgentLoomError, ConfigurationError
from ..filesystem.posix import open_directory, read_regular, walk_files


def _raise_skill_error(exc, *, importing=False):
    code, category = "SKILL_CONTENT_UNAVAILABLE", "unknown"
    if isinstance(exc, AgentLoomError):
        code, category = {
            "SYMLINK_FORBIDDEN": ("SKILL_PATH_FORBIDDEN", "permission"),
            "FILE_TYPE_FORBIDDEN": ("SKILL_PATH_FORBIDDEN", "permission"),
            "FILE_TOO_LARGE": ("SKILL_CONTENT_TOO_LARGE", "validation"),
            "FILE_CHANGED": ("SKILL_CONTENT_CHANGED", "conflict"),
        }.get(exc.code, (code, category))
    elif exc.errno == errno.ENOENT:
        category = "not_found"
    elif exc.errno in {errno.EACCES, errno.EPERM, errno.ENOTDIR, errno.ELOOP}:
        code, category = "SKILL_PATH_FORBIDDEN", "permission"
    error = ConfigurationError if importing else AgentLoomError
    raise error(code, category=category) from exc


def import_files(root):
    manifest, entry = {}, None
    try:
        with closing(walk_files(root)) as entries:
            for fd, relative, info in entries:
                if not stat.S_ISREG(info.st_mode):
                    raise AgentLoomError("FILE_TYPE_FORBIDDEN")
                data = read_regular(fd, PurePosixPath(relative).name)
                manifest[relative] = hashlib.sha256(data).hexdigest()
                if relative == "SKILL.md":
                    entry = data
    except (OSError, AgentLoomError) as exc:
        _raise_skill_error(exc, importing=True)
    if entry is None:
        raise ConfigurationError("SKILL_PACKAGE_INVALID")
    return entry, manifest


def read_resource_bytes(root, parts):
    try:
        fd = open_directory(root.joinpath(*parts[:-1]))
        try:
            return read_regular(fd, parts[-1])
        finally:
            os.close(fd)
    except (OSError, AgentLoomError) as exc:
        _raise_skill_error(exc)
