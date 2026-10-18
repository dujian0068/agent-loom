"""Business event adapters. Graph/context handlers prepare data; drivers commit it."""

from dataclasses import dataclass

from ..configuration.json_documents import copy_document, document_digest
from ..context import materialize
from ..errors import RequestError
from ..nodes import PreparedEffect
from .validation import validate_action


@dataclass(frozen=True)
class PreparedDelegate:
    operation_id: str
    effective_request_hash: str
    task: dict


class GraphExecutionHandler:
    def __init__(self, runner):
        self.runner = runner

    async def handle(self, event, context):
        from .runner import new_task

        op = self.runner.operation(event.operation_id)
        if (
            op is None
            or op["event_type"] != "executor.delegate"
            or op["permit"]
            or op["stages"].get("request", {}).get("outcome") != "allowed"
        ):
            raise RequestError("GRAPH_PREPARATION_NOT_AUTHORIZED")
        arguments = op["request"]["arguments"]
        validate_action(self.runner, op["role_key"], op["request"]["name"], arguments)
        role = arguments["executor"]
        task = new_task(
            role,
            self.runner.program.executors[role],
            arguments["task"],
            arguments.get("variables"),
            parent_operation=event.operation_id,
            system_prompt=self.runner._prompts.get(role),
        )
        return PreparedDelegate(event.operation_id, document_digest(op["request"]), task)


class ContextHandler:
    def __init__(self, runner):
        self.runner = runner

    async def handle(self, event, context):
        if event.event_type == "context.compact":
            op = self.runner.operation(event.operation_id)
            if context is not self.runner.lease or op is None or op["permit"]:
                raise RequestError("CONTEXT_SCOPE_MISMATCH")
            self.runner.check_active()
            return await self.runner.calls.compaction.prepare(event.operation_id)
        task_id = event.payload["task_id"]
        if context is None or context.task_id != task_id:
            raise RequestError("CONTEXT_SCOPE_MISMATCH")
        context.control.check()
        task = self.runner.task(task_id)
        if event.event_type == "context.materialize":
            return copy_document(materialize(task["context"]))
        if event.event_type == "context.mutate":
            kind = event.payload["kind"]
            if kind not in {"prepare_context", "accept_model", "consume_batch", "format_feedback"}:
                raise RequestError("CONTEXT_MUTATION_INVALID")
            return PreparedEffect(
                kind,
                {
                    **event.payload.get("body", {}),
                    "expected_context_revision": task["context"]["revision"],
                },
            )
        raise RequestError("CONTEXT_OPERATION_INVALID")


class EnvironmentHandler:
    def __init__(self, runner):
        self.runner = runner

    async def handle(self, event, context):
        self.runner.check_active()
        if event.event_type == "environment.prepare":
            if context is not self.runner.lease:
                raise RequestError("ENVIRONMENT_SCOPE_MISMATCH")
            return self.runner.prepare_environment()
        if (
            event.event_type in {"environment.read", "environment.write"}
            and "member" in event.payload
        ):
            from ..capabilities.contracts import ToolInvocationContext

            op = self.runner.operation(event.operation_id)
            if (
                not isinstance(context, ToolInvocationContext)
                or op is None
                or not op["permit"]
                or context.run_id != self.runner.run_id
                or context.task_id != op["task_id"]
                or context.operation_id != event.operation_id
                or context.lease_epoch != self.runner.lease.epoch
                or event.payload["member"] != context.spec.capability_key
                or document_digest(event.payload["arguments"])
                != document_digest(op["request"]["arguments"])
            ):
                raise RequestError("ENVIRONMENT_SCOPE_MISMATCH")
            context.check_active()
            return getattr(context.workspace, event.payload["member"])(**event.payload["arguments"])
        if event.event_type == "environment.read":
            if context is None or context.scope.run_id != self.runner.run_id:
                raise RequestError("ENVIRONMENT_SCOPE_MISMATCH")
            context.control.check()
            # Host paths, provider addresses and authority never enter Node material.
            return {
                "logical_root": "/work",
                "prepared": self.runner.state()["workspace"] is not None,
            }
        raise RequestError("ENVIRONMENT_OPERATION_INVALID")
