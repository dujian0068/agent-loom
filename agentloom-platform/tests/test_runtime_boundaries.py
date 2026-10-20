import asyncio
from types import SimpleNamespace

import pytest
from conftest import configured_agent, publish, request, submit
from langchain_core.messages import AIMessage, ToolMessage
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
    snapshot["draft"]["execution"]["tool_calls"] = 2
    control = Control(db, run.id, 1, snapshot, "planner")
    calls = []

    async def handler(req):
        calls.append(req.tool_call["id"])
        return ToolMessage(content="written", tool_call_id=req.tool_call["id"])

    def tool_request(call_id):
        call = {"id": call_id, "name": "write_file", "args": {"path": "test", "content": "ok"}}
        return SimpleNamespace(
            tool_call=call,
            state={
                "messages": [
                    AIMessage(
                        id="persisted-message-" + call_id,
                        content="",
                        tool_calls=[call],
                    )
                ]
            },
        )

    async def uncertain_handler(req):
        raise ConnectionError("connection lost after dispatch")

    async def exercise():
        first = tool_request("stable-id")
        result = await control.awrap_tool_call(first, handler)
        assert (await control.awrap_tool_call(first, handler)).content == result.content
        with pytest.raises(ConnectionError):
            await control.awrap_tool_call(tool_request("unknown"), uncertain_handler)
        with pytest.raises(RuntimeBlocked, match="人工核对"):
            await control.awrap_tool_call(tool_request("unknown"), handler)
        with pytest.raises(RuntimeBlocked, match="预算"):
            await control.awrap_tool_call(tool_request("over-budget"), handler)
        with db.session() as s:
            operations = list(s.scalars(select(Operation).where(Operation.run_id == run.id)))
            assert sorted(operation.status for operation in operations) == ["completed", "pending"]

    asyncio.run(exercise())
    assert calls == ["stable-id"]


def test_stop_cancels_inflight_model_and_next_input_does_not_revive_it(platform, monkeypatch):
    import threading
    import time

    from agentloom_platform import runtime

    app = configured_agent(platform)
    publish(platform, app)
    session, receipt = submit(platform, app)
    entered = asyncio.Event()
    cancelled = []
    thread_status = []
    io_may_finish = threading.Event()

    def inflight_sync_io():
        assert io_may_finish.wait(timeout=5)
        time.sleep(0.05)
        with platform["db"].session() as s:
            thread_status.append(s.get(Run, receipt["run_id"]).status)

    async def slow_model(self, req, handler):
        self.check("model_calls")
        asyncio.get_running_loop().run_in_executor(None, inflight_sync_io)
        entered.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.append(True)
            io_may_finish.set()
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
    assert thread_status == ["stopping"]
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


def test_process_ownership_outlives_cancelled_awaits_and_file_threads(platform, monkeypatch):
    import sys
    import time

    from agentloom_platform import worker

    observations = []

    def delayed_file_operation():
        time.sleep(0.05)
        with worker.run_lock(platform["settings"], "shutdown-test") as acquired:
            observations.append(acquired)

    async def finish_with_io_still_inflight(*args, **kwargs):
        asyncio.get_running_loop().run_in_executor(None, delayed_file_operation)

    monkeypatch.setattr(sys, "argv", ["worker", "--run", "shutdown-test"])
    monkeypatch.setattr(worker, "Settings", lambda: platform["settings"])
    monkeypatch.setattr(worker, "Database", lambda _: platform["db"])
    monkeypatch.setattr(worker, "run_one", finish_with_io_still_inflight)
    worker.main()
    assert observations == [False]
    with worker.run_lock(platform["settings"], "shutdown-test") as acquired:
        assert acquired
