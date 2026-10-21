import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from conftest import configured_agent, find_tool, publish, request, submit
from sqlalchemy import select

from agentloom_platform.persistence import Input, Operation, Run


@pytest.fixture
def model_server(tmp_path, monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost,::1")
    directory = tmp_path / "model-server"
    directory.mkdir()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    url = f"http://127.0.0.1:{port}/v1"
    with (directory / "server.log").open("w+") as log:
        server = subprocess.Popen(
            [sys.executable, str(Path(__file__).with_name("model_server.py")), str(port), str(directory)],
            stdout=log,
            stderr=log,
        )
        try:
            for _ in range(100):
                try:
                    httpx.get(url, timeout=0.2, trust_env=False)
                    break
                except httpx.TransportError:
                    assert server.poll() is None, "Local model fixture exited"
                    time.sleep(0.05)
            else:
                pytest.fail("Local model fixture did not start")
            yield url, directory
        finally:
            server.terminate()
            server.wait(timeout=10)


def prepare_app(platform, url, model_id, *, budget=20):
    client = platform["clients"]["admin"]
    response = request(
        client,
        "POST",
        f"/spaces/{platform['space']}/resources/model",
        {
            "name": "Local HTTP model",
            "spec": {"provider": "openai", "endpoint": url, "model_id": model_id},
        },
    )
    assert response.status_code == 201, response.text
    model = response.json()
    assert (
        request(
            client,
            "PUT",
            f"/resources/{model['id']}/credential",
            {
                "secret": "local-fixture-only-no-provider",
            },
        ).status_code
        == 200
    )
    app = configured_agent(platform, "plan_executor")
    app["draft"]["model"] = {"resource_id": model["id"], "version": 1, "members": []}
    app["draft"]["executors"]["research"]["capabilities"] = [find_tool(platform, "write_file")]
    app["draft"]["execution"]["model_calls"] = budget
    app = request(client, "PUT", f"/agents/{app['id']}/draft", app["draft"], app["revision"]).json()
    publish(platform, app)
    return app


def start_worker(platform, run_id, log):
    env = {
        **os.environ,
        **{
            "PLATFORM_" + key.upper(): str(value)
            for key, value in platform["settings"].model_dump().items()
            if key in {"database_url", "checkpoint_url", "data_dir", "secret_key", "worker_host_id"}
        },
        "PYTHONPATH": str(Path(__file__).parents[1] / "api" / "src"),
        "PLATFORM_POLL_SECONDS": "0.01",
    }
    return subprocess.Popen(
        [sys.executable, "-m", "agentloom_platform.worker", "--run", run_id],
        env=env,
        stdout=log,
        stderr=log,
    )


@pytest.mark.parametrize("mode", ["react", "plan_executor"])
def test_discovered_model_runs_with_saved_key_and_published_version(platform, model_server, mode):
    url, directory = model_server
    client = platform["clients"]["admin"]
    response = request(
        client,
        "POST",
        f"/spaces/{platform['space']}/model-catalog",
        {
            "endpoint": url,
            "credential": "catalog-e2e-key",
        },
    )
    assert response.status_code == 200, response.text
    models = response.json()["items"]
    assert models == [{"id": "catalog-chat-a"}, {"id": "catalog-chat-b"}]
    body = {
        "name": "Discovered model",
        "spec": {"endpoint": url, "model_id": models[0]["id"]},
        "credential": "catalog-e2e-key",
    }
    created = request(client, "POST", f"/spaces/{platform['space']}/resources/model", body)
    assert created.status_code == 201, created.text
    resource = created.json()
    app = configured_agent(platform, mode)
    app["draft"]["model"] = {"resource_id": resource["id"], "version": 1, "members": []}
    if mode == "plan_executor":
        app["draft"]["executors"]["research"]["capabilities"] = [find_tool(platform, "write_file")]
    updated = request(client, "PUT", f"/agents/{app['id']}/draft", app["draft"], app["revision"])
    assert updated.status_code == 200, updated.text
    app = updated.json()
    publish(platform, app)
    version = request(
        client,
        "POST",
        f"/resources/{resource['id']}/versions",
        {
            "name": body["name"],
            "spec": {**body["spec"], "model_id": models[1]["id"]},
        },
    )
    assert version.status_code == 201, version.text
    session, receipt = submit(platform, app)
    with (directory / "worker.log").open("w+") as log:
        process = start_worker(platform, receipt["run_id"], log)
        try:
            assert process.wait(timeout=30) == 0
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
    with platform["db"].session() as s:
        run = s.get(Run, receipt["run_id"])
        assert run.status == "completed", (run.status, run.error)
        if mode == "plan_executor":
            assert run.tool_calls == 1
    calls = [json.loads(line) for line in (directory / "requests.jsonl").read_text().splitlines()]
    assert calls and all(call["model"] == "catalog-chat-a" for call in calls)
    history = platform["clients"]["viewer"].get(f"/api/v1/sessions/{session['id']}/history")
    assert history.status_code == 200
    assert "catalog-e2e-key" not in history.text
    assert history.json()["runs"][0]["status"] == "completed"


def test_sigkill_mid_plan_recovers_checkpoint_without_repeating_completed_steps(platform, model_server):
    url, directory = model_server
    app = prepare_app(platform, url, "plan-crash")
    _, receipt = submit(platform, app)
    run_id = receipt["run_id"]
    with (directory / "worker.log").open("w+") as log:
        process = start_worker(platform, run_id, log)
        try:
            deadline = time.monotonic() + 30
            while not (directory / "writer-entered").exists() and time.monotonic() < deadline:
                assert process.poll() is None, "Worker exited before the crash boundary"
                time.sleep(0.02)
            assert (directory / "writer-entered").exists(), "Writer did not reach the HTTP boundary"
            with platform["db"].session() as s:
                run = s.get(Run, run_id)
                original_thread = run.thread_id
                assert run.status == "running" and run.tool_calls == 1
                assert s.scalar(select(Input).where(Input.run_id == run_id)).status == "accepted"
            process.kill()
            process.wait(timeout=10)
            (directory / "writer-released").touch()
            process = start_worker(platform, run_id, log)
            assert process.wait(timeout=40) == 0
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
        log.seek(0)
        diagnostics = log.read()
    with platform["db"].session() as s:
        run = s.get(Run, run_id)
        assert run.status == "completed", (run.error, diagnostics)
        assert run.result == "finish complete" and run.generation == 2 and run.thread_id == original_thread
        assert run.tool_calls == 1 and run.model_calls == 6  # Includes the killed HTTP attempt.
        operations = list(s.scalars(select(Operation).where(Operation.run_id == run_id)))
        assert len(operations) == 1 and operations[0].status == "completed"
    phases = [json.loads(line)["phase"] for line in (directory / "requests.jsonl").read_text().splitlines()]
    assert phases == ["plan", "research", "research", "writer", "writer", "finish"]
    assert (
        platform["settings"].data_dir / "workspaces" / run_id / "research.txt"
    ).read_text() == "Completed research"


@pytest.mark.parametrize(
    ("model_id", "budget", "expected", "http_calls"),
    [
        ("http-failure", 20, "模型或工具调用失败", 1),
        ("unknown-executor", 20, "未授权执行器", 1),
        ("normal", 1, "预算已耗尽", 1),
    ],
)
def test_real_http_errors_and_shared_plan_budget_fail_without_external_effects(
    platform,
    model_server,
    model_id,
    budget,
    expected,
    http_calls,
):
    url, directory = model_server
    app = prepare_app(platform, url, model_id, budget=budget)
    session, receipt = submit(platform, app)
    with (directory / "worker.log").open("w+") as log:
        process = start_worker(platform, receipt["run_id"], log)
        try:
            assert process.wait(timeout=30) == 0
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
    with platform["db"].session() as s:
        run = s.get(Run, receipt["run_id"])
        assert run.status == "failed" and expected in run.error, (run.status, run.error)
        assert run.tool_calls == 0 and run.result is None
    history = platform["clients"]["viewer"].get(f"/api/v1/sessions/{session['id']}/history")
    assert "private-provider-diagnostic" not in history.text and "local-fixture-only" not in history.text
    assert len((directory / "requests.jsonl").read_text().splitlines()) == http_calls
