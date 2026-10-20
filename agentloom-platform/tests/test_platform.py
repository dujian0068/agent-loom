import asyncio
import json
import subprocess
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from conftest import configured_agent, find_tool, publish, request, submit
from sqlalchemy import select

from agentloom_platform.persistence import Application, Membership, Release, Run
from agentloom_platform.tools import Workspace, skill_tool
from agentloom_platform.worker import maintenance, run_lock, run_one


@pytest.mark.parametrize("mode", ["react", "plan_executor"])
def test_http_to_real_graph_and_private_history(platform, mode):
    app = configured_agent(platform, mode)
    publish(platform, app)
    session, receipt = submit(platform, app)
    asyncio.run(run_one(platform["settings"], platform["db"], receipt["run_id"]))
    maintenance(platform["settings"], platform["db"])
    viewer = platform["clients"]["viewer"]
    history = viewer.get(f"/api/v1/sessions/{session['id']}/history").json()
    assert history["runs"][0]["status"] == "completed", history
    assert history["items"][0]["status"] == "accepted"
    assert history["items"][-1]["content"].startswith("测试模型已执行：")
    assert history["runs"][0]["model_calls"] == (1 if mode == "react" else 4)
    for who in ("editor", "admin"):
        assert platform["clients"][who].get(f"/api/v1/sessions/{session['id']}/history").status_code == 404
        assert platform["clients"][who].get(f"/api/v1/runs/{receipt['run_id']}/events").status_code == 404
    stream = viewer.get(f"/api/v1/runs/{receipt['run_id']}/events", headers={"Last-Event-ID": "2"})
    assert "id: 1\n" not in stream.text and "event: finished" in stream.text
    assert "thread_id" not in json.dumps(history)


def test_create_mode_cas_permissions_and_idempotency(platform):
    admin, viewer = platform["clients"]["admin"], platform["clients"]["viewer"]
    path = f"/spaces/{platform['space']}/agents"
    assert request(admin, "POST", path, {"name": "缺模式"}).status_code == 422
    assert request(viewer, "POST", path, {"name": "无权", "mode": "react"}).status_code == 403
    key = uuid.uuid4().hex
    first = request(admin, "POST", path, {"name": "固定身份", "mode": "react"}, key=key)
    assert request(admin, "POST", path, {"name": "固定身份", "mode": "react"}, key=key).json() == first.json()
    assert request(admin, "POST", path, {"name": "不同正文", "mode": "react"}, key=key).status_code == 409
    app = first.json()
    draft_path = f"/agents/{app['id']}/draft"
    assert request(admin, "PUT", draft_path, app["draft"]).status_code == 428
    assert request(admin, "PUT", draft_path, {**app["draft"], "mode": "plan_executor"}, 1).status_code == 422
    assert request(admin, "PUT", draft_path, app["draft"], 1).status_code == 200
    assert request(admin, "PUT", draft_path, app["draft"], 1).status_code == 409
    assert (
        admin.put("/api/v1" + draft_path, json=app["draft"], headers={"X-CSRF-Token": ""}).status_code == 403
    )
    assert admin.get(f"/api/v1/spaces/{platform['other_space']}/agents").status_code == 404
    response = admin.post(
        "/api/v1" + path,
        content='{"name":"A","mode":"react","mode":"plan_executor"}',
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 422
    assert admin.get("/api/v1/not-a-route").status_code == 404
    assert admin.get("/").status_code == 404


def test_concurrent_first_questions_and_message_dedup(platform):
    app = configured_agent(platform)
    publish(platform, app)
    client = platform["clients"]["viewer"]
    session = request(client, "POST", f"/agents/{app['id']}/sessions").json()
    path = f"/sessions/{session['id']}/inputs"
    message = {"client_message_id": "same-message", "content": "hello"}
    first = request(client, "POST", path, message)
    assert request(client, "POST", path, message).json()["run_id"] == first.json()["run_id"]
    assert request(client, "POST", path, {**message, "content": "changed"}).status_code == 409
    assert request(client, "POST", path, {**message, "client_message_id": "next"}).status_code == 409
    new_session = request(client, "POST", f"/agents/{app['id']}/sessions").json()

    def send(number):
        return request(
            client,
            "POST",
            f"/sessions/{new_session['id']}/inputs",
            {"client_message_id": str(number), "content": "concurrent"},
        ).status_code

    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(send, [1, 2])) == [202, 409]


def test_immutable_release_and_publication_barrier(platform):
    app = configured_agent(platform)
    first_release = publish(platform, app)
    session, receipt = submit(platform, app)
    admin = platform["clients"]["admin"]
    app["draft"]["description"] = "新的描述"
    updated = request(admin, "PUT", f"/agents/{app['id']}/draft", app["draft"], app["revision"]).json()
    new = request(
        admin,
        "POST",
        f"/agents/{app['id']}/releases",
        {"draft_revision": updated["revision"]},
        updated["revision"],
    )
    assert new.status_code == 202
    with platform["db"].session() as s:
        assert s.get(Run, receipt["run_id"]).status == "stopping"
        assert s.get(Release, first_release["release_id"]).snapshot["draft"]["description"] == "描述"
    blocked = request(
        platform["clients"]["viewer"],
        "POST",
        f"/sessions/{session['id']}/inputs",
        {"client_message_id": "late", "content": "late"},
    )
    assert blocked.status_code == 409
    maintenance(platform["settings"], platform["db"])
    with platform["db"].session() as s:
        assert s.get(Run, receipt["run_id"]).status == "cancelled"
        assert s.get(Application, app["id"]).release_id == new.json()["release_id"]
    # A delayed dispatch cannot revive the cancelled run.
    asyncio.run(run_one(platform["settings"], platform["db"], receipt["run_id"]))
    with platform["db"].session() as s:
        assert s.get(Run, receipt["run_id"]).model_calls == 0


def test_completed_checkpoint_reconciles_without_recalling_model(platform, monkeypatch):
    from agentloom_platform import worker

    app = configured_agent(platform, "plan_executor")
    publish(platform, app)
    _, receipt = submit(platform, app)
    original_finish = worker.finish
    monkeypatch.setattr(worker, "finish", lambda *args, **kwargs: None)
    asyncio.run(run_one(platform["settings"], platform["db"], receipt["run_id"]))
    with platform["db"].session() as s:
        calls = s.get(Run, receipt["run_id"]).model_calls
        assert calls == 4
        assert s.get(Run, receipt["run_id"]).status == "running"
    monkeypatch.setattr(worker, "finish", original_finish)
    asyncio.run(run_one(platform["settings"], platform["db"], receipt["run_id"]))
    with platform["db"].session() as s:
        run = s.get(Run, receipt["run_id"])
        assert run.status == "completed" and run.model_calls == calls


def test_live_writer_lock_blocks_takeover_and_stop_before_start(platform):
    app = configured_agent(platform)
    publish(platform, app)
    _, receipt = submit(platform, app)
    settings, db = platform["settings"], platform["db"]
    with run_lock(settings, receipt["run_id"]) as acquired:
        assert acquired
        asyncio.run(run_one(settings, db, receipt["run_id"]))
        with db.session() as s:
            assert s.get(Run, receipt["run_id"]).status == "queued"
    request(platform["clients"]["viewer"], "POST", f"/runs/{receipt['run_id']}/stop")
    maintenance(settings, db)
    with db.session() as s:
        assert s.get(Run, receipt["run_id"]).status == "cancelled"


def test_role_tools_skill_profiles_and_workspace_authorization(platform, tmp_path):
    admin = platform["clients"]["admin"]
    write = find_tool(platform, "write_file")
    app = configured_agent(platform, "plan_executor")
    app["draft"]["executors"]["writer"]["capabilities"] = [write]
    app["draft"]["environment"]["workspace_access"] = "read_only"
    updated = request(admin, "PUT", f"/agents/{app['id']}/draft", app["draft"], app["revision"]).json()
    report = request(
        admin, "POST", f"/agents/{app['id']}/validate", {"draft_revision": updated["revision"]}
    ).json()
    assert not report["valid"] and any(v["code"] == "WORKSPACE_READ_ONLY" for v in report["violations"])
    skill_path = f"/spaces/{platform['space']}/resources/skill"
    data = {
        "name": "技能",
        "spec": {"files": {"SKILL.md": "---\nname: report\ndescription: example\n---\n说明"}},
    }
    assert request(admin, "POST", skill_path, data).status_code == 201
    data["spec"]["files"]["../escape"] = "bad"
    assert request(admin, "POST", skill_path, data).status_code == 422
    data["spec"]["files"] = {
        "SKILL.md": "---\nname: report\ndescription: example\nallowed-tools: exec\n---\n说明"
    }
    assert request(admin, "POST", skill_path, data).status_code == 422
    workspace = Workspace(tmp_path / "safe", False)
    with pytest.raises(ValueError):
        workspace.path("../escape")
    with pytest.raises(ValueError):
        workspace.tools()["write_file"].invoke({"path": "file", "content": "no"})
    (workspace.root / "link").symlink_to(tmp_path)
    with pytest.raises(ValueError):
        workspace.path("link/escape")
    reader, _ = skill_tool(
        [{"id": "bound", "name": "one", "description": "", "spec": {"files": {"SKILL.md": "ok"}}}]
    )
    assert reader.invoke({"skill_id": "bound"}) == "ok"
    with pytest.raises(ValueError):
        reader.invoke({"skill_id": "unbound"})


def test_secrets_not_returned_and_cross_space_references_denied(platform):
    admin = platform["clients"]["admin"]
    path = f"/resources/{platform['model']}/credential"
    secret = "a-private-test-credential"
    response = request(admin, "PUT", path, {"secret": secret})
    assert response.status_code == 200 and secret not in response.text
    assert secret not in admin.get(f"/api/v1/spaces/{platform['space']}/resources").text
    with platform["db"].session() as s:
        from agentloom_platform.persistence import Receipt, Secret

        assert secret not in s.get(Secret, platform["model"]).ciphertext
        assert all(secret not in json.dumps(r.body) for r in s.scalars(select(Receipt)))
    app = configured_agent(platform)
    with platform["db"].session(write=True) as s:
        from agentloom_platform.persistence import Resource

        s.get(Resource, platform["model"]).space_id = platform["other_space"]
    assert (
        request(admin, "PUT", f"/agents/{app['id']}/draft", app["draft"], app["revision"]).status_code == 422
    )


def test_revoked_membership_does_not_receive_replayed_receipt(platform):
    client = platform["clients"]["editor"]
    key = uuid.uuid4().hex
    path = f"/spaces/{platform['space']}/agents"
    body = {"name": "private", "mode": "react"}
    assert request(client, "POST", path, body, key=key).status_code == 201
    with platform["db"].session(write=True) as s:
        s.delete(s.get(Membership, (platform["users"]["editor"], platform["space"])))
    assert request(client, "POST", path, body, key=key).status_code == 404


@pytest.mark.parametrize("mode", ["react", "plan_executor"])
def test_real_worker_process_uses_persistent_queue(platform, mode):
    app = configured_agent(platform, mode)
    publish(platform, app)
    _, receipt = submit(platform, app)
    import os

    env = {
        **os.environ,
        **{
            "PLATFORM_" + k.upper(): str(v)
            for k, v in platform["settings"].model_dump().items()
            if k in {"database_url", "checkpoint_url", "data_dir", "secret_key", "worker_host_id"}
        },
    }
    env["PLATFORM_ALLOW_TEST_MODEL"] = "true"
    env["PYTHONPATH"] = str(Path(__file__).parents[1] / "api" / "src")
    process = subprocess.run(
        [sys.executable, "-m", "agentloom_platform.worker", "--once"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert process.returncode == 0, process.stderr
    with platform["db"].session() as s:
        assert s.get(Run, receipt["run_id"]).status == "completed", process.stderr
