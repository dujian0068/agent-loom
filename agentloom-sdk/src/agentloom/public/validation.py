"""Validation shared by public handles, command facades and run admission."""

from uuid import UUID

from ..errors import RequestError


def identity(value, field):
    """Accept UUID values or explicit nonblank string keys, without lossy coercion."""
    if isinstance(value, UUID):
        return str(value)
    if not isinstance(value, str) or not value.strip():
        raise RequestError("IDENTITY_INVALID", field_path=field, category="validation")
    return value


def pagination(after, limit=100):
    if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 256:
        raise RequestError("INVALID_CURSOR", category="validation")
