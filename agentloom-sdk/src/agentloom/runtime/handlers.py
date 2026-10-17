"""Leaf handlers execute one attempt. They do not run hooks or mutate contexts."""

import asyncio
import time
from dataclasses import dataclass
from uuid import NAMESPACE_URL, uuid5

from ..capabilities import CapabilityContext, WorkspaceService
from ..configuration import json_copy, validate_schema
from ..errors import AgentLoomError
from ..models.material import PartialMaterial, protocol_error, validate_response, validate_usage
from ..types import ModelRequest, ProviderEvent


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
                    validate_usage(frame.data)
                if frame.kind == "completed":
                    response = validate_response(frame.data)
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
                    return response
        except BaseException as exc:
            # The call driver persists these as audit material, never as a decision.
            exc.partial_material = partial.snapshot()
            raise
        finally:
            close = getattr(iterator, "aclose", None)
            if close:
                await close()


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
        workspace = WorkspaceService(
            runner.runtime.environment,
            runner.state()["workspace"],
            runner.check_active,
            entry.binding.access_scope,
        )
        context = CapabilityContext(
            op["operation_id"],
            attempt.attempt_id,
            runner.run_id,
            op["task_id"],
            op["role_key"],
            workspace,
            entry.binding.config,
            runner.check_active,
            entry.spec,
        )
        result = await entry.registration.adapter.invoke(
            entry.spec.capability_key, arguments, context
        )
        value = json_copy(result)
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
            exc.execution_fact = "completed"
            exc.category = "protocol"
            exc.external_outcome = "known"
            raise
        return value


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
