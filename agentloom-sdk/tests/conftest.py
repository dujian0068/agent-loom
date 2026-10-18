import importlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from agentloom import (
    AgentDefinition,
    CapabilityBinding,
    CapabilityRegistration,
    CapabilitySpec,
    FileRuntimeStore,
    FunctionToolAdapter,
    MemoryRuntimeStore,
    ReactAgent,
    ScriptedModel,
    WorkspaceBinding,
)


@pytest.fixture(scope="session")
def installed_file_package(tmp_path_factory):
    root = tmp_path_factory.mktemp("installed-file-provider")
    source = Path(__file__).parent / "support" / "workspace_provider"
    shutil.copytree(
        source, root / "source", ignore=shutil.ignore_patterns("__pycache__", "*.egg-info", "build")
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--no-deps",
            "--no-build-isolation",
            "--no-index",
            "--target",
            str(root / "installed"),
            str(root / "source"),
        ],
        text=True,
        capture_output=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return root / "installed"


@pytest.fixture
def file_provider(installed_file_package, monkeypatch):
    monkeypatch.syspath_prepend(str(installed_file_package))
    return importlib.import_module("loom_test_files")


@pytest.fixture(params=("memory", "file"))
def runtime_store(request, tmp_path):
    store = (
        MemoryRuntimeStore() if request.param == "memory" else FileRuntimeStore(tmp_path / "state")
    )
    yield store
    store.close()


def assert_finished(runtime, run_id, status):
    state = runtime._load(run_id)
    assert state["status"] == status and state["terminal_seq"] is not None
    assert state["usage"]["loops_reserved"] == 0
    assert all(
        op["status"] != "pending" and op["ticket"] != "reserved"
        for op in state["operations"].values()
    )
    assert len([e for e in state["events"] if e["type"] == "run.completed"]) == 1
    return state


@pytest.fixture(autouse=True)
def isolated_working_directory(tmp_path, monkeypatch):
    # Exercise the real default store without sharing persistent IDs across tests.
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def durable_store_factory():
    def factory(root):
        return FileRuntimeStore(root / "state")

    return factory


def tool(function, name="echo"):
    return CapabilityRegistration(
        name, FunctionToolAdapter(function), (CapabilitySpec(name, name, name, {"type": "object"}),)
    )


def binding(name="echo"):
    return CapabilityBinding(name, "tool", name, name, name)


@pytest.fixture
def build(tmp_path):
    def factory(runtime, responder, *, cls=ReactAgent, **kwargs):
        kwargs.setdefault("definition", AgentDefinition("assistant", "SYSTEM"))
        kwargs.setdefault("workspace", WorkspaceBinding(tmp_path / "workspace"))
        kwargs.setdefault("model", ScriptedModel(responder))
        return cls(runtime_engine=runtime, **kwargs)

    return factory
