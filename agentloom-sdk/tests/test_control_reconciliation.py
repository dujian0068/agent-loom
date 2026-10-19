"""Stop/deadline recovery settles saved facts without invoking a new Hook or tool."""

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from conftest import assert_finished, binding, tool

from agentloom import (
    AgentDefinition,
    CapabilityRegistry,
    FunctionHookHandler,
    HookBinding,
    HookDecision,
    HookSet,
    InfrastructureError,
    LocalEnvironment,
    ModelResponse,
    PlanAgent,
    RuntimeEngine,
    ScriptedModel,
    ToolCall,
)
from agentloom.errors import LeaseLost
from agentloom.runtime.control import finalize_run
from agentloom.storage import claim


class Cut(Exception):
    pass


@pytest.mark.parametrize("stage", ["request", "response"])
@pytest.mark.parametrize("decision", ["allow", "deny"])
@pytest.mark.parametrize("patch", ["valid", "invalid", "conflict"])
async def test_saved_hook_is_reconciled_without_agent(runtime_store, build, stage, decision, patch):
    executed, hooks_called = [], []

    def hook(event, context):
        hooks_called.append(event.operation_id)
        payload = {"arguments": {"value": "NEW"}} if stage == "request" else {"result": "NEW"}
        system = context.reader.messages()[0]["record_id"]
        return HookDecision(
            decision,
            payload_patch=payload,
            context_patch={system: "CHANGED" if patch != "invalid" else "x" * 40000},
        )

    hooks = HookSet(
        (
            HookBinding("saved", "capability.invoke", stage, "saved"),
            HookBinding("remaining", "capability.invoke", stage, "never", ordinal=1),
            HookBinding(
                "other",
                "capability.invoke",
                "response" if stage == "request" else "request",
                "never",
            ),
        ),
        {
            "saved": FunctionHookHandler(hook),
            "never": FunctionHookHandler(lambda e, c: HookDecision()),
        },
    )

    def checkpoint(command, state):
        if command.startswith("hook-result:") and command.endswith(stage + ":saved"):
            raise Cut()

    async with RuntimeEngine(store=runtime_store, checkpoint_callback=checkpoint) as runtime:
        handle = await build(
            runtime,
            None,
            model=ScriptedModel(
                lambda r: ModelResponse(tool_calls=(ToolCall("echo", {"value": "OLD"}),)),
                context_window_tokens=32768,
            ),
            definition=AgentDefinition("test", "SYSTEM", capabilities=(binding(),)),
            capabilities=CapabilityRegistry(
                tools=(tool(lambda a, c: executed.append(a) or "RAW"),)
            ),
            hooks=hooks,
        ).start("question", run_id="saved")
        with pytest.raises(InfrastructureError):
            await handle.wait(timeout_s=3)
    if patch == "conflict":
        with runtime_store.transaction("default") as tx:
            state = tx.get("runs", "saved")
            state["tasks"][state["root_task_id"]]["context"]["records"][0]["message"]["content"] = (
                "CONCURRENT"
            )
            tx.put("runs", "saved", state)
    async with RuntimeEngine(store=runtime_store) as runtime:
        await runtime.runs.stop("saved", command_id="stop")
        result = await (await runtime.runs.attach("saved")).wait(timeout_s=3)
        state = assert_finished(runtime, result.run_id, "cancelled")
        op = next(o for o in state["operations"].values() if o["event_type"] == "capability.invoke")
        assert len(hooks_called) == 1 and len(executed) == int(stage == "response")
        assert op["response"]["status"] == ("blocked" if decision == "deny" else "failed")
        assert op["response"]["execution_fact"] == (
            "completed" if stage == "response" else "not_started"
        )
        if decision == "deny":
            assert op["response"]["error"]["code"] == "HOOK_DENIED"
        else:
            assert op["response"]["error"]["category"] == "cancelled"
        saved = op["hooks"][stage + ":saved"]
        assert saved["status"] == "applied"
        assert saved["patch_status"] == ("applied" if patch == "valid" else "rejected")
        remaining = op["hooks"][stage + ":remaining"]
        assert remaining["execution_status"] == "skipped_control" and remaining["result"] is None
        if stage == "request":
            assert op["request"]["arguments"]["value"] == ("NEW" if patch == "valid" else "OLD")
        else:
            assert op["raw"]["result"] == "RAW"
            assert op["effective_before_control"]["result"] == (
                "NEW" if patch == "valid" else "RAW"
            )
        assert result.error["code"] == "RUN_STOPPED"
        await runtime.runs.stop("saved", command_id="stop-again")
        assert runtime._load("saved")["terminal_seq"] == state["terminal_seq"]


async def test_started_hook_becomes_indeterminate_without_fabricated_allow(runtime_store, build):
    def checkpoint(command, state):
        if command.startswith("hook-start:"):
            raise Cut()

    async with RuntimeEngine(store=runtime_store, checkpoint_callback=checkpoint) as runtime:
        handle = await build(
            runtime,
            lambda r: pytest.fail("Model must not execute"),
            hooks=HookSet(
                (HookBinding("cut", "model.generate", "request", "never"),),
                {"never": FunctionHookHandler(lambda e, c: pytest.fail("Hook must not replay"))},
            ),
        ).start("question", run_id="started")
        with pytest.raises(InfrastructureError):
            await handle.wait(timeout_s=3)
    async with RuntimeEngine(store=runtime_store) as runtime:
        await runtime.runs.stop("started", command_id="stop")
        await (await runtime.runs.attach("started")).wait(timeout_s=3)
        state = assert_finished(runtime, "started", "cancelled")
        hook = next(iter(state["operations"].values()))["hooks"]["request:cut"]
        assert hook["status"] == "indeterminate" and hook["result"] is None
        assert hook["execution_status"] == "unknown"


@pytest.mark.parametrize("valid", [True, False])
async def test_saved_parameter_validation_needs_no_adapter_during_stop(runtime_store, build, valid):
    validations = []

    class Model(ScriptedModel):
        def validate_options(self, options):
            validations.append(dict(options.parameters))
            if options.parameters.get("temperature", 0) > 1:
                from agentloom import ConfigurationError

                raise ConfigurationError("MODEL_FEATURE_UNSUPPORTED")

    def checkpoint(command, state):
        if command.startswith("hook-result:"):
            raise Cut()

    async with RuntimeEngine(store=runtime_store, checkpoint_callback=checkpoint) as initial:
        model = Model(lambda r: pytest.fail("No model dispatch"))
        h = await build(
            initial,
            None,
            model=model,
            hooks=HookSet(
                (HookBinding("params", "model.generate", "request", "params"),),
                {
                    "params": FunctionHookHandler(
                        lambda e, c: HookDecision(
                            payload_patch={
                                "parameters": {"temperature": 0.5 if valid else 2},
                            }
                        )
                    )
                },
            ),
        ).start("question", run_id="params")
        with pytest.raises(InfrastructureError):
            await h.wait(timeout_s=2)
    before = list(validations)
    async with RuntimeEngine(store=runtime_store) as recovered:
        await recovered.runs.stop("params", command_id="stop")
        await (await recovered.runs.attach("params")).wait(timeout_s=2)
        state = assert_finished(recovered, "params", "cancelled")
        op = next(iter(state["operations"].values()))
        assert op["request"]["parameters"] == ({"temperature": 0.5} if valid else {})
        assert op["hooks"]["request:params"]["patch_status"] == ("applied" if valid else "rejected")
    assert validations == before


async def test_legacy_saved_deny_and_patch_use_frozen_schemas(runtime_store, build):
    def checkpoint(command, state):
        if command.startswith("hook-result:"):
            raise Cut()

    async with RuntimeEngine(store=runtime_store, checkpoint_callback=checkpoint) as initial:
        handle = await build(
            initial,
            lambda r: ModelResponse(
                tool_calls=(
                    ToolCall(
                        "delegate_task",
                        {"executor": "worker", "task": "OLD"},
                    ),
                )
            ),
            cls=PlanAgent,
            executors={"worker": AgentDefinition("worker", "WORKER", "do a task")},
            hooks=HookSet(
                (HookBinding("saved", "executor.delegate", "request", "saved"),),
                {
                    "saved": FunctionHookHandler(
                        lambda e, c: HookDecision(
                            "deny",
                            payload_patch={
                                "arguments": {"executor": "worker", "task": "NEW"},
                            },
                        )
                    )
                },
            ),
        ).start("question", run_id="legacy")
        with pytest.raises(InfrastructureError):
            await handle.wait(timeout_s=2)
    with runtime_store.transaction("default") as tx:
        state = tx.get("runs", "legacy")
        op = next(o for o in state["operations"].values() if o["event_type"] == "executor.delegate")
        hook = op["hooks"]["request:saved"]
        hook.pop("contract")
        hook.pop("parameter_validation")
        tx.put("runs", "legacy", state)
    async with RuntimeEngine(store=runtime_store) as recovered:
        await recovered.runs.stop("legacy", command_id="stop")
        await (await recovered.runs.attach("legacy")).wait(timeout_s=2)
        state = assert_finished(recovered, "legacy", "cancelled")
        op = state["operations"][op["operation_id"]]
        assert op["response"]["status"] == "blocked"
        assert op["request"]["arguments"]["task"] == "NEW"
        assert op["hooks"]["request:saved"]["patch_status"] == "applied"
        assert len(state["tasks"]) == 1


@pytest.mark.parametrize("stage", ["request", "response"])
def test_saved_deny_survives_process_death(tmp_path, stage):
    worker = Path(__file__).parent / "support" / "control_hook_process.py"
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")}
    first = subprocess.run(
        [sys.executable, str(worker), "prepare", str(tmp_path), stage],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert first.returncode == 73, first.stderr
    second = subprocess.run(
        [sys.executable, str(worker), "stop", str(tmp_path), stage],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert second.returncode == 0, second.stderr
    result = json.loads(second.stdout)
    assert result["run_status"] == "cancelled" and result["operation_status"] == "blocked"
    assert result["hook_status"] == "applied" and result["saved_decision"] == "deny"


@pytest.mark.parametrize("orphan", [False, True])
async def test_slow_revocation_renews_original_execution_lease(runtime_store, build, orphan):
    entered = asyncio.Event()
    lease_seconds = 1.0

    async def blocked(request):
        entered.set()
        await asyncio.Event().wait()

    class SlowEnvironment(LocalEnvironment):
        def revoke(self, scope):
            with runtime_store.transaction("default") as tx:
                before = tx.get("runs", scope["run_id"])["lease"]
            # Keep revocation longer than a whole TTL without making setup depend
            # on a sub-100 ms scheduler/filesystem response in parallel suites.
            time.sleep(lease_seconds * 1.5)
            with runtime_store.transaction("default") as tx:
                after = tx.get("runs", scope["run_id"])["lease"]
                assert after["worker_id"] == before["worker_id"]
                assert after["epoch"] == before["epoch"]
                assert after["expires_at"] > max(before["expires_at"], tx.now)
            return super().revoke(scope)

    async with RuntimeEngine(
        store=runtime_store, environment=SlowEnvironment(), lease_seconds=lease_seconds
    ) as runtime:
        handle = await build(runtime, blocked if orphan else lambda r: ModelResponse("done")).start(
            "question",
            run_id="slow",
            session_id="s",
        )
        if orphan:
            await asyncio.wait_for(entered.wait(), 2)
        else:
            assert (await handle.wait(timeout_s=6)).status == "succeeded"
    if orphan:
        async with RuntimeEngine(
            store=runtime_store, environment=SlowEnvironment(), lease_seconds=lease_seconds
        ) as runtime:
            await runtime.runs.stop("slow", command_id="stop")
            assert (
                await (await runtime.runs.attach("slow")).wait(timeout_s=6)
            ).status == "cancelled"


async def test_revocation_renewal_never_revives_replaced_epoch(runtime_store, build):
    def checkpoint(command, state):
        if command == "initialize":
            raise Cut()

    async with RuntimeEngine(store=runtime_store, checkpoint_callback=checkpoint) as initial:
        h = await build(initial, lambda r: pytest.fail("No dispatch")).start(
            "question", run_id="fence"
        )
        with pytest.raises(InfrastructureError):
            await h.wait(timeout_s=2)

    class ReplacedEnvironment(LocalEnvironment):
        def revoke(self, scope):
            with runtime_store.transaction("default") as tx:
                state = tx.get("runs", "fence")
                state["lease"]["epoch"] += 1
                tx.put("runs", "fence", state)
            time.sleep(0.1)

    runtime = RuntimeEngine(
        store=runtime_store, environment=ReplacedEnvironment(), lease_seconds=0.12
    )
    lease = claim(
        runtime_store, runtime.namespace, "fence", runtime.worker_id, runtime.lease_seconds
    )
    with pytest.raises(LeaseLost):
        finalize_run(runtime, lease, "cancelled")
    state = runtime._load("fence")
    assert state["terminal_seq"] is None and state["lease"]["epoch"] == lease.epoch + 1
