"""One Skill metadata contract for prepared packages and directory imports."""

from collections.abc import Mapping

from ..errors import ConfigurationError

SUPPORTED_METADATA = frozenset(
    {
        "name",
        "description",
        "license",
        "compatibility",
        "metadata",
        "argument-hint",
        "user-invocable",
        "disable-model-invocation",
    }
)


def validate_metadata(metadata):
    if not isinstance(metadata, Mapping) or not all(
        isinstance(metadata.get(key), str) and metadata[key].strip()
        for key in ("name", "description")
    ):
        raise ConfigurationError("SKILL_METADATA_INVALID")
    if set(metadata) - SUPPORTED_METADATA:
        raise ConfigurationError("SKILL_PROFILE_UNSUPPORTED")
    for key in ("user-invocable", "disable-model-invocation"):
        if key in metadata and type(metadata[key]) is not bool:
            raise ConfigurationError("SKILL_METADATA_INVALID")
