"""Independent sixth-round checks of output correction and error sidecars.

Run explicitly: .venv/bin/python -m pytest -q agentloom-sdk/qa/test_sdk_review_round6.py
Assertions describe the documented outcome; defects are not skipped or xfailed.
All model responses are local, deterministic fixtures. No provider key is used.
"""

import json

import pytest

from agentloom import (
    AgentDefinition,
    AgentLoomError,
    CapabilityBinding,
    CapabilityRegistration,
    CapabilityRegistry,
    CapabilitySpec,
    ExecutionPolicy,
    FileRuntimeStore,
    InfrastructureError,
    MemoryRuntimeStore,
    ModelResponse,
    PlanAgent,
    ReactAgent,
    RuntimeEngine,
    ScriptedModel,
    ToolCall,
    WorkspaceBinding,
)
from agentloom.configuration import json_copy


@pytest.fixture(autouse=True)
def isolated_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)


@pytest.fixture(params=["memory", "file"])
def store(request, tmp_path):
    backend = (
        MemoryRuntimeStore() if request.param == "memory" else FileRuntimeStore(tmp_path / "state")
    )
    yield backend
    backend.close()


def build_output_agent(runtime, role, schema, responder, *, prompt="SYSTEM"):
    definition = AgentDefinition(
        "target", prompt, "Return a validated result", output_schema=schema
    )
    options = {
        "runtime_engine": runtime,
        "workspace": WorkspaceBinding("workspace"),
        "execution_policy": ExecutionPolicy(loop_max=8, run_timeout_seconds=15),
    }
    if role == "executor":

        def dispatch(request):
            if request.role_key == "worker":
                return responder(request)
            if not any(message["role"] == "tool" for message in request.messages):
                return ModelResponse(
                    tool_calls=(ToolCall("delegate_task", {"executor": "worker", "task": "go"}),)
                )
            return ModelResponse("planner done")

        return PlanAgent(
            definition=AgentDefinition("planner", "Delegate to the worker"),
            executors={"worker": definition},
            model=ScriptedModel(dispatch),
            **options,
        )
    cls = ReactAgent if role == "react" else PlanAgent
    return cls(definition=definition, model=ScriptedModel(responder), **options)


def target_task(state, role):
    if role != "executor":
        return state["tasks"][state["root_task_id"]]
    return next(task for task in state["tasks"].values() if task["role_key"] == "worker")


def describe_task(state, role, requests):
    task = target_task(state, role)
    return {
        "run_id": state["run_id"],
        "role": role,
        "run_status": state["status"],
        "task_status": task["status"],
        "task_error": task["error"],
        "target_model_calls": len(requests),
        "format_feedback_count": sum(
            entry["kind"] == "format" for entry in task["context"]["records"]
        ),
    }


@pytest.mark.parametrize("role", ["react", "planner", "executor"])
@pytest.mark.parametrize(
    "candidate",
    [
        pytest.param("NaN", id="nan"),
        pytest.param("1e309", id="float-overflow"),
        pytest.param("9007199254740992", id="unsafe-integer"),
        pytest.param('"\\ud800"', id="unpaired-surrogate"),
        pytest.param("[" * 129 + "null" + "]" * 129, id="depth-129"),
    ],
)
async def test_noncanonical_final_candidate_uses_format_correction(store, role, candidate):
    """capabilities.md §1.3: invalid output gets another business model decision."""
    requests = []

    def respond(request):
        requests.append(request)
        return ModelResponse(candidate if len(requests) == 1 else "42")

    async with RuntimeEngine(store=store) as runtime:
        result = await build_output_agent(runtime, role, {}, respond).ainvoke("go")
        state = runtime._load(result.run_id)
        task = target_task(state, role)
        print(json.dumps(describe_task(state, role, requests)))
        assert task["status"] == "succeeded", (
            "Invalid candidate data must be corrected, not treated as a broken schema"
        )
        assert task["result"] == 42 and len(requests) == 2
        assert any(entry["kind"] == "format" for entry in task["context"]["records"])
        operations = [
            op
            for op in state["operations"].values()
            if op["event_type"] == "model.generate" and op["task_id"] == task["task_id"]
        ]
        assert len(operations) == 2 and all(len(op["attempts"]) == 1 for op in operations)
        assert result.status == "succeeded"


@pytest.mark.parametrize("role", ["react", "planner", "executor"])
@pytest.mark.parametrize("candidate", ["not-json", "[]"], ids=["syntax", "schema-mismatch"])
async def test_ordinary_bad_output_still_corrects_as_control(store, role, candidate):
    requests = []

    def respond(request):
        requests.append(request)
        return ModelResponse(candidate if len(requests) == 1 else "42")

    async with RuntimeEngine(store=store) as runtime:
        result = await build_output_agent(runtime, role, {"type": "number"}, respond).ainvoke("go")
        state = runtime._load(result.run_id)
        task = target_task(state, role)
        print(json.dumps(describe_task(state, role, requests)))
        assert task["status"] == "succeeded" and task["result"] == 42
        assert len(requests) == 2
        assert sum(entry["kind"] == "format" for entry in task["context"]["records"]) == 1


@pytest.mark.parametrize("role", ["react", "planner"])
@pytest.mark.parametrize(
    "candidate",
    [
        pytest.param("null", id="null"),
        pytest.param("false", id="false"),
        pytest.param("0", id="zero"),
        pytest.param('""', id="empty-text"),
        pytest.param("[]", id="empty-list"),
        pytest.param("{}", id="empty-object"),
        pytest.param("9007199254740991", id="max-safe-integer"),
        pytest.param("[" * 128 + "null" + "]" * 128, id="depth-128"),
    ],
)
async def test_valid_empty_and_limit_outputs_keep_mode_prompt(store, role, candidate):
    """Empty business text and JSON falsy values must not suppress SDK instructions."""
    requests = []

    def respond(request):
        requests.append(request)
        return ModelResponse(candidate)

    async with RuntimeEngine(store=store) as runtime:
        result = await build_output_agent(runtime, role, {}, respond, prompt="").ainvoke("go")
        state = runtime._load(result.run_id)
        task = target_task(state, role)
        assert result.status == "succeeded" and len(requests) == 1
        # Public DTO containers are immutable; compare their JSON projection.
        value = json_copy(result.output.value)
        assert value == json.loads(candidate)
        assert type(value) is type(json.loads(candidate))
        messages = requests[0].messages
        assert messages[0] == {"role": "system", "content": ""}
        assert messages[1]["role"] == "system" and messages[1]["content"].strip()
        assert ("Mode: ReAct." if role == "react" else "Mode: Plan.") in messages[1]["content"]
        instructions = [e for e in task["context"]["records"] if e["kind"] == "sdk_instructions"]
        assert len(instructions) == 1 and instructions[0]["fixed"] and instructions[0]["protected"]
        assert not any(e["kind"] == "format" for e in task["context"]["records"])


def sidecar_value(field, shape):
    if shape == "absent":
        return None
    if shape == "empty-list":
        return []
    if shape == "empty-object":
        return {}
    if shape == "text":
        return "unexpected"
    if field == "invalid_tool_result":
        return {"value": None}
    return {
        "status": "failed",
        "error": AgentLoomError("FAILED").to_dict(),
        "content": [],
        "metadata": {},
        "artifacts": [],
    }


@pytest.mark.parametrize("channel", ["raised", "reported"])
@pytest.mark.parametrize("field", ["tool_result", "invalid_tool_result"])
@pytest.mark.parametrize("shape", ["absent", "empty-list", "empty-object", "text", "valid"])
async def test_error_sidecar_shape_cannot_break_attempt_commit(store, channel, field, shape):
    """A malformed private sidecar must not strand an otherwise valid tool error."""
    calls = []

    class Native:
        async def invoke_once(self, request, context):
            calls.append(1)
            error = AgentLoomError("FAILED")
            setattr(error, field, sidecar_value(field, shape))
            if channel == "reported":
                context.report_completion(error=error)
            raise error

    def respond(request):
        if any(message["role"] == "tool" for message in request.messages):
            return ModelResponse("done")
        return ModelResponse(tool_calls=(ToolCall("work", {}),))

    async with RuntimeEngine(store=store) as runtime:
        agent = ReactAgent(
            definition=AgentDefinition(
                "round6",
                "SYSTEM",
                capabilities=(CapabilityBinding("work", "tool", "work", "work", "work"),),
            ),
            workspace=WorkspaceBinding("workspace"),
            runtime_engine=runtime,
            model=ScriptedModel(respond),
            capabilities=CapabilityRegistry(
                tools=(CapabilityRegistration("work", Native(), (CapabilitySpec("work", "work"),)),)
            ),
            execution_policy=ExecutionPolicy(run_timeout_seconds=15),
        )
        handle = await agent.start("go")
        driver_error = None
        try:
            await handle.wait(timeout_s=5)
        except InfrastructureError as exc:
            driver_error = exc.code
        state = runtime._load(handle.run_id)
        op = next(
            op for op in state["operations"].values() if op["event_type"] == "capability.invoke"
        )
        print(
            json.dumps(
                {
                    "run_id": handle.run_id,
                    "operation_id": op["operation_id"],
                    "channel": channel,
                    "field": field,
                    "shape": shape,
                    "driver_error": driver_error,
                    "run_status": state["status"],
                    "raw": op["raw"],
                    "calls": len(calls),
                }
            )
        )
        assert driver_error is None, "Malformed adapter sidecars must not break host persistence"
        assert state["status"] == "succeeded" and len(calls) == 1
        assert op["status"] == "failed" and op["raw"]["status"] == "failed"
        assert op["raw"]["execution_fact"] == ("unknown" if channel == "raised" else "completed")
        if shape == "valid" and channel == "raised":
            assert field in op["attempts"][0]
