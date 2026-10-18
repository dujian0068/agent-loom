"""Version-checked POSIX mutations and exact, single-file patches."""

import fnmatch
import os
import re
import shutil
import stat

from ..configuration import digest
from ..errors import AgentLoomError
from ..workspace.paths import path_parts


class FileMutations:
    def _entry_version(self, fd, name, path):
        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if stat.S_ISLNK(info.st_mode):
            raise AgentLoomError("SYMLINK_FORBIDDEN")
        if stat.S_ISREG(info.st_mode):
            return self._version(path, self._read_bytes(fd, name))
        if not stat.S_ISDIR(info.st_mode):
            raise AgentLoomError("FILE_TYPE_FORBIDDEN")
        child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
        try:
            entries = []
            for entry in sorted(os.listdir(child), key=lambda s: s.encode()):
                version = self._entry_version(child, entry, path.rstrip("/") + "/" + entry)
                entries.append([entry, version["kind"], version["etag"]])
        finally:
            os.close(child)
        return {"path": path, "kind": "directory", "size": len(entries), "etag": digest(entries)}

    def stat(self, scope, path, guard=lambda: None):
        from .local import _directory, _parts

        with self._lock(scope):
            self._check(scope, guard)
            if not _parts(path):
                fd = _directory(scope["path"])
                try:
                    return self._entry_version(fd, ".", path)
                finally:
                    os.close(fd)
            with self._parent(scope, path) as (fd, name):
                return self._entry_version(fd, name, path)

    def edit(
        self, scope, path, old_text, new_text, if_match, guard=lambda: None, *, expected_matches=1
    ):
        if (
            not isinstance(old_text, str)
            or not old_text
            or type(expected_matches) is not int
            or expected_matches < 1
        ):
            raise AgentLoomError("EDIT_ARGUMENTS_INVALID")
        original = self._whole_text(scope, path, guard)
        if self._version(path, original.encode())["etag"] != if_match:
            raise AgentLoomError("FILE_CHANGED", category="conflict")
        matched = original.count(old_text)
        if matched != expected_matches:
            raise AgentLoomError("EDIT_MATCH_COUNT", category="conflict")
        return {
            **self.write(
                scope,
                path,
                original.replace(old_text, new_text),
                guard,
                mode="replace",
                if_match=if_match,
            ),
            "matched_count": matched,
        }

    def _whole_text(self, scope, path, guard):
        with self._lock(scope):
            self._check(scope, guard)
            with self._parent(scope, path) as (fd, name):
                data = self._read_bytes(fd, name)
        try:
            if b"\0" in data:
                raise UnicodeError()
            return data.decode("utf-8")
        except UnicodeError as exc:
            raise AgentLoomError("BINARY_FILE") from exc

    def patch(self, scope, path, unified_diff, if_match, guard=lambda: None):
        from .local import _parts

        original = self._whole_text(scope, path, guard)
        if self._version(path, original.encode())["etag"] != if_match:
            raise AgentLoomError("FILE_CHANGED", category="conflict")
        lines = lf_lines(unified_diff)
        if len(lines) < 3 or not lines[0].startswith("--- ") or not lines[1].startswith("+++ "):
            raise AgentLoomError("PATCH_FORMAT_INVALID")
        for header, prefix in zip(lines[:2], ("a/", "b/")):
            candidate = header[4:].rstrip("\r\n").split("\t", 1)[0].removeprefix(prefix)
            if _parts(candidate) != _parts(path):
                raise AgentLoomError("PATCH_PATH_MISMATCH")
        source, output, position, index, hunks = lf_lines(original), [], 0, 2, 0
        while index < len(lines):
            match = re.fullmatch(
                r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@[^\n]*(?:\n|$)", lines[index]
            )
            if not match:
                raise AgentLoomError("PATCH_FORMAT_INVALID")
            old_start, old_count = int(match[1]), int(match[2] or 1)
            new_start, new_count = int(match[3]), int(match[4] or 1)
            start = old_start - 1 if old_count else old_start
            if start < position or start > len(source):
                raise AgentLoomError("PATCH_HUNK_CONFLICT", category="conflict")
            output.extend(source[position:start])
            if len(output) != (new_start - 1 if new_count else new_start):
                raise AgentLoomError("PATCH_HUNK_CONFLICT", category="conflict")
            position, removed, added = start, 0, 0
            index += 1
            while index < len(lines) and not lines[index].startswith("@@ "):
                line = lines[index]
                if not line or line[0] not in " +-":
                    raise AgentLoomError("PATCH_FORMAT_INVALID")
                marker, text = line[0], line[1:]
                if index + 1 < len(lines) and lines[index + 1].startswith(
                    "\\ No newline at end of file"
                ):
                    text = text.removesuffix("\n").removesuffix("\r")
                    index += 1
                if marker in " -":
                    if position >= len(source) or source[position] != text:
                        raise AgentLoomError("PATCH_HUNK_CONFLICT", category="conflict")
                    position += 1
                    removed += 1
                if marker in " +":
                    output.append(text)
                    added += 1
                index += 1
            if (removed, added) != (old_count, new_count):
                raise AgentLoomError("PATCH_HUNK_COUNT_INVALID")
            hunks += 1
        output.extend(source[position:])
        if not hunks:
            raise AgentLoomError("PATCH_FORMAT_INVALID")
        return {
            **self.write(scope, path, "".join(output), guard, mode="replace", if_match=if_match),
            "hunks_applied": hunks,
        }

    def move(self, scope, source, destination, if_match, guard=lambda: None):
        with self._lock(scope):
            self._check(scope, guard)
            with (
                self._parent(scope, source) as (source_fd, source_name),
                self._parent(scope, destination) as (dest_fd, dest_name),
            ):
                version = self._entry_version(source_fd, source_name, source)
                if version["etag"] != if_match:
                    raise AgentLoomError("FILE_CHANGED", category="conflict")
                if version["kind"] != "file":
                    raise AgentLoomError("DIRECTORY_MOVE_UNSUPPORTED")
                # link() is atomic create-only; rename() could overwrite a late target.
                os.link(
                    source_name,
                    dest_name,
                    src_dir_fd=source_fd,
                    dst_dir_fd=dest_fd,
                    follow_symlinks=False,
                )
                os.unlink(source_name, dir_fd=source_fd)
                os.fsync(source_fd)
                os.fsync(dest_fd)
                return {**version, "path": destination}

    def delete(self, scope, path, if_match, guard=lambda: None, *, recursive=False):
        with self._lock(scope):
            self._check(scope, guard)
            with self._parent(scope, path) as (fd, name):
                version = self._entry_version(fd, name, path)
                if version["etag"] != if_match:
                    raise AgentLoomError("FILE_CHANGED", category="conflict")
                if version["kind"] == "directory":
                    if recursive:
                        shutil.rmtree(name, dir_fd=fd)
                    else:
                        os.rmdir(name, dir_fd=fd)
                else:
                    os.unlink(name, dir_fd=fd)
                os.fsync(fd)
        return {"deleted_path": path, "previous_etag": if_match}

    def glob(self, scope, pattern, root=".", guard=lambda: None, *, limit=1000):
        if type(limit) is not int or not 1 <= limit <= 10000 or not isinstance(pattern, str):
            raise AgentLoomError("GLOB_ARGUMENTS_INVALID")
        if pattern.startswith("/") or ".." in pattern.split("/"):
            raise AgentLoomError("PATH_FORBIDDEN")
        root_parts = path_parts(root)
        pattern_parts = path_parts(pattern)
        directories_only = pattern.endswith("/")
        pending, paths = [root], []

        def matches(parts, segments):
            if not segments:
                return not parts
            if segments[0] == "**":
                return matches(parts, segments[1:]) or bool(parts) and matches(parts[1:], segments)
            return (
                bool(parts)
                and fnmatch.fnmatchcase(parts[0], segments[0])
                and matches(parts[1:], segments[1:])
            )

        while pending:
            current = pending.pop()
            for entry in self.list(scope, current, guard):
                # list() normalizes dot segments and repeated separators. Use
                # the same logical path parts, including the /work alias.
                relative = path_parts(entry["path"])[len(root_parts) :]
                if matches(relative, pattern_parts) and (
                    not directories_only or entry["kind"] == "directory"
                ):
                    paths.append(entry["path"])
                if entry["kind"] == "directory":
                    pending.append(entry["path"])
        paths.sort(key=lambda p: p.encode())
        return {"paths": paths[:limit], "truncated": len(paths) > limit}


def lf_lines(text):
    """Unified diff counts LF-terminated lines, not Unicode text separators."""
    parts = text.split("\n")
    return [part + "\n" for part in parts[:-1]] + ([parts[-1]] if parts[-1] else [])
