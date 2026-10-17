"""Independent assessment probes; inject local protocol facts, use no remote service."""

import asyncio
import json
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
from jsonschema import Draft202012Validator

from agentloom import (
    AgentDefinition,
    CapabilityBinding,
    CapabilityRegistration,
    CapabilityRegistry,
    CapabilitySpec,
    ConfigurationError,
    ExecutionPolicy,
    FileRuntimeStore,
    McpToolAdapter,
    MemoryRuntimeStore,
    ModelResponse,
    ReactAgent,
    RetryPolicy,
    RuntimeEngine,
    ScriptedModel,
    ToolCall,
    WorkspaceBinding,
)
from agentloom.configuration import check_schema
from agentloom.models.http import http_error


def schema_controls():
    literal = {"$ref": "https://example.invalid/resource"}
    cases = [
        ({"enum": [literal]}, literal),
        ({"const": literal}, literal),
        ({"type": "object", "properties": {"$ref": {"type": "string"}}}, literal),
    ]
    results = []
    for schema, value in cases:
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(value)
        try:
            check_schema(schema)
        except ConfigurationError as exc:
            result = {"schema": schema, "reference_validator": "accepted", "sdk": exc.code}
        else:
            result = {"schema": schema, "reference_validator": "accepted", "sdk": "accepted"}
        results.append(result)
    return results


class InjectedMcp(McpToolAdapter):
    """Use the production invoke/error path; replace only the external session."""

    def __init__(self):
        super().__init__(url="https://example.invalid/mcp")
        self.times = []

    @asynccontextmanager
    async def _session(self):
        yield self

    async def list_tools(self, cursor=None):
        return SimpleNamespace(
            tools=[
                SimpleNamespace(name="probe", inputSchema={"type": "object"}, outputSchema=None)
            ],
            nextCursor=None,
        )

    async def call_tool(self, name, arguments, meta=None):
        self.times.append(time.monotonic())
        if len(self.times) == 1:
            response = httpx.Response(
                429,
                headers={"Retry-After": "1"},
                request=httpx.Request("POST", self.url),
            )
            response.raise_for_status()
        return SimpleNamespace(
            isError=False,
            model_dump=lambda **kwargs: {"structuredContent": {"receipt": "ok"}},
        )


async def mcp_retry_probe(backend, directory):
    adapter = InjectedMcp()
    store = MemoryRuntimeStore() if backend == "memory" else FileRuntimeStore(directory / "state")

    def model(request):
        if any(message["role"] == "tool" for message in request.messages):
            return ModelResponse("done")
        return ModelResponse(tool_calls=(ToolCall("probe", {}),))

    try:
        async with RuntimeEngine(store=store) as runtime:
            agent = ReactAgent(
                definition=AgentDefinition(
                    "probe-agent",
                    "SYSTEM",
                    capabilities=(CapabilityBinding("probe", "mcp", "probe", "probe"),),
                ),
                model=ScriptedModel(model),
                runtime_engine=runtime,
                workspace=WorkspaceBinding(str(directory / "workspace")),
                capabilities=CapabilityRegistry(
                    tools=(
                        CapabilityRegistration(
                            "probe",
                            adapter,
                            (CapabilitySpec("probe", "probe", "probe", kind="mcp"),),
                        ),
                    )
                ),
                execution_policy=ExecutionPolicy(
                    run_timeout_seconds=10,
                    retry=RetryPolicy(enabled=True, max_attempts=2, delay_seconds=0),
                ),
            )
            result = await (await agent.start("question")).wait(timeout_s=4)
            op = next(
                op
                for op in runtime._load(result.run_id)["operations"].values()
                if op["event_type"] == "capability.invoke"
            )
            assert len(adapter.times) == 2
            return {
                "backend": backend,
                "run_status": result.status,
                "elapsed_between_calls_seconds": adapter.times[1] - adapter.times[0],
                "operation_status": op["status"],
                "attempts": op["attempts"],
            }
    finally:
        store.close()


async def main():
    evidence = {"schema_controls": schema_controls()}
    positive = http_error(429, httpx.Headers({"Retry-After": "1"}))
    evidence["shared_http_parser_retains_retry_after"] = positive.retry_after_at is not None
    with tempfile.TemporaryDirectory(prefix="agentloom-assessment-probe-") as temporary:
        root = Path(temporary)
        evidence["mcp_retry"] = [
            await mcp_retry_probe(backend, root / backend) for backend in ("memory", "file")
        ]
    print(json.dumps(evidence, ensure_ascii=False, indent=2))


asyncio.run(main())
