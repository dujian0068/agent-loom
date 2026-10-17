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
    ExecutionPolicy,
    FileRuntimeStore,
    FunctionHookHandler,
    FunctionToolAdapter,
    HookBinding,
    HookDecision,
    HookSet,
    ModelResponse,
    ReactAgent,
    RuntimeEngine,
    ScriptedModel,
    ToolCall,
    WorkspaceBinding,
)


async def main():
    mode, root, stage = sys.argv[1:]
    root = Path(root)
    store = FileRuntimeStore(root / "state")

    def checkpoint(command, state):
        if command.startswith("hook-result:"):
            os._exit(73)

    if mode == "prepare":
        async with RuntimeEngine(
            store=store, lease_seconds=0.3, checkpoint_callback=checkpoint
        ) as runtime:
            definition = AgentDefinition(
                "review",
                "SYSTEM",
                capabilities=(CapabilityBinding("echo", "tool", "echo", "echo", "echo"),),
            )
            hooks = HookSet(
                (HookBinding("deny", "capability.invoke", stage, "deny"),),
                {"deny": FunctionHookHandler(lambda e, c: HookDecision("deny"))},
            )
            catalog = CapabilityRegistry(
                tools=(
                    CapabilityRegistration(
                        "echo",
                        FunctionToolAdapter(lambda a, c: "RAW"),
                        (CapabilitySpec("echo", "echo", "echo"),),
                    ),
                )
            )
            agent = ReactAgent(
                definition=definition,
                model=ScriptedModel(lambda r: ModelResponse(tool_calls=(ToolCall("echo", {}),))),
                workspace=WorkspaceBinding(root / "workspace"),
                runtime_engine=runtime,
                hooks=hooks,
                capabilities=catalog,
                execution_policy=ExecutionPolicy(run_timeout_seconds=10),
            )
            await agent.ainvoke("question", run_id="process-cut", session_id="session")
    else:
        async with RuntimeEngine(store=store) as runtime:
            await runtime.runs.stop("process-cut", command_id="stop")
            result = await (await runtime.runs.attach("process-cut")).wait(timeout_s=4)
            op = next(
                o
                for o in runtime._load(result.run_id)["operations"].values()
                if o["event_type"] == "capability.invoke"
            )
            print(
                json.dumps(
                    {
                        "run_status": result.status,
                        "operation_status": op["status"],
                        "response": op["response"],
                        "hook_status": op["hooks"][stage + ":deny"]["status"],
                        "saved_decision": op["hooks"][stage + ":deny"]["result"]["decision"],
                    }
                )
            )
    store.close()


asyncio.run(main())
