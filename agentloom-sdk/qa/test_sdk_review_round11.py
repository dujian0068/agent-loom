"""Incremental audit of e53229a..e7f85c1, reusing the 2020-case baseline.

Local deterministic models only. Assertions describe required behavior; no xfail
or production changes are used to hide uncovered defects.
"""

import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from agentloom import (
    AgentDefinition,
    AgentLoomError,
    AgentNode,
    DirectorySkillLoader,
    FileRuntimeStore,
    MemoryRuntimeStore,
    ModelResponse,
    OpenAIResponsesAdapter,
    ReactAgent,
    RuntimeEngine,
    ScriptedModel,
    SkillPackage,
    WorkspaceBinding,
)
from agentloom.configuration.json_documents import copy_document
from agentloom.errors import WaitTimeout
from agentloom.nodes import PreparedEffect, Progress
from agentloom.runtime.recovery import validate_recovery


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


def build(runtime, responder):
    return ReactAgent(
        runtime_engine=runtime,
        definition=AgentDefinition("review11", "SYSTEM"),
        workspace=WorkspaceBinding(Path.cwd() / "workspace"),
        model=ScriptedModel(responder),
    )


def intercept_progress(monkeypatch, transform):
    original = AgentNode.activate

    async def activate(self, input, continuation, context):
        stream = original(self, input, continuation, context)
        try:
            outcome = await anext(stream)
            while True:
                proposed = transform(outcome) if isinstance(outcome, Progress) else outcome
                receipt = yield proposed
                outcome = await stream.asend(receipt)
        except StopAsyncIteration:
            return
        finally:
            await stream.aclose()

    monkeypatch.setattr(AgentNode, "activate", activate)


@pytest.mark.parametrize("newline", ["\n", "\r\n"], ids=["lf", "crlf"])
def test_skill_frontmatter_accepts_standard_line_endings_without_changing_hash(tmp_path, newline):
    text = newline.join(["---", "name: test", "description: test skill", "---", "Body", ""])
    data = text.encode()
    (tmp_path / "SKILL.md").write_bytes(data)
    package = SkillPackage.from_directory(tmp_path)
    assert package.metadata["name"] == "test"
    assert package.files_manifest["SKILL.md"] == hashlib.sha256(data).hexdigest()
    resource = DirectorySkillLoader().read_entry(package)
    assert resource["text"] == text and resource["size"] == len(data)


@pytest.mark.parametrize(
    "proposal", ["single", "duplicate_with_revision", "duplicate_without_revision"]
)
async def test_accept_effect_is_bounded_and_never_appends_twice(store, monkeypatch, proposal):
    changed, calls = [], []

    def transform(outcome):
        effects = [e for e in outcome.effects if e.kind == "accept_model"]
        if effects:
            changed.append(True)
            if proposal != "single":
                effect = effects[0]
                if proposal == "duplicate_without_revision":
                    body = copy_document(effect.body)
                    body.pop("expected_context_revision", None)
                    effect = PreparedEffect("accept_model", body)
                return replace(outcome, effects=(effect, effect))
        return outcome

    intercept_progress(monkeypatch, transform)
    async with RuntimeEngine(store=store) as runtime:
        handle = await build(runtime, lambda r: (calls.append(r), ModelResponse("done"))[1]).start(
            "go"
        )
        timeout = None
        try:
            await handle.wait(timeout_s=5)
        except WaitTimeout as exc:
            timeout = exc
        state = runtime._load(handle.run_id)
        task = state["tasks"][state["root_task_id"]]
        messages = [
            r["message"] for r in task["context"]["records"] if r["message"]["role"] == "assistant"
        ]
        print(
            json.dumps(
                {
                    "case": "accept_effect",
                    "proposal": proposal,
                    "backend": type(store).__name__,
                    "run_status": state["status"],
                    "activations": task["activation_no"],
                    "assistant_records": len(messages),
                    "model_calls": len(calls),
                    "loops_consumed": state["usage"]["loops_consumed"],
                }
            )
        )
        assert changed and len(calls) == 1
        assert timeout is None, (
            "Duplicate proposal was requeued instead of rejected or deduplicated"
        )
        assert len(messages) <= 1, f"Duplicate accept committed {len(messages)} assistant records"
        validate_recovery(state)


@pytest.mark.parametrize(
    "damage", ["none", "missing_record_ids", "duplicate_record_ids", "changed_sdk_text"]
)
async def test_model_intent_must_match_its_owned_context_before_commit(store, monkeypatch, damage):
    calls, changed = [], []

    def transform(outcome):
        revised = []
        for effect in outcome.effects:
            if effect.kind != "begin_model":
                revised.append(effect)
                continue
            op = copy_document(effect.body["operation"])
            if damage == "missing_record_ids":
                op["request"]["record_ids"] = ["missing"] * len(op["request"]["messages"])
            elif damage == "duplicate_record_ids":
                op["request"]["record_ids"] = [op["request"]["record_ids"][0]] * len(
                    op["request"]["messages"]
                )
            elif damage == "changed_sdk_text":
                op["request"]["messages"][1]["content"] = "UNAUTHORIZED SDK PROMPT REPLACEMENT"
            changed.append(True)
            revised.append(PreparedEffect("begin_model", {"operation": op}))
        return replace(outcome, effects=tuple(revised))

    intercept_progress(monkeypatch, transform)
    async with RuntimeEngine(store=store) as runtime:
        handle = await build(runtime, lambda r: (calls.append(r), ModelResponse("done"))[1]).start(
            "go"
        )
        failure = None
        try:
            result = await handle.wait(timeout_s=5)
        except AgentLoomError as exc:
            failure = exc
        state = runtime._load(handle.run_id)
        recovery_error = None
        try:
            validate_recovery(state)
        except AgentLoomError as exc:
            recovery_error = {"code": exc.code, "field_path": exc.field_path}
    resume_error, resume_calls = None, []
    async with RuntimeEngine(store=store) as reopened:
        try:
            attached = await build(
                reopened, lambda r: (resume_calls.append(r), ModelResponse("unexpected"))[1]
            ).start(run_id=handle.run_id)
            await attached.wait(timeout_s=5)
        except AgentLoomError as exc:
            resume_error = {"code": exc.code, "field_path": exc.field_path}
    print(
        json.dumps(
            {
                "case": "model_intent",
                "damage": damage,
                "backend": type(store).__name__,
                "run_status": state["status"],
                "model_calls": len(calls),
                "operation_count": len(state["operations"]),
                "wait_error": getattr(failure, "code", None),
                "recovery_error": recovery_error,
                "resume_error": resume_error,
                "resume_model_calls": len(resume_calls),
            }
        )
    )
    assert changed
    if damage == "none":
        assert failure is None and result.status == "succeeded" and len(calls) == 1
        assert recovery_error is None and resume_error is None and not resume_calls
        return
    assert failure is None, "Invalid proposal escaped as a persistence/infrastructure failure"
    assert not calls, "Unvalidated request reached the model adapter"
    assert result.status == "failed" and result.error["code"] == "NODE_EFFECT_INVALID"
    assert not state["operations"]
    assert recovery_error is None and resume_error is None and not resume_calls


@pytest.mark.parametrize("data", [b"", b"ordinary text", b"\xff\x00"])
def test_skill_resource_empty_text_and_binary_keep_original_bytes(tmp_path, data):
    (tmp_path / "SKILL.md").write_text("---\nname: test\ndescription: test skill\n---\nBody")
    (tmp_path / "asset").write_bytes(data)
    package = SkillPackage.from_directory(tmp_path)
    value = DirectorySkillLoader().read_resource(package, "asset")
    assert value["sha256"] == hashlib.sha256(data).hexdigest()
    assert value["size"] == len(data)
    assert value["text"] == (None if data.startswith(b"\xff") else data.decode())


@pytest.mark.parametrize("entrypoint", ["import", "read"])
@pytest.mark.parametrize("replacement", [b"changed!", b"x", b"longer than the original"])
def test_skill_read_rejects_in_place_changes(tmp_path, monkeypatch, entrypoint, replacement):
    (tmp_path / "SKILL.md").write_text("---\nname: test\ndescription: test skill\n---\nBody")
    asset = tmp_path / "asset"
    asset.write_bytes(b"original")
    package = SkillPackage.from_directory(tmp_path)
    identity, original_stat, changed = asset.stat().st_ino, os.fstat, []

    def fstat(fd):
        value = original_stat(fd)
        if value.st_ino == identity and not changed:
            changed.append(True)
            asset.write_bytes(replacement)
        return value

    monkeypatch.setattr(os, "fstat", fstat)
    with pytest.raises(AgentLoomError, match="SKILL_CONTENT_CHANGED"):
        if entrypoint == "import":
            SkillPackage.from_directory(tmp_path)
        else:
            DirectorySkillLoader().read_resource(package, "asset")
    assert changed


@pytest.mark.parametrize("text", ["", "done"])
async def test_responses_unknown_event_and_empty_completion_remain_supported(text):
    frames = [
        {"type": "response.future_metadata", "arbitrary": None},
        {
            "type": "response.completed",
            "response": {
                "status": "completed",
                "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
            },
        },
    ]
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content="".join("data: " + json.dumps(frame) + "\n\n" for frame in frames),
            )
        )
    ) as client:
        adapter = OpenAIResponsesAdapter(
            model="review11", api_key="fake", context_window_tokens=32768, client=client
        )
        request = SimpleNamespace(messages=[], tools=[], parameters={}, max_output_tokens=32)
        events = [
            event async for event in adapter.stream_once(request, SimpleNamespace(diagnostics=[]))
        ]
    assert len(events) == 1 and events[0].kind == "completed" and events[0].data["text"] == text
