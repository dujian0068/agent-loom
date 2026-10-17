"""Exit inside resource teardown, after a response but before invoke returns."""

import asyncio
import json
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

from agentloom import (
    AgentDefinition,
    AgentLoomError,
    CapabilityBinding,
    CapabilityRegistration,
    CapabilityRegistry,
    CapabilitySpec,
    ExecutionPolicy,
    FileRuntimeStore,
    McpToolAdapter,
    ModelDescriptor,
    ModelResponse,
    ProviderEvent,
    ReactAgent,
    RetryPolicy,
    RuntimeEngine,
    ScriptedModel,
    ToolCall,
    WorkspaceBinding,
)


async def main():
    root, kind, phase = Path(sys.argv[1]), sys.argv[2], sys.argv[3]

    def audit(value):
        with (root / "audit").open("a") as file:
            file.write(value + "\n")
            file.flush()
            os.fsync(file.fileno())

    def close():
        if phase == "crash":
            os._exit(74)

    class Model:
        descriptor = ModelDescriptor("completion-process", 32768)

        async def stream_once(self, request, context):
            audit("model")
            try:
                yield ProviderEvent("usage", {"input_tokens": 3, "output_tokens": 2})
                yield ProviderEvent("completed", ModelResponse("saved").to_dict())
            finally:
                close()

    class Mcp(McpToolAdapter):
        @asynccontextmanager
        async def _session(self):
            try:
                yield self
            finally:
                close()

        async def list_tools(self, cursor=None):
            return SimpleNamespace(
                nextCursor=None,
                tools=[
                    SimpleNamespace(name="work", inputSchema={"type": "object"}, outputSchema=None)
                ],
            )

        async def call_tool(self, name, arguments, meta=None):
            audit("tool")
            return SimpleNamespace(
                isError=kind == "mcp-error",
                model_dump=lambda **kwargs: {
                    "structuredContent": {"receipt": "saved"},
                    "isError": kind == "mcp-error",
                },
            )

    class Native:
        async def invoke_once(self, request, context):
            audit("tool")
            try:
                if kind.startswith("native-depth"):
                    from agentloom.configuration import json_values

                    # Four containers belong to the ToolResult envelope.
                    depth = int(
                        os.environ.get(
                            "COMPLETION_PROBE_DEPTH",
                            getattr(json_values, "MAX_JSON_DEPTH", 128) - 4,
                        )
                    )
                    if kind == "native-depth-invalid":
                        depth += 1
                    value = None
                    for _ in range(depth):
                        value = [value]
                    context.report_completion(
                        {
                            "status": "succeeded",
                            "content": [
                                {"type": "json", "json_ref": {"kind": "inline", "value": value}}
                            ],
                            "error": None,
                            "metadata": {},
                            "artifacts": [],
                        }
                    )
                    return
                context.report_completion(
                    error=AgentLoomError(
                        "KNOWN_RESPONSE",
                        category="transport",
                        retryable_transport=True,
                        retry_after_at=float("nan") if kind == "native-invalid" else None,
                    )
                )
            finally:
                close()

    def model(request):
        audit("model")
        return (
            ModelResponse("done")
            if any(item["role"] == "tool" for item in request.messages)
            else ModelResponse(tool_calls=(ToolCall("work", {}),))
        )

    options = {"definition": AgentDefinition("test", "SYSTEM"), "model": Model()}
    if kind != "model":
        capability_kind = "tool" if kind.startswith("native-") else "mcp"
        options = {
            "definition": AgentDefinition(
                "test",
                "SYSTEM",
                capabilities=(CapabilityBinding("work", capability_kind, "work", "a"),),
            ),
            "model": ScriptedModel(model),
            "capabilities": CapabilityRegistry(
                tools=(
                    CapabilityRegistration(
                        "a",
                        Native()
                        if capability_kind == "tool"
                        else Mcp(url="https://example.invalid/mcp"),
                        (CapabilitySpec("work", "work", kind=capability_kind),),
                    ),
                )
            ),
        }
    store = FileRuntimeStore(root / "state")
    try:
        async with RuntimeEngine(store=store, lease_seconds=0.3) as runtime:
            agent = ReactAgent(
                **options,
                workspace=WorkspaceBinding(root / "workspace"),
                runtime_engine=runtime,
                execution_policy=ExecutionPolicy(
                    run_timeout_seconds=15,
                    retry=RetryPolicy(enabled=True, max_attempts=2, delay_seconds=0),
                ),
            )
            result = await agent.ainvoke("go" if phase == "crash" else None, run_id="fixed")
            print(json.dumps({"status": result.status, "output": result.output.value}))
    finally:
        store.close()


asyncio.run(main())
