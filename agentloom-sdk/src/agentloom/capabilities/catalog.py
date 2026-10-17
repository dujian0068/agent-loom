"""Immutable role-bound capability views; reading a catalog never performs I/O."""

from dataclasses import dataclass
from types import MappingProxyType

from ..configuration import json_copy
from ..configuration.json_documents import freeze_document as freeze


@dataclass(frozen=True)
class ResolvedCatalog:
    tools: object
    skills: object
    program_key: str | None = None
    binding_key: str | None = None

    def __post_init__(self):
        object.__setattr__(self, "tools", MappingProxyType(dict(self.tools)))
        object.__setattr__(self, "skills", MappingProxyType(dict(self.skills)))

    @property
    def capabilities(self):
        return tuple(entry.public_spec for entry in self.tools.values())

    @property
    def model_name_to_key(self):
        return freeze({name: entry.spec.capability_key for name, entry in self.tools.items()})

    @property
    def skill_entries(self):
        return freeze(
            [
                {
                    "resource_key": entry.registration.package.package_key,
                    "entry_name": entry.registration.package.metadata["name"],
                    "name": name,
                    "description": entry.registration.package.metadata["description"],
                }
                for name, entry in self.skills.items()
                if not entry.registration.package.metadata.get("disable-model-invocation", False)
            ]
        )

    @property
    def skill_calls(self):
        routes = {
            "skill_list": "skill.list",
            "skill_read": "skill.read",
            "skill_read_resource": "skill.resource.read",
        }
        return freeze(
            [
                {
                    "model_name": spec["name"],
                    "event_type": routes[spec["name"]],
                    "input_schema": spec["parameters"],
                }
                for spec in self.model_tools()
                if spec["name"] in routes
            ]
        )

    @property
    def access_scope(self):
        return freeze(
            {
                entry.binding.binding_key: entry.binding.access_scope
                for entry in (*self.tools.values(), *self.skills.values())
            }
        )

    @property
    def environment_policy(self):
        # A catalog grants only the workspace access of its individual bindings.
        return freeze({"capability_access": self.access_scope})

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
