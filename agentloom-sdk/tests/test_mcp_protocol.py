import json
import sys
from pathlib import Path

import pytest

from agentloom import (
    AgentDefinition,
    CapabilityBinding,
    CapabilityRegistration,
    CapabilityRegistry,
    CapabilitySpec,
    ExecutionPolicy,
    McpToolAdapter,
    ModelResponse,
    RetryPolicy,
    RuntimeEngine,
    ToolCall,
)


@pytest.mark.parametrize(
    "mode,code,call_count",
    [
        ("success", None, 1),
        ("business_error", "MCP_TOOL_ERROR", 1),
        ("schema_drift", "MCP_CATALOG_MISMATCH", 0),
    ],
)
async def test_real_mcp_stdio_handshake_catalog_and_call(build, tmp_path, mode, code, call_count):
    pytest.importorskip("mcp")
    audit = tmp_path / "mcp.jsonl"
    adapter = McpToolAdapter(
        command=sys.executable,
        args=[str(Path(__file__).parent / "support/mcp_process.py"), str(audit)],
    )
    discovered = await adapter.discover()
    assert discovered[0]["name"] == "echo"
    schema = (
        {"type": "object", "required": ["fail"]}
        if mode == "schema_drift"
        else discovered[0]["inputSchema"]
    )
    spec = CapabilitySpec("remote_echo", "echo", "Echo", schema, "mcp")

    def model(request):
        feedback = [m for m in request.messages if m["role"] == "tool"]
        if feedback:
            response = json.loads(feedback[-1]["content"])
            if code:
                assert response["error"]["code"] == code
            else:
                assert response["result"] == {"echo": {"fail": False}}
            return ModelResponse("done")
        return ModelResponse(tool_calls=(ToolCall("remote", {"fail": mode == "business_error"}),))

    async with RuntimeEngine() as runtime:
        agent = build(
            runtime,
            model,
            definition=AgentDefinition(
                "a",
                "SYSTEM",
                capabilities=(
                    CapabilityBinding("remote", "mcp", "remote", "server", "remote_echo"),
                ),
            ),
            capabilities=CapabilityRegistry(
                tools=(CapabilityRegistration("server", adapter, (spec,)),)
            ),
            execution_policy=ExecutionPolicy(retry=RetryPolicy(True, 3, 0)),
        )
        result = await agent.ainvoke("question")
        assert result.status == "succeeded", result.error
        op = next(
            o
            for o in runtime._load(result.run_id)["operations"].values()
            if o["event_type"] == "capability.invoke"
        )
        assert len(op["attempts"]) == 1
        if mode == "business_error":
            assert op["raw"]["result"]["isError"] is True
        messages = [json.loads(line) for line in audit.read_text().splitlines()]
        calls = [m for m in messages if m["method"] == "tools/call"]
        assert len(calls) == call_count
        if calls:
            assert calls[0]["params"]["_meta"]["agentloom/operation_id"] == op["operation_id"]
