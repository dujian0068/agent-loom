from dataclasses import dataclass, field
from types import MappingProxyType

from ..configuration import check_schema, freeze, json_copy
from ..errors import ConfigurationError


@dataclass(frozen=True)
class CapabilitySpec:
    capability_key: str
    name: str
    description: str
    input_schema: dict = field(default_factory=lambda: {"type": "object"})
    kind: str = "tool"
    output_schema: dict | None = None

    def __post_init__(self):
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


@dataclass(frozen=True)
class ResolvedCatalog:
    tools: object
    skills: object

    def model_tools(self):
        result = [
            {
                "name": name,
                "description": entry.spec.description,
                "parameters": json_copy(entry.spec.input_schema),
            }
            for name, entry in self.tools.items()
        ]
        if self.skills:
            entries = [
                {"name": name, "description": entry.registration.package.metadata["description"]}
                for name, entry in self.skills.items()
                if not entry.registration.package.metadata.get("disable-model-invocation", False)
            ]
            if entries:
                description = "Available skills: " + str(entries)
                result += [
                    {
                        "name": "skill_list",
                        "description": description,
                        "parameters": {"type": "object", "additionalProperties": False},
                    },
                    {
                        "name": "skill_read",
                        "description": "Read an authorized SKILL.md. " + description,
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "raw_arguments": {"type": ["string", "null"]},
                                "arguments": {
                                    "type": ["array", "null"],
                                    "items": {"type": "string"},
                                },
                            },
                            "required": ["name"],
                            "additionalProperties": False,
                        },
                    },
                    {
                        "name": "skill_read_resource",
                        "description": "Read a file in an authorized Skill package",
                        "parameters": {
                            "type": "object",
                            "properties": {"name": {"type": "string"}, "path": {"type": "string"}},
                            "required": ["name", "path"],
                            "additionalProperties": False,
                        },
                    },
                ]
        return result


class CapabilityRegistry:
    def __init__(self, *, tools=(), skills=()):
        entries = {}
        for item in (*tools, *skills):
            if not item.adapter_key or item.adapter_key in entries:
                raise ConfigurationError("CAPABILITY_REGISTRATION_DUPLICATE")
            if isinstance(item, CapabilityRegistration):
                if not item.members or len({m.capability_key for m in item.members}) != len(
                    item.members
                ):
                    raise ConfigurationError("CAPABILITY_MEMBERS_INVALID")
                if not callable(getattr(item.adapter, "invoke", None)):
                    raise ConfigurationError("CAPABILITY_ADAPTER_INVALID")
            elif not isinstance(item, SkillRegistration):
                raise ConfigurationError("CAPABILITY_REGISTRATION_INVALID")
            entries[item.adapter_key] = item
        self._entries = MappingProxyType(entries)

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
                if spec is None or spec.kind != binding.kind:
                    raise ConfigurationError("CAPABILITY_BINDING_INVALID")
                try:
                    validate_schema(json_copy(binding.config), registration.config_schema)
                except Exception as exc:
                    raise ConfigurationError("CAPABILITY_CONFIG_INVALID") from exc
                tools[binding.visible_name] = ResolvedCapability(binding, registration, spec)
        return ResolvedCatalog(MappingProxyType(tools), MappingProxyType(skills))
