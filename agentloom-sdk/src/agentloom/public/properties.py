"""One immutable startup parameter entry; all validation lives in configuration."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TypedDict, Unpack

from ..configuration import AgentProperties, RuntimeProperties
from ..configuration.properties import UNSET
from ..configuration.services import ServiceProperties
from ..configuration.settings import (
    CONNECTION_KEYS,
    PARAMETER_KEYS,
    checked_values,
    resolve_configuration,
    resolve_groups,
)
from ..errors import ConfigurationError
from ..workspace.properties import FileProperties


class _PropertiesOverrides(TypedDict, total=False):
    runtime: RuntimeProperties
    defaults: AgentProperties
    services: ServiceProperties
    files: FileProperties


@dataclass(frozen=True, init=False, kw_only=True, eq=False, repr=False)
class PropertiesConfig:
    """Compile one mapping of prefixed startup parameters before opening resources.

    Use PropertiesConfig({"model.protocol": "chat_completions", ...}) or inject a
    ModelAdapter under "model.adapter". Protocol connections require an explicit
    URL, model name and context window. Auth is optional. A local file provider
    requires "workspace.root". All other parameters use the catalog defaults.

    Unknown keys, invalid types and conflicting settings raise ConfigurationError
    with a field_path. No clients, directories or network requests open here.
    Parameters are frozen and paths fixed now; each host/Agent reads this compiled
    configuration. Borrowed adapters/clients remain caller-owned and must retain
    their declared contracts. Credentials and service objects never enter recovery
    manifests. Run input, IDs, variables and leases are execution state.

    The old runtime/defaults/services/files keyword groups remain compatible;
    mixing them with a prefixed mapping is rejected. New code only needs this
    class. Group attributes are immutable internal/compatibility projections.
    """

    defaults: AgentProperties
    services: ServiceProperties
    runtime: RuntimeProperties
    files: FileProperties
    _budget: object = field(init=False, repr=False)
    _values: Mapping = field(init=False, repr=False)

    def __init__(
        self,
        values=UNSET,
        *,
        runtime=UNSET,
        defaults=UNSET,
        services=UNSET,
        files=UNSET,
    ):
        if values is UNSET and all(v is UNSET for v in (runtime, defaults, services, files)):
            values = {}
        if values is not UNSET:
            if any(v is not UNSET for v in (runtime, defaults, services, files)):
                raise ConfigurationError("PARAMETERS_CONFLICT", field_path="properties")
            resolved = resolve_configuration(values)
        else:
            resolved = resolve_groups(
                runtime=RuntimeProperties() if runtime is UNSET else runtime,
                defaults=defaults,
                services=services,
                files=FileProperties() if files is UNSET else files,
            )
        self._assign(resolved)

    def _assign(self, resolved):
        for key in ("runtime", "defaults", "services", "files"):
            object.__setattr__(self, key, getattr(resolved, key))
        object.__setattr__(self, "_budget", resolved.budget)
        object.__setattr__(self, "_values", resolved.values)

    def __getitem__(self, key):
        """Read a resolved parameter; unknown/unavailable keys never silently default."""
        if not isinstance(key, str) or key not in PARAMETER_KEYS:
            raise ConfigurationError("PROPERTY_UNKNOWN", field_path=str(key))
        if key == "model.adapter":
            return self.services.model
        if key not in self._values:
            raise ConfigurationError("PROPERTY_UNAVAILABLE", field_path=key)
        return self._values[key]

    def with_options(
        self,
        values=UNSET,
        **overrides: Unpack[_PropertiesOverrides],
    ) -> PropertiesConfig:
        """Return a centrally validated copy with selected prefixed values replaced.

        Unspecified values are inherited; mappings such as files.options and
        model.parameters are replaced whole. Selecting model.adapter replaces
        protocol connection settings; supplying connection settings replaces an
        injected adapter. No active host or previously built Agent is modified.

        Legacy keyword group replacement is supported independently. Mixed forms
        raise PARAMETERS_CONFLICT. Invalid flat keys raise PROPERTY_UNKNOWN;
        unknown legacy keyword arguments raise TypeError.
        """
        if values is not UNSET:
            if overrides:
                raise ConfigurationError("PARAMETERS_CONFLICT", field_path="properties")
            changes = checked_values(values)
            if not changes:
                return self
            base = dict(self._values)
            if "model.adapter" in changes:
                for key in CONNECTION_KEYS:
                    base.pop(key, None)
            elif CONNECTION_KEYS & changes.keys():
                base.pop("model.adapter", None)
            return PropertiesConfig(base | changes)
        unknown = overrides.keys() - {"runtime", "defaults", "services", "files"}
        if unknown:
            raise TypeError(f"Unknown PropertiesConfig options: {', '.join(sorted(unknown))}")
        if not overrides:
            return self
        groups = {key: getattr(self, key) for key in ("runtime", "defaults", "services", "files")}
        groups.update(overrides)
        connection = None
        if groups["services"] is self.services and "model.adapter" not in self._values:
            connection = {key: self._values[key] for key in CONNECTION_KEYS}
        derived = object.__new__(PropertiesConfig)
        derived._assign(resolve_groups(**groups, connection=connection))
        return derived
