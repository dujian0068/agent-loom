import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import pytest
from conftest import configured_agent, find_tool, request
from langchain_core.tools import ToolException
from sqlalchemy import select
from test_draft_preview import ask, preview
from test_process_recovery import model_server, start_worker  # noqa: F401 -- pytest fixture

from agentloom_platform import exec_service
from agentloom_platform.host_exec import exec_available, execute_command
from agentloom_platform.persistence import Operation, Run
from agentloom_platform.tools import Workspace, share_workspace


@pytest.fixture
def runner(platform):
    settings = platform["settings"]
    root = settings.data_dir / "workspaces"
    root.mkdir(exist_ok=True)
    # macOS limits Unix-socket path length; test data may live in long paths.
    with tempfile.TemporaryDirectory(prefix="al-exec-", dir="/tmp") as directory:
        settings.exec_socket_path = Path(directory) / "runner.sock"
        with (Path(directory) / "runner.log").open("w+") as log:
            process = subprocess.Popen(
                [
                    sys.executable,
                    exec_service.__file__,
                    "--socket",
                    str(settings.exec_socket_path),
                    "--workspaces",
                    str(root),
                    "--worker-uid",
                    str(os.getuid()),
                    "--test-allow-current-user",
                ],
                env={**os.environ, "PLATFORM_SECRET_KEY": "must-not-leak", "MODEL_API_KEY": "must-not-leak"},
                stdout=log,
                stderr=log,
            )
            try:
                for _ in range(100):
                    if exec_available(settings):
                        break
                    assert process.poll() is None, log.read()
                    time.sleep(0.02)
                else:
                    pytest.fail("Exec runner did not become ready")
                scope = uuid.uuid4().hex
                Workspace(root / scope, True, shared=True)
                yield settings, scope, root / scope
            finally:
                process.terminate()
                process.wait(timeout=10)
                settings.exec_socket_path = None


def execute(runner, command, timeout=10):
    settings, scope, _ = runner
    return asyncio.run(execute_command(settings, scope, command, timeout))


def test_real_command_output_workspace_and_environment(runner):
    result = execute(
        runner,
        "python3 -c 'import json, os; print(json.dumps(dict(os.environ))); print(os.getcwd())'; printf error >&2; printf data > result.txt",
    )
    env = json.loads(result["stdout"].splitlines()[0])
    assert "PLATFORM_SECRET_KEY" not in env and "MODEL_API_KEY" not in env
    assert "PYTHONPATH" not in env and "DATABASE_URL" not in env
    assert Path(result["stdout"].splitlines()[1]) == runner[2]
    assert result["stderr"] == "error" and result["exit_code"] == 0
    assert (runner[2] / "result.txt").read_text() == "data"
    assert (runner[2] / "result.txt").stat().st_mode & 0o777 == 0o660


def test_nonzero_exit_is_a_tool_error_with_stderr(runner):
    with pytest.raises(ToolException) as error:
        execute(runner, "printf failure >&2; exit 7")
    result = json.loads(str(error.value))
    assert result["exit_code"] == 7 and result["stderr"] == "failure"


def test_output_truncates_without_blocking_large_streams(runner):
    result = execute(
        runner, 'python3 -c \'import sys; print("a" * 200000); print("b" * 200000, file=sys.stderr)\''
    )
    assert len(result["stdout"].encode()) == exec_service.OUTPUT_LIMIT
    assert len(result["stderr"].encode()) == exec_service.OUTPUT_LIMIT
    assert result["stdout_truncated"] and result["stderr_truncated"]


@pytest.mark.parametrize("method", ["timeout", "cancel", "disconnect"])
def test_stop_kills_background_process_group(runner, method):
    async def scenario():
        settings, scope, workspace = runner
        command = "echo started > started; (trap '' TERM; sleep 1; echo leaked > leaked) & wait"
        if method == "disconnect":
            _reader, writer = await asyncio.open_unix_connection(str(settings.exec_socket_path))
            writer.write(
                json.dumps(
                    {
                        "op": "exec",
                        "protocol": 1,
                        "implementation": exec_service.IMPLEMENTATION,
                        "scope": scope,
                        "command": command,
                        "timeout": 10,
                    }
                ).encode()
                + b"\n"
            )
            await writer.drain()
            task = None
        else:
            task = asyncio.create_task(
                execute_command(settings, scope, command, 0.2 if method == "timeout" else 10)
            )
        for _ in range(100):
            if (workspace / "started").exists():
                break
            await asyncio.sleep(0.01)
        assert (workspace / "started").exists()
        if method == "disconnect":
            writer.close()
            await writer.wait_closed()
        else:
            if method == "cancel":
                task.cancel()
            with pytest.raises(ToolException) as error:
                await task
            result = json.loads(str(error.value))
            assert result["timed_out" if method == "timeout" else "cancelled"]
        await asyncio.sleep(1.1)
        assert not (workspace / "leaked").exists()

    asyncio.run(scenario())
    assert exec_available(runner[0])


def test_finished_shell_does_not_leave_background_jobs(runner):
    assert (
        execute(runner, "(sleep 1; echo leaked > leaked) >/dev/null 2>&1 & echo done")["stdout"] == "done\n"
    )
    time.sleep(1.1)
    assert not (runner[2] / "leaked").exists()


@pytest.mark.parametrize("scope", ["../escape", "/tmp", "not-a-session"])
def test_runner_rejects_untrusted_workspace_identifiers(runner, scope):
    with pytest.raises(ToolException, match="INVALID_WORKSPACE"):
        asyncio.run(execute_command(runner[0], scope, "echo must-not-run", 1))


def test_workspace_sharing_never_follows_links(runner, tmp_path):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    secret = private / "secret"
    secret.write_text("test-only")
    secret.chmod(0o600)
    (runner[2] / "link").symlink_to(private, target_is_directory=True)
    share_workspace(runner[2])
    assert private.stat().st_mode & 0o777 == 0o700
    assert secret.stat().st_mode & 0o777 == 0o600
    bad_scope = uuid.uuid4().hex
    (runner[2].parent / bad_scope).symlink_to(private, target_is_directory=True)
    with pytest.raises(ToolException, match="INVALID_WORKSPACE"):
        asyncio.run(execute_command(runner[0], bad_scope, "echo must-not-run", 1))


def test_runner_rejects_different_worker_identity(runner):
    async def scenario():
        path = runner[0].exec_socket_path.parent / "unauthorized.sock"
        denied = exec_service.ExecServer(runner[2].parent, os.getuid() + 999)
        server = await asyncio.start_unix_server(denied.handle, path=path)
        async with server:
            reader, writer = await asyncio.open_unix_connection(str(path))
            result = json.loads(await reader.readline())
            assert result["error"] == "UNAUTHORIZED_WORKER"
            writer.close()
            await writer.wait_closed()

    asyncio.run(scenario())


def test_runner_rejects_mismatched_implementation(runner):
    async def scenario():
        reader, writer = await asyncio.open_unix_connection(str(runner[0].exec_socket_path))
        writer.write(
            json.dumps(
                {
                    "op": "exec",
                    "protocol": 1,
                    "implementation": "old-version",
                    "scope": runner[1],
                    "command": "touch should-not-run",
                    "timeout": 1,
                }
            ).encode()
            + b"\n"
        )
        await writer.drain()
        result = json.loads(await reader.readline())
        assert result["error"] == "EXEC_VERSION_MISMATCH"
        writer.close()
        await writer.wait_closed()

    asyncio.run(scenario())
    assert not (runner[2] / "should-not-run").exists()


def test_exec_requires_opt_in_available_service_and_writable_workspace(platform, runner):
    app = configured_agent(platform, bindings=[find_tool(platform, "exec")])
    client = platform["clients"]["admin"]

    def validate():
        return request(
            client, "POST", f"/agents/{app['id']}/validate", {"draft_revision": app["revision"]}
        ).json()

    assert "EXEC_NOT_ENABLED" in {v["code"] for v in validate()["violations"]}
    app["draft"]["environment"].update(command_execution="host", workspace_access="read_only")
    app = request(client, "PUT", f"/agents/{app['id']}/draft", app["draft"], app["revision"]).json()
    assert "EXEC_REQUIRES_WRITABLE_WORKSPACE" in {v["code"] for v in validate()["violations"]}
    app["draft"]["environment"]["workspace_access"] = "read_write"
    app = request(client, "PUT", f"/agents/{app['id']}/draft", app["draft"], app["revision"]).json()
    assert validate()["valid"]
    saved = platform["settings"].exec_socket_path
    platform["settings"].exec_socket_path = None
    assert "EXEC_UNAVAILABLE" in {v["code"] for v in validate()["violations"]}
    platform["settings"].exec_socket_path = saved


def exec_app(platform, url, mode, model_id="host-exec"):
    client = platform["clients"]["admin"]
    response = request(
        client,
        "POST",
        f"/spaces/{platform['space']}/resources/model",
        {
            "name": "Local exec test model",
            "spec": {"provider": "openai", "endpoint": url, "model_id": model_id},
            "credential": "local-fixture-only",
        },
    )
    assert response.status_code == 201, response.text
    app = configured_agent(platform, mode)
    draft = app["draft"]
    draft["model"] = {"resource_id": response.json()["id"], "version": 1, "members": []}
    draft["environment"].update(command_execution="host", retention="session")
    if mode == "react":
        draft["definition"]["capabilities"] = [find_tool(platform, "exec")]
    else:
        draft["executors"]["research"]["capabilities"] = [find_tool(platform, "exec")]
        draft["executors"]["writer"]["capabilities"] = [find_tool(platform, "read_file")]
    return request(client, "PUT", f"/agents/{app['id']}/draft", draft, app["revision"]).json()


@pytest.mark.parametrize("mode", ["react", "plan_executor"])
def test_http_worker_preview_exec_and_file_tool_interoperate(platform, runner, model_server, mode):  # noqa: F811
    url, directory = model_server
    app = exec_app(platform, url, mode)
    session = preview(platform, app)
    receipt = ask(platform, session)
    with (directory / "worker.log").open("w+") as log:
        process = start_worker(platform, receipt["run_id"], log)
        try:
            assert process.wait(timeout=25) == 0
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
        log.seek(0)
        diagnostic = log.read()
    with platform["db"].session() as s:
        run = s.get(Run, receipt["run_id"])
        assert run.status == "completed", (run.error, diagnostic)
        assert run.release_id is None and run.tool_calls == (1 if mode == "react" else 2)
        operations = list(s.scalars(select(Operation).where(Operation.run_id == run.id)))
        assert all(op.status == "completed" and op.result["data"]["status"] == "success" for op in operations)
        assert any("EXEC_OK" in op.result["data"]["content"] for op in operations)
    assert (runner[2].parent / session["id"] / "exec.txt").read_text() == "host-exec-verified"


def test_worker_stop_settles_exec_receipt_before_cleanup(platform, runner, model_server):  # noqa: F811
    url, directory = model_server
    app = exec_app(platform, url, "react", "host-exec-stop")
    session = preview(platform, app)
    receipt = ask(platform, session)
    workspace = runner[2].parent / session["id"]
    with (directory / "worker.log").open("w+") as log:
        process = start_worker(platform, receipt["run_id"], log)
        try:
            for _ in range(200):
                if (workspace / "started.txt").exists():
                    break
                assert process.poll() is None
                time.sleep(0.01)
            assert (workspace / "started.txt").exists()
            assert (
                request(platform["clients"]["editor"], "POST", f"/runs/{receipt['run_id']}/stop").status_code
                == 202
            )
            assert process.wait(timeout=10) == 0
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
        log.seek(0)
        diagnostic = log.read()
    with platform["db"].session() as s:
        run = s.get(Run, receipt["run_id"])
        assert run.status == "cancelled", (run.error, diagnostic)
        operation = s.scalar(select(Operation).where(Operation.run_id == run.id))
        assert operation.status == "completed" and '"cancelled": true' in operation.result["data"]["content"]
    time.sleep(2.1)
    assert not (workspace / "leaked.txt").exists()


def test_worker_crash_stops_command_and_blocks_uncertain_replay(platform, runner, model_server):  # noqa: F811
    url, directory = model_server
    app = exec_app(platform, url, "react", "host-exec-stop")
    session = preview(platform, app)
    receipt = ask(platform, session)
    workspace = runner[2].parent / session["id"]
    with (directory / "worker.log").open("w+") as log:
        process = start_worker(platform, receipt["run_id"], log)
        try:
            for _ in range(300):
                if (workspace / "started.txt").exists():
                    break
                assert process.poll() is None
                time.sleep(0.01)
            assert (workspace / "started.txt").exists()
        finally:
            process.kill()
            process.wait(timeout=5)
        time.sleep(2.1)
        assert not (workspace / "leaked.txt").exists()
        recovered = start_worker(platform, receipt["run_id"], log)
        try:
            assert recovered.wait(timeout=10) == 0
        finally:
            if recovered.poll() is None:
                recovered.kill()
                recovered.wait(timeout=5)
        log.seek(0)
        diagnostic = log.read()
    with platform["db"].session() as s:
        run = s.get(Run, receipt["run_id"])
        assert run.status == "failed" and "结果未确认" in run.error, (run.error, diagnostic)
        assert run.tool_calls == 1
        operations = list(s.scalars(select(Operation).where(Operation.run_id == run.id)))
        assert len(operations) == 1 and operations[0].status == "pending"
