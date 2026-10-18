"""Host-owned provider lifecycle, durable identity and independently fenced cleanup."""

from ..configuration.json_values import canonical, digest, freeze
from ..environment import LocalEnvironment
from ..errors import ConfigurationError, InfrastructureError, LeaseLost
from .contracts import WorkspaceCallContext
from .loader import resolve_provider, synchronous_result, validate_instance
from .properties import FileProperties


class WorkspaceResources:
    def __init__(self, properties, environment):
        self.properties = FileProperties() if properties is None else properties
        if not isinstance(self.properties, FileProperties):
            raise ConfigurationError("INVALID_CONFIGURATION", field_path="files")
        self.definition = resolve_provider(self.properties)
        manifest = self.definition.manifest
        self.manifest = freeze(manifest) if manifest is not None else None
        self.local = self.properties.provider == "local"
        if not self.local and environment is not None:
            raise ConfigurationError(
                "FILE_ENVIRONMENT_CONFLICT",
                "A custom file provider cannot share an unverified script environment",
            )
        self.environment = (
            (environment if environment is not None else LocalEnvironment()) if self.local else None
        )
        self.backend = None

    def open(self):
        self.backend = self.definition.create(self.environment)
        validate_instance(self.backend)

    def close(self):
        if self.backend is not None:
            try:
                synchronous_result(self.backend.close())
            except Exception as exc:
                raise InfrastructureError("FILE_PROVIDER_CLOSE_FAILED", type(exc).__name__) from exc
            self.backend = None

    def validate_program(self, program):
        if self.local and program.workspace.root is None:
            raise ConfigurationError("WORKSPACE_ROOT_REQUIRED")
        if not self.local:
            from ..capabilities.tools import ScriptToolAdapter

            if any(
                isinstance(entry.registration.adapter, ScriptToolAdapter)
                for catalog in program.catalogs.values()
                for entry in catalog.tools.values()
            ):
                raise ConfigurationError("FILE_SANDBOX_INCOMPATIBLE")

    def _check_scope(self, scope):
        if canonical(scope.get("file_provider")) != canonical(self.manifest):
            raise InfrastructureError("WORKSPACE_PROVIDER_MISMATCH")

    @staticmethod
    def context(scope, guard, **values):
        return WorkspaceCallContext(
            workspace_id=scope.get("workspace_id", scope["path"]),
            scope=scope,
            check_active=guard,
            namespace=scope.get("namespace", "default"),
            session_id=values.pop("session_id", scope.get("session_id")),
            run_id=scope["run_id"],
            lease_epoch=scope["epoch"],
            **values,
        )

    def prepare(
        self, runtime, binding, session_id, run_id, epoch, guard, *, record_scope=lambda scope: None
    ):
        if self.local:
            scope = self.environment.prepare(
                binding, session_id, run_id, epoch, runtime.namespace, guard=guard
            )
            record_scope(scope)
            guard()
            return scope
        workspace_id = digest(
            {
                "namespace": runtime.namespace,
                "session_id": session_id,
                "run_id": None if binding.persistent_sandbox else run_id,
            }
        )
        scope = {
            "path": workspace_id,
            "workspace_id": workspace_id,
            "namespace": runtime.namespace,
            "session_id": session_id,
            "run_id": run_id,
            "epoch": epoch,
            "persistent": binding.persistent_sandbox,
            "file_provider": self.manifest,
        }
        guard()
        # A provider may acquire resources before prepare() fails or is stopped.
        # Persist the identity first so revocation/recovery can find those resources.
        record_scope(scope)
        synchronous_result(self.backend.prepare(self.context(scope, guard, purpose="prepare")))
        guard()
        return scope

    def revoke(self, runtime, scope):
        self._check_scope(scope)
        if self.local:
            return self.environment.revoke(scope)
        lease = runtime._load(scope["run_id"])["lease"]

        def guard():
            with runtime.store.transaction(runtime.namespace) as tx:
                state = tx.get("runs", scope["run_id"])
                current = (state or {}).get("lease") or {}
                if (
                    current.get("worker_id") != runtime.worker_id
                    or current.get("epoch") != lease["epoch"]
                    or current.get("expires_at", 0) <= tx.now
                ):
                    raise LeaseLost()

        guard()
        synchronous_result(self.backend.revoke(self.context(scope, guard, purpose="revoke")))
        guard()

    def cleanup(self, scope, guard, *, cleanup_job_id=None, cleanup_lease_epoch=None):
        self._check_scope(scope)
        guard()
        if self.local:
            return self.environment.cleanup(scope)
        if not scope["persistent"]:
            synchronous_result(
                self.backend.cleanup(
                    self.context(
                        scope,
                        guard,
                        purpose="cleanup",
                        cleanup_job_id=cleanup_job_id,
                        cleanup_lease_epoch=cleanup_lease_epoch,
                    )
                )
            )
        guard()
