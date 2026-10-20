import asyncio

import pytest
from conftest import configured_agent, publish, request, submit

from agentloom_platform import worker
from agentloom_platform.persistence import Application, ReleaseJob, Run


@pytest.mark.parametrize("retention", ["run", "session"])
def test_workspace_retention_and_cleanup_retry_gate_release_activation(platform, monkeypatch, retention):
    app = configured_agent(platform)
    app["draft"]["environment"]["retention"] = retention
    client = platform["clients"]["admin"]
    app = request(client, "PUT", f"/agents/{app['id']}/draft", app["draft"], app["revision"]).json()
    original = publish(platform, app)
    session, receipt = submit(platform, app)
    asyncio.run(worker.run_one(platform["settings"], platform["db"], receipt["run_id"]))
    scope = receipt["run_id"] if retention == "run" else session["id"]
    workspace = platform["settings"].data_dir / "workspaces" / scope
    (workspace / "result.txt").write_text("preserved until cleanup")
    app["draft"]["description"] = "next version"
    app = request(client, "PUT", f"/agents/{app['id']}/draft", app["draft"], app["revision"]).json()
    release = request(
        client,
        "POST",
        f"/agents/{app['id']}/releases",
        {
            "draft_revision": app["revision"],
        },
        app["revision"],
    ).json()

    original_remove = worker.shutil.rmtree

    def unavailable_disk(*args, **kwargs):
        raise OSError("temporary cleanup failure")

    monkeypatch.setattr(worker.shutil, "rmtree", unavailable_disk)
    worker.maintenance(platform["settings"], platform["db"])
    with platform["db"].session() as s:
        if retention == "run":
            assert s.get(Run, receipt["run_id"]).cleanup_state == "failed"
            assert s.get(Application, app["id"]).release_id == original["release_id"]
            assert s.get(ReleaseJob, release["job_id"]).status == "waiting_cleanup"
        else:
            assert s.get(Run, receipt["run_id"]).cleanup_state == "completed"
    monkeypatch.setattr(worker.shutil, "rmtree", original_remove)
    worker.maintenance(platform["settings"], platform["db"])
    with platform["db"].session() as s:
        assert s.get(Application, app["id"]).release_id == release["release_id"]
        assert s.get(ReleaseJob, release["job_id"]).status == "succeeded"
        assert s.get(Run, receipt["run_id"]).cleanup_state == "completed"
    assert workspace.exists() is (retention == "session")
    if retention == "session":
        assert (workspace / "result.txt").read_text() == "preserved until cleanup"
