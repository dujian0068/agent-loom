"""Shared HTTP transport for explicitly selected model protocols."""

from contextlib import asynccontextmanager

import httpx

from ..configuration.model_validation import validate_connection
from ..errors import ConfigurationError
from ..resources import managed_resource


class HTTPModelAdapter:
    parameters = frozenset({"temperature", "top_p"})

    def __init__(
        self,
        *,
        model,
        api_key=None,
        context_window_tokens,
        base_url,
        max_output_tokens=None,
        client=None,
    ):
        self.descriptor, self.base_url = validate_connection(
            model=model,
            api_key=api_key,
            context_window_tokens=context_window_tokens,
            base_url=base_url,
            max_output_tokens=max_output_tokens,
            client=client,
        )
        self._api_key, self._client = api_key, client

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
        headers = {"Accept": "text/event-stream"}
        if self._api_key is not None:
            headers["Authorization"] = "Bearer " + self._api_key
        return headers

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
