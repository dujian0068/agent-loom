"""Immutable, serializable file-provider selection, independent of live clients."""

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from ..errors import ConfigurationError
from .json_values import freeze, json_copy


@dataclass(frozen=True, kw_only=True)
class FileProperties:
    """Select an installed provider; SDK constructs its class with **options.

    provider defaults to local. options are copied/frozen JSON constructor
    parameters, omitted from repr and checkpoints (only a digest is persisted).
    No address is required by SDK; a provider may own its address/configuration.
    Construction neither imports plugins nor opens clients.
    """

    provider: str = "local"
    options: Mapping = field(default_factory=dict, repr=False)

    def __post_init__(self):
        if not isinstance(self.provider, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.-]*", self.provider
        ):
            raise ConfigurationError("FILE_PROVIDER_INVALID", field_path="files.provider")
        if not isinstance(self.options, Mapping) or any(
            not isinstance(key, str) or not key.isidentifier() for key in self.options
        ):
            raise ConfigurationError("FILE_OPTIONS_INVALID", field_path="files.options")
        object.__setattr__(self, "options", freeze(self.options))
        if self.provider == "local" and self.options:
            raise ConfigurationError(
                "FILE_OPTIONS_INVALID",
                "Configure the local root using workspace.root",
                field_path="files.options",
            )

    @property
    def options_hash(self):
        """Fingerprint constructor values, retaining integer versus float types."""
        encoded = json.dumps(
            json_copy(self.options),
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
