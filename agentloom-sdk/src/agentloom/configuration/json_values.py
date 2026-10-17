"""Canonical JSON values, immutable copies and stable content identities."""

import hashlib
import json
import math
from typing import Mapping

import rfc8785

from ..errors import ConfigurationError


def json_copy(value):
    def check(v):
        if isinstance(v, int) and abs(v) > 9007199254740991:
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
        if isinstance(v, (list, tuple)):
            for item in v:
                check(item)
            return
        if isinstance(v, Mapping) and all(isinstance(k, str) for k in v):
            for item in v.values():
                check(item)
            return
        raise ConfigurationError("INVALID_JSON", "Only finite JSON values are accepted")

    check(value)

    def plain(item):
        if isinstance(item, Mapping):
            return {key: plain(child) for key, child in item.items()}
        if isinstance(item, (tuple, list)):
            return [plain(child) for child in item]
        return item

    return json.loads(json.dumps(plain(value), ensure_ascii=False, allow_nan=False))


def canonical(value):
    try:
        return rfc8785.dumps(json_copy(value)).decode("utf-8")
    except (rfc8785.CanonicalizationError, UnicodeError) as exc:
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

    return walk(value)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()
