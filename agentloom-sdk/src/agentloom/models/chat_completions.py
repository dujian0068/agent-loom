"""OpenAI-compatible Chat Completions protocol, independent of model vendor."""

import httpx

from ..configuration import json_copy
from ..errors import AgentLoomError
from ..types import ModelResponse, ProviderEvent, ToolCall
from .http import frame_json, sse_data, tool_arguments
from .material import merge_usage, normalize_usage
from .provider_errors import provider_error
from .transport import HTTPModelAdapter


class ChatCompletionsAdapter(HTTPModelAdapter):
    """Chat Completions profile: finish frame AND [DONE] are required."""

    parameters = HTTPModelAdapter.parameters | {"reasoning_effort"}

    async def stream_once(self, request, context):
        body = {
            "model": self.descriptor.provider_model_id,
            "messages": [
                {key: value for key, value in message.items() if key != "provider_state"}
                for message in request.messages
            ],
            "stream": True,
            "stream_options": {"include_usage": True},
            "max_completion_tokens": request.max_output_tokens,
            **json_copy(request.parameters),
        }
        if request.tools:
            body["tools"] = [{"type": "function", "function": t} for t in request.tools]
        text, calls, usage, finish, identity = "", {}, None, None, None
        try:
            async with self._stream("/chat/completions", body, context) as response:
                async for raw in sse_data(response):
                    if raw == "[DONE]":
                        if finish is None:
                            raise AgentLoomError(
                                "MODEL_PROTOCOL_ERROR",
                                "Completion terminator preceded finish frame",
                            )
                        if finish not in {"stop", "tool_calls"}:
                            raise AgentLoomError(
                                "MODEL_RESPONSE_INCOMPLETE",
                                "Completion ended without a complete answer",
                            )
                        assembled = []
                        for index in sorted(calls):
                            call = calls[index]
                            if not call["name"] or not call["id"]:
                                raise AgentLoomError(
                                    "MODEL_PROTOCOL_ERROR", "Incomplete function identity"
                                )
                            args = tool_arguments(call["arguments"])
                            assembled.append(ToolCall(call["name"], args, call["id"]))
                        yield ProviderEvent(
                            "completed",
                            ModelResponse(text, tuple(assembled), usage, finish).to_dict(),
                        )
                        return
                    frame = frame_json(raw)
                    if "error" in frame:
                        raise provider_error(frame, AgentLoomError("MODEL_PROVIDER_ERROR"))
                    if frame.get("id"):
                        if identity is not None and identity != frame["id"]:
                            raise AgentLoomError("MODEL_PROTOCOL_ERROR", "Stream identity changed")
                        identity = frame["id"]
                    if "usage" in frame:
                        statistics = normalize_usage(
                            frame["usage"], "prompt_tokens", "completion_tokens"
                        )
                        usage = merge_usage(usage, statistics)
                    for choice in frame.get("choices", []):
                        if choice.get("index", 0) != 0:
                            raise AgentLoomError(
                                "MODEL_PROTOCOL_ERROR", "Only one completion is supported"
                            )
                        delta = choice.get("delta", {})
                        if finish is not None and any(
                            delta.get(k)
                            for k in ("content", "tool_calls", "reasoning_content", "refusal")
                        ):
                            raise AgentLoomError("MODEL_PROTOCOL_ERROR", "Content after completion")
                        if delta.get("content"):
                            text += delta["content"]
                            yield ProviderEvent("text_delta", delta["content"])
                        if delta.get("refusal"):
                            text += delta["refusal"]
                            yield ProviderEvent("text_delta", delta["refusal"])
                        if delta.get("reasoning_content"):
                            yield ProviderEvent("reasoning_delta", delta["reasoning_content"])
                        for item in delta.get("tool_calls", []):
                            index = item.get("index")
                            if type(index) is not int or index < 0:
                                raise AgentLoomError("MODEL_PROTOCOL_ERROR")
                            call = calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
                            if item.get("id"):
                                if call["id"] and call["id"] != item["id"]:
                                    raise AgentLoomError("MODEL_PROTOCOL_ERROR")
                                call["id"] = item["id"]
                            fn = item.get("function", {})
                            for key, kind in (
                                ("name", "tool_name_delta"),
                                ("arguments", "tool_arguments_delta"),
                            ):
                                if fn.get(key):
                                    call[key] += fn[key]
                                    yield ProviderEvent(kind, fn[key], index, call["id"] or None)
                        if choice.get("finish_reason") is not None:
                            finish = choice["finish_reason"]
        except httpx.TransportError as exc:
            raise AgentLoomError(
                "MODEL_CONNECTION_FAILED",
                category="transport",
                phase="connect",
                retryable_transport=True,
                external_outcome="unknown",
            ) from exc
        raise AgentLoomError(
            "MODEL_STREAM_INTERRUPTED",
            category="transport",
            retryable_transport=True,
            external_outcome="unknown",
        )
