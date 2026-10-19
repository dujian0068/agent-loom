"""Responses wire and continuation shapes, independent of HTTP/resource failures."""

from ..errors import AgentLoomError
from .material import protocol_error


def _require(condition):
    if not condition:
        raise protocol_error("Invalid Responses event or continuation")


def _string(value, *, empty=False):
    return isinstance(value, str) and (empty or bool(value))


def validate_output(output):
    _require(isinstance(output, list))
    for item in output:
        _require(isinstance(item, dict) and _string(item.get("type")))
        if item["type"] == "function_call":
            _require(all(_string(item.get(k)) for k in ("name", "call_id")))
            _require(_string(item.get("arguments"), empty=True))
        elif item["type"] == "message":
            _require(isinstance(item.get("content"), list))
            for block in item["content"]:
                _require(isinstance(block, dict) and _string(block.get("type")))
                field = {"output_text": "text", "refusal": "refusal"}.get(block["type"])
                if field is None:
                    raise AgentLoomError("MODEL_OUTPUT_TYPE_UNSUPPORTED", category="protocol")
                _require(_string(block.get(field), empty=True))
        elif item["type"] != "reasoning":
            raise AgentLoomError("MODEL_OUTPUT_TYPE_UNSUPPORTED", category="protocol")


def validate_frame(frame):
    kind = frame.get("type")
    _require(_string(kind))
    if kind in {"response.created", "response.completed"}:
        response = frame.get("response")
        _require(isinstance(response, dict))
        if kind == "response.created":
            _require(_string(response.get("id")))
        else:
            validate_output(response.get("output"))
    elif kind == "response.output_item.added":
        item = frame.get("item")
        _require(isinstance(item, dict) and _string(item.get("type")))
        if item["type"] == "function_call":
            _require(type(frame.get("output_index")) is int and frame["output_index"] >= 0)
            _require(all(_string(item.get(k)) for k in ("name", "call_id")))
    elif kind in {
        "response.output_text.delta",
        "response.reasoning_summary_text.delta",
        "response.function_call_arguments.delta",
    }:
        _require(_string(frame.get("delta"), empty=True))
        if kind == "response.function_call_arguments.delta":
            _require(type(frame.get("output_index")) is int and frame["output_index"] >= 0)
    elif kind in {"response.failed", "response.incomplete"}:
        _require(isinstance(frame.get("response"), dict))
