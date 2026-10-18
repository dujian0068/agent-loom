"""Serializable execution bindings, never clients or platform version identifiers."""

from dataclasses import asdict

from .definitions import variable_contract
from .json_documents import copy_document as json_copy
from .recovery import BINDING_VERSION


def recovery_manifest(program):
    return json_copy(
        {
            "binding_version": BINDING_VERSION,
            "mode": program.mode,
            "definitions": {key: asdict(value) for key, value in program.definitions.items()},
            "variable_contracts": {
                key: variable_contract(value) for key, value in program.definitions.items()
            },
            "execution_policy": asdict(program.execution_policy),
            "context_policy": asdict(program.context_policy),
            "model_options": asdict(program.model_options),
            "model_descriptor": asdict(program.model.descriptor),
            "hooks": [asdict(binding) for binding in program.hooks.bindings],
            "capabilities": {
                role: {
                    "tools": {
                        name: capability_manifest(entry.spec)
                        for name, entry in catalog.tools.items()
                    },
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
            "workspace_root": str(program.workspace.root)
            if program.workspace.root is not None
            else None,
            **(
                {"file_provider": program.runtime_engine._files.manifest}
                if program.runtime_engine._files.manifest is not None
                else {}
            ),
        }
    )


def capability_manifest(spec):
    """Keep original value-adapter checkpoints readable; fingerprint new explicit fields."""
    result = asdict(spec)
    for key in ("resource_key", "model_name", "handler_ref", "credential_binding_ref"):
        if result[key] is None:
            del result[key]
    return result
