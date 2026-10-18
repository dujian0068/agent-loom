"""Independent latest-workspace checks of candidate correction and prompt recovery.

Run explicitly: .venv/bin/python -m pytest -q agentloom-sdk/qa/test_sdk_review_round7.py
No live provider or credential is used. Existing QA and production files are untouched.
"""

import json

import pytest

from agentloom import (
    AgentDefinition,
    ExecutionPolicy,
    FileRuntimeStore,
    InfrastructureError,
    MemoryRuntimeStore,
    ModelResponse,
    PlanAgent,
    ReactAgent,
    RetryPolicy,
    RuntimeEngine,
    ScriptedModel,
    ToolCall,
    WorkspaceBinding,
)
from agentloom.configuration import json_copy
from agentloom.prompts import templates
from agentloom.runtime.recovery import validate_recovery


@pytest.fixture(autouse=True)
def isolated_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)


@pytest.fixture(params=["memory", "file"])
def store(request, tmp_path):
    value = (
        MemoryRuntimeStore() if request.param == "memory" else FileRuntimeStore(tmp_path / "state")
    )
    yield value
    value.close()


def build(runtime, role, schema, responder):
    definition = AgentDefinition(
        "review7",
        "Return the requested deliverable.",
        "Complete the assigned task",
        output_schema=schema,
    )
    options = {
        "runtime_engine": runtime,
        "workspace": WorkspaceBinding("workspace"),
        "execution_policy": ExecutionPolicy(
            loop_max=6 if role == "executor" else 2,
            run_timeout_seconds=15,
            retry=RetryPolicy(enabled=True, max_attempts=3, delay_seconds=0),
        ),
    }
    if role == "executor":

        def dispatch(request):
            if request.role_key == "worker":
                return responder(request)
            if not any(m["role"] == "tool" for m in request.messages):
                return ModelResponse(
                    tool_calls=(ToolCall("delegate_task", {"executor": "worker", "task": "go"}),)
                )
            return ModelResponse("planner done")

        return PlanAgent(
            definition=AgentDefinition("planner", "Delegate the task, then review its result."),
            executors={"worker": definition},
            model=ScriptedModel(dispatch),
            **options,
        )
    cls = ReactAgent if role == "react" else PlanAgent
    return cls(definition=definition, model=ScriptedModel(responder), **options)


def target(state, role):
    if role == "executor":
        return next(t for t in state["tasks"].values() if t["role_key"] == "worker")
    return state["tasks"][state["root_task_id"]]


def target_operations(state, task):
    return [
        op
        for op in state["operations"].values()
        if op["event_type"] == "model.generate" and op["task_id"] == task["task_id"]
    ]


@pytest.mark.parametrize("role", ["react", "planner", "executor"])
@pytest.mark.parametrize(
    "candidate,corrected,schema",
    [
        pytest.param(
            "[" * 1100 + "null" + "]" * 1100, "null", {"type": "null"}, id="parse-depth-null"
        ),
        pytest.param("9" * 5000, "0", {"type": "integer"}, id="integer-digits-zero"),
        pytest.param("-Infinity", "false", {"type": "boolean"}, id="negative-infinity-false"),
        pytest.param("{} {}", '""', {"type": "string"}, id="trailing-json-empty-string"),
    ],
)
async def test_parser_extremes_can_correct_to_falsy_json(store, role, candidate, corrected, schema):
    """Parser rejection is correctable; null/false/zero/empty final values are deliverable."""
    requests = []

    def respond(request):
        requests.append(request)
        return ModelResponse(candidate if len(requests) == 1 else corrected)

    async with RuntimeEngine(store=store) as runtime:
        agent = build(runtime, role, schema, respond)
        result = await agent.ainvoke("go")
        state = runtime._load(result.run_id)
        task = target(state, role)
        records = task["context"]["records"]
        operations = target_operations(state, task)
        print(
            json.dumps(
                {
                    "run_id": result.run_id,
                    "role": role,
                    "status": task["status"],
                    "calls": len(requests),
                }
            )
        )
        assert result.status == task["status"] == "succeeded"
        assert task["result"] == json.loads(corrected)
        assert type(task["result"]) is type(json.loads(corrected))
        assert len(requests) == len(operations) == 2
        assert all(len(op["attempts"]) == 1 for op in operations)
        assert sum(r["kind"] == "format" for r in records) == 1
        assert any(r["message"] == {"role": "assistant", "content": candidate} for r in records)
        if role != "executor":
            assert result.usage.loops_consumed == 2 and result.usage.loops_reserved == 0
            value = json_copy(result.output.value)
            assert value == json.loads(corrected) and type(value) is type(json.loads(corrected))
        assert result.usage.compaction_model_attempts == 0
        expected = state["prompt_contract"]["roles"][task["role_key"]]["content"]
        assert all(request.messages[1]["content"] == expected for request in requests)
        validate_recovery(state)


@pytest.mark.parametrize("role", ["react", "planner", "executor"])
async def test_resume_after_saved_correction_keeps_prompt_and_does_not_replay_answer(
    store, role, monkeypatch
):
    """Inject an observer failure after the correction commit, then reopen the runtime."""
    requests = []

    def respond(request):
        requests.append(request)
        return ModelResponse("[" * 1100 + "null" + "]" * 1100 if len(requests) == 1 else "42")

    def checkpoint(command, state):
        if command.startswith("correction:"):
            raise RuntimeError("review7: observer failed after durable correction")

    async with RuntimeEngine(store=store, checkpoint_callback=checkpoint) as runtime:
        handle = await build(runtime, role, {"type": "integer"}, respond).start(
            "go", run_id="correction"
        )
        with pytest.raises(InfrastructureError):
            await handle.wait(timeout_s=5)
        state = runtime._load(handle.run_id)
        task = target(state, role)
        saved_prompt = state["prompt_contract"]
        assert task["stage"] == "prepare" and len(requests) == 1
        assert sum(r["kind"] == "format" for r in task["context"]["records"]) == 1
        original_operation = target_operations(state, task)[0]["operation_id"]

    marker = "REVIEW7_FUTURE_TEMPLATE"
    monkeypatch.setattr(templates, "COMMON", templates.COMMON + " " + marker)
    monkeypatch.setattr(templates, "TEMPLATE_VERSION", "review7-future")
    async with RuntimeEngine(store=store) as runtime:
        resumed = await build(runtime, role, {"type": "integer"}, respond).ainvoke(
            run_id="correction"
        )
        state = runtime._load(resumed.run_id)
        task = target(state, role)
        operations = target_operations(state, task)
        assert resumed.status == task["status"] == "succeeded" and task["result"] == 42
        assert len(requests) == len(operations) == 2
        assert original_operation in {op["operation_id"] for op in operations}
        assert all(len(op["attempts"]) == 1 for op in operations)
        assert sum(r["kind"] == "format" for r in task["context"]["records"]) == 1
        assert state["prompt_contract"] == saved_prompt
        assert marker not in requests[-1].messages[1]["content"]
        validate_recovery(state)
        fresh = await build(runtime, role, {"type": "integer"}, respond).ainvoke("new task")
        assert fresh.status == "succeeded" and len(requests) == 3
        assert marker in requests[-1].messages[1]["content"]
        print(
            json.dumps(
                {
                    "role": role,
                    "resumed_run": resumed.run_id,
                    "fresh_run": fresh.run_id,
                    "target_calls": len(requests),
                }
            )
        )


@pytest.mark.parametrize("role", ["react", "planner", "executor"])
@pytest.mark.parametrize(
    "candidate", ["NaN", "[" * 1100 + "null" + "]" * 1100], ids=["nan-text", "deep-text"]
)
async def test_no_output_schema_preserves_json_looking_text(store, role, candidate):
    """The structured-output repair must not parse ordinary text answers as JSON."""
    requests = []

    def respond(request):
        requests.append(request)
        return ModelResponse(candidate)

    async with RuntimeEngine(store=store) as runtime:
        result = await build(runtime, role, None, respond).ainvoke("Quote the requested text")
        state = runtime._load(result.run_id)
        task = target(state, role)
        assert result.status == task["status"] == "succeeded"
        assert task["result"] == candidate and len(requests) == 1
        assert not any(r["kind"] == "format" for r in task["context"]["records"])
        if role != "executor":
            assert result.output.kind == "text" and result.output.value == candidate
        validate_recovery(state)
