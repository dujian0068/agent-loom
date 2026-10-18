import inspect
from dataclasses import dataclass

from ..errors import AgentLoomError
from ..workspace.schemas import file_definitions
from ..workspace.service import WorkspaceService as WorkspaceService
from .contracts import ValueToolAdapter
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
    report_diagnostic: object = None


class FunctionToolAdapter(ValueToolAdapter):
    """Trusted cooperative functions. CPU/blocking functions should use an isolated adapter."""

    def __init__(self, function):
        self.function = function

    async def invoke(self, member, arguments, context):
        value = self.function(arguments, context)
        return await value if inspect.isawaitable(value) else value


class FileToolAdapter(ValueToolAdapter):
    async def invoke(self, member, arguments, context):
        return getattr(context.workspace, member)(**arguments)


def file_tools(adapter_key="files"):
    definitions = file_definitions()
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


class ScriptToolAdapter(ValueToolAdapter):
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

        if context.workspace.sandbox_gateway is not self.gateway:
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
