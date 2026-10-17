"""A ReactAgent's resumable model/action/feedback loop, driven in its task scope."""

import asyncio
import json

from ..configuration import canonical, json_copy, validate_schema
from ..context import append
from ..context.results import project_result
from ..errors import AgentLoomError, InfrastructureError


def assistant_message(response):
    message = {"role": "assistant", "content": response["text"]}
    if response.get("provider_state"):
        message["provider_state"] = response["provider_state"]
    if response["tool_calls"]:
        message["tool_calls"] = [
            {
                "id": c["call_id"],
                "type": "function",
                "function": {
                    "name": c["name"],
                    "arguments": canonical(c["arguments"])
                    if not isinstance(c["arguments"], str)
                    else c["arguments"],
                },
            }
            for c in response["tool_calls"]
        ]
    return message


class AgentLoop:
    def __init__(self, agent, node_context):
        self.agent = agent
        self.runner = node_context.driver
        self.task_id = node_context.task_id

    async def run(self):
        runner = self.runner
        task_id = self.task_id
        try:
            while True:
                # Synchronous local adapters must not starve stop/renew/input tasks.
                await asyncio.sleep(0)
                task = runner.task(task_id)
                if task["status"] in {"succeeded", "failed", "cancelled"}:
                    return task
                runner.check_active()
                round_number = task["round"]
                if task["stage"] == "prepare":

                    def prepare(s):
                        current = s["tasks"][task_id]
                        current.update(status="running", stage="call_model")
                        if task_id == s["root_task_id"]:
                            for item in sorted(s["inputs"], key=lambda i: i["session_seq"]):
                                if (
                                    item.get("record_id") is None
                                    and item["delivery_state"] == "pending"
                                ):
                                    entry = append(
                                        current["context"],
                                        {"role": "user", "content": item["text"]},
                                        protected=True,
                                        fixed=True,
                                    )
                                    item["record_id"] = entry["record_id"]

                    runner.commit(f"prepare:{task_id}:{round_number}", prepare)
                    continue
                if task["stage"] == "call_model":
                    response = await runner.calls.model(task_id, f"model:{round_number}")
                    if response["status"] != "succeeded":
                        return self.fail(task_id, response["error"])
                    candidate = response["result"]
                    raw_candidate = runner.operation(response["operation_id"])["raw"]["result"]

                    def accept(s):
                        current = s["tasks"][task_id]
                        append(
                            current["context"],
                            assistant_message(candidate),
                            raw=assistant_message(raw_candidate),
                        )
                        current.update(
                            stage="batch" if candidate["tool_calls"] else "validate",
                            calls=candidate["tool_calls"],
                            candidate=candidate["text"],
                        )

                    runner.commit(f"decision:{task_id}:{round_number}", accept)
                    continue
                if task["stage"] == "batch":
                    calls = task["calls"]
                    if self.agent.execution_policy.parallel_enabled:
                        futures = [
                            asyncio.create_task(
                                runner.calls.action(task_id, call, ordinal, round_number)
                            )
                            for ordinal, call in enumerate(calls)
                        ]
                        try:
                            responses = await asyncio.gather(*futures)
                        except BaseException:
                            for future in futures:
                                future.cancel()
                            await asyncio.gather(*futures, return_exceptions=True)
                            raise
                    else:
                        responses = [
                            await runner.calls.action(task_id, call, ordinal, round_number)
                            for ordinal, call in enumerate(calls)
                        ]

                    def consume(s):
                        current = s["tasks"][task_id]
                        for call, response in zip(calls, responses):
                            op = s["operations"].get(response.get("operation_id"))
                            append(
                                current["context"],
                                {
                                    "role": "tool",
                                    "tool_call_id": call["call_id"],
                                    "content": canonical(
                                        response
                                        if op is None or op["event_type"] == "result.read"
                                        else project_result(response, runner.run_id)
                                    ),
                                },
                                raw={
                                    "role": "tool",
                                    "tool_call_id": call["call_id"],
                                    "content": canonical((op["raw"] if op else None) or response),
                                },
                            )
                        current.update(stage="prepare", round=round_number + 1, calls=[])

                    runner.commit(f"consume:{task_id}:{round_number}", consume)
                    continue
                if task["stage"] == "validate":
                    definition = self.agent.definition
                    try:
                        value = (
                            json_copy(json.loads(task["candidate"]))
                            if definition.output_schema is not None
                            else task["candidate"]
                        )
                        validate_schema(
                            value, definition.output_schema, code="OUTPUT_SCHEMA_INVALID"
                        )
                        if definition.output_schema is None and not value.strip():
                            raise AgentLoomError("OUTPUT_EMPTY")
                    except (ValueError, AgentLoomError):

                        def correction(s):
                            current = s["tasks"][task_id]
                            append(
                                current["context"],
                                {
                                    "role": "user",
                                    "content": "The final answer did not satisfy the required output format. Return a corrected complete answer. Required schema: "
                                    + canonical(definition.output_schema),
                                },
                                kind="format",
                            )
                            current.update(stage="prepare", round=round_number + 1)

                        runner.commit(f"correction:{task_id}:{round_number}", correction)
                        continue
                    runner.commit(
                        f"validate:{task_id}:{round_number}",
                        lambda s: s["tasks"][task_id].update(
                            status="succeeded", stage="completed", result=value
                        ),
                    )
                    return runner.task(task_id)
                raise InfrastructureError("CONTINUATION_INVALID")
        except InfrastructureError:
            raise
        except AgentLoomError as exc:
            if exc.code in {
                "LOOP_LIMIT_EXCEEDED",
                "RUN_TIMEOUT",
                "RUN_STOPPED",
                "COMPACTION_FAILED",
                "COMPACTION_FIXED_PROMPT_TOO_LARGE",
                "CONTEXT_PROTECTED_INPUT_TOO_LARGE",
                "CONTEXT_BUDGET_INVALID",
                "CONTEXT_WINDOW_EXCEEDED",
            }:
                raise
            return self.fail(task_id, exc.to_dict())

    def fail(self, task_id, error):
        self.runner.commit(
            "task-failed:" + task_id,
            lambda s: s["tasks"][task_id].update(status="failed", stage="completed", error=error),
        )
        return self.runner.task(task_id)
