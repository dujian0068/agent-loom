"""Bounded token accounting, committed atomically with each Attempt result."""

from ..configuration.json_values import MAX_JSON_INTEGER
from ..errors import AgentLoomError

TOKEN_KEYS = ("input_tokens", "output_tokens")


def account_model_usage(state, raw):
    """Return the settled material and whether its counters entered the totals.

    Overflow retains the complete provider material and existing partial totals,
    but fails this response before it can become a model decision. No saturated
    or noncanonical value is ever written to the store.
    """
    result = raw.get("result")
    usage = (result.get("usage") or {}) if isinstance(result, dict) else {}
    totals = state["usage"]
    counts = {key: usage[key] for key in TOKEN_KEYS if usage.get(key) is not None}
    additions = {key: (totals[key] or 0) + value for key, value in counts.items()}
    accounted = all(value <= MAX_JSON_INTEGER for value in additions.values())
    if accounted:
        totals.update(additions)
    else:
        raw = {
            **raw,
            "status": "failed",
            "error": AgentLoomError(
                "MODEL_USAGE_OVERFLOW",
                "Aggregate token counts exceed the canonical integer range",
                category="protocol",
                phase="receive",
            ).to_dict(),
        }
    known = accounted and len(counts) == len(TOKEN_KEYS)
    state["token_usage_known_attempts"] = state.get("token_usage_known_attempts", 0) + int(known)
    attempts = totals["business_model_attempts"] + totals["compaction_model_attempts"]
    totals["token_counts_complete"] = state["token_usage_known_attempts"] == attempts
    return raw, accounted
