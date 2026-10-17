"""Durable logical calls, attempts and explicit before/after stage dispatch."""

import asyncio
import copy
import math
import time
from uuid import NAMESPACE_URL, uuid5

from ..configuration import digest
from ..context import CompactionService, estimate, materialize
from ..errors import AgentLoomError, InfrastructureError, error_record
from ..types import CallStageEvent
from .handlers import AttemptContext, BusinessEvent
from .validation import validate_action


def success(result):
    return {"status": "succeeded", "result": result, "error": None, "execution_fact": "completed"}


def failure(error, *, blocked=False):
    return {
        "status": "blocked" if blocked else "failed",
        "result": None,
        "error": error,
        "execution_fact": "not_started"
        if blocked or error.get("external_outcome") == "not_started"
        else "unknown",
    }


class CallDriver:
    def __init__(self, runner):
        self.runner = runner
        self.compaction = CompactionService(runner)

    def begin(
        self,
        task_id,
        key,
        event_type,
        request,
        purpose="business",
        binding_key=None,
        source_call_id=None,
    ):
        runner = self.runner
        op_id = str(uuid5(NAMESPACE_URL, f"{runner.run_id}:{task_id}:{key}"))
        existing = runner.operation(op_id)
        if existing is not None:
            if source_call_id is not None and existing.get("source_call_id") is None:
                runner.commit(
                    "action-source:" + op_id,
                    lambda s: s["operations"][op_id].update(source_call_id=source_call_id),
                )
            return op_id
        task = runner.task(task_id)
        op = {
            "operation_id": op_id,
            "task_id": task_id,
            "role_key": task["role_key"],
            "purpose_key": key,
            "purpose": purpose,
            "event_type": event_type,
            "request": copy.deepcopy(request),
            "request_hash": None,
            "response": None,
            "status": "pending",
            "phase": "before",
            "raw": None,
            "stages": {},
            "hooks": {},
            "attempts": [],
            "ticket": None,
            "permit": None,
            "target_ref": None,
            "binding_key": binding_key,
            "source_call_id": source_call_id,
            "next_attempt_at": None,
            "output_reserve": runner.program.budget.output,
        }
        runner.commit("begin:" + op_id, lambda s: s["operations"].__setitem__(op_id, op))
        return op_id

    async def model(
        self, task_id, key, *, purpose="business", messages=None, max_output_tokens=None
    ):
        runner = self.runner
        task = runner.task(task_id)
        selected = materialize(task["context"]) if messages is None else []
        request = {
            "messages": messages if messages is not None else [r["message"] for r in selected],
            "record_ids": [r["record_id"] for r in selected],
            "tools": runner.tools(task["role_key"]) if purpose == "business" else [],
            "max_output_tokens": max_output_tokens
            or runner.program.model_options.max_output_tokens
            or min(
                runner.program.budget.output,
                runner.program.model.descriptor.max_output_tokens or runner.program.budget.output,
            ),
            "parameters": dict(runner.program.model_options.parameters),
        }
        op_id = self.begin(task_id, key, "model.generate", request, purpose)
        return await self.advance(op_id)

    async def action(self, task_id, call, ordinal, round_number):
        runner = self.runner
        key = f"action:{round_number}:{ordinal}"
        task = runner.task(task_id)
        if key in task.get("local_outcomes", {}):
            return task["local_outcomes"][key]
        try:
            validate_action(runner, task["role_key"], call["name"], call["arguments"])
        except AgentLoomError as exc:
            if isinstance(exc, InfrastructureError):
                raise
            error = {**exc.to_dict(), "phase": "prepare", "external_outcome": "not_started"}
            response = {**failure(error), "operation_id": None}

            def reject(state):
                runner.ensure_admission(state)
                state["tasks"][task_id].setdefault("local_outcomes", {})[key] = response

            runner.commit(f"local-reject:{task_id}:{key}", reject)
            return response
        name = call["name"]
        route = {
            "delegate_task": "executor.delegate",
            "skill_list": "skill.list",
            "skill_read": "skill.read",
            "skill_read_resource": "skill.resource.read",
            "result_read": "result.read",
        }.get(name, "capability.invoke")
        task = runner.task(task_id)
        catalog = runner.program.catalogs[task["role_key"]]
        entry = catalog.tools.get(name)
        if route.startswith("skill.") and isinstance(call["arguments"], dict):
            entry = catalog.skills.get(call["arguments"].get("name"))
        op_id = self.begin(
            task_id,
            key,
            route,
            {"name": name, "arguments": call["arguments"]},
            binding_key=entry.binding.binding_key if entry else None,
            source_call_id=call["call_id"],
        )
        return await self.advance(op_id)

    async def _stage(self, op_id, stage):
        op = self.runner.operation(op_id)
        event = CallStageEvent(
            op_id,
            op["event_type"] + (".before" if stage == "request" else ".after"),
            stage,
            op[stage],
            op["purpose"],
            op["role_key"],
        )
        return await self.runner.bus.request(event)

    async def advance(self, op_id):
        runner = self.runner
        op = runner.operation(op_id)
        if op["status"] != "pending":
            return op["response"]
        runner.check_active()
        if op["event_type"] == "model.generate" and op["purpose"] == "business":
            await self.wait_for_loop_capacity(op_id)
        before = await self._stage(op_id, "request")
        if before["outcome"] == "denied":
            response = failure(
                AgentLoomError(
                    "HOOK_DENIED", phase="prepare", external_outcome="not_started"
                ).to_dict(),
                blocked=True,
            )
            if (
                op["event_type"] == "model.generate"
                and op["purpose"] == "business"
                and op["task_id"] != runner.state()["root_task_id"]
            ):
                await self._permit(op_id, feedback_only=True)
            return self.finish(op_id, response)
        op = runner.operation(op_id)
        if op["raw"] is None:
            runner.check_active()
            if op["event_type"] == "executor.delegate":
                raw = await runner.delegate(op_id)
                runner.commit(
                    "delegate-raw:" + op_id,
                    lambda s: s["operations"][op_id].update(
                        raw=raw, response=copy.deepcopy(raw), phase="after"
                    ),
                )
            elif op["event_type"].startswith("skill.") or op["event_type"] == "result.read":
                try:
                    async with asyncio.timeout(
                        runner.program.execution_policy.tool_timeout_seconds
                    ):
                        raw = success(
                            await runner.bus.request(
                                BusinessEvent(op["event_type"], op_id, op["request"])
                            )
                        )
                except InfrastructureError:
                    raise
                except Exception as exc:
                    raw = failure(error_record(exc))
                runner.commit(
                    "internal-raw:" + op_id,
                    lambda s: s["operations"][op_id].update(
                        raw=raw, response=copy.deepcopy(raw), phase="after"
                    ),
                )
            else:
                if op["event_type"] == "capability.invoke":
                    try:
                        validate_action(
                            runner,
                            op["role_key"],
                            op["request"]["name"],
                            op["request"]["arguments"],
                        )
                    except AgentLoomError as exc:
                        if isinstance(exc, InfrastructureError):
                            raise
                        return self.finish(
                            op_id,
                            failure(
                                {
                                    **exc.to_dict(),
                                    "phase": "prepare",
                                    "external_outcome": "not_started",
                                }
                            ),
                        )
                if (
                    op["event_type"] == "model.generate"
                    and op["purpose"] == "business"
                    and not op["permit"]
                ):
                    await self.compaction.prepare(op_id)
                await self._permit(op_id)
                await self._attempts(op_id)
        op = runner.operation(op_id)
        after = await self._stage(op_id, "response")
        response = runner.operation(op_id)["response"]
        if after["outcome"] == "denied":
            response = failure(AgentLoomError("HOOK_DENIED", phase="after").to_dict(), blocked=True)
            response["execution_fact"] = op["raw"]["execution_fact"]
        return self.finish(op_id, response)

    async def wait_for_loop_capacity(self, op_id):
        """Do not run preparation hooks or compaction while business slots are full."""
        runner = self.runner
        while True:
            runner.check_active()
            state = runner.state()
            op = state["operations"][op_id]
            # An existing reservation belongs to this logical call and its retries.
            if op["permit"] or op["ticket"] == "reserved":
                return
            usage = state["usage"]
            limit = runner.program.execution_policy.loop_max
            if usage["loops_consumed"] >= limit:
                raise AgentLoomError("LOOP_LIMIT_EXCEEDED")
            if usage["loops_consumed"] + usage["loops_reserved"] < limit:
                return
            await asyncio.sleep(0.005)

    async def _permit(self, op_id, *, feedback_only=False):
        runner = self.runner
        while True:
            op = runner.operation(op_id)
            if op["permit"] or feedback_only and op["ticket"] is not None:
                return
            runner.check_active()
            model = op["event_type"] == "model.generate"
            business = model and op["purpose"] == "business"
            state = runner.state()
            if (
                business
                and state["usage"]["loops_consumed"] + state["usage"]["loops_reserved"]
                >= runner.program.execution_policy.loop_max
            ):
                if state["usage"]["loops_reserved"]:
                    await asyncio.sleep(0.005)
                    continue
                raise AgentLoomError("LOOP_LIMIT_EXCEEDED")
            if (
                model
                and not feedback_only
                and estimate(
                    op["request"]["messages"], op["request"]["tools"], op["request"]["parameters"]
                )
                > runner.program.budget.input
            ):
                raise AgentLoomError("CONTEXT_WINDOW_EXCEEDED")

            def grant(s):
                runner.ensure_admission(s)
                current = s["operations"][op_id]
                if not feedback_only:
                    current.update(
                        permit={"epoch": runner.lease.epoch},
                        phase="execute",
                        request_hash=digest(current["request"]),
                    )
                if business:
                    if (
                        s["usage"]["loops_consumed"] + s["usage"]["loops_reserved"]
                        >= runner.program.execution_policy.loop_max
                    ):
                        raise AgentLoomError("LOOP_LIMIT_EXCEEDED")
                    current["ticket"] = "reserved"
                    s["usage"]["loops_reserved"] += 1
                    if not feedback_only and op["task_id"] == s["root_task_id"]:
                        ids = set(current["request"]["record_ids"])
                        for item in s["inputs"]:
                            if item.get("record_id") in ids and item["delivery_state"] == "pending":
                                item.update(delivery_state="claimed", claimed_by=op_id)

            runner.commit(("feedback-ticket:" if feedback_only else "permit:") + op_id, grant)
            return

    async def _attempts(self, op_id):
        runner = self.runner
        policy = runner.program.execution_policy
        while runner.operation(op_id)["raw"] is None:
            runner.check_active()
            op = runner.operation(op_id)
            if op["attempts"] and op["attempts"][-1]["status"] == "started":
                error = AgentLoomError(
                    "CALL_OUTCOME_UNKNOWN", category="unknown", external_outcome="unknown"
                ).to_dict()
                self._save_attempt(op_id, failure(error), None, force_final=True)
                continue
            while op["next_attempt_at"] and op["next_attempt_at"] > time.time():
                # The persisted timestamp, not a process-local timer, is authoritative.
                await asyncio.sleep(min(0.2, max(0, op["next_attempt_at"] - time.time())))
                runner.check_active()
                op = runner.operation(op_id)
            runner.check_active()
            index = len(op["attempts"]) + 1
            attempt_id = str(uuid5(NAMESPACE_URL, op_id + ":attempt:" + str(index)))
            attempt = {
                "attempt_id": attempt_id,
                "attempt_no": index,
                "status": "started",
                "started_at": time.time(),
                "first_output_at": None,
                "error": None,
            }

            def start(s):
                runner.ensure_admission(s)
                s["operations"][op_id]["attempts"].append(attempt)
                key = (
                    (
                        "compaction_model_attempts"
                        if op["purpose"] == "compaction"
                        else "business_model_attempts"
                    )
                    if op["event_type"] == "model.generate"
                    else "tool_attempts"
                )
                s["usage"][key] += 1
                if op["event_type"] == "model.generate":
                    s["usage"]["token_counts_complete"] = False

            runner.commit(f"attempt-start:{op_id}:{index}", start)
            ctx = AttemptContext(
                op_id, attempt_id, index, runner.lease.epoch, runner.state()["deadline_at"]
            )
            partial = None
            try:
                event = BusinessEvent(op["event_type"], op_id, runner.operation(op_id)["request"])
                if op["event_type"] == "model.generate":
                    value = await runner.bus.request(event, ctx)
                else:
                    async with asyncio.timeout(policy.tool_timeout_seconds):
                        value = await runner.bus.request(event, ctx)
                raw = success(value)
            except InfrastructureError:
                raise
            except TimeoutError:
                raw = failure(
                    AgentLoomError(
                        "TOOL_TIMEOUT", category="timeout", external_outcome="unknown"
                    ).to_dict()
                )
            except Exception as exc:
                raw = failure(error_record(exc))
                if getattr(exc, "result_material", None) is not None:
                    raw["result"] = exc.result_material
                if getattr(exc, "partial_text", ""):
                    partial = {
                        "attempt_id": attempt_id,
                        "text": exc.partial_text,
                        "complete": False,
                    }
            self._save_attempt(op_id, raw, partial)

    def _save_attempt(self, op_id, raw, partial, *, force_final=False):
        runner = self.runner
        op = runner.operation(op_id)
        index = len(op["attempts"])
        retry = runner.program.execution_policy.retry
        error = raw["error"]
        retrying = bool(
            not force_final
            and error
            and retry.enabled
            and index < retry.max_attempts
            and (
                error.get("retryable_transport")
                and error.get("category") in {"transport", "rate_limit"}
                or error.get("category") == "timeout"
                and error.get("code")
                in {"MODEL_FIRST_OUTPUT_TIMEOUT", "MODEL_STREAM_IDLE_TIMEOUT", "TOOL_TIMEOUT"}
            )
        )

        def save(s):
            current = s["operations"][op_id]
            attempt = current["attempts"][-1]
            attempt.update(
                status="unknown" if force_final else raw["status"],
                error=error,
                ended_at=time.time(),
                result=copy.deepcopy(raw),
            )
            if partial:
                s["partial_results"].append(partial)
            if retrying:
                next_attempt_at = time.time() + retry.delay_seconds
                server_deadline = error.get("retry_after_at")
                if type(server_deadline) in (int, float) and math.isfinite(server_deadline):
                    next_attempt_at = max(next_attempt_at, server_deadline)
                current["next_attempt_at"] = next_attempt_at
                attempt["next_attempt_at"] = next_attempt_at
            else:
                current.update(
                    raw=raw, response=copy.deepcopy(raw), phase="after", next_attempt_at=None
                )
            if current["event_type"] == "model.generate":
                usage = (raw["result"] or {}).get("usage")
                known = bool(
                    usage
                    and all(
                        type(usage.get(k)) is int and usage[k] >= 0
                        for k in ("input_tokens", "output_tokens")
                    )
                )
                u = s["usage"]
                if known:
                    for key in ("input_tokens", "output_tokens"):
                        u[key] = (u[key] or 0) + usage[key]
                attempts = u["business_model_attempts"] + u["compaction_model_attempts"]
                s["token_usage_known_attempts"] = s.get("token_usage_known_attempts", 0) + int(
                    known
                )
                u["token_counts_complete"] = s["token_usage_known_attempts"] == attempts

        runner.commit(f"attempt-result:{op_id}:{index}", save)

    def finish(self, op_id, response):
        runner = self.runner
        response = {**response, "operation_id": op_id}

        def complete(s):
            from .control import emit

            op = s["operations"][op_id]
            op.update(status=response["status"], phase="completed", response=response)
            if op["ticket"] == "reserved":
                consumed = response["status"] == "succeeded" or op["task_id"] != s["root_task_id"]
                op["ticket"] = "consumed" if consumed else "released"
                s["usage"]["loops_reserved"] -= 1
                if consumed:
                    s["usage"]["loops_consumed"] += 1
                    op["consumption_kind"] = (
                        "decision" if response["status"] == "succeeded" else "error_feedback"
                    )
            emit(
                s,
                "operation.completed",
                {
                    "operation_id": op_id,
                    "task_id": op["task_id"],
                    "role_key": op["role_key"],
                    "event_type": op["event_type"],
                    "status": response["status"],
                },
            )

        runner.commit("complete:" + op_id, complete)
        return runner.operation(op_id)["response"]
