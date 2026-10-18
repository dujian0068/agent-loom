"""One source for model-visible file argument schemas and service validation."""

from ..environment.limits import MAX_FILE_READ_BYTES


def file_definitions():
    string = {"type": "string"}
    definitions = {
        "read": (
            {
                "path": string,
                "offset": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": MAX_FILE_READ_BYTES},
            },
            ["path"],
        ),
        "write": (
            {
                "path": string,
                "content": string,
                "mode": {"enum": ["create_only", "replace"]},
                "if_match": string,
            },
            ["path", "content"],
        ),
        "list": (
            {
                "path": string,
                "cursor": string,
                "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
            },
            [],
        ),
        "mkdir": ({"path": string, "parents": {"type": "boolean"}}, ["path"]),
        "stat": ({"path": string}, ["path"]),
        "edit": (
            {
                "path": string,
                "old_text": string,
                "new_text": string,
                "if_match": string,
                "expected_matches": {"type": "integer", "minimum": 1},
            },
            ["path", "old_text", "new_text", "if_match"],
        ),
        "patch": (
            {"path": string, "unified_diff": string, "if_match": string},
            ["path", "unified_diff", "if_match"],
        ),
        "move": (
            {"source": string, "destination": string, "if_match": string},
            ["source", "destination", "if_match"],
        ),
        "delete": (
            {"path": string, "if_match": string, "recursive": {"type": "boolean"}},
            ["path", "if_match"],
        ),
        "glob": (
            {
                "pattern": string,
                "root": string,
                "limit": {"type": "integer", "minimum": 1, "maximum": 10000},
            },
            ["pattern"],
        ),
        "grep": (
            {
                "path": string,
                "pattern": string,
                "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
                "literal": {"const": True},
            },
            ["pattern"],
        ),
    }
    return definitions
