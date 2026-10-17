"""Disposable worker; forced exit occurs after a real durable transaction commits."""

import asyncio
import json
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from agentloom import (
    AgentBuilder,
    AgentDefinition,
    AgentLoom,
    AgentProperties,
    CapabilityBinding,
    CapabilityRegistration,
    CapabilityRegistry,
    CapabilitySpec,
    FileRuntimeStore,
    FunctionHookHandler,
    HookBinding,
    HookDecision,
    HookInput,
    HookSet,
    ModelResponse,
    PlanAgent,
    PropertiesConfig,
    RuntimeEngine,
    RuntimeProperties,
    ScriptedModel,
    ServiceProperties,
    ToolCall,
    ToolResult,
    WorkspaceBinding,
)


async def main():
    root, cut = Path(sys.argv[1]), sys.argv[2]

    def assemble(**options):
        if sys.argv[3:] == ["builder"]:
            definition, executors = options.pop("definition"), options.pop("executors")
            return AgentBuilder(**options).build(definition, executors=executors)
        return PlanAgent(**options)

    @asynccontextmanager
    async def open_agent(store, **options):
        if sys.argv[3:] == ["properties"]:
            definition, executors = options.pop("definition"), options.pop("executors")
            parameters = PropertiesConfig(
                runtime=RuntimeProperties(state_directory=root / "state", lease_seconds=0.4),
                defaults=AgentProperties(workspace=options.pop("workspace")),
                services=ServiceProperties(store=store, checkpoint_callback=checkpoint, **options),
            )
            async with AgentLoom(parameters) as app:
                yield app.runtime, app.build(definition, executors=executors)
        else:
            async with RuntimeEngine(
                store=store, lease_seconds=0.4, checkpoint_callback=checkpoint
            ) as runtime:
                yield runtime, assemble(runtime_engine=runtime, **options)

    def audit(line):
        with (root / "audit").open("a") as fp:
            fp.write(line + "\n")
            fp.flush()
            os.fsync(fp.fileno())

    def checkpoint(command, state):
        match = (
            cut == "raw"
            and command.startswith("attempt-result:")
            or cut == "tool_permit"
            and command.startswith("permit:")
            and state["operations"][command.split(":", 1)[1]]["event_type"] == "capability.invoke"
            or cut == "tool_raw"
            and command.startswith("attempt-result:")
            and any(
                op["event_type"] == "capability.invoke" and op["raw"] is not None
                for op in state["operations"].values()
            )
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
        assert isinstance(event, HookInput)
        await context.reader.read_context(event.context_refs[0], limit=1)
        audit("hook:" + context.metadata.operation_id)
        return HookDecision()

    async def marker(arguments, context):
        audit("tool:" + arguments["word"])
        return arguments["word"]

    class MarkerAdapter:
        async def invoke_once(self, request, context):
            return ToolResult.from_value(await marker(request.arguments, context))

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
        async with open_agent(
            store,
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
            capabilities=CapabilityRegistry(
                tools=(
                    CapabilityRegistration(
                        "marker",
                        MarkerAdapter(),
                        (CapabilitySpec("mark", "mark", "Write audit", {"type": "object"}),),
                    ),
                )
            ),
            hooks=HookSet(
                (HookBinding("audit", "model.generate", "request", "audit"),),
                {"audit": FunctionHookHandler(hook)},
            ),
        ) as (runtime, agent):
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
