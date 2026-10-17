"""Semantic checkpoint corruption is rejected before environment or model dispatch."""

import pytest

from agentloom import InfrastructureError, LocalEnvironment, ModelResponse, RuntimeEngine


@pytest.mark.parametrize("corruption", ["consumed", "reserved", "attempts", "owner", "request"])
async def test_recovery_rejects_inconsistent_facts(runtime_store, build, corruption):
    class Cut(Exception):
        pass

    def checkpoint(command, state):
        if command.startswith("attempt-start:"):
            raise Cut()

    async with RuntimeEngine(store=runtime_store, checkpoint_callback=checkpoint) as initial:
        h = await build(initial, lambda r: pytest.fail("Crash precedes dispatch")).start(
            "question",
            run_id="corrupt",
        )
        with pytest.raises(InfrastructureError):
            await h.wait(timeout_s=2)
    with runtime_store.transaction("default") as tx:
        state = tx.get("runs", "corrupt")
        op = next(iter(state["operations"].values()))
        if corruption in {"consumed", "reserved", "attempts"}:
            key = {
                "consumed": "loops_consumed",
                "reserved": "loops_reserved",
                "attempts": "business_model_attempts",
            }[corruption]
            state["usage"][key] += 1
        elif corruption == "owner":
            op["task_id"] = "missing-task"
        else:
            op["request"]["messages"][0]["content"] = "CHANGED AFTER FREEZE"
        tx.put("runs", "corrupt", state)
    before = runtime_store.load("default", "corrupt")

    class NoPrepare(LocalEnvironment):
        def prepare(self, *args, **kwargs):
            pytest.fail("Recovery must validate before environment work")

    async with RuntimeEngine(store=runtime_store, environment=NoPrepare()) as recovered:
        agent = build(recovered, lambda r: pytest.fail("Corrupt state must not dispatch"))
        h = await agent.start(run_id="corrupt")
        with pytest.raises(InfrastructureError, match="PERSISTENCE_INVARIANT_BROKEN"):
            await h.wait(timeout_s=2)
        after = recovered._load("corrupt")
        assert not agent.model.requests
        assert after["usage"] == before["usage"] and after["operations"] == before["operations"]


@pytest.mark.parametrize("cut", ["attempt-result:", "complete:", "decision:"])
async def test_complete_model_crash_boundaries_resume_without_new_attempt(
    runtime_store, build, cut
):
    class Cut(Exception):
        pass

    def checkpoint(command, state):
        if command.startswith(cut):
            raise Cut()

    async with RuntimeEngine(store=runtime_store, checkpoint_callback=checkpoint) as first:
        h = await build(
            first,
            lambda r: ModelResponse(
                "saved",
                usage={
                    "input_tokens": 2,
                    "output_tokens": 3,
                },
            ),
        ).start("question", run_id="resume")
        with pytest.raises(InfrastructureError):
            await h.wait(timeout_s=2)
    async with RuntimeEngine(store=runtime_store) as second:
        agent = build(second, lambda r: pytest.fail("Complete response must not be repeated"))
        result = await (await agent.start(run_id="resume")).wait(timeout_s=2)
        assert result.status == "succeeded" and result.output.value == "saved"
        assert result.usage.business_model_attempts == result.usage.loops_consumed == 1
        assert result.usage.input_tokens == 2 and result.usage.output_tokens == 3
