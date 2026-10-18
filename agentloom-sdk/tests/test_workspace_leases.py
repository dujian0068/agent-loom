"""Execution ownership across synchronous SPI calls, interruption and takeover."""

import asyncio
import threading
import time
from functools import wraps

import pytest

from agentloom import (
    FILE_OPERATIONS,
    AgentDefinition,
    AgentLoomError,
    CapabilityBinding,
    CapabilityRegistry,
    ExecutionPolicy,
    FileProperties,
    InfrastructureError,
    ModelResponse,
    PlanAgent,
    ReactAgent,
    RuntimeEngine,
    ScriptedModel,
    ToolCall,
    WorkspaceBinding,
    file_tools,
)
from agentloom.runtime.control import request_stop
from agentloom.storage import CommitCoordinator, claim


@pytest.fixture
def guarded_provider(file_provider, monkeypatch):
    """Keep real installed entry-point discovery; instrument the selected class."""
    base = file_provider.TestFiles

    class GuardedFiles(base):
        def __init__(self):
            super().__init__()
            self.before = lambda context, phase: None
            self.contexts = []

        def prepare(self, context):
            super().prepare(context)
            self.data[context.workspace_id]["a.txt"] = "one\n"
            self.contexts.append(context)
            self.before(context, "prepare")

        def close(self):
            run_ids = {context.run_id for context in self.contexts}
            assert not any(
                thread.name == "agentloom-execution-lease:" + run_id
                for thread in threading.enumerate()
                for run_id in run_ids
            ), "Execution heartbeat outlived its provider"
            super().close()

    def guarded(method):
        @wraps(method)
        def call(self, context, *args, **kwargs):
            self.contexts.append(context)
            self.before(context, method.__name__)
            return method(self, context, *args, **kwargs)

        return call

    for name in FILE_OPERATIONS:
        setattr(GuardedFiles, name, guarded(getattr(base, name)))
    monkeypatch.setattr(file_provider, "TestFiles", GuardedFiles)
    return file_provider


def file_definition(*names):
    return AgentDefinition(
        "files",
        "Use the authorized file tools.",
        "Perform file operations.",
        capabilities=tuple(
            CapabilityBinding(name, "builtin_tool", name, "files", name) for name in names
        ),
    )


def program(runtime, model, *, operation="write", executor=False, policy=None):
    options = {
        "runtime_engine": runtime,
        "model": ScriptedModel(model),
        "workspace": WorkspaceBinding(),
        "capabilities": CapabilityRegistry(tools=(file_tools(),)),
        "execution_policy": policy or ExecutionPolicy(loop_max=4),
    }
    if executor:
        return PlanAgent(
            definition=AgentDefinition("planner", "Delegate file work."),
            executors={"worker": file_definition(operation)},
            **options,
        )
    return ReactAgent(definition=file_definition(operation), **options)


def delay(context, seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        context.check_active()
        time.sleep(0.01)
    context.check_active()


@pytest.mark.parametrize("operation", FILE_OPERATIONS)
async def test_executor_keeps_lease_for_every_sync_file_operation(
    runtime_store, guarded_provider, operation
):
    etag = guarded_provider.version("one\n")
    arguments = {
        "read": {"path": "a.txt"},
        "write": {"path": "new.txt", "content": "new"},
        "list": {},
        "mkdir": {"path": "new"},
        "stat": {"path": "a.txt"},
        "edit": {"path": "a.txt", "old_text": "one", "new_text": "two", "if_match": etag},
        "patch": {
            "path": "a.txt",
            "unified_diff": "--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-one\n+two\n",
            "if_match": etag,
        },
        "move": {"source": "a.txt", "destination": "new.txt", "if_match": etag},
        "delete": {"path": "a.txt", "if_match": etag},
        "glob": {"pattern": "*.txt"},
        "grep": {"pattern": "one"},
    }[operation]
    requests = []

    def model(request):
        requests.append(request)
        if any(message["role"] == "tool" for message in request.messages):
            return ModelResponse("done")
        call = (
            ToolCall("delegate_task", {"executor": "worker", "task": "Perform file work."})
            if request.role_key == "planner"
            else ToolCall(operation, arguments)
        )
        return ModelResponse(tool_calls=(call,))

    async with RuntimeEngine(
        store=runtime_store, lease_seconds=0.3, files=FileProperties(provider="test-files")
    ) as runtime:
        provider = runtime._files.backend
        provider.before = lambda context, phase: (
            delay(context, 0.65) if phase == operation else None
        )
        result = await program(runtime, model, operation=operation, executor=True).ainvoke("go")
        assert result.status == "succeeded"
        state = runtime._load(result.run_id)
        tool = next(
            op for op in state["operations"].values() if op["event_type"] == "capability.invoke"
        )
        assert tool["raw"]["status"] == "succeeded"
        assert len(tool["attempts"]) == 1
        assert [request.role_key for request in requests] == [
            "planner",
            "worker",
            "worker",
            "planner",
        ]
        assert any(
            call["method"] == operation and call["role"] == "worker" for call in provider.calls
        )
        assert state["lease"]["epoch"] == 1


@pytest.mark.parametrize("phase", ["prepare", "write"])
@pytest.mark.parametrize("fault", ["stop", "deadline", "takeover", "renewal"])
async def test_sync_file_guard_rejects_interruption_before_publication(
    runtime_store, guarded_provider, monkeypatch, phase, fault
):
    entered = threading.Event()
    renewal_failed = threading.Event()
    contexts, requests, control_errors = [], [], []
    original_renew = CommitCoordinator.renew

    def renew(coordinator, ttl):
        if (
            fault == "renewal"
            and entered.is_set()
            and threading.current_thread().name.startswith("agentloom-execution-lease:")
        ):
            renewal_failed.set()
            raise InfrastructureError("TEST_RENEWAL_FAILED")
        return original_renew(coordinator, ttl)

    monkeypatch.setattr(CommitCoordinator, "renew", renew)

    def block(context, current):
        if current == phase:
            contexts.append(context)
            entered.set()
            delay(context, 4)
            pytest.fail("Interrupted provider was allowed to continue")

    def interrupt():
        try:
            assert entered.wait(5)
            if fault == "renewal":
                assert renewal_failed.wait(5)
                return
            context = contexts[0]
            with runtime_store.transaction("default") as tx:
                state = tx.get("runs", context.run_id)
                if fault == "stop":
                    request_stop(tx, context.run_id, AgentLoomError("RUN_STOPPED").to_dict())
                else:
                    if fault == "takeover":
                        state["lease"]["expires_at"] = 0
                    else:
                        state["deadline_at"] = tx.now - 1
                    tx.put("runs", context.run_id, state)
            if fault == "takeover":
                successor = claim(runtime_store, "default", context.run_id, "successor", 30)
                assert successor is not None and successor.epoch == context.lease_epoch + 1
        except BaseException as exc:
            control_errors.append(exc)

    def model(request):
        requests.append(request)
        return ModelResponse(tool_calls=(ToolCall("write", {"path": "new.txt", "content": "new"}),))

    async with RuntimeEngine(
        store=runtime_store, lease_seconds=0.6, files=FileProperties(provider="test-files")
    ) as runtime:
        provider = runtime._files.backend
        provider.before = block
        controller = threading.Thread(target=interrupt)
        controller.start()
        try:
            handle = await program(runtime, model).start("go")
            if fault in {"takeover", "renewal"}:
                code = "LEASE_LOST" if fault == "takeover" else "TEST_RENEWAL_FAILED"
                with pytest.raises(InfrastructureError, match=code):
                    await handle.wait(timeout_s=6)
            else:
                result = await handle.wait(timeout_s=6)
                assert result.status == ("cancelled" if fault == "stop" else "timed_out")
        finally:
            controller.join(6)
        assert not controller.is_alive() and not control_errors
        assert len(requests) == (0 if phase == "prepare" else 1)
        assert not any(call["method"] == "write" for call in provider.calls)
        assert all("new.txt" not in files for files in provider.data.values())
        state = runtime._load(handle.run_id)
        if fault == "takeover":
            assert state["lease"]["worker_id"] == "successor"
            assert state["lease"]["expires_at"] > time.time()
        if fault in {"takeover", "renewal"}:
            assert state["terminal_seq"] is None
        else:
            assert any(call["method"] == "revoke" for call in provider.calls)
            assert state["cleanup_job"]["scope"]["workspace_id"] == contexts[0].workspace_id


async def test_sync_tool_deadline_still_expires_with_healthy_lease(runtime_store, guarded_provider):
    def model(request):
        if any(message["role"] == "tool" for message in request.messages):
            return ModelResponse("Write timed out.")
        return ModelResponse(tool_calls=(ToolCall("write", {"path": "new.txt", "content": "new"}),))

    async with RuntimeEngine(
        store=runtime_store, lease_seconds=0.3, files=FileProperties(provider="test-files")
    ) as runtime:
        provider = runtime._files.backend
        provider.before = lambda context, phase: delay(context, 2) if phase == "write" else None
        result = await program(
            runtime, model, policy=ExecutionPolicy(loop_max=2, tool_timeout_seconds=1)
        ).ainvoke("go")
        assert result.status == "succeeded"
        tool = next(
            op
            for op in runtime._load(result.run_id)["operations"].values()
            if op["event_type"] == "capability.invoke"
        )
        assert tool["raw"]["error"]["code"] == "TOOL_TIMEOUT"
        assert len(tool["attempts"]) == 1
        assert not any(call["method"] == "write" for call in provider.calls)


async def test_partial_prepare_failure_keeps_scope_for_revocation(runtime_store, guarded_provider):
    def fail(context, phase):
        if phase == "prepare":
            raise AgentLoomError("FILE_ACCESS_DENIED", category="permission")

    async with RuntimeEngine(
        store=runtime_store, files=FileProperties(provider="test-files")
    ) as runtime:
        provider = runtime._files.backend
        provider.before = fail
        result = await program(runtime, lambda request: pytest.fail("Preparation failed")).ainvoke(
            "go"
        )
        assert result.status == "failed" and result.error["code"] == "FILE_ACCESS_DENIED"
        state = runtime._load(result.run_id)
        assert state["root_task_id"] is None
        scope = state["cleanup_job"]["scope"]
        assert scope["workspace_id"] == provider.contexts[0].workspace_id
        assert any(call["method"] == "revoke" for call in provider.calls)


async def test_shutdown_drains_execution_heartbeat_and_slow_revocation(
    runtime_store, guarded_provider
):
    entered = asyncio.Event()

    async def model(request):
        entered.set()
        await asyncio.Event().wait()

    async with RuntimeEngine(
        store=runtime_store, lease_seconds=0.3, files=FileProperties(provider="test-files")
    ) as runtime:
        provider = runtime._files.backend
        original_revoke = provider.revoke

        def revoke(context):
            delay(context, 0.65)
            original_revoke(context)

        provider.revoke = revoke
        handle = await program(runtime, model).start("go")
        await entered.wait()
    state = runtime_store.load("default", handle.run_id)
    assert state["status"] == "running" and state["terminal_seq"] is None
    assert state["lease"]["expires_at"] == 0
    assert any(call["method"] == "revoke" for call in provider.calls)
    assert provider.closed == 1
