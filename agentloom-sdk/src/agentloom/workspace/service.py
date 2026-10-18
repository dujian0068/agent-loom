"""Authorized file view: validate, dispatch once, validate the public result."""

import time
from dataclasses import replace

from ..configuration import freeze, validate_schema
from ..environment.limits import DEFAULT_FILE_READ_BYTES, MAX_FILE_BYTES
from ..errors import AgentLoomError
from .contracts import WorkspaceCallContext
from .loader import synchronous_result
from .paths import path_parts
from .results import validate_result
from .schemas import file_definitions

_DEFINITIONS = file_definitions()
_WRITES = {"write", "mkdir", "edit", "patch", "move", "delete"}


class WorkspaceService:
    """A role-bound file view. Providers are shared; authorization is per call.

    The four positional arguments retain the existing local service constructor.
    Runtime uses its selected provider and a trusted WorkspaceCallContext. Model
    arguments cannot select implementations, credentials, scope or execution IDs.
    """

    def __init__(self, environment, scope, guard, access_scope, *, backend=None, context=None):
        if backend is None:
            from .local import LocalWorkspaceBackend

            backend = LocalWorkspaceBackend()
            backend.environment = environment
        self._backend = backend
        self._environment = environment
        self._access = freeze(access_scope)
        self._context = context or WorkspaceCallContext(
            workspace_id=scope.get("workspace_id", scope["path"]),
            scope=scope,
            check_active=guard,
            namespace=scope.get("namespace", "default"),
            run_id=scope.get("run_id", ""),
            lease_epoch=scope.get("epoch", 0),
            access_scope=access_scope,
        )

    @property
    def sandbox_gateway(self):
        """Only the matching isolated local environment may expose script access."""
        from .local import LocalWorkspaceBackend

        if isinstance(self._backend, LocalWorkspaceBackend) and getattr(
            self._environment, "process_isolation", False
        ):
            return getattr(self._environment, "sandbox_gateway", None)
        return None

    def _allowed(self, path, *, write=False):
        parts = path_parts(path)
        if write and self._access.get("read_only", False):
            raise AgentLoomError("WORKSPACE_READ_ONLY", category="permission")
        if write and not parts:
            raise AgentLoomError("WORKSPACE_ROOT_PROTECTED", category="permission")
        roots = self._access.get("paths")
        if roots is not None and not any(
            parts[: len(path_parts(root))] == path_parts(root) for root in roots
        ):
            raise AgentLoomError("PATH_FORBIDDEN", category="permission")
        return parts

    def _validate_arguments(self, member, arguments):
        fields, required = _DEFINITIONS[member]
        # Optional None parameters are omitted from the model's schema.
        supplied = {
            key: value
            for key, value in arguments.items()
            if value is not None or (member, key) not in {("write", "if_match"), ("list", "cursor")}
        }
        validate_schema(
            supplied,
            {
                "type": "object",
                "properties": fields,
                "required": required,
                "additionalProperties": False,
            },
            code="FILE_ARGUMENTS_INVALID",
        )
        if any(
            type(arguments[key]) is not int
            for key in ("offset", "limit", "expected_matches")
            if key in arguments
        ):
            raise AgentLoomError(
                "FILE_ARGUMENTS_INVALID", category="validation", external_outcome="not_started"
            )
        paths = (
            (arguments["source"], arguments["destination"])
            if member == "move"
            else (arguments.get("path", arguments.get("root", ".")),)
        )
        for path in paths:
            self._allowed(path, write=member in _WRITES)
        for name in ("content", "new_text", "unified_diff"):
            if name in arguments and len(arguments[name].encode("utf-8")) > MAX_FILE_BYTES:
                raise AgentLoomError("FILE_TOO_LARGE")
        if member == "write" and (arguments["mode"] == "replace") != (
            arguments["if_match"] is not None
        ):
            raise AgentLoomError("INVALID_WRITE_MODE")
        if member == "edit" and not arguments["old_text"]:
            raise AgentLoomError("EDIT_ARGUMENTS_INVALID")
        if member == "glob":
            path_parts(arguments["pattern"])
            if arguments["pattern"].startswith("/"):
                raise AgentLoomError("PATH_FORBIDDEN", category="permission")

    def _call(self, member, arguments):
        try:
            self._validate_arguments(member, arguments)
        except AgentLoomError as exc:
            exc.phase = "prepare"
            exc.external_outcome = "not_started"
            exc.execution_fact = "not_started"
            raise

        def check():
            self._context.check_active()
            if self._context.deadline_at is not None and time.time() >= self._context.deadline_at:
                raise AgentLoomError("TOOL_TIMEOUT", category="timeout", external_outcome="unknown")

        context = replace(self._context, check_active=check)
        check()
        result = synchronous_result(getattr(self._backend, member)(context, **arguments))
        result = validate_result(member, arguments, result, self._allowed)
        self._context.check_active()
        if self._context.deadline_at is not None and time.time() >= self._context.deadline_at:
            # A returned outcome is evidence of completion. A late reply must
            # not be reclassified as unknown and cause an automatic second write.
            error = AgentLoomError("TOOL_TIMEOUT", category="timeout", external_outcome="known")
            error.execution_fact = "completed"
            error.result_material = result
            raise error
        return result

    def read(self, path, *, offset=0, limit=DEFAULT_FILE_READ_BYTES):
        return self._call("read", dict(path=path, offset=offset, limit=limit))

    def write(self, path, content, *, mode="create_only", if_match=None):
        return self._call("write", dict(path=path, content=content, mode=mode, if_match=if_match))

    def list(self, path=".", *, cursor=None, limit=100):
        return self._call("list", dict(path=path, cursor=cursor, limit=limit))

    def mkdir(self, path, *, parents=False):
        return self._call("mkdir", dict(path=path, parents=parents))

    def stat(self, path):
        return self._call("stat", dict(path=path))

    def edit(self, path, old_text, new_text, if_match, *, expected_matches=1):
        return self._call(
            "edit",
            dict(
                path=path,
                old_text=old_text,
                new_text=new_text,
                if_match=if_match,
                expected_matches=expected_matches,
            ),
        )

    def patch(self, path, unified_diff, if_match):
        return self._call("patch", dict(path=path, unified_diff=unified_diff, if_match=if_match))

    def move(self, source, destination, if_match):
        return self._call("move", dict(source=source, destination=destination, if_match=if_match))

    def delete(self, path, if_match, *, recursive=False):
        return self._call("delete", dict(path=path, if_match=if_match, recursive=recursive))

    def glob(self, pattern, root=".", *, limit=1000):
        return self._call("glob", dict(pattern=pattern, root=root, limit=limit))

    def grep(self, pattern, path=".", *, limit=1000, literal=True):
        return self._call("grep", dict(pattern=pattern, path=path, limit=limit, literal=literal))
