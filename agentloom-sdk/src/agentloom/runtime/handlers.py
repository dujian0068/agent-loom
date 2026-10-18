"""Leaf handlers execute one attempt. They do not run hooks or mutate contexts."""

import asyncio
import time
from dataclasses import dataclass, field
from uuid import NAMESPACE_URL, uuid5

from ..capabilities import CapabilityRequest, ToolInvocationContext, WorkspaceService
from ..capabilities.invocation import (
    invoke_adapter,
    normalize_completion,
    normalize_error_completion,
)
from ..configuration import validate_schema
from ..configuration.json_documents import document_digest as digest
from ..errors import AgentLoomError, InfrastructureError
from ..models.material import (
    PartialMaterial,
    merge_usage,
    normalize_usage,
    protocol_error,
    validate_response,
)
from ..resources import close_resource
from ..types import ModelRequest, ProviderEvent
from .completion import failed_result, success


@dataclass(frozen=True)
class BusinessEvent:
    event_type: str
    operation_id: str
    payload: dict


@dataclass(frozen=True)
class AttemptContext:
    operation_id: str
    attempt_id: str
    attempt_no: int
    lease_epoch: int
    deadline_at: float
    diagnostics: list = field(default_factory=list, compare=False)
    completion: object = field(default=None, compare=False, repr=False)


class ModelHandler:
    def __init__(self, runner):
        self.runner = runner

    async def handle(self, event, attempt):
        runner = self.runner
        op = runner.operation(event.operation_id)
        if not op["permit"] or (op["purpose"] == "business" and op["ticket"] != "reserved"):
            raise AgentLoomError("MODEL_PERMIT_REQUIRED")
        payload = event.payload
        request = ModelRequest(
            payload["messages"],
            payload["tools"],
            op["role_key"],
            op["purpose"],
            op["operation_id"],
            payload["max_output_tokens"],
            payload["parameters"],
        )
        iterator = runner.program.model.stream_once(request, attempt).__aiter__()
        policy = runner.program.execution_policy
        started, last_output, received = time.monotonic(), None, False
        partial = PartialMaterial()
        usage = None
        primary = None
        try:
            while True:
                runner.check_active()
                timeout = None
                code = "MODEL_FIRST_OUTPUT_TIMEOUT"
                if not received:
                    timeout = policy.model_first_output_timeout_seconds - (
                        time.monotonic() - started
                    )
                elif policy.model_stream_idle_timeout_seconds is not None:
                    code = "MODEL_STREAM_IDLE_TIMEOUT"
                    timeout = policy.model_stream_idle_timeout_seconds - (
                        time.monotonic() - last_output
                    )
                if timeout is not None and timeout <= 0:
                    raise AgentLoomError(
                        code, category="timeout", phase="receive", external_outcome="unknown"
                    )
                try:
                    if timeout is None:
                        frame = await anext(iterator)
                    else:
                        async with asyncio.timeout(timeout):
                            frame = await anext(iterator)
                except TimeoutError as exc:
                    raise AgentLoomError(
                        code, category="timeout", phase="receive", external_outcome="unknown"
                    ) from exc
                except StopAsyncIteration as exc:
                    raise AgentLoomError(
                        "MODEL_STREAM_INTERRUPTED",
                        category="transport",
                        retryable_transport=True,
                        external_outcome="unknown",
                    ) from exc
                response = None
                if not isinstance(frame, ProviderEvent):
                    raise protocol_error("Model adapter must yield ProviderEvent")
                partial.add(frame)
                if frame.kind == "usage":
                    usage = merge_usage(usage, normalize_usage(frame.data))
                if frame.kind == "completed":
                    response = validate_response(frame.data)
                    response["usage"] = merge_usage(usage, normalize_usage(response.get("usage")))
                    for ordinal, call in enumerate(response["tool_calls"]):
                        call["provider_call_id"] = call["call_id"]
                        call["call_id"] = str(
                            uuid5(NAMESPACE_URL, op["operation_id"] + ":call:" + str(ordinal))
                        )
                        call["ordinal"] = ordinal
                effective = (
                    frame.kind == "completed"
                    or frame.kind
                    in {"text_delta", "reasoning_delta", "tool_name_delta", "tool_arguments_delta"}
                    and bool(frame.data)
                )
                if effective:
                    last_output = time.monotonic()
                    if not received:
                        runner.first_output(op["operation_id"], attempt.attempt_id)
                        received = True
                if frame.kind == "error":
                    raise AgentLoomError("MODEL_PROVIDER_ERROR")
                if frame.kind == "completed":
                    attempt.completion.record(success(response))
                    return response
        except BaseException as exc:
            # The call driver persists these as audit material, never as a decision.
            exc.partial_material = partial.snapshot()
            primary = exc
            raise
        finally:
            close = getattr(iterator, "aclose", None)
            if close:
                await close_resource(close, attempt.diagnostics.append, primary=primary)


class ToolHandler:
    def __init__(self, runner):
        self.runner = runner

    async def handle(self, event, attempt):
        runner = self.runner
        op = runner.operation(event.operation_id)
        catalog = runner.program.catalogs[op["role_key"]]
        entry = catalog.tools.get(event.payload["name"])
        if entry is None:
            raise AgentLoomError(
                "CAPABILITY_NOT_FOUND", category="permission", external_outcome="not_started"
            )
        arguments = event.payload["arguments"]
        if not isinstance(arguments, dict):
            raise AgentLoomError(
                "TOOL_ARGUMENTS_INVALID", category="validation", external_outcome="not_started"
            )
        validate_schema(arguments, entry.spec.input_schema)
        state = runner.state()
        file_context = runner.runtime._files.context(
            state["workspace"],
            runner.check_active,
            session_id=state["session_id"],
            role_key=op["role_key"],
            operation_id=op["operation_id"],
            attempt_id=attempt.attempt_id,
            attempt_no=attempt.attempt_no,
            deadline_at=min(
                attempt.deadline_at,
                time.time() + runner.program.execution_policy.tool_timeout_seconds,
            ),
            access_scope=entry.binding.access_scope,
        )
        workspace = WorkspaceService(
            runner.runtime.environment,
            runner.state()["workspace"],
            runner.check_active,
            entry.binding.access_scope,
            backend=runner.runtime._files.backend,
            context=file_context,
        )
        state = runner.state()
        runner.check_active()
        # The frozen request permit survives takeover. The current attempt is
        # admitted under the new lease; requiring the old permit's epoch would
        # reject a valid crash between permit commit and the first dispatch.
        if not op["permit"] or attempt.lease_epoch != runner.lease.epoch:
            raise AgentLoomError("CAPABILITY_PERMIT_REQUIRED", external_outcome="not_started")
        if op["request_hash"] != digest(event.payload):
            raise AgentLoomError("CAPABILITY_REQUEST_MISMATCH", external_outcome="not_started")
        scope = {"run_id": runner.run_id, "task_id": op["task_id"]}

        def complete(result=None, *, error=None):
            try:
                outcome = (
                    normalize_error_completion(error)
                    if error is not None
                    else normalize_completion(result)
                )
                outcome = self._validate_output(outcome, entry)
            except InfrastructureError:
                raise
            except AgentLoomError as exc:
                attempt.completion.record(failed_result(exc))
                raise
            attempt.completion.record(outcome.raw_result())
            if error is not None:
                raise error

        context = ToolInvocationContext(
            content_scope_ref=scope,
            run_id=runner.run_id,
            graph_execution_id=state["graph_id"],
            node_task_id=op["task_id"],
            invocation_id=state["invocation_id"],
            session_id=state["session_id"],
            role_key=op["role_key"],
            execution_binding_ref={
                "program_key": runner.program.program_key,
                "binding_key": op["role_key"],
                "capability_binding_key": entry.binding.binding_key,
            },
            binding_config=entry.binding.config,
            workspace_scope_ref={"run_id": runner.run_id, "lease_epoch": runner.lease.epoch},
            operation_id=op["operation_id"],
            attempt_id=attempt.attempt_id,
            attempt_no=attempt.attempt_no,
            lease_epoch=attempt.lease_epoch,
            deadline_at=min(
                attempt.deadline_at,
                time.time() + runner.program.execution_policy.tool_timeout_seconds,
            ),
            authorization_ref={
                **scope,
                "binding_key": entry.binding.binding_key,
                "access_scope": entry.binding.access_scope,
            },
            cancellation=runner.check_active,
            workspace=workspace,
            check_active=runner.check_active,
            spec=entry.spec,
            report_diagnostic=attempt.diagnostics.append,
            report_completion=complete,
        )
        request = CapabilityRequest(
            op["operation_id"],
            entry.spec.resource_key or entry.registration.adapter_key,
            entry.spec.capability_key,
            arguments,
        )
        outcome = await invoke_adapter(entry.registration.adapter, request, context)
        return self._validate_output(outcome, entry)

    @staticmethod
    def _validate_output(outcome, entry):
        value = outcome.value
        if outcome.envelope["status"] != "succeeded":
            return outcome
        try:
            validate_schema(
                value,
                entry.spec.output_schema,
                code="MCP_OUTPUT_SCHEMA_INVALID"
                if entry.binding.kind == "mcp"
                else "CAPABILITY_OUTPUT_SCHEMA_INVALID",
            )
        except AgentLoomError as exc:
            # The invocation completed even though its returned value is invalid.
            exc.result_material = value
            exc.tool_result = outcome.envelope
            exc.execution_fact = "completed"
            exc.category = "protocol"
            exc.external_outcome = "known"
            raise
        return outcome


class SkillHandler:
    def __init__(self, runner):
        self.runner = runner

    async def handle(self, event, context):
        runner = self.runner
        op = runner.operation(event.operation_id)
        skills = runner.program.catalogs[op["role_key"]].skills
        visible = {
            name: entry
            for name, entry in skills.items()
            if not entry.registration.package.metadata.get("disable-model-invocation", False)
        }
        arguments = event.payload["arguments"]
        if not isinstance(arguments, dict):
            raise AgentLoomError("SKILL_ARGUMENTS_INVALID")
        if event.event_type == "skill.list":
            return {
                "entries": [
                    {
                        **entry.registration.loader.list_entries(entry.registration.package)[0],
                        "name": name,
                    }
                    for name, entry in visible.items()
                ]
            }
        entry = visible.get(arguments.get("name"))
        if entry is None:
            raise AgentLoomError("SKILL_NOT_AUTHORIZED", category="permission")
        registration = entry.registration
        if event.event_type == "skill.read":
            return registration.loader.read_entry(
                registration.package,
                raw_arguments=arguments.get("raw_arguments"),
                arguments=arguments.get("arguments"),
            )
        return registration.loader.read_resource(registration.package, arguments["path"])
