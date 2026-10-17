"""Bounded canonical JSON values, immutable copies and stable identities.

Depth counts containers, including the root; scalars have depth zero. External
materials have one fixed limit, independent of the Python call stack. Runtime
documents reserve additional depth for SDK envelopes (see json_documents).
"""

import hashlib
import math
from collections.abc import Mapping

import rfc8785

from ..errors import ConfigurationError

MAX_JSON_INTEGER = 2**53 - 1
MAX_JSON_DEPTH = 128


class FrozenDict(dict):
    """JSON-compatible immutable mapping, including its nested values."""

    def _immutable(self, *args, **kwargs):
        raise TypeError("Configuration is immutable")

    __setitem__ = __delitem__ = clear = pop = popitem = setdefault = update = __ior__ = _immutable

    def __deepcopy__(self, memo):
        return self


def _scalar(value):
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeError as exc:
            raise ConfigurationError(
                "INVALID_CANONICAL_JSON", "Invalid Unicode scalar value"
            ) from exc
        return str(value)
    if isinstance(value, int):
        if abs(value) > MAX_JSON_INTEGER:
            raise ConfigurationError(
                "INVALID_CANONICAL_JSON", "Integer exceeds the interoperable RFC 8785 range"
            )
        return int(value)
    if isinstance(value, float) and math.isfinite(value):
        return float(value)
    raise ConfigurationError("INVALID_JSON", "Only finite JSON values are accepted")


def _copy(value, *, frozen, max_depth):
    """Walk with an explicit stack; shared children are values, cycles are errors."""
    root, ancestors = [None], set()
    # A finish item freezes a container only after all its children were copied.
    pending = [(False, value, root, 0, 0)]
    while pending:
        finish, item, parent, key, depth = pending.pop()
        if finish:
            ancestors.remove(item)
            if frozen:
                result = parent[key]
                parent[key] = FrozenDict(result) if isinstance(result, dict) else tuple(result)
            continue
        if not isinstance(item, (list, tuple, Mapping)):
            parent[key] = _scalar(item)
            continue
        if id(item) in ancestors:
            raise ConfigurationError("INVALID_JSON", "Circular containers are not JSON values")
        if depth >= max_depth:
            raise ConfigurationError(
                "INVALID_JSON", f"JSON nesting exceeds the supported depth of {max_depth}"
            )
        ancestors.add(id(item))
        pending.append((True, id(item), parent, key, depth))
        if isinstance(item, Mapping):
            children = list(item.items())
            for child_key, _ in children:
                if not isinstance(child_key, str):
                    raise ConfigurationError("INVALID_JSON", "JSON object keys must be strings")
                _scalar(child_key)
            target = {}
        else:
            children = list(enumerate(item))
            target = [None] * len(children)
        parent[key] = target
        for child_key, child in reversed(children):
            pending.append((False, child, target, child_key, depth + 1))
    return root[0]


def json_copy(value, *, max_depth=MAX_JSON_DEPTH):
    return _copy(value, frozen=False, max_depth=max_depth)


def freeze(value, *, max_depth=MAX_JSON_DEPTH):
    return _copy(value, frozen=True, max_depth=max_depth)


def canonical(value, *, max_depth=MAX_JSON_DEPTH):
    """RFC 8785 ordering with iterative containers and the library's scalar codec."""
    value = json_copy(value, max_depth=max_depth)
    chunks, pending = [], [(False, value)]
    try:
        while pending:
            literal, item = pending.pop()
            if literal:
                chunks.append(item)
            elif isinstance(item, (dict, list)):
                mapping = isinstance(item, dict)
                chunks.append(b"{" if mapping else b"[")
                pending.append((True, b"}" if mapping else b"]"))
                entries = (
                    [(key, item[key]) for key in sorted(item, key=lambda k: k.encode("utf-16be"))]
                    if mapping
                    else list(enumerate(item))
                )
                for index in range(len(entries) - 1, -1, -1):
                    key, child = entries[index]
                    pending.append((False, child))
                    if mapping:
                        pending.append((True, b":"))
                        pending.append((True, rfc8785.dumps(key)))
                    if index:
                        pending.append((True, b","))
            else:
                chunks.append(rfc8785.dumps(item))
        return b"".join(chunks).decode("utf-8")
    except (rfc8785.CanonicalizationError, UnicodeError) as exc:
        raise ConfigurationError(
            "INVALID_CANONICAL_JSON", "Values must fit the RFC 8785 JSON profile"
        ) from exc


def digest(value, *, max_depth=MAX_JSON_DEPTH):
    return hashlib.sha256(canonical(value, max_depth=max_depth).encode()).hexdigest()
