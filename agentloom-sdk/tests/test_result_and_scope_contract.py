import asyncio
import json

import pytest
from conftest import binding, tool

from agentloom import (
    AgentDefinition,
    AgentLoomError,
    CapabilityRegistry,
    FunctionHookHandler,
    HookBinding,
    HookDecision,
    HookSet,
    LocalEnvironment,
    MemoryRuntimeStore,
    ModelResponse,
    PlanAgent,
    RuntimeEngine,
    ToolCall,
    WorkspaceBinding,
)
from agentloom.storage import claim


async def test_large_effective_result_preview_and_explicit_slice(build):
    raw = "数据" * 16000 + "RAW_TAIL"
    effective = "数据" * 16000 + "EFFECTIVE_TAIL"
    ref = None

    def model(request):
        nonlocal ref
        feedback = [json.loads(m["content"]) for m in request.messages if m["role"] == "tool"]
        if not feedback:
            return ModelResponse(tool_calls=(ToolCall("echo", {}),))
        if len(feedback) == 1:
            preview = feedback[0]["result"]
            ref = preview["result_ref"]
            assert preview["truncated"] and preview["preview_end"] <= 65536
            assert preview["text"].encode() == effective.encode()[: preview["preview_end"]]
            assert "EFFECTIVE_TAIL" not in json.dumps(request.messages)
            return ModelResponse(
                tool_calls=(
                    ToolCall("result_read", {"result_ref": ref, "offset": 96000, "limit": 1024}),
                )
            )
        assert feedback[-1]["result"]["text"] == "EFFECTIVE_TAIL"
        return ModelResponse("done")

    async with RuntimeEngine() as runtime:
        hooks = HookSet(
            (HookBinding("effective", "capability.invoke", "response", "h"),),
            {
                "h": FunctionHookHandler(
                    lambda e, c: HookDecision(payload_patch={"result": effective})
                )
            },
        )
        agent = build(
            runtime,
            model,
            definition=AgentDefinition("a", "SYSTEM", capabilities=(binding(),)),
            capabilities=CapabilityRegistry(tools=(tool(lambda a, c: raw),)),
            hooks=hooks,
        )
        result = await agent.ainvoke("question")
        assert result.status == "succeeded", result.error
        operations = runtime._load(result.run_id)["operations"]
        assert operations[ref["operation_id"]]["raw"]["result"] == raw
        read = next(o for o in operations.values() if o["event_type"] == "result.read")
        assert read["attempts"] == [] and result.usage.loops_consumed == 3


async def test_planner_cannot_read_executor_private_result_reference(build):
    def model(request):
        feedback = [json.loads(m["content"]) for m in request.messages if m["role"] == "tool"]
        if request.role_key == "worker":
            if not feedback:
                return ModelResponse(tool_calls=(ToolCall("echo", {}),))
            return ModelResponse(json.dumps(feedback[0]["result"]["result_ref"]))
        if not feedback:
            return ModelResponse(
                tool_calls=(ToolCall("delegate_task", {"executor": "worker", "task": "work"}),)
            )
        if len(feedback) == 1:
            return ModelResponse(
                tool_calls=(
                    ToolCall("result_read", {"result_ref": json.loads(feedback[0]["result"])}),
                )
            )
        assert feedback[-1]["error"]["code"] == "RESULT_ACCESS_DENIED"
        return ModelResponse("done")

    async with RuntimeEngine() as runtime:
        result = await build(
            runtime,
            model,
            cls=PlanAgent,
            executors={
                "worker": AgentDefinition("worker", "PRIVATE", "work", capabilities=(binding(),))
            },
            capabilities=CapabilityRegistry(tools=(tool(lambda a, c: "x" * 70000),)),
        ).ainvoke("question")
        assert result.status == "succeeded", result.error


def test_file_pagination_detects_same_length_edit_and_revoked_epoch_cannot_reopen(tmp_path):
    env = LocalEnvironment()
    binding = WorkspaceBinding(tmp_path)
    scope = env.prepare(binding, "s", "r", 1)
    first = env.write(scope, "a", "你好")
    env.write(scope, "b", "b")
    page = env.list_page(scope, limit=1)
    assert page["entries"][0]["etag"] == first["etag"]
    assert env.list_page(scope, cursor=page["next_cursor"])["entries"][0]["name"] == "b"
    env.write(scope, "a", "世界", mode="replace", if_match=first["etag"])
    with pytest.raises(AgentLoomError, match="DIRECTORY_CHANGED"):
        env.list_page(scope, cursor=page["next_cursor"])
    assert env.read(scope, "a", limit=4)["text"] == "世"
    with pytest.raises(AgentLoomError, match="INVALID_OFFSET"):
        env.read(scope, "a", offset=1)
    with pytest.raises(AgentLoomError, match="FILE_SLICE_INVALID"):
        env.read(scope, "a", offset=999)
    env.revoke(scope)
    with pytest.raises(AgentLoomError, match="LEASE_LOST"):
        env.prepare(binding, "s", "r", 1)
    newer = env.prepare(binding, "s", "r", 2)
    assert env.read(newer, "a")["text"] == "世界"


async def test_attached_follower_takes_over_after_owner_lease_expires(build):
    store = MemoryRuntimeStore()
    async with RuntimeEngine(store=store) as runtime:
        handle = await build(runtime, lambda r: ModelResponse("never")).start(
            "question", run_id="follower"
        )
        # No await between acceptance and close: the run is queued, no external call began.
    owner = claim(store, "default", handle.run_id, "missing-owner", 0.08)
    assert owner is not None
    async with RuntimeEngine(store=store, lease_seconds=0.2) as runtime:
        agent = build(runtime, lambda r: ModelResponse("recovered"))
        handle = await agent.start(run_id=handle.run_id)
        await asyncio.sleep(0.02)
        assert not agent.model.requests
        result = await handle.wait(timeout_s=2)
        assert result.output.value == "recovered"
