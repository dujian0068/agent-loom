"""Offline JSON Schema assembly and runtime validation, with SDK error boundaries."""

from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.exceptions import NoSuchResource
from referencing.jsonschema import DRAFT202012

from ..errors import ConfigurationError, RequestError
from .json_values import json_copy


def _unavailable(uri):
    # Even an unchecked recovery schema must never trigger network retrieval.
    raise NoSuchResource(ref=uri)


OFFLINE_REGISTRY = Registry(retrieve=_unavailable)


def check_schema(schema):
    """Check schema positions and local reference targets without evaluating an instance.

    Traversal follows the dialect's schema locations, so enum/const/default data
    and property names are never mistaken for reference keywords. References are
    resolved in their resource's $id/anchor scope; recursive references are legal.
    """
    if schema is None:
        return
    try:
        value = json_copy(schema)
        Draft202012Validator.check_schema(value)
        root = Resource.from_contents(value, default_specification=DRAFT202012)
        pending = [(root, OFFLINE_REGISTRY.resolver_with_root(root))]
        seen = set()
        while pending:
            resource, resolver = pending.pop()
            contents = resource.contents
            if isinstance(contents, bool) or id(contents) in seen:
                continue
            seen.add(id(contents))
            for key in ("$ref", "$dynamicRef"):
                if key not in contents:
                    continue
                reference = contents[key]
                if not reference.startswith("#"):
                    raise ConfigurationError(
                        "REMOTE_SCHEMA_REF", "Only local JSON Schema references are supported"
                    )
                resolved = resolver.lookup(reference)
                Draft202012Validator.check_schema(resolved.contents)
                pending.append(
                    (
                        Resource.from_contents(
                            resolved.contents, default_specification=DRAFT202012
                        ),
                        resolved.resolver,
                    )
                )
            pending.extend(
                (child, resolver.in_subresource(child)) for child in resource.subresources()
            )
    except ConfigurationError:
        raise
    except Exception as exc:
        raise ConfigurationError(
            "INVALID_SCHEMA", type(exc).__name__, category="validation"
        ) from exc


def validate_schema(value, schema, *, code="INPUT_SCHEMA_INVALID"):
    """Separate an invalid instance from an unusable schema or reference.

    RequestError permits normal input/output correction. ConfigurationError is a
    schema failure and must end the affected role, never request model correction.
    """
    if schema is None:
        return
    try:
        validator = Draft202012Validator(json_copy(schema), registry=OFFLINE_REGISTRY)
        errors = sorted(validator.iter_errors(value), key=lambda e: str(list(e.path)))
    except Exception as exc:
        raise ConfigurationError(
            "INVALID_SCHEMA", type(exc).__name__, category="validation"
        ) from exc
    if errors:
        error = errors[0]
        raise RequestError(
            code,
            f"Schema validation failed ({error.validator})",
            field_path="/" + "/".join(map(str, error.path)),
            category="validation",
        )
