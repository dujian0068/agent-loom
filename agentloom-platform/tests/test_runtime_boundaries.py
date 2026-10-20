import asyncio
from types import SimpleNamespace

import pytest
from conftest import configured_agent, publish, request, submit
from langchain_core.messages import ToolMessage
from sqlalchemy import select

from agentloom_platform.persistence import Operation, Release, Run
from agentloom_platform.runtime import Control, RuntimeBlocked
from agentloom_platform.worker import run_one


def test_tool_receipts_prevent_replay_and_budget_rejection_has_no_pending_operation(platform):
    app = configured_agent(platform)
    publish(platform, app)
    _, receipt = submit(platform, app)
    db = platform["db"]
    with db.session(write=True) as s:
        run = s.get(Run, receipt["run_id"])
        run.status, run.generation = "running", 1
        snapshot = s.get(Release, run.release_id).snapshot
    snapshot["draft"]["execution"]["tool_calls"] = 1
    control = Control(db, run.id, 1, snapshot, "planner")
    calls = []

    async def handler(req):
        calls.append(req.tool_call["id"])
        return ToolMessage(content="written", tool_call_id=req.tool_call["id"])

    async def exercise():
        first = SimpleNamespace(tool_call={"id": "stable-id"})
        result = await control.awrap_tool_call(first, handler)
        assert (await control.awrap_tool_call(first, handler)).content == result.content
        with pytest.raises(RuntimeBlocked, match="预算"):
            await control.awrap_tool_call(SimpleNamespace(tool_call={"id": "over-budget"}), handler)
        with db.session() as s:
            operations = list(s.scalars(select(Operation).where(Operation.run_id == run.id)))
            assert len(operations) == 1 and operations[0].status == "completed"
        with db.session(write=True) as s:
            s.add(Operation(run_id=run.id, call_id="planner:unknown", status="pending"))
        with pytest.raises(RuntimeBlocked, match="人工核对"):
            await control.awrap_tool_call(SimpleNamespace(tool_call={"id": "unknown"}), handler)

    asyncio.run(exercise())
    assert calls == ["stable-id"]


def test_stop_cancels_inflight_model_and_next_input_does_not_revive_it(platform, monkeypatch):
    from agentloom_platform import runtime

    app = configured_agent(platform)
    publish(platform, app)
    session, receipt = submit(platform, app)
    entered = asyncio.Event()
    cancelled = []

    async def slow_model(self, req, handler):
        self.check("model_calls")
        entered.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    monkeypatch.setattr(runtime.Control, "awrap_model_call", slow_model)

    async def exercise():
        task = asyncio.create_task(run_one(platform["settings"], platform["db"], receipt["run_id"]))
        await asyncio.wait_for(entered.wait(), timeout=10)
        response = await asyncio.to_thread(
            request, platform["clients"]["viewer"], "POST", f"/runs/{receipt['run_id']}/stop"
        )
        assert response.status_code == 202
        await asyncio.wait_for(task, timeout=10)

    asyncio.run(exercise())
    assert cancelled == [True]
    with platform["db"].session() as s:
        run = s.get(Run, receipt["run_id"])
        assert run.status == "cancelled" and run.result is None
    response = request(
        platform["clients"]["viewer"],
        "POST",
        f"/sessions/{session['id']}/inputs",
        {"client_message_id": "new-question", "content": "重新提问"},
    )
    assert response.status_code == 202
    assert response.json()["run_id"] != receipt["run_id"]


def test_password_whitespace_is_preserved(platform):
    admin = platform["clients"]["admin"]
    password = "  intentional-spaces  "
    response = request(
        admin,
        "POST",
        f"/spaces/{platform['space']}/members",
        {"username": "spaces", "password": password, "role": "viewer"},
    )
    assert response.status_code == 201, response.text
    login = admin.post("/api/v1/auth/login", json={"username": "spaces", "password": password.strip()})
    assert login.status_code == 401
    assert (
        admin.post("/api/v1/auth/login", json={"username": "spaces", "password": password}).status_code == 200
    )
