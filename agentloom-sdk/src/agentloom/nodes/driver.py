"""Activate/commit/asend handshake with durable waits and scoped service expiry."""

import asyncio
import inspect

from ..configuration.json_documents import copy_document
from ..errors import AgentLoomError, InfrastructureError, RequestError
from ..runtime.batches import stable_id
from .commands import RuntimeCommit
from .contracts import (
    ActivationReceipt,
    Complete,
    ContinuationRef,
    Fail,
    NodeInput,
    PayloadRef,
    PreparedEffect,
    Progress,
    Wait,
    WaitCondition,
    continuation_ref,
)
from .ports import bind_context


class NodeDriver:
    def __init__(self, runner):
        self.runner = runner

    def _submit(self, task, activation_id, kind, body, command_id):
        self.runner.check_active()
        try:
            return self.runner.coordinator.commit(
                RuntimeCommit(
                    command_id,
                    kind,
                    self.runner.run_id,
                    task["task_id"],
                    activation_id,
                    task["node_revision"],
                    body,
                ),
                self.runner.lease,
            )
        except AgentLoomError:
            raise
        except Exception as exc:
            raise InfrastructureError("COMMIT_ACKNOWLEDGEMENT_FAILED") from exc

    async def activate(self, task_id, node):
        task = self.runner.task(task_id)
        activation_id = stable_id(
            task_id, "activation", task["activation_no"] + 1, self.runner.lease.epoch
        )
        receipt = self._submit(task, activation_id, "ActivateTask", {}, "activate:" + activation_id)
        task = self.runner.task(task_id)
        context = bind_context(self.runner, task, activation_id)
        stream = None
        try:
            stream = node.activate(
                NodeInput(
                    task_id,
                    PayloadRef(task_id, task["input"]),
                    PayloadRef(task_id, task["resume_input_ref"])
                    if task.get("resume_input_ref")
                    else None,
                ),
                continuation_ref(task),
                context,
            )
            if not inspect.isasyncgen(stream):
                if inspect.iscoroutine(stream):
                    stream.close()
                raise RequestError("NODE_OUTCOME_INVALID")
            outcome = await anext(stream)
            while True:
                task = self.runner.task(task_id)
                kind, body, command_id = self._outcome(task, outcome, activation_id)
                receipt = self._submit(task, activation_id, kind, body, command_id)
                if not isinstance(outcome, Progress):
                    return ActivationReceipt(
                        task_id, activation_id, self.runner.task(task_id)["status"], receipt
                    )
                await asyncio.sleep(0)
                outcome = await stream.asend(receipt)
        except InfrastructureError:
            raise
        except asyncio.CancelledError:
            raise
        except RequestError as exc:
            if exc.code == "COMMIT_CONFLICT":
                # The submitted command is idempotent; a new activation reads the
                # committed head instead of reusing a generator's speculative state.
                self.runner.engine.requeue(task_id, activation_id)
                return None
            return self._fail(
                task_id,
                activation_id,
                exc.code if exc.code.startswith("NODE_") else "NODE_OUTCOME_INVALID",
            )
        except StopAsyncIteration:
            return self._fail(task_id, activation_id, "NODE_MISSING_TERMINAL_OUTCOME")
        except AgentLoomError as exc:
            if exc.code in {"RUN_STOPPED", "RUN_TIMEOUT", "LOOP_LIMIT_EXCEEDED"}:
                raise
            return self._fail(task_id, activation_id, "NODE_IMPLEMENTATION_FAILED")
        except Exception:
            return self._fail(task_id, activation_id, "NODE_IMPLEMENTATION_FAILED")
        finally:
            context.control.close()
            if inspect.isasyncgen(stream):
                await stream.aclose()

    def _fail(self, task_id, activation_id, code):
        task = self.runner.task(task_id)
        error = AgentLoomError(code, category="protocol").to_dict()
        error.update(
            scope_kind="task", scope_id=task_id, source_operation_id=None, details_ref=None
        )
        receipt = self._submit(
            task, activation_id, "FailTask", {"error": error}, "node-failed:" + activation_id
        )
        return ActivationReceipt(task_id, activation_id, "failed", receipt)

    def _outcome(self, task, outcome, activation_id):
        task_id = task["task_id"]
        if not isinstance(outcome, (Progress, Wait, Complete, Fail)):
            raise RequestError("NODE_OUTCOME_INVALID")
        if isinstance(outcome, Fail):
            if not isinstance(outcome.error.get("code"), str):
                raise RequestError("NODE_OUTCOME_INVALID")
            body = {"error": copy_document(outcome.error)}
            if outcome.partial_result_ref is not None:
                if (
                    not isinstance(outcome.partial_result_ref, PayloadRef)
                    or outcome.partial_result_ref.owner_id != task_id
                ):
                    raise RequestError("NODE_OUTCOME_INVALID")
                body["partial_result_ref"] = outcome.partial_result_ref.to_dict()
            if outcome.accounting is not None:
                body["accounting"] = copy_document(outcome.accounting)
            return "FailTask", body, "task-failed:" + task_id
        ref = outcome.continuation
        if not isinstance(ref, ContinuationRef):
            raise RequestError("NODE_OUTCOME_INVALID")
        if ref.owner_id != task_id or type(ref.revision) is not int:
            raise RequestError("NODE_OUTCOME_INVALID")
        if ref.revision != task["node_revision"]:
            raise RequestError("COMMIT_CONFLICT")
        effects = outcome.writes if isinstance(outcome, Complete) else outcome.effects
        if not isinstance(effects, tuple) or not all(
            isinstance(effect, PreparedEffect) for effect in effects
        ):
            raise RequestError("NODE_OUTCOME_INVALID")
        body = {"continuation": copy_document(ref.value), "effects": [e.to_dict() for e in effects]}
        prefix = {
            "prepare_context": "prepare",
            "accept_model": "decision",
            "consume_batch": "consume",
            "format_feedback": "correction",
        }
        command = next((prefix[e.kind] for e in effects if e.kind in prefix), "progress")
        command_id = (
            f"{command}:{task_id}:{task['round']}"
            if command != "progress"
            else f"progress:{activation_id}:{task['node_revision']}"
        )
        for effect in effects:
            if effect.kind == "begin_model":
                command_id = "begin:" + effect.body["operation"]["operation_id"]
        if isinstance(outcome, Progress):
            return "ProgressNode", body, command_id
        if isinstance(outcome, Complete):
            if (
                not isinstance(outcome.output_ref, PayloadRef)
                or outcome.output_ref.owner_id != task_id
            ):
                raise RequestError("NODE_OUTCOME_INVALID")
            body["output"] = copy_document(outcome.output_ref.value)
            return "CompleteTask", body, f"validate:{task_id}:{task['round']}"
        if (
            not isinstance(outcome.condition, WaitCondition)
            or type(outcome.condition.generation) is not int
            or outcome.condition.generation != 1
            or type(outcome.condition.observed_revision) is not int
            or outcome.condition.observed_revision < 0
        ):
            raise RequestError("NODE_OUTCOME_INVALID")
        self._validate_wait(task_id, outcome.condition)
        if (
            not isinstance(outcome.resume_input_ref, PayloadRef)
            or outcome.resume_input_ref.owner_id != task_id
        ):
            raise RequestError("NODE_OUTCOME_INVALID")
        body.update(
            condition=outcome.condition.to_dict(),
            resume_input_ref=outcome.resume_input_ref.to_dict(),
        )
        return "WaitNode", body, "wait:" + activation_id

    def _validate_wait(self, task_id, condition):
        state = self.runner.state()
        if condition.kind == "operation_completed":
            target = state["operations"].get(condition.ref_id)
            valid = target is not None and target["task_id"] == task_id
        elif condition.kind == "batch_settled":
            target = state.get("batches", {}).get(condition.ref_id)
            valid = target is not None and target["owner_task_id"] == task_id
        elif condition.kind == "child_completed":
            target = state["tasks"].get(condition.ref_id)
            parent = state["operations"].get(target["parent_operation"]) if target else None
            valid = parent is not None and parent["task_id"] == task_id
        elif condition.kind == "budget_available":
            valid = condition.ref_id == self.runner.run_id
        else:
            valid = False
        if not valid:
            raise RequestError("NODE_OUTCOME_INVALID")
