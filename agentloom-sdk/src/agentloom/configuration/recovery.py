"""Compare execution bindings with JSON type semantics and explicit format versions."""

from ..errors import RequestError
from .definitions import ambiguous_legacy_variable
from .json_documents import canonical_document

BINDING_VERSION = 2


def require_same_bindings(saved, current):
    """Reject changed/ambiguous configurations before lease or environment work.

    Legacy manifests remain readable when their variable declarations have an
    unambiguous representation. Missing type evidence is never reconstructed
    from the newly supplied program, since that could authorize a changed run.
    Numeric JSON representations otherwise retain RFC 8785 equivalence; booleans
    are distinct from numbers and variable default types are explicit metadata.
    """
    expected = current
    if (
        isinstance(saved, dict)
        and "binding_version" not in saved
        and isinstance(saved.get("definitions"), dict)
        and all(
            isinstance(d, dict) and isinstance(d.get("variables"), dict)
            for d in saved["definitions"].values()
        )
    ):
        for role, definition in saved["definitions"].items():
            for name, value in definition["variables"].items():
                if ambiguous_legacy_variable(value):
                    raise RequestError(
                        "RECOVERY_BINDING_MISMATCH",
                        "The legacy checkpoint did not preserve this variable declaration type; "
                        "its execution configuration cannot be safely verified",
                        field_path=f"bindings.definitions.{role}.variables.{name}",
                    )
        expected = {
            key: value
            for key, value in current.items()
            if key not in {"binding_version", "variable_contracts"}
        }
    if canonical_document(saved) != canonical_document(expected):
        raise RequestError(
            "RECOVERY_BINDING_MISMATCH",
            "Reconstruct the original execution configuration and resource manifests",
        )
