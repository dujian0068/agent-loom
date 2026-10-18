"""Independent workspace SPI boundary and lease regression checks.

These are offline tests. Discovery metadata is substituted in the lifecycle tests;
the existing SDK suite separately installs the provider as a real distribution.
Production code and previous QA assertions are not changed.
"""

import importlib.util
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentloom import (
    AgentDefinition,
    AgentLoomError,
    CapabilityBinding,
    CapabilityRegistry,
    ExecutionPolicy,
    FileProperties,
    FileRuntimeStore,
    LocalEnvironment,
    MemoryRuntimeStore,
    ModelResponse,
    PlanAgent,
    ReactAgent,
    RuntimeEngine,
    ScriptedModel,
    ToolCall,
    WorkspaceBinding,
    file_tools,
)
from agentloom.environment.limits import MAX_FILE_BYTES, MAX_FILE_READ_BYTES
from agentloom.workspace import loader
from agentloom.workspace.service import WorkspaceService


@pytest.fixture(autouse=True)
def isolated_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)


@pytest.fixture(params=["memory", "file"])
def store(request, tmp_path):
    value = (
        MemoryRuntimeStore() if request.param == "memory" else FileRuntimeStore(tmp_path / "state")
    )
    yield value
    value.close()


@pytest.fixture
def delayed_provider(monkeypatch):
    source = (
        Path(__file__).resolve().parents[1]
        / "tests/support/workspace_provider/src/loom_test_files/__init__.py"
    )
    spec = importlib.util.spec_from_file_location("review8_provider", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class DelayedFiles(module.TestFiles):
        def __init__(self, phase, delay):
            super().__init__()
            self.phase, self.delay = phase, delay

        def _delay(self, context, phase):
            if self.phase == phase:
                end = time.monotonic() + self.delay
                while time.monotonic() < end:
                    # A cooperative sync driver checks its valid execution right
                    # during I/O and immediately before publishing a write.
                    context.check_active()
                    time.sleep(0.02)
                context.check_active()

        def prepare(self, context):
            self._delay(context, "prepare")
            return super().prepare(context)

        def write(self, context, path, content, *, mode="create_only", if_match=None):
            self._delay(context, "write")
            return super().write(context, path, content, mode=mode, if_match=if_match)

    real_entry_points = loader.metadata.entry_points
    entry_point = SimpleNamespace(
        name="review8-files",
        value="review8_provider:DelayedFiles",
        dist=SimpleNamespace(metadata={"Name": "review8-files"}, version="1.0.0"),
        load=lambda: DelayedFiles,
    )

    def entry_points(*, group):
        return (
            [*real_entry_points(group=group), entry_point]
            if group == loader.ENTRY_POINT_GROUP
            else real_entry_points(group=group)
        )

    monkeypatch.setattr(loader.metadata, "entry_points", entry_points)


@pytest.mark.parametrize("mode", ["react", "plan"])
@pytest.mark.parametrize("phase", ["prepare", "write"])
@pytest.mark.parametrize("lease_seconds", [0.3, 3.0], ids=["crosses-lease", "within-lease"])
async def test_cooperative_sync_provider_outlives_lease_but_not_deadline(
    store, delayed_provider, mode, phase, lease_seconds
):
    """A healthy long call must retain ownership until the tool/root deadline."""
    requests = []

    def respond(request):
        requests.append(request)
        return (
            ModelResponse(tool_calls=(ToolCall("write", {"path": "a.txt", "content": "ok"}),))
            if len(requests) == 1
            else ModelResponse("done")
        )

    async with RuntimeEngine(
        store=store,
        lease_seconds=lease_seconds,
        files=FileProperties(provider="review8-files", options={"phase": phase, "delay": 0.7}),
    ) as runtime:
        definition = AgentDefinition(
            "review8",
            "Write one file and finish.",
            capabilities=(
                CapabilityBinding(
                    binding_key="write",
                    kind="builtin_tool",
                    visible_name="write",
                    adapter_key="files",
                    member_key="write",
                ),
            ),
        )
        cls = ReactAgent if mode == "react" else PlanAgent
        agent = cls(
            runtime_engine=runtime,
            definition=definition,
            model=ScriptedModel(respond),
            workspace=WorkspaceBinding(),
            capabilities=CapabilityRegistry(tools=(file_tools(),)),
            execution_policy=ExecutionPolicy(
                loop_max=2, tool_timeout_seconds=5, run_timeout_seconds=15
            ),
        )
        handle = await agent.start("go")
        try:
            result = await handle.wait(timeout_s=6)
        finally:
            state = runtime._load(handle.run_id)
            print(
                json.dumps(
                    {
                        "mode": mode,
                        "phase": phase,
                        "lease_seconds": lease_seconds,
                        "status": state["status"],
                        "model_calls": len(requests),
                        "provider_calls": runtime._files.backend.calls,
                    }
                )
            )
        assert result.status == "succeeded"
        assert len(requests) == 2


@pytest.fixture
def local_files(tmp_path):
    environment = LocalEnvironment()
    scope = environment.prepare(WorkspaceBinding(tmp_path / "work"), "s", "r", 1)
    return WorkspaceService(environment, scope, lambda: None, {})


@pytest.mark.parametrize("text", ["", "a", "中", "😀", "a中😀\n", "a" * MAX_FILE_READ_BYTES])
def test_local_file_empty_unicode_and_maximum_slice(local_files, text):
    created = local_files.write("边界.txt", text)
    value = local_files.read("边界.txt", limit=MAX_FILE_READ_BYTES)
    assert value["text"] == text
    assert value["etag"] == created["etag"]
    assert value["end_offset"] == len(text.encode())
    assert value["next_offset"] is None
    eof = local_files.read("边界.txt", offset=len(text.encode()))
    assert eof["text"] == "" and eof["next_offset"] is None


@pytest.mark.parametrize("offset", [1, 2, 4, 5, 6, 8])
def test_local_file_invalid_utf8_offset_and_past_end(local_files, offset):
    local_files.write("a", "中😀")
    with pytest.raises(AgentLoomError):
        local_files.read("a", offset=offset)


@pytest.mark.parametrize("operation", ["read", "list", "glob", "grep"])
@pytest.mark.parametrize("limit", [None, False, 0, -1, 1.0, 10**20])
def test_invalid_limits_rejected_before_provider_call(local_files, operation, limit, monkeypatch):
    calls = []

    def provider_call(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("Invalid arguments reached the provider")

    monkeypatch.setattr(local_files._backend, operation, provider_call)
    arguments = {
        "read": {"path": "a"},
        "list": {},
        "glob": {"pattern": "**/*"},
        "grep": {"pattern": "x"},
    }[operation]
    with pytest.raises(AgentLoomError) as error:
        getattr(local_files, operation)(**arguments, limit=limit)
    assert error.value.external_outcome == "not_started"
    assert error.value.execution_fact == "not_started"
    assert not calls


def test_maximum_file_and_multibyte_overflow_are_atomic(local_files):
    text = "a" * MAX_FILE_BYTES
    version = local_files.write("maximum", text)
    assert version["size"] == MAX_FILE_BYTES
    assert local_files.read("maximum", offset=MAX_FILE_BYTES)["next_offset"] is None
    with pytest.raises(AgentLoomError, match="FILE_TOO_LARGE"):
        local_files.write("maximum", text + "中", mode="replace", if_match=version["etag"])
    assert local_files.stat("maximum")["etag"] == version["etag"]


def test_empty_search_pagination_mutation_and_stale_version(local_files):
    assert local_files.list()["entries"] == []
    assert local_files.glob("**/*") == {"paths": [], "truncated": False}
    assert local_files.grep("x") == {"matches": [], "truncated": False}
    first = local_files.write("a", "x")
    local_files.write("b", "x")
    page = local_files.list(limit=1)
    assert [entry["name"] for entry in page["entries"]] == ["a"]
    local_files.delete("b", local_files.stat("b")["etag"])
    with pytest.raises(AgentLoomError, match="DIRECTORY_CHANGED"):
        local_files.list(cursor=page["next_cursor"], limit=1)
    local_files.write("a", "new", mode="replace", if_match=first["etag"])
    with pytest.raises(AgentLoomError, match="FILE_CHANGED"):
        local_files.delete("a", first["etag"])
    assert local_files.read("a")["text"] == "new"


@pytest.mark.parametrize(
    "root", ["sub", "./sub", "sub/.", "sub//", "/work/sub", "/work/./sub", "/work/sub/."]
)
def test_glob_normalizes_equivalent_authorized_roots(local_files, root):
    """All accepted spellings of one directory must find the same direct file."""
    local_files.mkdir("sub")
    local_files.write("sub/a.txt", "needle")
    assert local_files.read(root + "/a.txt")["text"] == "needle"
    assert [entry["name"] for entry in local_files.list(root)["entries"]] == ["a.txt"]
    found = local_files.glob("*.txt", root=root)
    print(json.dumps({"root": root, "result": found}))
    assert [Path(path).name for path in found["paths"]] == ["a.txt"]
    assert found["truncated"] is False
