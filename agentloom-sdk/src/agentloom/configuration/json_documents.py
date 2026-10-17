"""Internal JSON documents assembled from already validated boundary materials.

The larger document budget accommodates SDK state/audit/context wrappers and
previously persisted results. It is not an adapter input/output allowance. All
copies and canonical encodings are iterative; both stores use this same profile.
"""

from .json_values import canonical, digest, freeze, json_copy

MAX_DOCUMENT_DEPTH = 512


def copy_document(value):
    return json_copy(value, max_depth=MAX_DOCUMENT_DEPTH)


def freeze_document(value):
    return freeze(value, max_depth=MAX_DOCUMENT_DEPTH)


def canonical_document(value):
    return canonical(value, max_depth=MAX_DOCUMENT_DEPTH)


def document_digest(value):
    return digest(value, max_depth=MAX_DOCUMENT_DEPTH)
