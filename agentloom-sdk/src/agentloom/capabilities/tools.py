import inspect
from dataclasses import dataclass

from ..errors import AgentLoomError
from .registry import CapabilityRegistration, CapabilitySpec


@dataclass(frozen=True)
class CapabilityContext:
    operation_id: str
    attempt_id: str
    run_id: str
    task_id: str
    role_key: str
    workspace: object
    config: object
    check_active: object
    spec: CapabilitySpec | None = None


class FunctionToolAdapter:
    """Trusted cooperative functions. CPU/blocking functions should use an isolated adapter."""

    def __init__(self, function):
        self.function = function

    async def invoke(self, member, arguments, context):
        value = self.function(arguments, context)
        return await value if inspect.isawaitable(value) else value


class WorkspaceService:
    """A role-bound view, not the environment root or a raw host path."""

    def __init__(self, environment, scope, guard, access_scope):
        self._environment, self._scope, self._guard = environment, scope, guard
        self._access = access_scope

    def _allowed(self, path, *, write=False):
        from ..environment.local import _parts

        parts = _parts(path)
        if write and self._access.get("read_only", False):
            raise AgentLoomError("WORKSPACE_READ_ONLY", category="permission")
        roots = self._access.get("paths")
        if roots is not None and not any(
            parts[: len(_parts(root))] == _parts(root) for root in roots
        ):
            raise AgentLoomError("PATH_FORBIDDEN", category="permission")

    def read(self, path, **kwargs):
        self._allowed(path)
        return self._environment.read(self._scope, path, self._guard, **kwargs)

    def write(self, path, content, **kwargs):
        self._allowed(path, write=True)
        return self._environment.write(self._scope, path, content, self._guard, **kwargs)

    def list(self, path=".", **kwargs):
        self._allowed(path)
        return self._environment.list_page(self._scope, path, self._guard, **kwargs)

    def mkdir(self, path, **kwargs):
        self._allowed(path, write=True)
        return self._environment.mkdir(self._scope, path, self._guard, **kwargs)

    def grep(self, pattern, path=".", **kwargs):
        self._allowed(path)
        limit = kwargs.pop("limit", 1000)
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise AgentLoomError("SEARCH_ARGUMENTS_INVALID")
        matches = self._environment.grep(
            self._scope, pattern, path, self._guard, limit=limit + 1, **kwargs
        )
        return {"matches": matches[:limit], "truncated": len(matches) > limit}

    def stat(self, path):
        self._allowed(path)
        return self._environment.stat(self._scope, path, self._guard)

    def edit(self, path, old_text, new_text, if_match, **kwargs):
        self._allowed(path, write=True)
        return self._environment.edit(
            self._scope, path, old_text, new_text, if_match, self._guard, **kwargs
        )

    def patch(self, path, unified_diff, if_match):
        self._allowed(path, write=True)
        return self._environment.patch(self._scope, path, unified_diff, if_match, self._guard)

    def move(self, source, destination, if_match):
        self._allowed(source, write=True)
        self._allowed(destination, write=True)
        return self._environment.move(self._scope, source, destination, if_match, self._guard)

    def delete(self, path, if_match, **kwargs):
        self._allowed(path, write=True)
        return self._environment.delete(self._scope, path, if_match, self._guard, **kwargs)

    def glob(self, pattern, root=".", **kwargs):
        self._allowed(root)
        return self._environment.glob(self._scope, pattern, root, self._guard, **kwargs)


class FileToolAdapter:
    async def invoke(self, member, arguments, context):
        return getattr(context.workspace, member)(**arguments)


def file_tools(adapter_key="files"):
    string = {"type": "string"}
    definitions = {
        "read": (
            {
                "path": string,
                "offset": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 65536},
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
    return CapabilityRegistration(
        adapter_key,
        FileToolAdapter(),
        tuple(
            CapabilitySpec(
                key,
                key,
                "Workspace " + key,
                {
                    "type": "object",
                    "properties": props,
                    "required": required,
                    "additionalProperties": False,
                },
                "builtin_tool",
            )
            for key, (props, required) in definitions.items()
        ),
    )


class ScriptToolAdapter:
    """Delegates to an explicitly supplied isolation/fencing backend, never to a host shell."""

    def __init__(self, gateway):
        if not callable(getattr(gateway, "execute", None)) or not callable(
            getattr(gateway, "revoke", None)
        ):
            from ..errors import ConfigurationError

            raise ConfigurationError("SANDBOX_GATEWAY_REQUIRED")
        self.gateway = gateway

    async def invoke(self, member, arguments, context):
        import asyncio

        from ..errors import InfrastructureError

        environment = context.workspace._environment
        if (
            not getattr(environment, "process_isolation", False)
            or getattr(environment, "sandbox_gateway", None) is not self.gateway
        ):
            raise AgentLoomError(
                "SANDBOX_ENVIRONMENT_REQUIRED",
                "Script execution requires an isolated EnvironmentProvider",
            )
        context.check_active()
        try:
            return await self.gateway.execute(arguments, context)
        except asyncio.CancelledError:
            receipt = await self.gateway.revoke(context)
            if not receipt or not receipt.get("revoked"):
                raise InfrastructureError("ENVIRONMENT_REVOCATION_UNCONFIRMED")
            raise
