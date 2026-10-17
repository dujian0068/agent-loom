"""Serializable execution bindings, never clients or platform version identifiers."""

from dataclasses import asdict

from .models import json_copy


def recovery_manifest(program):
    return json_copy(
        {
            "mode": program.mode,
            "definitions": {key: asdict(value) for key, value in program.definitions.items()},
            "execution_policy": asdict(program.execution_policy),
            "context_policy": asdict(program.context_policy),
            "model_options": asdict(program.model_options),
            "model_descriptor": asdict(program.model.descriptor),
            "hooks": [asdict(binding) for binding in program.hooks.bindings],
            "capabilities": {
                role: {
                    "tools": {name: asdict(entry.spec) for name, entry in catalog.tools.items()},
                    "skills": {
                        name: {
                            "package_key": entry.registration.package.package_key,
                            "metadata": entry.registration.package.metadata,
                            "files": entry.registration.package.files_manifest,
                        }
                        for name, entry in catalog.skills.items()
                    },
                }
                for role, catalog in program.catalogs.items()
            },
            "persistent_sandbox": program.workspace.persistent_sandbox,
            "workspace_root": str(program.workspace.root),
        }
    )
