import asyncio
import time

import pytest
from conftest import configured_agent, find_tool, publish, request, submit
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import StructuredTool
from langchain_mcp_adapters.client import MultiServerMCPClient
from sqlalchemy import select

from agentloom_platform import runtime
from agentloom_platform.persistence import (
    Membership,
    Operation,
    Release,
    Resource,
    ResourceVersion,
    Run,
    User,
)
from agentloom_platform.worker import run_one


def test_distinct_model_messages_can_reuse_a_tool_call_id(platform, monkeypatch):
    """OpenAI-compatible servers may restart call numbering on each response."""
    app = configured_agent(platform, bindings=[find_tool(platform, "write_file")])
    publish(platform, app)
    _, receipt = submit(platform, app)

    def generate(self, messages, stop=None, run_manager=None, **kwargs):
        completed = sum(isinstance(message, ToolMessage) for message in messages)
        if completed < 2:
            message = AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "call_0",
                        "name": "write_file",
                        "type": "tool_call",
                        "args": {"path": f"result-{completed}.txt", "content": f"answer-{completed}"},
                    }
                ],
            )
        else:
            message = AIMessage(content="Both files written")
        return ChatResult(generations=[ChatGeneration(message=message)])

    monkeypatch.setattr(runtime.TestModel, "_generate", generate)
    asyncio.run(run_one(platform["settings"], platform["db"], receipt["run_id"]))
    root = platform["settings"].data_dir / "workspaces" / receipt["run_id"]
    assert sorted(path.name for path in root.iterdir()) == ["result-0.txt", "result-1.txt"]
    assert (root / "result-1.txt").read_text() == "answer-1"
    with platform["db"].session() as s:
        run = s.get(Run, receipt["run_id"])
        assert run.status == "completed" and run.tool_calls == 2 and run.model_calls == 3
        assert len(list(s.scalars(select(Operation).where(Operation.run_id == run.id)))) == 2


def mcp_binding(platform):
    """A previously discovered and explicitly enabled member."""
    with platform["db"].session(write=True) as s:
        resource = Resource(space_id=platform["space"], kind="mcp", name="Approved MCP", description="")
        s.add(resource)
        s.flush()
        s.add(
            ResourceVersion(
                resource_id=resource.id,
                version=1,
                status="ready",
                spec={
                    "url": "http://127.0.0.1:1/mcp",
                    "members": [
                        {
                            "name": "echo",
                            "description": "Approved description",
                            "schema": {
                                "type": "object",
                                "properties": {"text": {"type": "string"}},
                                "required": ["text"],
                            },
                        }
                    ],
                },
            )
        )
    return {"resource_id": resource.id, "version": 1, "members": ["echo"]}


@pytest.mark.parametrize("revocation", ["membership", "account", "resource", "deadline"])
def test_authorization_checked_before_workspace_or_mcp_access(platform, monkeypatch, revocation):
    binding = mcp_binding(platform)
    app = configured_agent(platform, bindings=[binding])
    publish(platform, app)
    _, receipt = submit(platform, app)
    discoveries = []

    async def discover(*args, **kwargs):
        discoveries.append(True)
        return []

    monkeypatch.setattr(MultiServerMCPClient, "get_tools", discover)
    with platform["db"].session(write=True) as s:
        if revocation == "membership":
            s.delete(s.get(Membership, (platform["users"]["viewer"], platform["space"])))
        elif revocation == "account":
            s.get(User, platform["users"]["viewer"]).enabled = False
        elif revocation == "resource":
            s.get(Resource, binding["resource_id"]).enabled = False
        else:
            s.get(Run, receipt["run_id"]).deadline_at = time.time() - 1
    asyncio.run(run_one(platform["settings"], platform["db"], receipt["run_id"]))
    assert discoveries == []
    assert not (platform["settings"].data_dir / "workspaces" / receipt["run_id"]).exists()
    with platform["db"].session() as s:
        run = s.get(Run, receipt["run_id"])
        assert run.status == "failed" and run.model_calls == 0 and run.tool_calls == 0


def test_mcp_member_description_is_frozen_and_roles_receive_only_selected_capabilities(platform, monkeypatch):
    binding = mcp_binding(platform)
    admin = platform["clients"]["admin"]
    skill = request(
        admin,
        "POST",
        f"/spaces/{platform['space']}/resources/skill",
        {
            "name": "Writing rules",
            "description": "Private writing rules",
            "spec": {
                "files": {
                    "SKILL.md": "---\nname: writing\ndescription: Private writing rules\n---\nWriter instructions only",
                }
            },
        },
    ).json()
    app = configured_agent(platform, "plan_executor")
    app["draft"]["executors"]["research"]["capabilities"] = [binding]
    app["draft"]["executors"]["writer"]["capabilities"] = [
        find_tool(platform, "write_file"),
        {"resource_id": skill["id"], "version": 1, "members": []},
    ]
    app = request(admin, "PUT", f"/agents/{app['id']}/draft", app["draft"], app["revision"]).json()
    publish(platform, app)
    _, receipt = submit(platform, app)
    with platform["db"].session(write=True) as s:
        run = s.get(Run, receipt["run_id"])
        run.status, run.generation = "running", 1
        snapshot = s.get(Release, run.release_id).snapshot
    frozen = snapshot["resources"][f"{binding['resource_id']}:1"]["spec"]["members"][0]
    captured = {}

    async def discover(*args, **kwargs):
        return [
            StructuredTool(
                name="echo",
                description="Changed remote instructions",
                args_schema=frozen["schema"],
                func=lambda text: text,
            )
        ]

    def capture(snapshot, model, role_tools, saver, controls):
        captured.update(role_tools)

    monkeypatch.setattr(MultiServerMCPClient, "get_tools", discover)
    monkeypatch.setattr(runtime, "build_graph", capture)
    asyncio.run(runtime.assemble(platform["settings"], platform["db"], run, snapshot, None))
    assert captured["planner"] == ([], "")
    assert [tool.name for tool in captured["research"][0]] == ["echo"]
    assert captured["research"][0][0].description == "Approved description"
    writer_tools, writer_prompt = captured["writer"]
    assert {tool.name for tool in writer_tools} == {"write_file", "read_skill"}
    assert "Private writing rules" in writer_prompt and "Private writing rules" not in captured["research"][1]
    reader = next(tool for tool in writer_tools if tool.name == "read_skill")
    assert "Writer instructions only" in reader.invoke({"skill_id": skill["id"]})
    with pytest.raises(ValueError):
        reader.invoke({"skill_id": "not-authorized"})
