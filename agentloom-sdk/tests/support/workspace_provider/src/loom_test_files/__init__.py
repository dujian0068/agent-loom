"""Independent test distribution: uses only the public SPI, never SDK file drivers.

This is a contract fixture, not a production shared-storage implementation.
"""

import base64
import fnmatch
import hashlib
import json
import threading
from pathlib import Path, PurePosixPath

from agentloom import AgentLoomError, WorkspaceBackend


def version(text):
    return hashlib.sha256(text.encode()).hexdigest()


def logical(path):
    return str(PurePosixPath(path.removeprefix("/work/") if path != "/work" else "."))


class TestFiles(WorkspaceBackend):
    def __init__(self, label="test", state_path=None, audit_path=None):
        self.label, self.state_path, self.audit_path = label, state_path, audit_path
        self.lock = threading.RLock()
        self.data, self.calls, self.closed = {}, [], 0
        if state_path and Path(state_path).exists():
            self.data = json.loads(Path(state_path).read_text())

    def _call(self, name, context):
        context.check_active()
        event = {
            "method": name,
            "workspace_id": context.workspace_id,
            "run_id": context.run_id,
            "role": context.role_key,
            "operation_id": context.operation_id,
            "attempt_id": context.attempt_id,
            "attempt_no": context.attempt_no,
            "purpose": context.purpose,
        }
        self.calls.append(event)
        if self.audit_path:
            with open(self.audit_path, "a") as fp:
                fp.write(json.dumps(event) + "\n")
        return self.data.setdefault(context.workspace_id, {".": None})

    def _save(self, context):
        context.check_active()
        if self.state_path:
            target = Path(self.state_path)
            staging = target.with_suffix(".tmp")
            staging.write_text(json.dumps(self.data))
            staging.replace(target)

    def _stat(self, files, path):
        path = logical(path)
        if path not in files:
            raise FileNotFoundError()
        content = files[path]
        if content is None:
            children = {
                k: v
                for k, v in files.items()
                if k != path and (path == "." or k.startswith(path + "/"))
            }
            return {
                "path": path,
                "kind": "directory",
                "size": len(children),
                "etag": version(json.dumps(children, sort_keys=True)),
            }
        sha = version(content)
        return {
            "path": path,
            "kind": "file",
            "size": len(content.encode()),
            "etag": sha,
            "sha256": sha,
        }

    def prepare(self, context):
        self._call("prepare", context)

    def revoke(self, context):
        self._call("revoke", context)

    def close(self):
        self.closed += 1

    def read(self, context, path, *, offset=0, limit=16384):
        with self.lock:
            files = self._call("read", context)
            info = self._stat(files, path)
            data = files[logical(path)].encode()
            if offset > len(data):
                raise AgentLoomError("FILE_SLICE_INVALID")
            data[:offset].decode()
            end = min(offset + limit, len(data))
            while end > offset:
                try:
                    text = data[offset:end].decode()
                    break
                except UnicodeDecodeError:
                    end -= 1
            else:
                text = ""
            return {
                **info,
                "text": text,
                "offset": offset,
                "end_offset": end,
                "next_offset": end if end < len(data) else None,
                "total_bytes": len(data),
            }

    def write(self, context, path, content, *, mode="create_only", if_match=None):
        with self.lock:
            files = self._call("write", context)
            path = logical(path)
            if mode == "create_only" and path in files:
                raise AgentLoomError("FILE_EXISTS", category="conflict")
            if mode == "replace" and self._stat(files, path)["etag"] != if_match:
                raise AgentLoomError("FILE_CHANGED", category="conflict")
            if str(PurePosixPath(path).parent) not in files:
                raise FileNotFoundError()
            context.check_active()
            files[path] = content
            self._save(context)
            return self._stat(files, path)

    def list(self, context, path=".", *, cursor=None, limit=100):
        with self.lock:
            files = self._call("list", context)
            path = logical(path)
            etag = self._stat(files, path)["etag"]
            entries = [
                {**self._stat(files, key), "name": PurePosixPath(key).name}
                for key in sorted(files)
                if key != path and str(PurePosixPath(key).parent) == path
            ]
            offset = 0
            if cursor:
                old, offset = json.loads(base64.b64decode(cursor))
                if old != etag:
                    raise AgentLoomError("DIRECTORY_CHANGED", category="conflict")
            end = min(offset + limit, len(entries))
            next_cursor = (
                base64.b64encode(json.dumps([etag, end]).encode()).decode()
                if end < len(entries)
                else None
            )
            return {"entries": entries[offset:end], "etag": etag, "next_cursor": next_cursor}

    def mkdir(self, context, path, *, parents=False):
        with self.lock:
            files = self._call("mkdir", context)
            path = logical(path)
            if path in files and files[path] is not None:
                raise FileExistsError()
            if parents:
                for parent in reversed(PurePosixPath(path).parents):
                    files.setdefault(str(parent), None)
            elif str(PurePosixPath(path).parent) not in files:
                raise FileNotFoundError()
            files[path] = None
            self._save(context)
            return {"path": path, "kind": "directory"}

    def stat(self, context, path):
        with self.lock:
            return self._stat(self._call("stat", context), path)

    def edit(self, context, path, old_text, new_text, if_match, *, expected_matches=1):
        with self.lock:
            files = self._call("edit", context)
            text = files[logical(path)]
            count = text.count(old_text)
            if count != expected_matches:
                raise AgentLoomError("EDIT_MATCH_COUNT", category="conflict")
            return {
                **self.write(
                    context,
                    path,
                    text.replace(old_text, new_text),
                    mode="replace",
                    if_match=if_match,
                ),
                "matched_count": count,
            }

    def patch(self, context, path, unified_diff, if_match):
        with self.lock:
            files = self._call("patch", context)
            lines = unified_diff.splitlines(True)
            old = "".join(line[1:] for line in lines[3:] if line[:1] in {"-", " "})
            new = "".join(line[1:] for line in lines[3:] if line[:1] in {"+", " "})
            if files[logical(path)] != old:
                raise AgentLoomError("PATCH_HUNK_CONFLICT", category="conflict")
            return {
                **self.write(context, path, new, mode="replace", if_match=if_match),
                "hunks_applied": 1,
            }

    def move(self, context, source, destination, if_match):
        with self.lock:
            files = self._call("move", context)
            source, destination = logical(source), logical(destination)
            if self._stat(files, source)["etag"] != if_match:
                raise AgentLoomError("FILE_CHANGED", category="conflict")
            if destination in files:
                raise FileExistsError()
            files[destination] = files.pop(source)
            self._save(context)
            return self._stat(files, destination)

    def delete(self, context, path, if_match, *, recursive=False):
        with self.lock:
            files = self._call("delete", context)
            path = logical(path)
            if self._stat(files, path)["etag"] != if_match:
                raise AgentLoomError("FILE_CHANGED", category="conflict")
            children = [key for key in files if key.startswith(path + "/")]
            if children and not recursive:
                raise AgentLoomError("DIRECTORY_NOT_EMPTY")
            for key in [*children, path]:
                del files[key]
            self._save(context)
            return {"deleted_path": path, "previous_etag": if_match}

    def glob(self, context, pattern, root=".", *, limit=1000):
        with self.lock:
            files = self._call("glob", context)
            root = logical(root)
            paths = [
                key
                for key in sorted(files)
                if key != root
                and (root == "." or key.startswith(root + "/"))
                and fnmatch.fnmatchcase(key if root == "." else key[len(root) + 1 :], pattern)
            ]
            return {"paths": paths[:limit], "truncated": len(paths) > limit}

    def grep(self, context, pattern, path=".", *, limit=1000, literal=True):
        with self.lock:
            files = self._call("grep", context)
            path = logical(path)
            matches = [
                {"path": key, "line": line, "column": text.index(pattern) + 1, "text": text}
                for key, value in sorted(files.items())
                if value is not None and (key == path or path == "." or key.startswith(path + "/"))
                for line, text in enumerate(value.splitlines(), 1)
                if pattern in text
            ]
            return {"matches": matches[:limit], "truncated": len(matches) > limit}
