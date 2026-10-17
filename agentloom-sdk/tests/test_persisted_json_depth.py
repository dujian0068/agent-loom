"""Accepted boundary values survive SDK wrapping, persistence and recovery."""

import json

import pytest
import rfc8785
from conftest import binding

from agentloom import (
    AgentDefinition,
    CapabilityRegistration,
    CapabilityRegistry,
    CapabilitySpec,
    ConfigurationError,
    FunctionHookHandler,
    FunctionToolAdapter,
    HookBinding,
    HookDecision,
    HookSet,
    ModelResponse,
    RuntimeEngine,
    ToolCall,
)
from agentloom.configuration import canonical, freeze, json_copy
from agentloom.configuration.json_documents import (
    MAX_DOCUMENT_DEPTH,
    canonical_document,
    copy_document,
    freeze_document,
)
from agentloom.configuration.json_values import MAX_JSON_DEPTH
from agentloom.runtime.recovery import validate_recovery


def nested(depth, shape="list"):
    value = None
    for index in range(depth):
        value = {"child": value} if shape == "dict" or shape == "mixed" and index % 2 else [value]
    return value


@pytest.mark.parametrize("shape", ["list", "dict", "mixed"])
@pytest.mark.parametrize("convert", [json_copy, freeze, canonical])
def test_material_limit_counts_containers_and_is_exact(convert, shape):
    assert convert(nested(MAX_JSON_DEPTH, shape)) is not None
    with pytest.raises(ConfigurationError, match="supported depth of 128"):
        convert(nested(MAX_JSON_DEPTH + 1, shape))


@pytest.mark.parametrize("convert", [copy_document, freeze_document, canonical_document])
def test_document_limit_is_iterative_and_separate_from_material_budget(convert):
    assert convert(nested(MAX_DOCUMENT_DEPTH)) is not None
    with pytest.raises(ConfigurationError, match="supported depth of 512"):
        convert(nested(MAX_DOCUMENT_DEPTH + 1))


@pytest.mark.parametrize("shape", ["list", "dict", "mixed"])
def test_exact_material_limit_can_be_embedded_in_storage(runtime_store, shape):
    material = json_copy(nested(MAX_JSON_DEPTH, shape))
    record = {"attempt": {"result": material}, "audit": {"value": material}}
    with runtime_store.transaction("depth") as tx:
        tx.put("records", "one", record)
    record["attempt"]["result"] = "mutated"
    with runtime_store.transaction("depth") as tx:
        saved = tx.get("records", "one")
        listed = tx.list("records")
    assert canonical_document(saved) == canonical_document(listed[0])
    assert canonical_document(saved["attempt"]["result"]) == canonical(material)
    saved["attempt"]["result"] = "mutated again"
    with runtime_store.transaction("depth") as tx:
        assert tx.get("records", "one")["attempt"]["result"] != "mutated again"


def test_iterative_canonical_encoding_keeps_existing_rfc8785_hashes():
    # Unicode keys exercise UTF-16 ordering; floats exercise the library codec.
    value = {
        "\ue000": [1e-27, 1e30, -0.0, 333333333.33333329],
        "😀": {"z": '\n\t\\"', "a": [None, True, False, [], {}]},
        "é": [1, -2, 9007199254740991],
    }
    assert canonical(value).encode() == rfc8785.dumps(value)
    assert json.loads(canonical(value)) == value


@pytest.mark.parametrize("kind", ["function", "legacy", "native", "callback"])
@pytest.mark.parametrize("over_limit", [False, True])
async def test_exact_tool_envelope_limit_survives_hooks_and_restart(
    runtime_store, build, kind, over_limit
):
    depth = MAX_JSON_DEPTH - 4 + over_limit
    calls, observed = [], []

    def result():
        calls.append(1)
        return nested(depth)

    class Legacy:
        async def invoke(self, member, arguments, context):
            return result()

    class Native:
        async def invoke_once(self, request, context):
            envelope = {
                "status": "succeeded",
                "error": None,
                "metadata": {},
                "artifacts": [],
                "content": [{"type": "json", "json_ref": {"kind": "inline", "value": result()}}],
            }
            if kind == "callback":
                context.report_completion(envelope)
            return envelope

    async def hook(event, context):
        observed.append(await context.reader.read_payload())
        return HookDecision()

    adapter = (
        FunctionToolAdapter(lambda args, ctx: result())
        if kind == "function"
        else Legacy()
        if kind == "legacy"
        else Native()
    )

    def make(runtime):
        return build(
            runtime,
            lambda request: (
                ModelResponse("done")
                if any(item["role"] == "tool" for item in request.messages)
                else ModelResponse(tool_calls=(ToolCall("work", {}),))
            ),
            definition=AgentDefinition("depth", "SYSTEM", capabilities=(binding("work"),)),
            capabilities=CapabilityRegistry(
                tools=(CapabilityRegistration("work", adapter, (CapabilitySpec("work", "work"),)),)
            ),
            hooks=HookSet(
                (HookBinding("observe", "capability.invoke", "response", "observe"),),
                {"observe": FunctionHookHandler(hook)},
            ),
        )

    async with RuntimeEngine(store=runtime_store) as runtime:
        first = await make(runtime).ainvoke("go", session_id="depth")
        assert first.status == "succeeded" and len(calls) == len(observed) == 1
        state = runtime._load(first.run_id)
        validate_recovery(state)
        op = next(
            op for op in state["operations"].values() if op["event_type"] == "capability.invoke"
        )
        raw = op["raw"]
        assert raw["execution_fact"] == "completed"
        if over_limit:
            assert raw["error"]["code"] == "CAPABILITY_RESULT_INVALID"
            assert raw["result"] is None
            assert "invalid_tool_result" not in op["attempts"][0]
        else:
            assert raw["status"] == "succeeded"
            assert canonical(raw["result"]) == canonical(nested(depth))
            assert canonical_document(observed[0]["result"]) == canonical(nested(depth))
    async with RuntimeEngine(store=runtime_store) as runtime:
        resumed = await make(runtime).ainvoke(None, run_id=first.run_id, session_id="depth")
        assert resumed.status == "succeeded" and len(calls) == 1
        next_turn = await make(runtime).ainvoke("next", session_id="depth")
        assert next_turn.status == "succeeded"
