import asyncio
import json

import pytest

from agentloom import (
    AgentDefinition,
    AgentInput,
    ExecutionPolicy,
    InfrastructureError,
    LocalEnvironment,
    ModelResponse,
    RequestError,
    RuntimeEngine,
    VariableSpec,
)


async def test_history_has_each_question_once_instead_of_copying_prior_projection(build):
    async with RuntimeEngine() as runtime:
        for index in range(5):
            agent = build(runtime, lambda r: ModelResponse("done"))
            await agent.ainvoke(f"QUESTION_{index}", session_id="session")
            messages = json.dumps(agent.model.requests[0].messages)
            assert all(messages.count(f"QUESTION_{n}") == 1 for n in range(index + 1))


async def test_typed_variables_json_null_and_no_template_execution(build):
    definition = AgentDefinition(
        "a",
        "Hello {{name}}",
        variables={"name": VariableSpec({"type": "string"})},
        input_schema={"type": "null"},
        output_schema={"type": "null"},
    )
    async with RuntimeEngine() as runtime:
        agent = build(runtime, lambda r: ModelResponse("null"), definition=definition)
        with pytest.raises(RequestError, match="VARIABLE_REQUIRED"):
            await agent.start(AgentInput(None))
        with pytest.raises(RequestError, match="VARIABLE_TYPE_INVALID"):
            await agent.start(AgentInput(None, variables={"name": 1}))
        result = await agent.ainvoke(
            AgentInput(None, variables={"name": "{{literal}} $(untouched)"})
        )
        assert result.status == "succeeded" and result.output.value is None
        assert agent.model.requests[0].messages[0]["content"] == "Hello {{literal}} $(untouched)"


async def test_orphaned_stop_needs_no_model_or_agent_reconstruction(
    tmp_path, build, durable_store_factory
):
    store = durable_store_factory(tmp_path)
    started = asyncio.Event()

    async def model(request):
        started.set()
        await asyncio.sleep(60)
        return ModelResponse("late")

    try:
        async with RuntimeEngine(store=store) as runtime:
            await build(runtime, model).start("question", run_id="orphan")
            await started.wait()
        async with RuntimeEngine(store=store) as runtime:
            handle = await runtime.runs.attach("orphan")
            await handle.stop(command_id="stop-without-agent")
            result = await handle.wait(timeout_s=2)
            assert result.status == "cancelled"
    finally:
        store.close()


async def test_deadline_is_not_reset_during_shutdown_and_recovery(
    tmp_path, build, durable_store_factory
):
    store = durable_store_factory(tmp_path)
    started = asyncio.Event()

    async def model(request):
        started.set()
        await asyncio.sleep(60)
        return ModelResponse("late")

    policy = ExecutionPolicy(run_timeout_seconds=1)
    try:
        async with RuntimeEngine(store=store) as runtime:
            await build(runtime, model, execution_policy=policy).start(
                "question", run_id="deadline"
            )
            await started.wait()
        await asyncio.sleep(1.05)
        async with RuntimeEngine(store=store) as runtime:
            agent = build(
                runtime,
                lambda r: pytest.fail("Original deadline has elapsed"),
                execution_policy=policy,
            )
            result = await (await agent.start(run_id="deadline")).wait(timeout_s=2)
            assert result.status == "timed_out" and result.error["code"] == "RUN_TIMEOUT"
    finally:
        store.close()


async def test_cleanup_retries_independently_and_never_reopens_answer(build):
    class Environment(LocalEnvironment):
        failures = 1

        def cleanup(self, scope):
            if self.failures:
                self.failures -= 1
                raise OSError("temporary cleanup failure")
            return super().cleanup(scope)

    async with RuntimeEngine(environment=Environment(), cleanup_retry_seconds=0.02) as runtime:
        agent = build(runtime, lambda r: ModelResponse("done"))
        handle = await agent.start("question")
        await handle.wait()
        events = [event async for event in handle.events()]
        for _ in range(100):
            if (await handle.snapshot())["cleanup_state"] == "succeeded":
                break
            await asyncio.sleep(0.01)
        assert (await handle.snapshot())["cleanup_state"] == "succeeded"
        assert len(agent.model.requests) == 1
        assert [event async for event in handle.events(after_seq=events[-1].seq)] == []


async def test_lost_commit_acknowledgement_reuses_transaction_receipt(build):
    seen = []

    def fail_ack(command, state):
        if command.startswith("attempt-result:"):
            seen.append(command)
            raise InfrastructureError("ACK_LOST")

    async with RuntimeEngine(checkpoint_callback=fail_ack) as runtime:
        agent = build(runtime, lambda r: ModelResponse("done"))
        result = await agent.ainvoke("question")
        assert result.status == "succeeded" and len(agent.model.requests) == len(seen) == 1


async def test_deletion_closes_identity_and_protects_history_dependencies(build):
    async with RuntimeEngine() as runtime:
        first = await build(runtime, lambda r: ModelResponse("one")).ainvoke(
            "first", session_id="s"
        )
        second = await build(runtime, lambda r: ModelResponse("two")).ainvoke(
            "second", session_id="s"
        )
        for _ in range(100):
            states = [await runtime.runs.get(rid) for rid in (first.run_id, second.run_id)]
            if all(s["cleanup_state"] == "succeeded" for s in states):
                break
            await asyncio.sleep(0.01)
        with pytest.raises(RequestError, match="RECORD_REFERENCED"):
            await runtime.records.delete(first.run_id, command_id="delete-parent")
        await runtime.records.delete(second.run_id, command_id="delete-child")
        await runtime.records.delete(first.run_id, command_id="delete-parent")
        with pytest.raises(RequestError, match="RUN_ADMISSION_CLOSED"):
            await build(runtime, lambda r: ModelResponse("bad")).start("first", run_id=first.run_id)
