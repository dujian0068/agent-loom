import asyncio
import json
import uuid

import pytest
from conftest import configured_agent, find_tool, publish, request, submit
from sqlalchemy import func, select
from test_process_recovery import model_server, start_worker  # noqa: F401 -- shared pytest fixture

from agentloom_platform.persistence import Application, ChatSession, Membership, Release, Run
from agentloom_platform.worker import maintenance, run_one


def preview(platform, app, role="editor", key=None):
    response = request(
        platform["clients"][role],
        "POST",
        f"/agents/{app['id']}/previews",
        {"draft_revision": app["revision"]},
        app["revision"],
        key=key,
    )
    assert response.status_code == 201, response.text
    return response.json()


def ask(platform, session, role="editor", message_id=None):
    response = request(
        platform["clients"][role],
        "POST",
        f"/sessions/{session['id']}/inputs",
        {"client_message_id": message_id or uuid.uuid4().hex, "content": "测试当前草稿"},
    )
    assert response.status_code == 202, response.text
    return response.json()


@pytest.mark.parametrize("mode", ["react", "plan_executor"])
def test_unpublished_draft_runs_privately_without_creating_a_release(platform, mode):
    app = configured_agent(platform, mode)
    session = preview(platform, app)
    receipt = ask(platform, session)
    asyncio.run(run_one(platform["settings"], platform["db"], receipt["run_id"]))
    maintenance(platform["settings"], platform["db"])
    client = platform["clients"]["editor"]
    history = client.get(f"/api/v1/sessions/{session['id']}/history").json()
    assert history["draft_revision"] == app["revision"]
    assert history["runs"][0]["status"] == "completed", history
    assert history["runs"][0]["release_version"] is None
    assert history["runs"][0]["cleanup_state"] == "completed"
    assert history["runs"][0]["model_calls"] == (1 if mode == "react" else 4)
    assert client.get(f"/api/v1/spaces/{platform['space']}/sessions").json()["items"] == []
    assert client.get(f"/api/v1/agents/{app['id']}/previews").json()["items"][0]["id"] == session["id"]
    assert client.get(f"/api/v1/agents/{app['id']}/releases").json()["items"] == []
    for other in ("admin", "viewer"):
        assert platform["clients"][other].get(f"/api/v1/sessions/{session['id']}/history").status_code == 404
    assert publish(platform, app)["version"] == 1


def test_preview_freezes_draft_and_does_not_stop_a_published_run(platform):
    app = configured_agent(platform)
    release = publish(platform, app)
    _, production = submit(platform, app)
    admin = platform["clients"]["admin"]
    app["draft"]["definition"]["prompt"] = "new draft prompt"
    app = request(admin, "PUT", f"/agents/{app['id']}/draft", app["draft"], app["revision"]).json()
    session = preview(platform, app)
    app["draft"]["definition"]["prompt"] = "subsequent edit"
    updated = request(admin, "PUT", f"/agents/{app['id']}/draft", app["draft"], app["revision"]).json()
    receipt = ask(platform, session, message_id="same")
    assert ask(platform, session, message_id="same")["run_id"] == receipt["run_id"]
    with platform["db"].session() as s:
        assert (
            s.get(ChatSession, session["id"]).preview_snapshot["draft"]["definition"]["prompt"]
            == "new draft prompt"
        )
        assert s.get(Run, receipt["run_id"]).release_id is None
        assert s.get(Run, production["run_id"]).status == "queued"
        assert s.get(Application, app["id"]).release_id == release["release_id"]
    newer = preview(platform, updated)
    assert newer["id"] != session["id"] and newer["draft_revision"] == updated["revision"]
    # Publishing continues to stop both kinds of active Run before switching.
    publish(platform, updated)
    with platform["db"].session() as s:
        assert s.get(Run, receipt["run_id"]).status == "cancelled"
        assert s.get(Run, production["run_id"]).status == "cancelled"


def test_preview_requires_editor_role_current_revision_and_valid_configuration(platform):
    app = configured_agent(platform)
    path = f"/agents/{app['id']}/previews"
    body = {"draft_revision": app["revision"]}
    admin = platform["clients"]["admin"]
    assert request(platform["clients"]["viewer"], "POST", path, body, app["revision"]).status_code == 403
    assert request(admin, "POST", path, body).status_code == 428
    assert request(admin, "POST", path, body, app["revision"] - 1).status_code == 409
    app["draft"]["definition"]["prompt"] = ""
    app = request(admin, "PUT", f"/agents/{app['id']}/draft", app["draft"], app["revision"]).json()
    response = request(admin, "POST", path, {"draft_revision": app["revision"]}, app["revision"])
    assert (
        response.status_code == 422 and response.json()["error"]["violations"][0]["code"] == "PROMPT_REQUIRED"
    )
    with platform["db"].session() as s:
        assert s.scalar(select(func.count()).select_from(ChatSession)) == 0


def test_preview_downgrade_revokes_history_replay_and_worker_access(platform):
    app = configured_agent(platform)
    key = uuid.uuid4().hex
    session = preview(platform, app, key=key)
    assert preview(platform, app, key=key)["id"] == session["id"]
    receipt = ask(platform, session)
    with platform["db"].session(write=True) as s:
        s.get(Membership, (platform["users"]["editor"], platform["space"])).role = "viewer"
    client = platform["clients"]["editor"]
    for path in (
        f"/sessions/{session['id']}/history",
        f"/runs/{receipt['run_id']}",
        f"/runs/{receipt['run_id']}/events",
        f"/agents/{app['id']}/previews",
    ):
        assert client.get("/api/v1" + path).status_code == 403
    assert (
        request(
            client,
            "POST",
            f"/agents/{app['id']}/previews",
            {"draft_revision": app["revision"]},
            app["revision"],
            key=key,
        ).status_code
        == 403
    )
    asyncio.run(run_one(platform["settings"], platform["db"], receipt["run_id"]))
    with platform["db"].session() as s:
        run = s.get(Run, receipt["run_id"])
        assert run.status == "failed" and run.model_calls == 0
    assert not (platform["settings"].data_dir / "workspaces" / receipt["run_id"]).exists()


@pytest.mark.parametrize("mode", ["react", "plan_executor"])
def test_draft_preview_uses_real_http_worker_tools_and_frozen_model(platform, model_server, mode):  # noqa: F811 -- pytest fixture
    url, directory = model_server
    client = platform["clients"]["admin"]
    resource = request(
        client,
        "POST",
        f"/spaces/{platform['space']}/resources/model",
        {
            "name": "Preview HTTP model",
            "spec": {"endpoint": url, "model_id": "catalog-chat-a"},
            "credential": "catalog-e2e-key",
        },
    ).json()
    app = configured_agent(platform, mode)
    app["draft"]["model"] = {"resource_id": resource["id"], "version": 1, "members": []}
    if mode == "react":
        app["draft"]["definition"]["prompt"] = "研究任务"
        app["draft"]["definition"]["capabilities"] = [find_tool(platform, "write_file")]
    else:
        app["draft"]["executors"]["research"]["capabilities"] = [find_tool(platform, "write_file")]
    app["draft"]["environment"]["retention"] = "session"
    app = request(client, "PUT", f"/agents/{app['id']}/draft", app["draft"], app["revision"]).json()
    session = preview(platform, app)
    response = request(
        client,
        "POST",
        f"/resources/{resource['id']}/versions",
        {
            "name": "Preview HTTP model",
            "spec": {"endpoint": url, "model_id": "catalog-chat-b"},
        },
    )
    assert response.status_code == 201, response.text
    app["draft"]["model"]["version"] = 2
    assert (
        request(client, "PUT", f"/agents/{app['id']}/draft", app["draft"], app["revision"]).status_code == 200
    )
    receipt = ask(platform, session)
    with (directory / "preview-worker.log").open("w+") as log:
        worker = start_worker(platform, receipt["run_id"], log)
        try:
            assert worker.wait(timeout=30) == 0
        finally:
            if worker.poll() is None:
                worker.kill()
                worker.wait(timeout=10)
    history = platform["clients"]["editor"].get(f"/api/v1/sessions/{session['id']}/history").json()
    assert history["runs"][0]["status"] == "completed", history
    assert history["runs"][0]["tool_calls"] == 1
    assert (
        platform["settings"].data_dir / "workspaces" / session["id"] / "research.txt"
    ).read_text() == "Completed research"
    stream = platform["clients"]["editor"].get(f"/api/v1/runs/{receipt['run_id']}/events").text
    assert stream.count("event: tool\n") == 2
    assert '"name": "write_file"' in stream and '"status": "completed"' in stream
    assert "catalog-e2e-key" not in stream and "Completed research" not in stream
    calls = [json.loads(line) for line in (directory / "requests.jsonl").read_text().splitlines()]
    assert all(call["model"] == "catalog-chat-a" for call in calls)
    with platform["db"].session() as s:
        assert s.scalar(select(func.count()).select_from(Release)) == 0
