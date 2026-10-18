"""Installed provider discovery plus runtime authorization, recovery and ownership."""

import asyncio
import json
import sys
import time
from types import SimpleNamespace

import pytest

from agentloom import (
    FILE_OPERATIONS,
    AgentDefinition,
    AgentLoom,
    AgentLoomError,
    AgentProperties,
    CapabilityBinding,
    CapabilityRegistry,
    ConfigurationError,
    ExecutionPolicy,
    FileProperties,
    FunctionHookHandler,
    HookBinding,
    HookDecision,
    HookSet,
    InfrastructureError,
    ModelResponse,
    PropertiesConfig,
    ReactAgent,
    RequestError,
    RetryPolicy,
    RuntimeEngine,
    ScriptedModel,
    ServiceProperties,
    ToolCall,
    WorkspaceBackend,
    WorkspaceBinding,
    file_tools,
)
from agentloom.workspace.loader import ENTRY_POINT_GROUP, resolve_provider
from agentloom.workspace.service import WorkspaceService


def properties(**options):
    return FileProperties(provider="test-files", options=options)


def definition(*operations, access=None):
    return AgentDefinition(
        "files",
        "Use the authorized file tools.",
        capabilities=tuple(
            CapabilityBinding(
                binding_key=name,
                kind="builtin_tool",
                visible_name=name,
                adapter_key="files",
                member_key=name,
                access_scope=access or {},
            )
            for name in operations
        ),
    )


def agent(runtime, respond, *operations, **options):
    return ReactAgent(
        runtime_engine=runtime,
        model=ScriptedModel(respond),
        definition=definition(*operations),
        workspace=WorkspaceBinding(),
        capabilities=CapabilityRegistry(tools=(file_tools(),)),
        **options,
    )


def service(runtime, *, persistent=False, session="s", run="r", guard=lambda: None, access=None):
    scope = runtime._files.prepare(
        runtime, WorkspaceBinding(persistent_sandbox=persistent), session, run, 1, guard
    )
    return WorkspaceService(
        runtime.environment,
        scope,
        guard,
        access or {},
        backend=runtime._files.backend,
        context=runtime._files.context(scope, guard, access_scope=access or {}),
    )


async def test_installed_package_all_eleven_operations_and_default_options(file_provider, tmp_path):
    async with RuntimeEngine(files=properties()) as runtime:
        provider = runtime._files.backend
        files = service(runtime)
        files.mkdir("dir")
        first = files.write("dir/a.txt", "one\n")
        assert files.read("dir/a.txt")["text"] == "one\n"
        assert files.stat("dir/a.txt")["etag"] == first["etag"]
        assert files.list("dir")["entries"][0]["name"] == "a.txt"
        second = files.edit("dir/a.txt", "one", "two", first["etag"])
        with pytest.raises(AgentLoomError, match="FILE_CHANGED"):
            files.write("dir/a.txt", "stale", mode="replace", if_match=first["etag"])
        patched = files.patch(
            "dir/a.txt",
            "--- a/dir/a.txt\n+++ b/dir/a.txt\n@@ -1 +1 @@\n-two\n+three\n",
            second["etag"],
        )
        moved = files.move("dir/a.txt", "dir/b.txt", patched["etag"])
        assert files.glob("*.txt", "dir")["paths"] == ["dir/b.txt"]
        assert files.grep("three", "dir")["matches"][0]["path"] == "dir/b.txt"
        files.delete("dir/b.txt", moved["etag"])
        assert set(FILE_OPERATIONS) <= {c["method"] for c in provider.calls}
        assert "loom_test_files.unselected" not in sys.modules
        assert not (tmp_path / "workspace").exists()
    assert provider.closed == 1
    await runtime.aclose()
    assert provider.closed == 1


async def test_config_instantiates_once_shares_runs_but_isolates_workspaces(
    file_provider, runtime_store
):
    config = PropertiesConfig(
        files=properties(label="PRIVATE_OPTIONS"),
        defaults=AgentProperties(workspace=WorkspaceBinding()),
        services=ServiceProperties(
            model=ScriptedModel(lambda request: ModelResponse("done")), store=runtime_store
        ),
    )
    app = AgentLoom(config)
    assert app._runtime is None
    async with app:
        provider = app.runtime._files.backend
        one = service(app.runtime, session="one")
        two = service(app.runtime, session="two")
        one.write("private", "one")
        with pytest.raises(FileNotFoundError):
            two.read("private")
        assert app.runtime._files.backend is provider
        result = await app.build(AgentDefinition("a", "SYSTEM")).ainvoke("go")
        assert result.status == "succeeded"
        assert "PRIVATE_OPTIONS" not in json.dumps(app.runtime._load(result.run_id))
        assert app.runtime._load(result.run_id)["bindings"]["file_provider"]["version"] == "1.2.3"


@pytest.mark.parametrize(
    "operation,arguments,access,code",
    [
        ("write", {"path": "ok", "content": "x"}, {"read_only": True}, "WORKSPACE_READ_ONLY"),
        ("read", {"path": "private"}, {"paths": ["allowed"]}, "PATH_FORBIDDEN"),
        (
            "move",
            {"source": "ok/a", "destination": "private", "if_match": "v"},
            {"paths": ["ok"]},
            "PATH_FORBIDDEN",
        ),
        ("read", {"path": "../private"}, {}, "PATH_FORBIDDEN"),
        ("write", {"path": ".", "content": "x"}, {}, "WORKSPACE_ROOT_PROTECTED"),
        ("delete", {"path": "a", "if_match": None}, {}, "FILE_ARGUMENTS_INVALID"),
        ("grep", {"pattern": "x", "literal": False}, {}, "FILE_ARGUMENTS_INVALID"),
    ],
)
async def test_authorization_and_arguments_fail_before_provider(
    file_provider, operation, arguments, access, code
):
    async with RuntimeEngine(files=properties()) as runtime:
        files = service(runtime, access=access)
        before = list(runtime._files.backend.calls)
        with pytest.raises(AgentLoomError, match=code) as error:
            getattr(files, operation)(**arguments)
        assert error.value.execution_fact == error.value.external_outcome == "not_started"
        assert runtime._files.backend.calls == before


@pytest.mark.parametrize(
    "bad",
    [
        {"paths": ["/etc/passwd"], "truncated": False},
        {"paths": ["../escape"], "truncated": False},
        {"paths": ["private/a"], "truncated": False},
        {"paths": [], "truncated": False, "credentials": "SECRET"},
        {"paths": ["ok/z", "ok/a"], "truncated": False},
        {"paths": ["ok/a", "ok/b"], "truncated": False},
    ],
)
async def test_provider_results_cannot_escape_contract(file_provider, bad):
    async with RuntimeEngine(files=properties()) as runtime:
        files = service(runtime, access={"paths": ["ok"]})
        runtime._files.backend.glob = lambda *a, **k: bad
        with pytest.raises(AgentLoomError, match="FILE_RESULT_INVALID") as error:
            files.glob("*", "ok", limit=1)
        assert error.value.execution_fact == "completed"


def test_selection_errors_do_not_import_other_modules(file_provider, monkeypatch):
    with pytest.raises(ConfigurationError, match="FILE_PROVIDER_NOT_FOUND"):
        resolve_provider(FileProperties(provider="absent-provider"))
    from agentloom.workspace import loader

    points = list(loader.metadata.entry_points(group=ENTRY_POINT_GROUP))
    chosen = next(p for p in points if p.name == "test-files")
    monkeypatch.setattr(loader.metadata, "entry_points", lambda **kwargs: [chosen, chosen])
    with pytest.raises(ConfigurationError, match="FILE_PROVIDER_DUPLICATE"):
        RuntimeEngine(files=properties())


@pytest.mark.parametrize(
    "kind", ["not-class", "not-subclass", "incomplete", "wrong-signature", "async"]
)
async def test_invalid_provider_interface_rejected_before_construction(
    file_provider, monkeypatch, kind
):
    from agentloom.workspace import loader

    base = file_provider.TestFiles
    if kind == "not-class":

        def target():
            return base()
    elif kind == "not-subclass":
        target = object
    elif kind == "incomplete":
        target = type("Incomplete", (WorkspaceBackend,), {})
    elif kind == "wrong-signature":
        target = type("WrongSignature", (base,), {"read": lambda self: None})
    else:

        async def read(self, *args, **kwargs):
            return {}

        target = type("AsyncProvider", (base,), {"read": read})
    point = SimpleNamespace(
        name="bad",
        value="bad:Backend",
        dist=SimpleNamespace(metadata={"Name": "bad"}, version="1"),
        load=lambda: target,
    )
    monkeypatch.setattr(loader.metadata, "entry_points", lambda **k: [point])
    runtime = RuntimeEngine(files=FileProperties(provider="bad"))
    with pytest.raises(ConfigurationError, match="FILE_PROVIDER_INTERFACE_INVALID"):
        await runtime.__aenter__()
    assert runtime.store._closed


async def test_constructor_and_close_failure_cleanup_is_retryable(file_provider, monkeypatch):
    runtime = RuntimeEngine(files=properties(unknown="SECRET"))
    with pytest.raises(ConfigurationError, match="FILE_PROVIDER_LOAD_FAILED") as error:
        await runtime.__aenter__()
    assert runtime.store._closed and "SECRET" not in str(error.value)
    runtime = RuntimeEngine(files=properties())
    await runtime.__aenter__()
    provider = runtime._files.backend
    calls = 0

    def fail_once():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("SECRET")

    monkeypatch.setattr(provider, "close", fail_once)
    with pytest.raises(InfrastructureError, match="FILE_PROVIDER_CLOSE_FAILED") as error:
        await runtime.aclose()
    assert "SECRET" not in str(error.value)
    assert not runtime.store._closed
    await runtime.aclose()
    assert calls == 2 and runtime.store._closed


@pytest.mark.parametrize("change", ["options", "local", "version"])
async def test_recovery_rejects_changed_provider_before_prepare(
    file_provider, runtime_store, monkeypatch, change
):
    def crash(command, state):
        if command.startswith("environment:"):
            raise RuntimeError("crash")

    async with RuntimeEngine(
        files=properties(), store=runtime_store, checkpoint_callback=crash
    ) as runtime:
        handle = await agent(runtime, lambda r: ModelResponse("done")).start(
            "go", run_id="recovery"
        )
        with pytest.raises(InfrastructureError):
            await handle.wait(timeout_s=2)
    changed = properties(label="changed") if change == "options" else properties()
    if change == "local":
        changed = FileProperties()
    if change == "version":
        from agentloom.workspace import loader

        original = loader.metadata.entry_points(group=ENTRY_POINT_GROUP)
        point = next(p for p in original if p.name == "test-files")
        ep = SimpleNamespace(
            name=point.name,
            value=point.value,
            load=point.load,
            dist=SimpleNamespace(metadata={"Name": "agentloom-test-file-provider"}, version="2"),
        )
        monkeypatch.setattr(loader.metadata, "entry_points", lambda **k: [ep])
    async with RuntimeEngine(files=changed, store=runtime_store) as runtime:
        if change == "local":
            other = ReactAgent(
                runtime_engine=runtime,
                model=ScriptedModel(lambda r: ModelResponse("done")),
                definition=definition(),
                workspace=WorkspaceBinding("work"),
            )
        else:
            other = agent(runtime, lambda r: ModelResponse("done"))
        with pytest.raises(RequestError, match="RECOVERY_BINDING_MISMATCH"):
            await other.start(run_id="recovery")
        if change != "local":
            assert not runtime._files.backend.calls


async def test_raw_recovery_keeps_write_once_and_repeats_only_after_hook(
    file_provider, runtime_store
):
    hooks_seen = []

    async def hook(event, context):
        hooks_seen.append(event.stage)
        return HookDecision()

    def respond(request):
        if not any(m["role"] == "tool" for m in request.messages):
            return ModelResponse(tool_calls=(ToolCall("write", {"path": "a", "content": "one"}),))
        return ModelResponse("done")

    hooks = HookSet(
        (HookBinding("after", "capability.invoke", "response", "h"),),
        {"h": FunctionHookHandler(hook)},
    )

    def crash(command, state):
        if command.startswith("attempt-result:") and any(
            o["event_type"] == "capability.invoke" and o.get("raw")
            for o in state["operations"].values()
        ):
            raise RuntimeError("crash after raw")

    async with RuntimeEngine(
        files=properties(), store=runtime_store, checkpoint_callback=crash
    ) as runtime:
        handle = await agent(runtime, respond, "write", hooks=hooks).start(
            "go", run_id="recover-write"
        )
        with pytest.raises(InfrastructureError):
            await handle.wait(timeout_s=3)
        assert hooks_seen == []
        assert runtime._load(handle.run_id)["status"] not in {"succeeded", "failed"}
        assert len([c for c in runtime._files.backend.calls if c["method"] == "write"]) == 1
    async with RuntimeEngine(files=properties(), store=runtime_store) as runtime:
        result = await agent(runtime, respond, "write", hooks=hooks).ainvoke(run_id="recover-write")
        assert result.status == "succeeded"
        assert not [c for c in runtime._files.backend.calls if c["method"] == "write"]
        assert hooks_seen == ["response"]


async def test_hooks_and_retry_remain_in_runtime_pipeline(
    file_provider, runtime_store, monkeypatch
):
    stages = []

    async def hook(event, context):
        stages.append(event.stage)
        return HookDecision()

    calls = 0

    def respond(request):
        if len([m for m in request.messages if m["role"] == "tool"]) == 0:
            return ModelResponse(tool_calls=(ToolCall("write", {"path": "file", "content": "ok"}),))
        return ModelResponse("done")

    async with RuntimeEngine(files=properties(), store=runtime_store) as runtime:
        original = runtime._files.backend.write

        def flaky(context, *args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise AgentLoomError(
                    "TEMPORARY",
                    category="transport",
                    retryable_transport=True,
                    external_outcome="not_started",
                )
            return original(context, *args, **kwargs)

        monkeypatch.setattr(runtime._files.backend, "write", flaky)
        hooks = HookSet(
            tuple(HookBinding(s, "capability.invoke", s, "h") for s in ("request", "response")),
            {"h": FunctionHookHandler(hook)},
        )
        result = await agent(
            runtime,
            respond,
            "write",
            hooks=hooks,
            execution_policy=ExecutionPolicy(
                retry=RetryPolicy(enabled=True, max_attempts=2, delay_seconds=0.001)
            ),
        ).ainvoke("go")
        assert result.status == "succeeded" and calls == 2
        assert stages == ["request", "response"]
        tool_ops = [
            o
            for o in runtime._load(result.run_id)["operations"].values()
            if o["event_type"] == "capability.invoke"
        ]
        assert len(tool_ops) == 1 and len(tool_ops[0]["attempts"]) == 2


async def test_cleanup_routes_to_provider_and_preserves_persistent_scope(
    file_provider, runtime_store
):
    async with RuntimeEngine(files=properties(), store=runtime_store) as runtime:
        files = service(runtime)
        files.mkdir("dir")
        files.write("dir/a", "x")
        persistent = service(runtime, persistent=True, run="persistent")
        persistent.write("keep", "yes")
        runtime._files.cleanup(files._context.scope, lambda: None)
        assert files.list()["entries"] == []
        runtime._files.cleanup(persistent._context.scope, lambda: None)
        assert persistent.read("keep")["text"] == "yes"
        with pytest.raises(InfrastructureError, match="WORKSPACE_PROVIDER_MISMATCH"):
            runtime._files.cleanup({**files._context.scope, "file_provider": None}, lambda: None)


def test_configuration_is_frozen_and_default_local_requires_root(file_provider):
    values = {"label": "first"}
    p = properties(**values)
    values["label"] = "second"
    assert p.options["label"] == "first"
    assert "first" not in repr(p)
    with pytest.raises(TypeError):
        p.options["label"] = "changed"
    runtime = RuntimeEngine()
    with pytest.raises(ConfigurationError, match="WORKSPACE_ROOT_REQUIRED"):
        agent(runtime, lambda r: ModelResponse("done"))
    runtime.store.close()


@pytest.mark.parametrize("before,after", [(True, 1), (False, 0), (1, 1.0), ([1], [1.0])])
def test_constructor_option_fingerprint_preserves_types(before, after):
    assert properties(label=before).options_hash != properties(label=after).options_hash
    assert properties(a=1, b=2).options_hash == properties(b=2, a=1).options_hash


async def test_instance_shadowing_is_validated_and_closed(file_provider, monkeypatch):
    created = []
    original = file_provider.TestFiles.__init__

    def initialize(self, **kwargs):
        original(self, **kwargs)
        self.read = None
        created.append(self)

    monkeypatch.setattr(file_provider.TestFiles, "__init__", initialize)
    runtime = RuntimeEngine(files=properties())
    with pytest.raises(ConfigurationError, match="FILE_PROVIDER_INTERFACE_INVALID"):
        await runtime.__aenter__()
    assert created[0].closed == 1 and runtime.store._closed


async def test_invalid_instance_close_failure_retains_ownership_for_retry(
    file_provider, monkeypatch
):
    original = file_provider.TestFiles.__init__

    def initialize(self, **kwargs):
        original(self, **kwargs)
        self.read = None

    def close(self):
        self.closed += 1
        if self.closed == 1:
            raise OSError("first close failed")

    monkeypatch.setattr(file_provider.TestFiles, "__init__", initialize)
    monkeypatch.setattr(file_provider.TestFiles, "close", close)
    runtime = RuntimeEngine(files=properties())
    with pytest.raises(ConfigurationError, match="FILE_PROVIDER_INTERFACE_INVALID") as error:
        await runtime.__aenter__()
    assert error.value.__notes__
    instance = runtime._files.backend
    assert instance.closed == 1 and not runtime.store._closed
    await runtime.aclose()
    assert instance.closed == 2 and runtime.store._closed


async def test_accidental_coroutine_result_is_rejected_without_execution(file_provider):
    async with RuntimeEngine(files=properties()) as runtime:
        files = service(runtime)

        async def unexpected():
            pytest.fail("SDK awaited provider result")

        runtime._files.backend.read = lambda *a, **k: unexpected()
        with pytest.raises(AgentLoomError, match="FILE_PROVIDER_ASYNC_RESULT"):
            files.read("a")


async def test_expired_guard_prevents_provider_call_and_default_cleanup(file_provider):
    active = True

    def guard():
        if not active:
            raise InfrastructureError("LEASE_LOST")

    async with RuntimeEngine(files=properties()) as runtime:
        files = service(runtime, guard=guard)
        files.write("a", "content")
        calls = list(runtime._files.backend.calls)
        active = False
        with pytest.raises(InfrastructureError, match="LEASE_LOST"):
            files.write("b", "no")
        with pytest.raises(InfrastructureError, match="LEASE_LOST"):
            runtime._files.cleanup(files._context.scope, guard)
        assert runtime._files.backend.calls == calls


@pytest.mark.parametrize(
    "arguments",
    [
        {"path": "a", "offset": None},
        {"path": "a", "offset": 1.0},
        {"path": "a", "limit": None},
        {"path": "a", "limit": 1.0},
    ],
)
async def test_public_service_rejects_invalid_optional_values(file_provider, arguments):
    async with RuntimeEngine(files=properties()) as runtime:
        files = service(runtime)
        before = list(runtime._files.backend.calls)
        with pytest.raises(AgentLoomError, match="FILE_ARGUMENTS_INVALID"):
            files.read(**arguments)
        assert runtime._files.backend.calls == before


async def test_background_cleanup_receives_independent_job_identity(file_provider, runtime_store):
    from agentloom.runtime.cleanup import CleanupService

    received = []
    async with RuntimeEngine(files=properties(), store=runtime_store) as runtime:
        provider = runtime._files.backend
        original = provider.cleanup

        def cleanup(context):
            assert context.purpose == "cleanup"
            assert context.operation_id is None and context.attempt_id is None
            assert context.cleanup_job_id and context.cleanup_lease_epoch >= 1
            context.check_active()
            received.append(context)
            return original(context)

        provider.cleanup = cleanup

        def respond(request):
            if not any(m["role"] == "tool" for m in request.messages):
                return ModelResponse(
                    tool_calls=(ToolCall("write", {"path": "a", "content": "ok"}),)
                )
            return ModelResponse("done")

        result = await agent(runtime, respond, "write").ainvoke("go")
        CleanupService(runtime).scan_once()
        # Another maintenance worker may already own this job; a scan does not
        # join that worker. Await the documented durable cleanup outcome.
        async with asyncio.timeout(3):
            while runtime._load(result.run_id)["cleanup_state"] != "succeeded":
                await asyncio.sleep(0.01)
        state = runtime._load(result.run_id)
        assert state["cleanup_state"] == "succeeded"
        assert received[-1].cleanup_job_id == state["cleanup_job"]["job_id"]
        assert provider.data[received[-1].workspace_id] == {".": None}


async def test_late_completed_write_is_never_retried_as_unknown(file_provider, runtime_store):
    def respond(request):
        if not any(m["role"] == "tool" for m in request.messages):
            return ModelResponse(tool_calls=(ToolCall("write", {"path": "a", "content": "ok"}),))
        return ModelResponse("done")

    async with RuntimeEngine(files=properties(), store=runtime_store) as runtime:
        original = runtime._files.backend.write

        def slow_return(context, *args, **kwargs):
            value = original(context, *args, **kwargs)
            time.sleep(1.1)
            return value

        runtime._files.backend.write = slow_return
        result = await agent(
            runtime,
            respond,
            "write",
            execution_policy=ExecutionPolicy(
                tool_timeout_seconds=1,
                retry=RetryPolicy(enabled=True, max_attempts=2, delay_seconds=0),
            ),
        ).ainvoke("go")
        assert result.status == "succeeded"
        op = next(
            o
            for o in runtime._load(result.run_id)["operations"].values()
            if o["event_type"] == "capability.invoke"
        )
        assert len(op["attempts"]) == 1
        assert op["raw"]["execution_fact"] == "completed"
        assert op["raw"]["error"]["code"] == "TOOL_TIMEOUT"
        assert op["raw"]["result"]["path"] == "a"
        assert len([c for c in runtime._files.backend.calls if c["method"] == "write"]) == 1
