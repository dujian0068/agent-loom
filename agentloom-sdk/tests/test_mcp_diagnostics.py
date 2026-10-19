"""Known MCP protocol failures retain their original exception cause."""

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from agentloom import AgentLoomError, McpToolAdapter
from agentloom.capabilities.invocation import normalize_error_completion


@pytest.mark.parametrize("callback", [False, True])
@pytest.mark.parametrize("malformed", ["nan", "circular", "dump_error"])
async def test_mcp_result_cause_survives_transport_cleanup(callback, malformed):
    def dump(**kwargs):
        if malformed == "dump_error":
            raise ValueError("invalid serialization")
        value = {"x": float("nan")}
        if malformed == "circular":
            value["x"] = value
        return {"structuredContent": value}

    class Adapter(McpToolAdapter):
        @asynccontextmanager
        async def _session(self):
            yield self

        async def list_tools(self, cursor=None):
            return SimpleNamespace(
                nextCursor=None,
                tools=[SimpleNamespace(name="x", inputSchema={}, outputSchema=None)],
            )

        async def call_tool(self, *args, **kwargs):
            return SimpleNamespace(isError=False, model_dump=dump)

    completions = []

    def complete(*, error):
        completions.append(error)
        raise error

    context = SimpleNamespace(
        check_active=lambda: None,
        spec=SimpleNamespace(name="x", input_schema={}, output_schema=None),
        operation_id="op",
    )
    if callback:
        context.report_completion = complete
    with pytest.raises(AgentLoomError, match="MCP_PROTOCOL_ERROR") as caught:
        await Adapter(url="http://example.invalid").invoke("x", {}, context)
    assert caught.value.__cause__ is not None and caught.value.__cause__ is not caught.value
    assert caught.value.external_outcome == "known" and caught.value.execution_fact == "completed"
    assert bool(completions) == callback


async def test_group_unwrap_retains_leaf_cause_without_a_cycle():
    original = ValueError("source")
    leaf = AgentLoomError("KNOWN_FAILURE")
    leaf.__cause__ = original

    class Adapter(McpToolAdapter):
        @asynccontextmanager
        async def _session(self):
            raise ExceptionGroup("transport", [leaf])
            yield

    with pytest.raises(AgentLoomError, match="KNOWN_FAILURE") as caught:
        await Adapter(url="http://example.invalid").invoke(
            "x", {}, SimpleNamespace(check_active=lambda: None)
        )
    assert caught.value is leaf and leaf.__cause__ is original


def test_reported_error_contract_still_rejects_plain_exceptions():
    with pytest.raises(AgentLoomError, match="CAPABILITY_RESULT_INVALID"):
        normalize_error_completion(ValueError("not an OperationError"))
    assert normalize_error_completion(AgentLoomError("VALID")).envelope["error"]["code"] == "VALID"


@pytest.mark.parametrize("duplicate", [False, True])
async def test_catalog_mismatch_uses_same_conflict_category(duplicate):
    tool = SimpleNamespace(name="x", inputSchema={}, outputSchema=None)

    class Adapter(McpToolAdapter):
        @asynccontextmanager
        async def _session(self):
            yield self

        async def list_tools(self, cursor=None):
            return SimpleNamespace(nextCursor=None, tools=[tool, tool] if duplicate else [])

    context = SimpleNamespace(
        check_active=lambda: None,
        spec=SimpleNamespace(name="x", input_schema={}, output_schema=None),
    )
    with pytest.raises(AgentLoomError, match="MCP_CATALOG_MISMATCH") as caught:
        await Adapter(url="http://example.invalid").invoke("x", {}, context)
    assert caught.value.category == "conflict" and caught.value.external_outcome == "not_started"
