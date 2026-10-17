"""Leave a running file-backed Run and its live lease behind on process death."""

import asyncio
import os
import sys
from pathlib import Path

from agentloom import (
    AgentDefinition,
    ExecutionPolicy,
    FileRuntimeStore,
    ReactAgent,
    RuntimeEngine,
    ScriptedModel,
    WorkspaceBinding,
)


async def main():
    root = Path(sys.argv[1])

    async def die(request):
        os._exit(0)

    async with RuntimeEngine(store=FileRuntimeStore(root / "state"), lease_seconds=0.2) as runtime:
        agent = ReactAgent(
            definition=AgentDefinition("orphan", "SYSTEM"),
            model=ScriptedModel(die),
            workspace=WorkspaceBinding(root / "workspace"),
            runtime_engine=runtime,
            execution_policy=ExecutionPolicy(run_timeout_seconds=1),
        )
        await (await agent.start("question", run_id="process-orphan")).wait()


asyncio.run(main())
