"""Disposable worker; forced exit occurs after a real durable transaction commits."""

import asyncio
import json
import os
import sys
from pathlib import Path

from agentloom import (
    AgentDefinition,
    CapabilityBinding,
    CapabilityRegistration,
    CapabilityRegistry,
    CapabilitySpec,
    FileRuntimeStore,
    FunctionHookHandler,
    FunctionToolAdapter,
    HookBinding,
    HookDecision,
    HookSet,
    ModelResponse,
    PlanAgent,
    RuntimeEngine,
    ScriptedModel,
    ToolCall,
    WorkspaceBinding,
)


async def main():
    root, cut = Path(sys.argv[1]), sys.argv[2]

    def audit(line):
        with (root / "audit").open("a") as fp:
            fp.write(line + "\n")
            fp.flush()
            os.fsync(fp.fileno())

    def checkpoint(command, state):
        match = (
            cut == "raw"
            and command.startswith("attempt-result:")
            or cut == "hook_result"
            and command.startswith("hook-result:")
            or cut == "hook_apply"
            and command.startswith("hook-apply:")
            or cut == "child"
            and command.startswith("validate:")
            and any(
                t["parent_operation"] and t["status"] == "succeeded"
                for t in state["tasks"].values()
            )
            or cut == "batch"
            and command.startswith("consume:")
            or cut == "terminal"
            and command == "terminal"
        )
        if match:
            os._exit(73)

    async def hook(event, context):
        audit("hook:" + event.operation_id)
        return HookDecision()

    async def marker(arguments, context):
        audit("tool:" + arguments["word"])
        return arguments["word"]

    def model(request):
        audit("model:" + request.role_key)
        feedback = [m for m in request.messages if m["role"] == "tool"]
        if request.role_key == "planner":
            return (
                ModelResponse("combined")
                if feedback
                else ModelResponse(
                    tool_calls=tuple(
                        ToolCall("delegate_task", {"executor": "worker", "task": word})
                        for word in ("one", "two")
                    )
                )
            )
        return (
            ModelResponse("worker done")
            if feedback
            else ModelResponse(
                tool_calls=(ToolCall("mark", {"word": request.messages[-1]["content"]}),)
            )
        )

    store = FileRuntimeStore(root / "state")
    try:
        async with RuntimeEngine(
            store=store, lease_seconds=0.4, checkpoint_callback=checkpoint
        ) as runtime:
            agent = PlanAgent(
                definition=AgentDefinition("planner", "P"),
                executors={
                    "worker": AgentDefinition(
                        "worker",
                        "W",
                        "One task",
                        capabilities=(CapabilityBinding("mark", "tool", "mark", "marker", "mark"),),
                    )
                },
                model=ScriptedModel(model),
                workspace=WorkspaceBinding(root / "files"),
                runtime_engine=runtime,
                capabilities=CapabilityRegistry(
                    tools=(
                        CapabilityRegistration(
                            "marker",
                            FunctionToolAdapter(marker),
                            (CapabilitySpec("mark", "mark", "Write audit", {"type": "object"}),),
                        ),
                    )
                ),
                hooks=HookSet(
                    (HookBinding("audit", "model.generate", "request", "audit"),),
                    {"audit": FunctionHookHandler(hook)},
                ),
            )
            handle = await agent.start(
                None if store.load("default", "run") else "question", run_id="run"
            )
            result = await handle.wait(timeout_s=10)
            for _ in range(200):
                if (await handle.snapshot())["cleanup_state"] == "succeeded":
                    break
                await asyncio.sleep(0.01)
            print(
                json.dumps(
                    {
                        "status": result.status,
                        "loops": result.usage.loops_consumed,
                        "cleanup": (await handle.snapshot())["cleanup_state"],
                    }
                )
            )
    finally:
        store.close()


if __name__ == "__main__":
    asyncio.run(main())
