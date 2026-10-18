"""Process/wheel probe: only public SDK APIs and a provider's installed entry point."""

import asyncio
import json
import os
import sys
from pathlib import Path

from agentloom import (
    FILE_OPERATIONS,
    AgentDefinition,
    AgentLoom,
    AgentProperties,
    CapabilityBinding,
    CapabilityRegistry,
    FileProperties,
    ModelResponse,
    PropertiesConfig,
    RequestError,
    RuntimeProperties,
    ScriptedModel,
    ServiceProperties,
    ToolCall,
    WorkspaceBinding,
    file_tools,
)


async def main(root, mode, phase):
    def respond(request):
        assert "test-files" not in json.dumps(request.messages)
        feedback = [json.loads(m["content"]) for m in request.messages if m["role"] == "tool"]
        assert all(item["status"] == "succeeded" for item in feedback), feedback
        if request.role_key == "planner":
            return (
                ModelResponse("done")
                if feedback
                else ModelResponse(
                    tool_calls=(ToolCall("delegate_task", {"executor": "worker", "task": "files"}),)
                )
            )
        outputs = [item["result"] for item in feedback]
        index = len(outputs)
        if index == 0:
            name, args = "mkdir", {"path": "dir"}
        elif index == 1:
            name, args = "write", {"path": "dir/a.txt", "content": "one\n"}
        elif index == 2:
            name, args = "read", {"path": "dir/a.txt"}
        elif index == 3:
            name, args = "list", {"path": "dir"}
        elif index == 4:
            name, args = "stat", {"path": "dir/a.txt"}
        elif index == 5:
            assert outputs[2]["text"] == "one\n"
            name, args = (
                "edit",
                {
                    "path": "dir/a.txt",
                    "old_text": "one",
                    "new_text": "two",
                    "if_match": outputs[1]["etag"],
                },
            )
        elif index == 6:
            name, args = (
                "patch",
                {
                    "path": "dir/a.txt",
                    "unified_diff": "--- a/dir/a.txt\n+++ b/dir/a.txt\n@@ -1 +1 @@\n-two\n+three\n",
                    "if_match": outputs[5]["etag"],
                },
            )
        elif index == 7:
            name, args = (
                "move",
                {"source": "dir/a.txt", "destination": "dir/b.txt", "if_match": outputs[6]["etag"]},
            )
        elif index == 8:
            name, args = "glob", {"pattern": "*.txt", "root": "dir"}
        elif index == 9:
            name, args = "grep", {"pattern": "three", "path": "dir"}
        elif index == 10:
            assert outputs[8]["paths"] == ["dir/b.txt"]
            assert outputs[9]["matches"][0]["text"] == "three"
            name, args = "delete", {"path": "dir/b.txt", "if_match": outputs[7]["etag"]}
        else:
            assert index == 11
            return ModelResponse("done")
        return ModelResponse(tool_calls=(ToolCall(name, args),))

    def checkpoint(command, state):
        if (
            phase == "cut"
            and command.startswith("attempt-result:")
            and any(
                o["event_type"] == "capability.invoke"
                and o.get("raw")
                and o["request"]["name"] == "write"
                for o in state["operations"].values()
            )
        ):
            os._exit(75)

    parameters = PropertiesConfig(
        files=FileProperties(
            provider="test-files",
            options={
                "label": "changed" if phase == "change" else "fixed",
                "state_path": str(root / "provider.json"),
                "audit_path": str(root / "calls.jsonl"),
            },
        ),
        defaults=AgentProperties(workspace=WorkspaceBinding(persistent_sandbox=True)),
        runtime=RuntimeProperties(
            state_directory=root / "state", lease_seconds=0.3 if phase == "cut" else 30
        ),
        services=ServiceProperties(
            model=ScriptedModel(respond),
            capabilities=CapabilityRegistry(tools=(file_tools(),)),
            checkpoint_callback=checkpoint,
        ),
    )
    worker = AgentDefinition(
        "worker",
        "Use the file tools.",
        "Perform the requested file work.",
        capabilities=tuple(
            CapabilityBinding(
                binding_key=name,
                kind="builtin_tool",
                visible_name=name,
                adapter_key="files",
                member_key=name,
            )
            for name in FILE_OPERATIONS
        ),
    )
    async with AgentLoom(parameters) as app:
        program = (
            app.build(worker)
            if mode == "react"
            else app.build(
                AgentDefinition("planner", "Delegate the file work."), executors={"worker": worker}
            )
        )
        try:
            handle = await program.start(
                "go" if phase in {"all", "cut"} else None, run_id="file-spi"
            )
        except RequestError as exc:
            print(json.dumps({"error": exc.code}))
            return
        result = await handle.wait(timeout_s=10)
        assert result.status == "succeeded", result
        events = [e async for e in handle.events()]
        assert events[-1].type == "run.completed"
        print(
            json.dumps(
                {"status": result.status, "mode": mode, "loops": result.usage.loops_consumed}
            )
        )


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1]), sys.argv[2], sys.argv[3]))
