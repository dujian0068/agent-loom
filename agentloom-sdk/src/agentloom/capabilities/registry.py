from dataclasses import dataclass, field, replace
from types import MappingProxyType

from ..configuration import check_schema, freeze, json_copy
from ..errors import ConfigurationError, RequestError
from .catalog import ResolvedCatalog


@dataclass(frozen=True)
class CapabilitySpec:
    capability_key: str
    name: str | None = None
    description: str = ""
    input_schema: dict = field(default_factory=lambda: {"type": "object"})
    kind: str = "tool"
    output_schema: dict | None = None
    resource_key: str | None = None
    model_name: str | None = None
    handler_ref: str | None = None
    credential_binding_ref: object = None

    def __post_init__(self):
        if self.name is None:
            object.__setattr__(self, "name", self.model_name)
        if any(
            not isinstance(value, str) or not value.strip()
            for value in (self.capability_key, self.name)
        ):
            raise ConfigurationError("CAPABILITY_MEMBERS_INVALID")
        if not isinstance(self.kind, str) or self.kind not in {
            "tool",
            "builtin_tool",
            "hosted",
            "builtin",
            "script",
            "mcp",
        }:
            raise ConfigurationError("CAPABILITY_KIND_INVALID")
        if not isinstance(self.description, str):
            raise ConfigurationError("CAPABILITY_MEMBERS_INVALID", field_path="description")
        for key in ("resource_key", "model_name", "handler_ref"):
            value = getattr(self, key)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ConfigurationError("CAPABILITY_MEMBERS_INVALID", field_path=key)
        object.__setattr__(self, "credential_binding_ref", freeze(self.credential_binding_ref))
        for name in ("input_schema", "output_schema"):
            check_schema(getattr(self, name))
            object.__setattr__(self, name, freeze(getattr(self, name)))


@dataclass(frozen=True)
class CapabilityRegistration:
    adapter_key: str
    adapter: object
    members: tuple[CapabilitySpec, ...]
    config_schema: dict = field(
        default_factory=lambda: {"type": "object", "additionalProperties": False}
    )

    def __post_init__(self):
        object.__setattr__(self, "members", tuple(self.members))
        if not all(isinstance(member, CapabilitySpec) for member in self.members):
            raise ConfigurationError("CAPABILITY_MEMBERS_INVALID")
        check_schema(self.config_schema)
        object.__setattr__(self, "config_schema", freeze(self.config_schema))


@dataclass(frozen=True)
class SkillRegistration:
    adapter_key: str
    package: object
    loader: object


@dataclass(frozen=True)
class ResolvedCapability:
    binding: object
    registration: object
    spec: CapabilitySpec | None

    @property
    def public_spec(self):
        return replace(
            self.spec,
            resource_key=self.spec.resource_key or self.registration.adapter_key,
            model_name=self.binding.visible_name,
            handler_ref=self.registration.adapter_key,
            kind={"tool": "hosted", "builtin_tool": "builtin"}.get(self.spec.kind, self.spec.kind),
        )


class CapabilityRegistry:
    """Immutable registrations; bind_program returns a separate authorized view.

    Agent construction exposes that view as agent.capabilities and its stable key
    as agent.program_key. The originally injected registry is never mutated.
    """

    def __setattr__(self, name, value):
        if name in self.__dict__:
            raise AttributeError("CapabilityRegistry is immutable")
        object.__setattr__(self, name, value)

    def __init__(self, *, tools=(), skills=()):
        entries = {}
        for item in (*tools, *skills):
            if not isinstance(item, (CapabilityRegistration, SkillRegistration)):
                raise ConfigurationError("CAPABILITY_REGISTRATION_INVALID")
            if not isinstance(item.adapter_key, str):
                raise ConfigurationError("CAPABILITY_REGISTRATION_INVALID")
            if not item.adapter_key or item.adapter_key in entries:
                raise ConfigurationError("CAPABILITY_REGISTRATION_DUPLICATE")
            if isinstance(item, CapabilityRegistration):
                if not item.members or len({m.capability_key for m in item.members}) != len(
                    item.members
                ):
                    raise ConfigurationError("CAPABILITY_MEMBERS_INVALID")
                if not any(
                    callable(getattr(item.adapter, name, None))
                    for name in ("invoke_once", "invoke")
                ):
                    raise ConfigurationError("CAPABILITY_ADAPTER_INVALID")
                if any(
                    member.handler_ref not in (None, item.adapter_key) for member in item.members
                ):
                    raise ConfigurationError("CAPABILITY_HANDLER_INVALID")
            elif not isinstance(item, SkillRegistration):
                raise ConfigurationError("CAPABILITY_REGISTRATION_INVALID")
            entries[item.adapter_key] = item
        self._entries = MappingProxyType(entries)
        self._catalogs = MappingProxyType({})

    def bind_program(self, program_key, definitions):
        """Resolve fixed role/node choices locally; return a new immutable registry."""
        if not isinstance(program_key, str) or not program_key:
            raise ConfigurationError("CAPABILITY_PROGRAM_KEY_INVALID")
        bound = object.__new__(type(self))
        bound._entries = self._entries
        bound._catalogs = MappingProxyType(
            {
                (program_key, role): replace(
                    self.resolve_definition(definition), program_key=program_key, binding_key=role
                )
                for role, definition in definitions.items()
            }
        )
        return bound

    def for_role(self, program_key, role_key):
        """Return this program's read-only role catalog, or reject an unknown binding."""
        return self.for_node_binding(program_key, role_key)

    def for_node_binding(self, program_key, binding_key):
        """Direct Agents bind react/planner/Executor keys as their node binding keys."""
        if not isinstance(program_key, str) or not isinstance(binding_key, str):
            raise RequestError("CAPABILITY_CATALOG_NOT_FOUND")
        catalog = self._catalogs.get((program_key, binding_key))
        if catalog is None:
            raise RequestError("CAPABILITY_CATALOG_NOT_FOUND")
        return catalog

    def resolve(self, catalog, model_name):
        """Resolve only a tool in an owned authorized view; Skill routes are separate."""
        if (
            not isinstance(catalog, ResolvedCatalog)
            or self._catalogs.get((catalog.program_key, catalog.binding_key)) is not catalog
        ):
            raise RequestError("CAPABILITY_CATALOG_FORBIDDEN", category="permission")
        if not isinstance(model_name, str) or model_name not in catalog.tools:
            raise RequestError("CAPABILITY_NOT_FOUND", category="permission")
        return catalog.tools[model_name].public_spec

    def resolve_definition(self, definition):
        from ..configuration import validate_schema

        tools, skills, keys, names = {}, {}, set(), set()
        reserved = {
            "delegate_task",
            "skill_list",
            "skill_read",
            "skill_read_resource",
            "result_read",
        }
        for binding in definition.capabilities:
            if (
                binding.binding_key in keys
                or binding.visible_name in names
                or binding.visible_name in reserved
            ):
                raise ConfigurationError("CAPABILITY_BINDING_DUPLICATE")
            keys.add(binding.binding_key)
            names.add(binding.visible_name)
            registration = self._entries.get(binding.adapter_key)
            if registration is None:
                raise ConfigurationError("CAPABILITY_NOT_FOUND")
            if isinstance(registration, SkillRegistration):
                if binding.kind != "skill" or binding.config:
                    raise ConfigurationError("SKILL_BINDING_INVALID")
                if binding.member_key not in (None, registration.package.metadata["name"]):
                    raise ConfigurationError("CAPABILITY_NOT_FOUND")
                if any(
                    e.registration.package.package_key == registration.package.package_key
                    for e in skills.values()
                ):
                    raise ConfigurationError("SKILL_BINDING_AMBIGUOUS")
                skills[binding.visible_name] = ResolvedCapability(binding, registration, None)
            else:
                member = binding.member_key
                if member is None:
                    if len(registration.members) != 1:
                        raise ConfigurationError("CAPABILITY_MEMBER_REQUIRED")
                    member = registration.members[0].capability_key
                spec = next((s for s in registration.members if s.capability_key == member), None)
                allowed_kinds = {
                    "tool": {"tool", "hosted", "script"},
                    "builtin_tool": {"builtin_tool", "builtin"},
                    "mcp": {"mcp"},
                }
                if spec is None or spec.kind not in allowed_kinds.get(binding.kind, set()):
                    raise ConfigurationError("CAPABILITY_BINDING_INVALID")
                try:
                    validate_schema(json_copy(binding.config), registration.config_schema)
                except Exception as exc:
                    raise ConfigurationError("CAPABILITY_CONFIG_INVALID") from exc
                tools[binding.visible_name] = ResolvedCapability(binding, registration, spec)
        return ResolvedCatalog(MappingProxyType(tools), MappingProxyType(skills))
