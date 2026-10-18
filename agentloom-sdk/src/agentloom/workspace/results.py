"""Provider output must fit the existing file DTO and authorized logical paths."""

from ..configuration import json_copy, validate_schema
from ..errors import AgentLoomError
from .paths import path_parts

_TEXT = {"type": "string"}
_TOKEN = {"type": "string", "minLength": 1}
_UINT = {"type": "integer", "minimum": 0}
_POSITION = {"type": "integer", "minimum": 1}
_VERSION = {
    "path": _TEXT,
    "kind": {"enum": ["file", "directory"]},
    "size": _UINT,
    "etag": _TOKEN,
    "sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
}


def _object(fields, required=None):
    return {
        "type": "object",
        "properties": fields,
        "required": list(required or fields),
        "additionalProperties": False,
    }


def _version(extra=None):
    fields = {**_VERSION, **(extra or {})}
    return _object(fields, fields.keys() - {"sha256"})


_SCHEMAS = {
    "stat": _version(),
    "write": _version(),
    "move": _version(),
    "read": _version(
        {
            "text": _TEXT,
            "offset": _UINT,
            "end_offset": _UINT,
            "next_offset": {"type": ["integer", "null"], "minimum": 0},
            "total_bytes": _UINT,
        }
    ),
    "edit": _version({"matched_count": _POSITION}),
    "patch": _version({"hunks_applied": _POSITION}),
    "mkdir": _object({"path": _TEXT, "kind": {"const": "directory"}}),
    "delete": _object({"deleted_path": _TEXT, "previous_etag": _TOKEN}),
    "list": _object(
        {
            "entries": {"type": "array", "items": _version({"name": _TOKEN})},
            "etag": _TOKEN,
            "next_cursor": {"type": ["string", "null"]},
        }
    ),
    "glob": _object({"paths": {"type": "array", "items": _TEXT}, "truncated": {"type": "boolean"}}),
    "grep": _object(
        {
            "matches": {
                "type": "array",
                "items": _object(
                    {"path": _TEXT, "line": _POSITION, "column": _POSITION, "text": _TEXT}
                ),
            },
            "truncated": {"type": "boolean"},
        }
    ),
}


def validate_result(member, arguments, result, allowed):
    try:
        value = json_copy(result)
        validate_schema(value, _SCHEMAS[member])
        root = path_parts(arguments.get("path", arguments.get("root", ".")))

        def path(value, *, exact=None, child=False):
            parts = allowed(value)
            if exact is not None and parts != path_parts(exact):
                raise ValueError()
            if child and (parts[: len(root)] != root or len(parts) <= len(root)):
                raise ValueError()
            return parts

        if "path" in value:
            path(value["path"], exact=arguments.get("destination", arguments.get("path")))
        if member in {"read", "write", "edit", "patch", "move"}:
            if value["kind"] != "file" or "sha256" not in value:
                raise ValueError()
        if member == "read":
            end, total = value["end_offset"], value["total_bytes"]
            size = len(value["text"].encode("utf-8"))
            if (
                value["offset"] != arguments["offset"]
                or total != value["size"]
                or end != value["offset"] + size
                or end > total
                or size > arguments["limit"]
                or value["next_offset"] != (end if end < total else None)
            ):
                raise ValueError()
        elif member == "delete":
            path(value["deleted_path"], exact=arguments["path"])
            if value["previous_etag"] != arguments["if_match"]:
                raise ValueError()
        elif member == "list":
            entries = value["entries"]
            paths = []
            for entry in entries:
                parts = path(entry["path"], child=True)
                if len(parts) != len(root) + 1 or entry["name"] != parts[-1]:
                    raise ValueError()
                paths.append(entry["path"])
            if (
                paths != sorted(set(paths), key=lambda p: p.encode())
                or len(paths) > arguments["limit"]
            ):
                raise ValueError()
            if value["next_cursor"] is not None and (not value["next_cursor"] or not entries):
                raise ValueError()
        elif member == "glob":
            paths = value["paths"]
            for item in paths:
                path(item, child=True)
            if (
                paths != sorted(set(paths), key=lambda p: p.encode())
                or len(paths) > arguments["limit"]
            ):
                raise ValueError()
        elif member == "grep":
            ordering = []
            for match in value["matches"]:
                parts = path(match["path"])
                if parts[: len(root)] != root:
                    raise ValueError()
                ordering.append((match["path"], match["line"], match["column"]))
            if ordering != sorted(set(ordering)) or len(ordering) > arguments["limit"]:
                raise ValueError()
        return value
    except (AgentLoomError, ValueError, TypeError, KeyError, UnicodeError) as exc:
        error = AgentLoomError("FILE_RESULT_INVALID", category="protocol", external_outcome="known")
        error.execution_fact = "completed"
        raise error from exc
