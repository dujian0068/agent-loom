import math
from dataclasses import dataclass

from ..configuration import canonical
from ..errors import ConfigurationError


@dataclass(frozen=True)
class ContextBudget:
    window: int
    output: int
    safety: int
    input: int
    trigger: int

    @classmethod
    def resolve(cls, descriptor, policy, options):
        window = descriptor.context_window_tokens
        if type(window) is not int or window <= 0:
            raise ConfigurationError("MODEL_CONTEXT_WINDOW_REQUIRED")
        output = policy.output_reserve_tokens or min(
            descriptor.max_output_tokens or 4096, window // 4
        )
        safety = (
            policy.safety_margin_tokens
            if policy.safety_margin_tokens is not None
            else max(256, math.ceil(window * 0.01))
        )
        available = window - output - safety
        if (
            available <= 0
            or output <= 0
            or options.max_output_tokens
            and options.max_output_tokens > output
        ):
            raise ConfigurationError("CONTEXT_BUDGET_INVALID")
        if (
            descriptor.max_output_tokens
            and options.max_output_tokens
            and options.max_output_tokens > descriptor.max_output_tokens
        ):
            raise ConfigurationError("MODEL_OUTPUT_LIMIT_EXCEEDED")
        return cls(window, output, safety, available, math.floor(policy.trigger_ratio * available))


def estimate(messages, tools=(), parameters=None):
    """utf8-envelope-v1; conservative input estimate, never provider token usage."""
    return (
        len(
            canonical(
                {"messages": messages, "tools": tools, "parameters": parameters or {}}
            ).encode()
        )
        + 256
        + 32 * len(messages)
        + 64 * len(tools)
    )
