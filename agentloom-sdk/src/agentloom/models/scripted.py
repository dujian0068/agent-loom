import copy
import inspect

from ..errors import AgentLoomError
from ..types import ModelDescriptor, ModelResponse, ProviderEvent


class ScriptedModel:
    """Deterministic, credential-free adapter for tests and local development."""

    def __init__(self, responder, *, context_window_tokens=1048576, max_output_tokens=4096):
        self.responder = responder
        self.descriptor = ModelDescriptor("scripted", context_window_tokens, max_output_tokens)
        self.requests = []

    async def stream_once(self, request, context):
        self.requests.append(copy.deepcopy(request))
        result = self.responder(request)
        if inspect.isawaitable(result):
            result = await result
        if not isinstance(result, ModelResponse):
            raise AgentLoomError("MODEL_PROTOCOL_ERROR")
        yield ProviderEvent("completed", result.to_dict())
