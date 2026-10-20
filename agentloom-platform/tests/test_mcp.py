import asyncio
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from conftest import configured_agent, publish, request, submit
from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.checkpoint.memory import InMemorySaver

from agentloom_platform.persistence import Release, Run
from agentloom_platform.runtime import RuntimeBlocked, assemble


@pytest.fixture
def mcp_url(tmp_path, monkeypatch):
    # Do not send this loopback-only fixture through an OS-configured proxy.
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost,::1")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    url = f"http://127.0.0.1:{port}/mcp"
    with (tmp_path / "mcp.log").open("w+") as log:
        process = subprocess.Popen(
            [sys.executable, str(Path(__file__).with_name("mcp_server.py")), str(port)],
            stdout=log,
            stderr=log,
        )
        try:
            for _ in range(100):
                try:
                    httpx.get(url, timeout=0.2, trust_env=False)
                    break
                except httpx.TransportError:
                    if process.poll() is not None:
                        log.seek(0)
                        pytest.fail(log.read())
                    time.sleep(0.05)
            else:
                pytest.fail("MCP fixture did not start")
            yield url
        finally:
            process.terminate()
            process.wait(timeout=10)


def test_real_http_discovery_explicit_selection_and_schema_drift(platform, mcp_url, monkeypatch):
    client = platform["clients"]["admin"]
    connection = request(
        client,
        "POST",
        f"/spaces/{platform['space']}/resources/mcp",
        {"name": "计算服务", "spec": {"url": mcp_url}},
    ).json()
    discovery_path = f"/resources/{connection['id']}/discover"
    assert client.post("/api/v1" + discovery_path).status_code == 428
    response = request(client, "POST", discovery_path, key="discover-once")
    assert response.status_code == 200, response.text
    resource = response.json()
    version = resource["versions"][0]
    assert version["status"] == "discovered"
    assert {m["name"] for m in version["spec"]["members"]} == {"add", "echo"}

    async def should_not_rediscover(*args):
        raise AssertionError("A completed discovery retry must reuse its receipt")

    from agentloom_platform import http

    monkeypatch.setattr(http, "discover_mcp", should_not_rediscover)
    assert request(client, "POST", discovery_path, key="discover-once").json() == resource
    enabled = request(
        client,
        "POST",
        f"/resources/{connection['id']}/enable-members",
        {"discovery_version": version["version"], "members": ["add"]},
    )
    assert enabled.status_code == 200, enabled.text
    selected = enabled.json()["versions"][0]
    assert selected["status"] == "ready"
    assert [m["name"] for m in selected["spec"]["members"]] == ["add"]
    binding = {"resource_id": connection["id"], "version": selected["version"], "members": ["add"]}
    app = configured_agent(platform, bindings=[binding])
    publish(platform, app)
    _, receipt = submit(platform, app)
    with platform["db"].session() as s:
        run = s.get(Run, receipt["run_id"])
        snapshot = s.get(Release, run.release_id).snapshot

    async def exercise():
        tools = await MultiServerMCPClient({"test": {"url": mcp_url, "transport": "http"}}).get_tools()
        tool = next(t for t in tools if t.name == "add")
        result = await tool.ainvoke({"left": 2, "right": 3})
        assert result[0]["text"] == "5"
        await assemble(platform["settings"], platform["db"], run, snapshot, InMemorySaver())
        frozen = snapshot["resources"][f"{connection['id']}:{selected['version']}"]["spec"]["members"][0]
        frozen["schema"] = {"type": "object", "properties": {}}
        with pytest.raises(RuntimeBlocked, match="参数已变化"):
            await assemble(platform["settings"], platform["db"], run, snapshot, InMemorySaver())

    asyncio.run(exercise())
