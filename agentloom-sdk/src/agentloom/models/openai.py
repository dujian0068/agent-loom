"""Legacy names retaining their historical default endpoint.

New applications select ChatCompletionsAdapter or ResponsesAdapter and provide
an endpoint explicitly, or configure model.protocol through PropertiesConfig.
"""

from .chat_completions import ChatCompletionsAdapter
from .responses_adapter import ResponsesAdapter


class OpenAIChatAdapter(ChatCompletionsAdapter):
    """Compatibility constructor; prefer ChatCompletionsAdapter."""

    def __init__(self, *, base_url="https://api.openai.com/v1", **options):
        super().__init__(base_url=base_url, **options)


class OpenAIResponsesAdapter(ResponsesAdapter):
    """Compatibility constructor; prefer ResponsesAdapter."""

    def __init__(self, *, base_url="https://api.openai.com/v1", **options):
        super().__init__(base_url=base_url, **options)
