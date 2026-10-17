"""Ordered hooks with durable result/apply boundaries and restricted context views."""

from __future__ import annotations

import asyncio
import copy
import inspect
from dataclasses import dataclass, field
from types import MappingProxyType

from ..configuration import freeze, json_copy, number, validate_schema
from ..context.budget import estimate
from ..context.records import materialize, revise_call_arguments
from ..errors import AgentLoomError, ConfigurationError, InfrastructureError, error_record
from ..types import CallStageEvent
from .bus import EventBusImpl, HandlerBinding

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


class HookReadPort:
    def __init__(self, records, payload, context_id, revision):
        self._records, self._payload = copy.deepcopy(records), json_copy(payload)
        self._context_id, self._revision = context_id, revision
        self._active = True

    def _check(self):
        if not self._active:
            raise AgentLoomError("HOOK_READ_PORT_EXPIRED")

    @property
    def ref(self):
        self._check()
        return {"context_id": self._context_id, "revision": self._revision}

    def messages(self):
        self._check()
        return [
            {
                "record_id": r["record_id"],
                "message": copy.deepcopy(r["message"]),
                "protected": r["protected"],
            }
            for r in self._records
        ]

    async def read_payload(self):
        self._check()
        return json_copy(self._payload)

    async def read_context(self, ref, *, after_seq=0, limit=100):
        self._check()
        if ref != {"context_id": self._context_id, "revision": self._revision}:
            raise AgentLoomError("HOOK_CONTEXT_FORBIDDEN")
        if (
            type(limit) is not int
            or not 1 <= limit <= 256
            or type(after_seq) is not int
            or after_seq < 0
        ):
            raise AgentLoomError("INVALID_CURSOR")
        records = self.messages()[after_seq : after_seq + limit]
        return {
            "records": records,
            "next_seq": after_seq + limit if after_seq + limit < len(self._records) else None,
        }


@dataclass(frozen=True)
class HookCallContext:
    reader: HookReadPort
    metadata: dict


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
                    "payload": op[stage],
                    "result": None,
                    "diagnostic": None,
                }
                runner.commit(
                    f"hook-prepare:{op_id}:{key}",
                    lambda s: s["operations"][op_id]["hooks"].__setitem__(key, saved),
                )
                invocation = runner.operation(op_id)["hooks"][key]
            if invocation["status"] == "prepared":
                runner.commit(
                    f"hook-start:{op_id}:{key}",
                    lambda s: s["operations"][op_id]["hooks"][key].update(status="started"),
                )
                reader = HookReadPort(
                    invocation["records"],
                    invocation["payload"],
                    runner.task(op["task_id"])["context"]["context_id"],
                    invocation["revision"],
                )
                hook_event = CallStageEvent(
                    op_id,
                    op["event_type"],
                    stage,
                    freeze(invocation["payload"]),
                    op["purpose"],
                    op["role_key"],
                )
                try:
                    async with asyncio.timeout(binding.timeout_ms / 1000):
                        decision = await self.resolve(binding.handler_key).handler.handle(
                            hook_event,
                            HookCallContext(
                                reader,
                                freeze(
                                    {
                                        "run_id": runner.run_id,
                                        "task_id": op["task_id"],
                                        "context_ref": reader.ref,
                                    }
                                ),
                            ),
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
                    reader._active = False
                runner.commit(
                    f"hook-result:{op_id}:{key}",
                    lambda s: s["operations"][op_id]["hooks"][key].update(
                        status="result_saved", result=result, diagnostic=diagnostic
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

                def apply(state):
                    current = state["operations"][op_id]
                    record = current["hooks"][key]
                    candidate = copy.deepcopy(state["tasks"][current["task_id"]]["context"])
                    payload = copy.deepcopy(current[stage])
                    try:
                        self._patch(current, stage, record, candidate, payload)
                        state["tasks"][current["task_id"]]["context"] = candidate
                        current[stage] = payload
                    except InfrastructureError:
                        raise
                    except Exception as exc:
                        record["diagnostic"] = (
                            exc.to_dict()
                            if isinstance(exc, AgentLoomError)
                            else AgentLoomError("HOOK_PATCH_INVALID", type(exc).__name__).to_dict()
                        )
                    record["status"] = "applied"

                runner.commit(f"hook-apply:{op_id}:{key}", apply)
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

    def _patch(self, op, stage, hook, context, payload):
        decision = hook["result"]
        patches = decision["context_patch"]
        if not isinstance(patches, dict) or not isinstance(decision["payload_patch"], dict):
            raise AgentLoomError("HOOK_PATCH_INVALID")
        originals = {r["record_id"]: r for r in hook["records"]}
        current = {r["record_id"]: r for r in context["records"]}
        for key, content in patches.items():
            if key not in originals or key not in current or not isinstance(content, str):
                raise AgentLoomError("HOOK_CONTEXT_FORBIDDEN")
            if current[key]["protected"]:
                raise AgentLoomError("HOOK_PROTECTED_INPUT")
            if current[key]["message"] != originals[key]["message"]:
                raise AgentLoomError("CONTEXT_REVISION_CONFLICT")
            current[key]["message"]["content"] = content
        patch = decision["payload_patch"]
        allowed = (
            {"result"}
            if stage == "response"
            else (
                {"messages", "max_output_tokens", "parameters"}
                if op["event_type"] == "model.generate"
                else {"arguments"}
            )
        )
        if op["event_type"] == "skill.list":
            allowed = set()
        if set(patch) - allowed:
            raise AgentLoomError("HOOK_PATCH_FORBIDDEN")
        if stage == "response" and patch and payload["status"] != "succeeded":
            raise AgentLoomError("HOOK_CANNOT_OVERRIDE_FAILURE")
        if "messages" in patch:
            messages = patch["messages"]
            ids = op["request"].get("record_ids", [])
            if not isinstance(messages, list) or len(messages) != len(ids):
                raise AgentLoomError("HOOK_PROTOCOL_CHANGED")
            for rid, message in zip(ids, messages):
                original = originals[rid]["message"]
                if not isinstance(message, dict) or not isinstance(message.get("content"), str):
                    raise AgentLoomError("HOOK_CONTEXT_INVALID")
                if {k: v for k, v in message.items() if k != "content"} != {
                    k: v for k, v in original.items() if k != "content"
                }:
                    raise AgentLoomError("HOOK_PROTOCOL_CHANGED")
                if original.get("content") != message.get("content"):
                    if current[rid]["protected"]:
                        raise AgentLoomError("HOOK_PROTECTED_INPUT")
                    if rid in patches and patches[rid] != message.get("content"):
                        raise AgentLoomError("HOOK_PATCH_CONFLICT")
                    if current[rid]["message"] != original and rid not in patches:
                        raise AgentLoomError("CONTEXT_REVISION_CONFLICT")
                    current[rid]["message"]["content"] = message["content"]
        if "max_output_tokens" in patch and (
            type(patch["max_output_tokens"]) is not int
            or not 0 < patch["max_output_tokens"] <= op["output_reserve"]
            or self.runner.program.model.descriptor.max_output_tokens is not None
            and patch["max_output_tokens"] > self.runner.program.model.descriptor.max_output_tokens
        ):
            raise AgentLoomError("HOOK_OUTPUT_BUDGET_INVALID")
        if "parameters" in patch:
            parameters = patch["parameters"]
            if not isinstance(parameters, dict) or set(parameters) - {
                "temperature",
                "top_p",
                "reasoning_effort",
                "reasoning",
            }:
                raise AgentLoomError("HOOK_PATCH_FORBIDDEN")
            from ..configuration import ModelOptions

            if not hasattr(self.runner.program.model, "validate_options"):
                raise AgentLoomError("MODEL_FEATURE_UNSUPPORTED")
            self.runner.program.model.validate_options(ModelOptions(parameters=parameters))
        if "arguments" in patch:
            arguments = patch["arguments"]
            original = payload["arguments"]
            if not isinstance(arguments, dict):
                raise AgentLoomError("HOOK_ARGUMENTS_INVALID")
            route = op["event_type"]
            if route in {"executor.delegate", "skill.read", "skill.resource.read"}:
                mutable = (
                    {"task", "variables"}
                    if route == "executor.delegate"
                    else ({"raw_arguments", "arguments"} if route == "skill.read" else {"path"})
                )
                if {k: v for k, v in arguments.items() if k not in mutable} != {
                    k: v for k, v in original.items() if k not in mutable
                }:
                    raise AgentLoomError("HOOK_TARGET_IMMUTABLE")
            if (
                route == "skill.read"
                and arguments.get("raw_arguments") is not None
                and arguments.get("arguments") is not None
            ):
                raise AgentLoomError("SKILL_ARGUMENTS_INVALID")
            if route == "skill.read":
                from ..capabilities.skills import parse_arguments

                parse_arguments(arguments.get("raw_arguments"), arguments.get("arguments"))
            if route == "capability.invoke":
                entry = self.runner.program.catalogs[op["role_key"]].tools.get(payload["name"])
                if entry is None:
                    raise AgentLoomError("CAPABILITY_NOT_FOUND")
                validate_schema(arguments, entry.spec.input_schema)
            if route == "executor.delegate":
                definition = self.runner.program.executors.get(original.get("executor"))
                if definition is None:
                    raise AgentLoomError("EXECUTOR_NOT_FOUND")
                validate_schema(arguments.get("task"), definition.input_schema)
                definition.render(arguments.get("variables"))
        if "result" in patch:
            result, original = patch["result"], payload["result"]
            if op["event_type"] == "model.generate":
                if (
                    not isinstance(result, dict)
                    or not isinstance(result.get("text"), str)
                    or not isinstance(result.get("tool_calls"), list)
                ):
                    raise AgentLoomError("HOOK_MODEL_RESPONSE_INVALID")
                if {k: v for k, v in result.items() if k not in {"text", "tool_calls"}} != {
                    k: v for k, v in original.items() if k not in {"text", "tool_calls"}
                }:
                    raise AgentLoomError("HOOK_PATCH_FORBIDDEN")
                calls, old_calls = result["tool_calls"], original["tool_calls"]
                if len(calls) != len(old_calls) or any(
                    not isinstance(c, dict)
                    or {k: v for k, v in c.items() if k not in {"name", "arguments"}}
                    != {k: v for k, v in old.items() if k not in {"name", "arguments"}}
                    or set(c) != set(old)
                    or not isinstance(c.get("name"), str)
                    for c, old in zip(calls, old_calls)
                ):
                    raise AgentLoomError("HOOK_MODEL_CALL_SLOTS_IMMUTABLE")
                from ..runtime.validation import validate_action

                for call, old in zip(calls, old_calls):
                    if call["name"] != old["name"] or call["arguments"] != old["arguments"]:
                        validate_action(
                            self.runner, op["role_key"], call["name"], call["arguments"]
                        )
            if op["event_type"] == "capability.invoke":
                entry = self.runner.program.catalogs[op["role_key"]].tools[op["request"]["name"]]
                validate_schema(result, entry.spec.output_schema, code="HOOK_RESULT_SCHEMA_INVALID")
            if op["event_type"] == "executor.delegate":
                definition = self.runner.program.executors[op["request"]["arguments"]["executor"]]
                validate_schema(result, definition.output_schema, code="HOOK_RESULT_SCHEMA_INVALID")
                if definition.output_schema is None and (
                    not isinstance(result, str) or not result.strip()
                ):
                    raise AgentLoomError("HOOK_RESULT_SCHEMA_INVALID")
            if op["event_type"] in {"skill.read", "skill.resource.read", "result.read"}:
                if (
                    not isinstance(result, dict)
                    or original.get("text") is None
                    or not isinstance(result.get("text"), str)
                ):
                    raise AgentLoomError("HOOK_SKILL_TEXT_INVALID")
                if {k: v for k, v in result.items() if k != "text"} != {
                    k: v for k, v in original.items() if k != "text"
                }:
                    raise AgentLoomError("HOOK_PATCH_FORBIDDEN")
                if op["event_type"] == "result.read" and len(result["text"].encode()) > 65536:
                    raise AgentLoomError("HOOK_RESULT_TOO_LARGE")
        payload.update(patch)
        if "arguments" in patch:
            revise_call_arguments(
                context, hook["records"], op.get("source_call_id"), patch["arguments"]
            )
        if (
            stage == "request"
            and op["event_type"] == "model.generate"
            and op["purpose"] == "business"
        ):
            # A source edit invalidates the entire old summary. Re-materialize
            # both the messages and their mapping before the next hook/freeze.
            selected = materialize(context)
            payload["messages"] = [r["message"] for r in selected]
            payload["record_ids"] = [r["record_id"] for r in selected]
        if patches or patch:
            model_request = (
                stage == "request"
                and op["event_type"] == "model.generate"
                and op["purpose"] == "business"
            )
            tools = payload["tools"] if model_request else self.runner.tools(op["role_key"])
            parameters = (
                payload["parameters"]
                if model_request
                else dict(self.runner.program.model_options.parameters)
            )
            fixed = [r["message"] for r in materialize(context) if r["fixed"]]
            if estimate(fixed, tools, parameters) > self.runner.program.budget.input:
                raise AgentLoomError("HOOK_FIXED_INPUT_TOO_LARGE")
        if patches or "messages" in patch or "arguments" in patch:
            context["revision"] += 1
