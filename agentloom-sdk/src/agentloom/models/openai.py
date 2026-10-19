from __future__ import annotations

from contextlib import asynccontextmanager

import httpx

from ..configuration import json_copy, number
from ..errors import AgentLoomError, ConfigurationError
from ..resources import managed_resource
from ..types import ModelDescriptor, ModelResponse, ProviderEvent, ToolCall
from .http import frame_json, sse_data, tool_arguments
from .material import merge_usage, normalize_usage
from .provider_errors import provider_error
from .response_protocol import validate_frame
from .responses import response_inputs


class _OpenAIAdapter:
    parameters = frozenset({"temperature", "top_p"})

    def __init__(
        self,
        *,
        model,
        api_key,
        context_window_tokens,
        base_url="https://api.openai.com/v1",
        max_output_tokens=None,
        client=None,
    ):
        number(context_window_tokens, "context_window_tokens", 1, 10**9, integer=True)
        if not isinstance(model, str) or not model or not isinstance(api_key, str) or not api_key:
            raise ConfigurationError("MODEL_CONFIGURATION_INVALID")
        if not base_url.startswith(("https://", "http://")):
            raise ConfigurationError("MODEL_CONFIGURATION_INVALID")
        self.descriptor = ModelDescriptor(model, context_window_tokens, max_output_tokens)
        self._api_key, self.base_url, self._client = api_key, base_url.rstrip("/"), client

    def validate_options(self, options):
        if set(options.parameters) - self.parameters:
            raise ConfigurationError("MODEL_FEATURE_UNSUPPORTED")

    @asynccontextmanager
    async def _connection(self):
        if self._client is not None:
            yield self._client
        else:
            async with httpx.AsyncClient(timeout=None, follow_redirects=False) as client:
                yield client

    def _headers(self):
        return {"Authorization": "Bearer " + self._api_key, "Accept": "text/event-stream"}

    @asynccontextmanager
    async def _stream(self, path, body, context):
        diagnostics = getattr(context, "diagnostics", None)
        report = diagnostics.append if diagnostics is not None else None
        async with managed_resource(self._connection(), report) as client:
            async with managed_resource(
                client.stream(
                    "POST",
                    self.base_url + path,
                    json=body,
                    headers=self._headers(),
                    timeout=None,
                ),
                report,
            ) as response:
                yield response


class OpenAIChatAdapter(_OpenAIAdapter):
    """Chat Completions profile: finish frame AND [DONE] are required."""

    parameters = _OpenAIAdapter.parameters | {"reasoning_effort"}

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


class OpenAIResponsesAdapter(_OpenAIAdapter):
    parameters = _OpenAIAdapter.parameters | {"reasoning", "text"}

    async def stream_once(self, request, context):
        body = {
            "model": self.descriptor.provider_model_id,
            "input": response_inputs(request.messages),
            "stream": True,
            "store": False,
            "include": ["reasoning.encrypted_content"],
            "max_output_tokens": request.max_output_tokens,
            "tools": [{"type": "function", **t} for t in request.tools],
            **json_copy(request.parameters),
        }
        identity, call_ids = None, {}
        try:
            async with self._stream("/responses", body, context) as response:
                async for raw in sse_data(response):
                    frame = frame_json(raw)
                    validate_frame(frame)
                    kind = frame.get("type")
                    if kind == "response.created":
                        identity = frame["response"]["id"]
                    elif kind in {"response.failed", "response.incomplete", "error"}:
                        raise provider_error(
                            frame.get("response", frame),
                            AgentLoomError(
                                "MODEL_RESPONSE_INCOMPLETE"
                                if kind == "response.incomplete"
                                else "MODEL_PROVIDER_ERROR"
                            ),
                        )
                    elif kind == "response.output_text.delta":
                        yield ProviderEvent("text_delta", frame.get("delta", ""))
                    elif kind == "response.function_call_arguments.delta":
                        yield ProviderEvent(
                            "tool_arguments_delta",
                            frame.get("delta", ""),
                            frame.get("output_index"),
                            call_ids.get(frame.get("output_index")),
                        )
                    elif kind == "response.reasoning_summary_text.delta":
                        yield ProviderEvent("reasoning_delta", frame.get("delta", ""))
                    elif (
                        kind == "response.output_item.added"
                        and frame.get("item", {}).get("type") == "function_call"
                    ):
                        index = frame.get("output_index")
                        call_ids[index] = frame["item"].get("call_id")
                        yield ProviderEvent(
                            "tool_name_delta",
                            frame["item"].get("name", ""),
                            index,
                            call_ids[index],
                        )
                    elif kind == "response.completed":
                        result = frame["response"]
                        if (
                            result.get("status") != "completed"
                            or identity is not None
                            and identity != result.get("id")
                        ):
                            raise AgentLoomError("MODEL_PROTOCOL_ERROR")
                        texts, calls = [], []
                        for item in result.get("output", []):
                            if item["type"] == "message":
                                texts.extend(
                                    c["text"] if c["type"] == "output_text" else c["refusal"]
                                    for c in item["content"]
                                    if c["type"] in {"output_text", "refusal"}
                                )
                            elif item["type"] == "function_call":
                                arguments = tool_arguments(item["arguments"])
                                calls.append(ToolCall(item["name"], arguments, item["call_id"]))
                            elif item["type"] != "reasoning":
                                raise AgentLoomError("MODEL_OUTPUT_TYPE_UNSUPPORTED")
                        usage = normalize_usage(result.get("usage"))
                        yield ProviderEvent(
                            "completed",
                            ModelResponse(
                                "".join(texts),
                                tuple(calls),
                                usage,
                                "stop",
                                {
                                    "protocol": "openai.responses",
                                    "output": result.get("output", []),
                                },
                            ).to_dict(),
                        )
                        return
        except httpx.TransportError as exc:
            raise AgentLoomError(
                "MODEL_CONNECTION_FAILED",
                category="transport",
                retryable_transport=True,
                external_outcome="unknown",
            ) from exc
        raise AgentLoomError(
            "MODEL_STREAM_INTERRUPTED",
            category="transport",
            retryable_transport=True,
            external_outcome="unknown",
        )
