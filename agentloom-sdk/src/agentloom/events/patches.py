"""Pure Hook patch validation and application, independent of user callbacks."""

from ..configuration import validate_schema
from ..configuration.actions import validate_action_contract
from ..configuration.json_documents import copy_document
from ..configuration.json_documents import document_digest as digest
from ..context.budget import estimate
from ..context.records import materialize, revise_call_arguments
from ..context.results import PREVIEW_BYTES
from ..errors import AgentLoomError, InfrastructureError
from .patch_contract import legacy_patch_contract


def apply_saved_hook(state, op_id, key):
    current = state["operations"][op_id]
    hook = current["hooks"][key]
    if hook["status"] != "result_saved":
        return
    stage = key.split(":", 1)[0]
    candidate = copy_document(state["tasks"][current["task_id"]]["context"])
    payload = copy_document(current[stage])
    try:
        contract = hook.get("contract") or legacy_patch_contract(state, current)
        PatchValidator(contract).patch(current, stage, hook, candidate, payload)
        state["tasks"][current["task_id"]]["context"] = candidate
        current[stage] = payload
        hook["patch_status"] = "applied"
    except InfrastructureError:
        raise
    except Exception as exc:
        hook["patch_status"] = "rejected"
        hook["diagnostic"] = (
            exc.to_dict()
            if isinstance(exc, AgentLoomError)
            else AgentLoomError("HOOK_PATCH_INVALID", type(exc).__name__).to_dict()
        )
    hook["status"] = "applied"


class PatchValidator:
    def __init__(self, contract):
        self.contract = contract

    def patch(self, op, stage, hook, context, payload):
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
            or self.contract["model_output_limit"] is not None
            and patch["max_output_tokens"] > self.contract["model_output_limit"]
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
            check = hook.get("parameter_validation")
            if not check or check.get("hash") != digest(parameters):
                raise AgentLoomError("HOOK_PARAMETER_VALIDATION_UNAVAILABLE")
            if check.get("error"):
                raise AgentLoomError(**check["error"])
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
                validate_action_contract(self.contract["actions"], payload["name"], arguments)
            if route == "executor.delegate":
                validate_action_contract(self.contract["actions"], payload["name"], arguments)
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
                for call, old in zip(calls, old_calls):
                    if call["name"] != old["name"] or call["arguments"] != old["arguments"]:
                        validate_action_contract(
                            self.contract["actions"], call["name"], call["arguments"]
                        )
            if op["event_type"] == "capability.invoke":
                validate_schema(
                    result,
                    self.contract["outputs"][op["request"]["name"]],
                    code="HOOK_RESULT_SCHEMA_INVALID",
                )
            if op["event_type"] == "executor.delegate":
                definition = self.contract["actions"]["executors"][
                    op["request"]["arguments"]["executor"]
                ]["definition"]
                validate_schema(
                    result, definition["output_schema"], code="HOOK_RESULT_SCHEMA_INVALID"
                )
                if definition["output_schema"] is None and (
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
                if (
                    op["event_type"] == "result.read"
                    and len(result["text"].encode()) > PREVIEW_BYTES
                ):
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
            tools = payload["tools"] if model_request else self.contract["actions"]["tools"]
            parameters = payload["parameters"] if model_request else self.contract["parameters"]
            fixed = [r["message"] for r in materialize(context) if r["fixed"]]
            if estimate(fixed, tools, parameters) > self.contract["input_budget"]:
                raise AgentLoomError("HOOK_FIXED_INPUT_TOO_LARGE")
        if patches or "messages" in patch or "arguments" in patch:
            context["revision"] += 1
