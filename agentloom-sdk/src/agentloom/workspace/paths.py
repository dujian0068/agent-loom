"""Logical paths shared by SDK authorization and provider implementations."""

from pathlib import PurePosixPath

from ..errors import AgentLoomError


def path_parts(path):
    if not isinstance(path, str) or "\0" in path or "\\" in path:
        raise AgentLoomError("PATH_FORBIDDEN", category="permission")
    if path == "/work":
        path = "."
    elif path.startswith("/work/"):
        # /work is a logical alias, never a host absolute path. Repeated
        # separators must not leave an absolute component after stripping it.
        path = path[6:].lstrip("/")
    elif path.startswith("/"):
        raise AgentLoomError("PATH_FORBIDDEN", category="permission")
    parts = PurePosixPath(path).parts
    if any(p == ".." or p.startswith(".agentloom") for p in parts):
        raise AgentLoomError("PATH_FORBIDDEN", category="permission")
    return parts
