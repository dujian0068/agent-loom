"""Native action, final-output and replanning contracts of the LangChain adaptations."""

import json
from importlib.resources import files

from conftest import binding, tool

from agentloom import (
    AgentDefinition,
    AgentLoomError,
    CapabilityRegistry,
    ModelResponse,
    PlanAgent,
    RuntimeEngine,
    ToolCall,
)
from agentloom.prompts import templates
from agentloom.runtime.recovery import validate_recovery


def test_prompt_attribution_is_available_offline_in_the_package():
    resources = files("agentloom.prompts")
    metadata = json.loads(resources.joinpath("sources.json").read_text())
    assert metadata["template_version"] == templates.TEMPLATE_VERSION
    assert {s["id"] for s in metadata["sources"]} == {
        "react",
        "native_tool_calls",
        "planner",
        "executor",
    }
    for source in metadata["sources"]:
        assert len(source["commit"]) == 40 and len(source["sha256"]) == 64
        assert f"/blob/{source['commit']}/" in source["url"]
        assert source["license"] == "MIT"
    licenses = resources.joinpath("LICENSE.langchain").read_text()
    assert licenses.count("MIT License") == 3
    assert "LangChain, Inc." in licenses


async def test_textual_stage_names_are_answer_content_not_an_action_protocol(runtime_store, build):
    answer = "Action: echo\nAction Input: {}\nObservation: a heading\nFinal Answer: TERMINATE"

    def must_not_execute(arguments, context):
        raise AssertionError("Only native tool calls may execute a tool")

    async with RuntimeEngine(store=runtime_store) as runtime:
        agent = build(
            runtime,
            lambda request: ModelResponse(answer),
            definition=AgentDefinition(
                "a", "Explain these protocol labels.", capabilities=(binding(),)
            ),
            capabilities=CapabilityRegistry(tools=(tool(must_not_execute),)),
        )
        result = await agent.ainvoke("Explain the labels")
        assert result.status == "succeeded" and result.output.value == answer
        state = runtime._load(result.run_id)
        assert len(state["operations"]) == 1
        assert "native tool calls" in agent.model.requests[0].messages[1]["content"]
        validate_recovery(state)


async def test_planner_can_replan_from_child_failure_with_frozen_role_instructions(
    runtime_store, build
):
    schema = {"type": "integer"}
    calls = []

    def respond(request):
        instructions = request.messages[1]["content"]
        calls.append((request.role_key, instructions))
        assert "native tool calls" in instructions
        assert "write_todos" not in instructions and "<END_OF_PLAN>" not in instructions
        if request.role_key == "worker":
            assert "Mode: Executor." in instructions and "Do not delegate" in instructions
            assert "SECRET_PLANNER_BUSINESS" not in json.dumps(request.messages)
            assert all(t["name"] != "delegate_task" for t in request.tools)
            task = next(m["content"] for m in request.messages if m["role"] == "user")
            if task == "Find a number":
                raise AgentLoomError("EVIDENCE_UNAVAILABLE", category="protocol")
            assert "EVIDENCE_UNAVAILABLE" in task
            return ModelResponse("42")
        assert "No executors are available." not in instructions
        assert "delegate_task" in instructions
        assert "SECRET_WORKER_BUSINESS" not in json.dumps(request.messages)
        feedback = [m for m in request.messages if m["role"] == "tool"]
        if len(feedback) == 2:
            return ModelResponse("42")
        if feedback:
            assert "EVIDENCE_UNAVAILABLE" in feedback[0]["content"]
        return ModelResponse(
            tool_calls=(
                ToolCall(
                    "delegate_task",
                    {
                        "executor": "worker",
                        "task": "Find a number"
                        if not feedback
                        else "Previous step failed with EVIDENCE_UNAVAILABLE. Use this supplied fact: 42.",
                    },
                ),
            )
        )

    async with RuntimeEngine(store=runtime_store) as runtime:
        agent = build(
            runtime,
            respond,
            cls=PlanAgent,
            definition=AgentDefinition("p", "SECRET_PLANNER_BUSINESS", output_schema=schema),
            executors={
                "worker": AgentDefinition(
                    "w",
                    "SECRET_WORKER_BUSINESS",
                    "Extract a number",
                    output_schema=schema,
                )
            },
        )
        result = await agent.ainvoke("Return the number")
        assert result.status == "succeeded" and result.output.value == 42
        assert [role for role, _ in calls] == ["planner", "worker", "planner", "worker", "planner"]
        state = runtime._load(result.run_id)
        children = [t for t in state["tasks"].values() if t["role_key"] == "worker"]
        assert sorted(t["status"] for t in children) == ["failed", "succeeded"]
        for role, instructions in calls:
            assert instructions == state["prompt_contract"]["roles"][role]["content"]
        validate_recovery(state)
