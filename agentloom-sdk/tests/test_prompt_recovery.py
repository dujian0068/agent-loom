"""Instruction snapshots survive restart, including roles not yet instantiated."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agentloom import (
    AgentDefinition,
    InfrastructureError,
    LocalEnvironment,
    ModelResponse,
    PlanAgent,
    RuntimeEngine,
    ToolCall,
)
from agentloom.prompts import templates
from agentloom.prompts.assembly import PROMPT_RECORD_KIND
from agentloom.runtime.recovery import validate_recovery


class Crash(Exception):
    pass


@pytest.mark.parametrize("cut", ["before_root", "before_child"])
def test_real_process_exit_and_template_upgrade(tmp_path, cut):
    worker = Path(__file__).parent / "support" / "prompt_process.py"
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")}
    first = subprocess.run(
        [sys.executable, str(worker), str(tmp_path), cut],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert first.returncode == 75, first.stderr
    time.sleep(0.35)
    second = subprocess.run(
        [sys.executable, str(worker), str(tmp_path), "resume"],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert second.returncode == 0, second.stderr
    assert not second.stderr, "Shutdown must retrieve all maintenance task outcomes"
    assert json.loads(second.stdout) == {"status": "succeeded", "loops": 3}
    audit = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert [entry["role"] for entry in audit] == ["planner", "worker", "planner"]
    assert "Mode: Plan." in audit[0]["instructions"]
    assert "Mode: Executor." in audit[1]["instructions"]
    assert audit[0]["instructions"] == audit[2]["instructions"]


def plan_options():
    return dict(
        cls=PlanAgent,
        definition=AgentDefinition("p", "整理报告。"),
        executors={"worker": AgentDefinition("w", "提取事实。", "提取事实")},
    )


def response(request):
    if request.role_key == "planner" and not any(m["role"] == "tool" for m in request.messages):
        return ModelResponse(
            tool_calls=(ToolCall("delegate_task", {"executor": "worker", "task": "提取数字"}),)
        )
    return ModelResponse("done")


@pytest.mark.parametrize("cut", ["environment:", "decision:"])
async def test_upgrade_uses_admission_snapshot_for_new_root_and_child(
    runtime_store, build, monkeypatch, cut
):
    def checkpoint(command, state):
        if command.startswith(cut):
            raise Crash()

    async with RuntimeEngine(store=runtime_store, checkpoint_callback=checkpoint) as initial:
        agent = build(initial, response, **plan_options())
        handle = await agent.start("报告", run_id="frozen")
        with pytest.raises(InfrastructureError):
            await handle.wait(timeout_s=3)
        saved = initial._load("frozen")["prompt_contract"]
        first_calls = len(agent.model.requests)

    monkeypatch.setattr(templates, "COMMON", "NEW SDK TEMPLATE")
    monkeypatch.setattr(templates, "TEMPLATE_VERSION", "future-template")
    async with RuntimeEngine(store=runtime_store) as recovered:
        agent = build(recovered, response, **plan_options())
        result = await agent.ainvoke(run_id="frozen")
        assert result.status == "succeeded"
        assert len(agent.model.requests) + first_calls == 3
        for request in agent.model.requests:
            assert request.messages[1]["content"] == saved["roles"][request.role_key]["content"]
            assert "NEW SDK TEMPLATE" not in request.messages[1]["content"]
        state = recovered._load("frozen")
        assert state["prompt_contract"] == saved
        validate_recovery(state)
        new_agent = build(recovered, response, **plan_options())
        new_result = await new_agent.ainvoke("新报告")
        assert new_result.status == "succeeded"
        assert all("NEW SDK TEMPLATE" in r.messages[1]["content"] for r in new_agent.model.requests)


async def test_legacy_run_keeps_old_layout_for_later_executor(runtime_store, build):
    def checkpoint(command, state):
        if command == "initialize":
            raise Crash()

    async with RuntimeEngine(store=runtime_store, checkpoint_callback=checkpoint) as initial:
        handle = await build(initial, response, **plan_options()).start("报告", run_id="legacy")
        with pytest.raises(InfrastructureError):
            await handle.wait(timeout_s=3)
    with runtime_store.transaction("default") as tx:
        state = tx.get("runs", "legacy")
        del state["prompt_contract"]
        context = state["tasks"][state["root_task_id"]]["context"]
        context["records"] = [r for r in context["records"] if r["kind"] != PROMPT_RECORD_KIND]
        context["revision"] = len(context["records"])
        for seq, record in enumerate(context["records"], 1):
            record["context_seq"] = seq
        tx.put("runs", "legacy", state)
    async with RuntimeEngine(store=runtime_store) as recovered:
        agent = build(recovered, response, **plan_options())
        assert (await agent.ainvoke(run_id="legacy")).status == "succeeded"
        assert len(agent.model.requests) == 3
        assert all(
            len([m for m in r.messages if m["role"] == "system"]) == 1 for r in agent.model.requests
        )
        state = recovered._load("legacy")
        assert "prompt_contract" not in state
        validate_recovery(state)


@pytest.mark.parametrize(
    "corruption",
    [
        "contract",
        "version",
        "roles",
        "profile",
        "text",
        "record",
        "flags",
        "position",
        "summary",
        "request",
    ],
)
async def test_prompt_corruption_fails_before_dispatch(runtime_store, build, corruption):
    def checkpoint(command, state):
        if command.startswith("begin:"):
            raise Crash()

    async with RuntimeEngine(store=runtime_store, checkpoint_callback=checkpoint) as initial:
        handle = await build(initial, lambda r: pytest.fail("cut precedes dispatch")).start(
            "go", run_id="corrupt-prompt"
        )
        with pytest.raises(InfrastructureError):
            await handle.wait(timeout_s=3)
    with runtime_store.transaction("default") as tx:
        state = tx.get("runs", "corrupt-prompt")
        contract = state["prompt_contract"]
        context = state["tasks"][state["root_task_id"]]["context"]
        sdk = context["records"][1]
        if corruption == "contract":
            state["prompt_contract"] = None
        elif corruption == "version":
            contract["schema_version"] = True
        elif corruption == "roles":
            contract["roles"] = {}
        elif corruption == "profile":
            contract["roles"]["react"]["profile"] = "planner"
        elif corruption == "text":
            sdk["message"]["content"] = "changed"
        elif corruption == "record":
            sdk["kind"] = "message"
        elif corruption == "flags":
            sdk["protected"] = False
        elif corruption == "position":
            context["records"][0], context["records"][1] = sdk, context["records"][0]
            for seq, entry in enumerate(context["records"], 1):
                entry["context_seq"] = seq
        elif corruption == "summary":
            context["snapshot"] = {"sources": {sdk["record_id"]: "ignored"}}
        else:
            next(iter(state["operations"].values()))["request"]["messages"][1]["content"] = (
                "changed"
            )
        tx.put("runs", "corrupt-prompt", state)

    class NoEnvironment(LocalEnvironment):
        def prepare(self, *args, **kwargs):
            pytest.fail("Prompt validation must precede environment preparation")

    async with RuntimeEngine(store=runtime_store, environment=NoEnvironment()) as recovered:
        agent = build(recovered, lambda r: pytest.fail("Corrupt prompt dispatched"))
        handle = await agent.start(run_id="corrupt-prompt")
        with pytest.raises(InfrastructureError, match="PERSISTENCE_INVARIANT_BROKEN") as error:
            await handle.wait(timeout_s=3)
        assert error.value.field_path.startswith("prompt_contract.")
        assert not agent.model.requests
