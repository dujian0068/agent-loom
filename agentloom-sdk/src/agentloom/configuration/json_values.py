"""Canonical JSON values, immutable copies and stable content identities."""

import hashlib
import json
import math
from typing import Mapping

import rfc8785

from ..errors import ConfigurationError

MAX_JSON_INTEGER = 2**53 - 1


def json_copy(value):
    ancestors = set()

    def check(v):
        if isinstance(v, int) and abs(v) > MAX_JSON_INTEGER:
            raise ConfigurationError(
                "INVALID_CANONICAL_JSON", "Integer exceeds the interoperable RFC 8785 range"
            )
        if isinstance(v, str):
            try:
                v.encode("utf-8")
            except UnicodeError as exc:
                raise ConfigurationError(
                    "INVALID_CANONICAL_JSON", "Invalid Unicode scalar value"
                ) from exc
        if v is None or isinstance(v, (str, bool, int)):
            return
        if isinstance(v, float) and math.isfinite(v):
            return
        if isinstance(v, (list, tuple, Mapping)):
            identity = id(v)
            if identity in ancestors:
                raise ConfigurationError("INVALID_JSON", "Circular containers are not JSON values")
            ancestors.add(identity)
            try:
                if isinstance(v, Mapping):
                    for key, item in v.items():
                        if not isinstance(key, str):
                            raise ConfigurationError(
                                "INVALID_JSON", "JSON object keys must be strings"
                            )
                        check(key)
                        check(item)
                else:
                    for item in v:
                        check(item)
            finally:
                # Shared children in separate branches are valid JSON values.
                ancestors.remove(identity)
            return
        raise ConfigurationError("INVALID_JSON", "Only finite JSON values are accepted")

    def plain(item):
        if isinstance(item, Mapping):
            return {key: plain(child) for key, child in item.items()}
        if isinstance(item, (tuple, list)):
            return [plain(child) for child in item]
        return item

    try:
        check(value)
        return json.loads(json.dumps(plain(value), ensure_ascii=False, allow_nan=False))
    except RecursionError as exc:
        raise ConfigurationError(
            "INVALID_JSON", "JSON nesting exceeds the supported depth"
        ) from exc


def canonical(value):
    try:
        return rfc8785.dumps(json_copy(value)).decode("utf-8")
    except (rfc8785.CanonicalizationError, UnicodeError, RecursionError) as exc:
        raise ConfigurationError(
            "INVALID_CANONICAL_JSON", "Values must fit the RFC 8785 JSON profile"
        ) from exc


class FrozenDict(dict):
    """JSON-compatible immutable mapping, including its nested values."""

    def _immutable(self, *args, **kwargs):
        raise TypeError("Configuration is immutable")

    __setitem__ = __delitem__ = clear = pop = popitem = setdefault = update = __ior__ = _immutable

    def __deepcopy__(self, memo):
        return self


def freeze(value):
    value = json_copy(value)

    def walk(item):
        if isinstance(item, dict):
            return FrozenDict({key: walk(child) for key, child in item.items()})
        if isinstance(item, list):
            return tuple(walk(child) for child in item)
        return item

    try:
        return walk(value)
    except RecursionError as exc:
        raise ConfigurationError(
            "INVALID_JSON", "JSON nesting exceeds the supported depth"
        ) from exc


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()
