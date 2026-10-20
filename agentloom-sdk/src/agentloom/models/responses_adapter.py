"""Responses protocol adapter for any compatible endpoint."""

import httpx

from ..configuration import json_copy
from ..errors import AgentLoomError
from ..types import ModelResponse, ProviderEvent, ToolCall
from .http import frame_json, sse_data, tool_arguments
from .material import normalize_usage
from .provider_errors import provider_error
from .response_protocol import validate_frame
from .responses import response_inputs
from .transport import HTTPModelAdapter


class ResponsesAdapter(HTTPModelAdapter):
    parameters = HTTPModelAdapter.parameters | {"reasoning", "text"}

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
