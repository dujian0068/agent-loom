"""Translate normalized context without losing stateless Responses continuation items."""

from ..configuration import json_copy
from ..errors import AgentLoomError


def response_inputs(messages):
    result, call_ids = [], {}
    for message in messages:
        if message["role"] == "tool":
            result.append(
                {
                    "type": "function_call_output",
                    "call_id": call_ids.get(message["tool_call_id"], message["tool_call_id"]),
                    "output": message["content"],
                }
            )
            continue
        state = message.get("provider_state", {})
        if message["role"] != "assistant" or state.get("protocol") != "openai.responses":
            if message.get("content"):
                result.append({"role": message["role"], "content": message["content"]})
            result.extend(
                {
                    "type": "function_call",
                    "call_id": call["id"],
                    "name": call["function"]["name"],
                    "arguments": call["function"]["arguments"],
                }
                for call in message.get("tool_calls", [])
            )
            continue
        output = json_copy(state["output"])
        calls = iter(message.get("tool_calls", []))
        original_text = "".join(
            content["text"] if content["type"] == "output_text" else content["refusal"]
            for item in output
            if item["type"] == "message"
            for content in item.get("content", [])
            if content["type"] in {"output_text", "refusal"}
        )
        changed_text = message.get("content", "") != original_text
        text_target = next((item for item in reversed(output) if item["type"] == "message"), None)
        for item in output:
            if item["type"] == "function_call":
                call = next(calls, None)
                if call is None:
                    raise AgentLoomError("MODEL_CONTINUATION_INVALID")
                call_ids[call["id"]] = item["call_id"]
                item.update(name=call["function"]["name"], arguments=call["function"]["arguments"])
            elif item["type"] == "message" and changed_text:
                item["content"] = [
                    {
                        "type": "output_text",
                        "text": message["content"] if item is text_target else "",
                        "annotations": [],
                    }
                ]
            elif item["type"] not in {"reasoning", "message"}:
                # Server-side tools have not been granted by the local capability catalog.
                raise AgentLoomError("MODEL_OUTPUT_TYPE_UNSUPPORTED")
            result.append(item)
        if next(calls, None) is not None:
            raise AgentLoomError("MODEL_CONTINUATION_INVALID")
        if changed_text and text_target is None and message.get("content"):
            result.append({"role": "assistant", "content": message["content"]})
    return result
