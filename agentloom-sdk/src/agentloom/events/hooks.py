"""Ordered hooks with durable result/apply boundaries and restricted context views."""

from __future__ import annotations

import asyncio
import inspect
import time
from dataclasses import dataclass, field
from types import MappingProxyType
from uuid import NAMESPACE_URL, uuid5

from ..configuration import json_copy, number
from ..errors import AgentLoomError, ConfigurationError, InfrastructureError, error_record
from .bus import EventBusImpl, HandlerBinding
from .hook_reads import HookReadPort
from .hook_views import HookCallContext, HookContext, HookInput
from .patch_contract import patch_contract, validate_parameter_patch
from .patches import apply_saved_hook

ROUTES = frozenset(
    {
        "model.generate",
        "capability.invoke",
        "executor.delegate",
        "skill.list",
        "skill.read",
        "skill.resource.read",
        "result.read",
    }
)


@dataclass(frozen=True)
class HookMatcher:
    role_keys: tuple[str, ...] | None = None
    capability_binding_keys: tuple[str, ...] | None = None
    purposes: tuple[str, ...] | None = None

    def __post_init__(self):
        for name in ("role_keys", "capability_binding_keys", "purposes"):
            value = getattr(self, name)
            if value is not None:
                if (
                    not isinstance(value, (tuple, list))
                    or not value
                    or any(not isinstance(v, str) or not v for v in value)
                ):
                    raise ConfigurationError("HOOK_MATCHER_INVALID")
                object.__setattr__(self, name, tuple(value))
        if self.purposes and set(self.purposes) - {"business", "compaction", "system"}:
            raise ConfigurationError("HOOK_MATCHER_INVALID")


@dataclass(frozen=True)
class HookBinding:
    binding_key: str
    event_type: str
    stage: str
    handler_key: str
    ordinal: int = 0
    matcher: HookMatcher = field(default_factory=HookMatcher)
    timeout_ms: int = 5000


@dataclass(frozen=True)
class HookDecision:
    decision: str = "allow"
    payload_patch: dict = field(default_factory=dict)
    context_patch: dict = field(default_factory=dict)


class HookSet:
    def __setattr__(self, name, value):
        if name in self.__dict__:
            raise AttributeError("HookSet is immutable")
        object.__setattr__(self, name, value)

    def __init__(self, bindings=(), handlers=None):
        self.bindings = tuple(bindings)
        self.handlers = MappingProxyType(dict(handlers or {}))
        keys, positions = set(), set()
        for binding in self.bindings:
            if not isinstance(binding, HookBinding) or any(
                not isinstance(key, str) or not key
                for key in (binding.binding_key, binding.handler_key)
            ):
                raise ConfigurationError("HOOK_BINDING_INVALID")
            position = (binding.event_type, binding.stage, binding.ordinal)
            if binding.binding_key in keys or position in positions:
                raise ConfigurationError("HOOK_BINDING_DUPLICATE")
            keys.add(binding.binding_key)
            positions.add(position)
            if binding.event_type not in ROUTES or binding.stage not in {"request", "response"}:
                raise ConfigurationError("HOOK_ROUTE_INVALID")
            if not callable(getattr(self.handlers.get(binding.handler_key), "handle", None)):
                raise ConfigurationError("HOOK_HANDLER_INVALID")
            number(binding.ordinal, "hook.ordinal", 0, 100000, integer=True)
            number(binding.timeout_ms, "hook.timeout_ms", 1, 3600000, integer=True)
            if not isinstance(binding.matcher, HookMatcher):
                raise ConfigurationError("HOOK_MATCHER_INVALID")
            if binding.matcher.capability_binding_keys and binding.event_type not in {
                "capability.invoke",
                "skill.read",
                "skill.resource.read",
            }:
                raise ConfigurationError("HOOK_MATCHER_INVALID")


class FunctionHookHandler:
    def __init__(self, function):
        self.function = function

    async def handle(self, event, context):
        result = self.function(event, context)
        return await result if inspect.isawaitable(result) else result


class HookHandler(EventBusImpl):
    def __init__(self, hook_set, runner):
        super().__init__()
        self.hook_set, self.runner = hook_set, runner
        for key, handler in hook_set.handlers.items():
            self.register_handler(key, 1, HandlerBinding(handler))

    async def handle(self, event, context=None):
        return await self.request(event, context)

    async def request(self, event, context=None):
        runner = self.runner
        op_id, stage = event.operation_id, event.stage
        op = runner.operation(op_id)
        if stage in op["stages"]:
            return op["stages"][stage]
        if event.event_type != op["event_type"] + (".before" if stage == "request" else ".after"):
            raise AgentLoomError("STAGE_SOURCE_INVALID")
        chain = []
        for binding in self.hook_set.bindings:
            m = binding.matcher
            if binding.event_type != op["event_type"] or binding.stage != stage:
                continue
            if m.role_keys and op["role_key"] not in m.role_keys:
                continue
            if m.purposes and op["purpose"] not in m.purposes:
                continue
            if m.capability_binding_keys and op.get("binding_key") not in m.capability_binding_keys:
                continue
            chain.append(binding)
        denied = False
        for binding in sorted(chain, key=lambda b: b.ordinal):
            runner.check_active()
            key = stage + ":" + binding.binding_key
            op = runner.operation(op_id)
            invocation = op["hooks"].get(key)
            if invocation is None:
                task = runner.task(op["task_id"])
                saved = {
                    "status": "prepared",
                    "records": task["context"]["records"],
                    "revision": task["context"]["revision"],
                    "context_ref": {
                        "context_id": task["context"]["context_id"],
                        "revision": task["context"]["revision"],
                    },
                    "payload": op[stage],
                    "result": None,
                    "diagnostic": None,
                    "contract": patch_contract(runner, op["role_key"]),
                }
                runner.commit(
                    f"hook-prepare:{op_id}:{key}",
                    lambda s: s["operations"][op_id]["hooks"].__setitem__(key, saved),
                )
                invocation = runner.operation(op_id)["hooks"][key]
            if invocation["status"] == "prepared":
                ref = invocation.get(
                    "context_ref",
                    {
                        "context_id": runner.task(op["task_id"])["context"]["context_id"],
                        "revision": invocation["revision"],
                    },
                )
                reader = HookReadPort(
                    invocation["records"],
                    invocation["payload"],
                    ref["context_id"],
                    ref["revision"],
                    guard=runner.check_active,
                )
                state = runner.state()
                metadata = HookContext(
                    hook_invocation_id=str(uuid5(NAMESPACE_URL, op_id + ":hook:" + key)),
                    operation_id=op_id,
                    phase=stage,
                    scope_ref={
                        "run_id": runner.run_id,
                        "graph_execution_id": state["graph_id"],
                        "node_task_id": op["task_id"],
                        "invocation_id": state["invocation_id"],
                        "content_scope_ref": {"run_id": runner.run_id, "task_id": op["task_id"]},
                        "session_id": state["session_id"],
                        "workspace_scope_ref": {
                            "run_id": runner.run_id,
                            "lease_epoch": runner.lease.epoch,
                        },
                        "subject_kind": "node_task",
                        "subject_id": op["task_id"],
                    },
                    deadline_at=min(state["deadline_at"], time.time() + binding.timeout_ms / 1000),
                    input_revision=invocation["revision"],
                    input_context_refs=(reader.ref,),
                    mapping_version=1,
                    run_id=runner.run_id,
                    task_id=op["task_id"],
                    role_key=op["role_key"],
                    purpose=op["purpose"],
                    context_ref=reader.ref,
                )
                runner.commit(
                    f"hook-start:{op_id}:{key}",
                    lambda s: s["operations"][op_id]["hooks"][key].update(
                        status="started", metadata=json_copy(metadata)
                    ),
                )
                hook_event = HookInput(
                    op["event_type"],
                    stage,
                    {"kind": "inline", "value": invocation["payload"]},
                    (reader.ref,),
                    metadata,
                )
                try:
                    async with asyncio.timeout(binding.timeout_ms / 1000):
                        decision = await self.resolve(binding.handler_key).handler.handle(
                            hook_event,
                            HookCallContext(reader, metadata),
                        )
                    if not isinstance(decision, HookDecision) or decision.decision not in {
                        "allow",
                        "deny",
                    }:
                        raise AgentLoomError("HOOK_DECISION_INVALID")
                    result = {
                        "decision": decision.decision,
                        "payload_patch": {},
                        "context_patch": {},
                    }
                    try:
                        result.update(
                            payload_patch=json_copy(decision.payload_patch),
                            context_patch=json_copy(decision.context_patch),
                        )
                        diagnostic = None
                    except Exception as exc:
                        # A well-formed deny remains authoritative even when its
                        # optional patch cannot be represented as canonical JSON.
                        diagnostic = AgentLoomError(
                            "HOOK_PATCH_INVALID", type(exc).__name__
                        ).to_dict()
                except InfrastructureError:
                    raise
                except Exception as exc:
                    result, diagnostic = (
                        {"decision": "allow", "payload_patch": {}, "context_patch": {}},
                        error_record(exc),
                    )
                finally:
                    reader.close()
                parameter_validation = (
                    validate_parameter_patch(result, runner.program.model)
                    if stage == "request" and op["event_type"] == "model.generate"
                    else None
                )
                runner.commit(
                    f"hook-result:{op_id}:{key}",
                    lambda s: s["operations"][op_id]["hooks"][key].update(
                        status="result_saved",
                        result=result,
                        diagnostic=diagnostic,
                        parameter_validation=parameter_validation,
                    ),
                )
                invocation = runner.operation(op_id)["hooks"][key]
            elif invocation["status"] == "started":
                # Side effects of a hook are also unknown after process loss. Never replay it.
                runner.commit(
                    f"hook-unknown:{op_id}:{key}",
                    lambda s: s["operations"][op_id]["hooks"][key].update(
                        status="result_saved",
                        result={"decision": "allow", "payload_patch": {}, "context_patch": {}},
                        diagnostic={"code": "HOOK_OUTCOME_UNKNOWN"},
                    ),
                )
                invocation = runner.operation(op_id)["hooks"][key]
            if invocation["status"] == "result_saved":
                runner.commit(
                    f"hook-apply:{op_id}:{key}",
                    lambda state: apply_saved_hook(state, op_id, key),
                )
                invocation = runner.operation(op_id)["hooks"][key]
            if invocation["result"]["decision"] == "deny":
                denied = True
                break
        result = {"outcome": "denied" if denied else "allowed"}
        runner.commit(
            f"stage:{op_id}:{stage}",
            lambda s: s["operations"][op_id]["stages"].__setitem__(stage, result),
        )
        return result
