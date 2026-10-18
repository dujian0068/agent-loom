"""Real process boundaries for typed recovery bindings and legacy compatibility."""

import asyncio
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

from agentloom import (
    AgentDefinition,
    FileRuntimeStore,
    ModelResponse,
    PlanAgent,
    ReactAgent,
    RequestError,
    RuntimeEngine,
    ScriptedModel,
    ToolCall,
    VariableSpec,
    WorkspaceBinding,
)


async def main():
    root, role, profile, phase = Path(sys.argv[1]), *sys.argv[2:]
    store = FileRuntimeStore(root / "state")
    variable = VariableSpec({"type": "string"}, "SAFE", False)
    if profile == "plain":
        variable = "SAFE"
    elif profile == "number":
        variable = 1.0
    elif profile == "schema":
        variable = True
    if phase == "change":
        if profile == "spec":
            variable = asdict(variable)
        elif profile == "number":
            variable = 1
    definition = AgentDefinition(
        "target",
        "Mode: {{mode}}",
        "Perform the task",
        variables={"mode": variable},
        output_schema={"const": 1 if phase == "change" else True} if profile == "schema" else None,
    )

    def checkpoint(command, state):
        if phase == "cut" and command.startswith(
            "decision:" if role == "worker" else "environment:"
        ):
            os._exit(75)

    def respond(request):
        if request.role_key == role:
            assert request.messages[0]["content"] == definition.render()
        with (root / "audit.jsonl").open("a") as fp:
            fp.write(
                json.dumps({"role": request.role_key, "prompt": request.messages[0]["content"]})
                + "\n"
            )
            fp.flush()
            os.fsync(fp.fileno())
        if role == "worker" and request.role_key == "planner":
            if not any(m["role"] == "tool" for m in request.messages):
                return ModelResponse(
                    tool_calls=(ToolCall("delegate_task", {"executor": "worker", "task": "go"}),)
                )
            return ModelResponse("done")
        return ModelResponse("true" if profile == "schema" else "done")

    try:
        async with RuntimeEngine(
            store=store, lease_seconds=0.3, checkpoint_callback=checkpoint
        ) as runtime:
            options = {
                "definition": definition,
                "model": ScriptedModel(respond),
                "runtime_engine": runtime,
                "workspace": WorkspaceBinding(root / "workspace"),
            }
            if role == "worker":
                options.update(
                    definition=AgentDefinition("planner", "Delegate then summarize."),
                    executors={"worker": definition},
                )
            agent = (PlanAgent if role == "worker" else ReactAgent)(**options)
            try:
                handle = await agent.start("go" if phase == "cut" else None, run_id="binding")
            except RequestError as exc:
                print(json.dumps({"error": exc.code, "field": exc.field_path}))
                return
            result = await handle.wait(timeout_s=5)
            events = [event async for event in handle.events()]
            assert events[-1].type == "run.completed"
            print(json.dumps({"status": result.status, "loops": result.usage.loops_consumed}))
    finally:
        store.close()


if __name__ == "__main__":
    asyncio.run(main())
