"""Shared host services; each accepted run gets an independent lease-scoped runner."""

import asyncio
from dataclasses import asdict

from ..configuration import canonical, digest, json_copy, number, validate_schema
from ..configuration.assembly import recovery_manifest
from ..environment import LocalEnvironment
from ..errors import ConfigurationError, InfrastructureError, RequestError
from ..public.handles import RunHandle
from ..storage import TERMINAL, CommitCoordinator, FileRuntimeStore, claim
from ..types import AgentInput, RunUsage, new_id
from .cleanup import CleanupService
from .control import emit, settle_orphaned_stops
from .runner import ExecutionRunner
from .services import (
    EventService,
    InputCommands,
    OperationService,
    QueryServices,
    RecordService,
    RunService,
    SessionService,
)


class RuntimeEngine:
    def __init__(
        self,
        *,
        store=None,
        namespace="default",
        environment=None,
        lease_seconds=30,
        cleanup_retry_seconds=30,
        checkpoint_callback=None,
    ):
        if not isinstance(namespace, str) or not namespace.strip():
            raise ConfigurationError("STORAGE_NAMESPACE_INVALID")
        number(lease_seconds, "lease_seconds", 0, 3600, open_low=True)
        number(cleanup_retry_seconds, "cleanup_retry_seconds", 0, 3600, open_low=True)
        self._owns_store = store is None
        self.store = store if store is not None else FileRuntimeStore()
        self.environment = environment if environment is not None else LocalEnvironment()
        self.namespace, self.lease_seconds, self.cleanup_retry_seconds = (
            namespace,
            lease_seconds,
            cleanup_retry_seconds,
        )
        self.checkpoint_callback, self.worker_id = checkpoint_callback, new_id()
        self.runs, self.inputs = RunService(self), InputCommands(self)
        self.tasks, self.operations = QueryServices(self, "tasks"), OperationService(self)
        self.events, self.sessions, self.records = (
            EventService(self, "events"),
            SessionService(self),
            RecordService(self),
        )
        self._status, self._loop = "created", None
        self._workers, self._failures = {}, {}
        self._cleanup = None

    async def __aenter__(self):
        if self._status != "created":
            raise InfrastructureError("RUNTIME_ALREADY_OPENED")
        self._loop = asyncio.get_running_loop()
        self._status = "open"
        self._cleanup = asyncio.create_task(self._maintenance(), name="agentloom-maintenance")
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        await self.aclose()

    def ensure_open(self):
        if self._status != "open":
            raise InfrastructureError("RUNTIME_CLOSED")
        if asyncio.get_running_loop() is not self._loop:
            raise InfrastructureError("RUNTIME_LOOP_MISMATCH")

    async def aclose(self):
        if self._status == "closed":
            return
        self._status = "closing"
        tasks = list(self._workers.values()) + ([self._cleanup] if self._cleanup else [])
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._workers.clear()
        if self._owns_store:
            self.store.close()
        self._status = "closed"
        # All injected clients and stores remain owned by the caller.

    def _load(self, run_id):
        state = self.store.load(self.namespace, str(run_id))
        if state is None:
            raise RequestError("RUN_NOT_FOUND")
        if state.get("schema_version") != 1:
            raise InfrastructureError("CHECKPOINT_VERSION_UNSUPPORTED")
        return state

    def raise_local_failure(self, run_id):
        failure = self._failures.get(run_id)
        if failure:
            raise failure

    async def open_run(self, program, input, *, session_id=None, run_id=None, input_id=None):
        self.ensure_open()
        if input_id is not None and run_id is None:
            raise RequestError("INVALID_ID_COMBINATION")
        if input is None and input_id is not None:
            raise RequestError("INVALID_ID_COMBINATION")
        supplied_run_id = run_id is not None
        run_id = str(run_id) if supplied_run_id else new_id()
        session_id = str(session_id) if session_id is not None else None
        input_id = str(input_id) if input_id is not None else None
        if not run_id or session_id == "" or input_id == "":
            raise RequestError("IDENTITY_INVALID")
        if isinstance(input, str):
            input = AgentInput(input)
        if input is not None and not isinstance(input, AgentInput):
            raise RequestError("AGENT_INPUT_REQUIRED")
        payload = (
            {
                "content": json_copy(input.content),
                "variables": json_copy(input.variables),
                "artifacts": json_copy(input.artifacts),
            }
            if input is not None
            else None
        )
        with self.store.transaction(self.namespace) as tx:
            state = tx.get("runs", run_id)
            if state:
                if session_id is not None and session_id != state["session_id"]:
                    raise RequestError("INPUT_CONFLICT")
                if payload is not None and (
                    digest(payload) != state["input_hash"]
                    or input_id is not None
                    and input_id != state["input_id"]
                ):
                    raise RequestError("INPUT_CONFLICT")
                if state["mode"] != program.mode:
                    raise RequestError("RECOVERY_BINDING_MISMATCH")
                if state["status"] not in TERMINAL and state["bindings"] != recovery_manifest(
                    program
                ):
                    raise RequestError(
                        "RECOVERY_BINDING_MISMATCH",
                        "Reconstruct the original execution configuration and resource manifests",
                    )
            else:
                if tx.get("admission", run_id):
                    raise RequestError("RUN_ADMISSION_CLOSED")
                if payload is None:
                    raise RequestError("RUN_NOT_FOUND" if supplied_run_id else "INPUT_REQUIRED")
                if payload["artifacts"]:
                    raise RequestError(
                        "ARTIFACT_INPUT_UNSUPPORTED",
                        "Use an explicitly authorized capability or Skill resource",
                    )
                validate_schema(payload["content"], program.definition.input_schema)
                if program.definition.input_schema is None and (
                    not isinstance(payload["content"], str) or not payload["content"].strip()
                ):
                    raise RequestError("INPUT_INVALID")
                program.definition.render(payload["variables"])
                session_id, input_id = session_id or new_id(), input_id or new_id()
                if tx.get("inputs", input_id):
                    raise RequestError("INPUT_CONFLICT")
                previous = [r for r in tx.list("runs") if r["session_id"] == session_id]
                if any(r["status"] not in TERMINAL for r in previous):
                    raise RequestError("SESSION_BUSY")
                session = tx.get("sessions", session_id) or {"session_id": session_id, "seq": 0}
                session["seq"] += 1
                original = {
                    "input_id": input_id,
                    "origin_run_id": run_id,
                    "text": payload["content"]
                    if isinstance(payload["content"], str)
                    else canonical(payload["content"]),
                    "kind": "question",
                    "delivery_state": "pending",
                    "record_id": None,
                    "session_seq": session["seq"],
                }
                carried = []
                for old in previous:
                    changed = False
                    for item in old["inputs"]:
                        if (
                            item["input_id"] != old["input_id"]
                            and item["delivery_state"] == "pending_next_run"
                        ):
                            carried.append(
                                {
                                    **item,
                                    "delivery_state": "pending",
                                    "record_id": None,
                                    "claimed_by": None,
                                }
                            )
                            item["delivery_state"] = "carried"
                            index = tx.get("inputs", item["input_id"])
                            index["delivery_run_id"] = run_id
                            tx.put("inputs", item["input_id"], index)
                            changed = True
                    if changed:
                        tx.put("runs", old["run_id"], old)
                state = {
                    "schema_version": 1,
                    "revision": 1,
                    "run_id": run_id,
                    "session_id": session_id,
                    "input_id": input_id,
                    "initial_input": payload,
                    "history_sources": [r["run_id"] for r in previous],
                    "input_hash": digest(payload),
                    "mode": program.mode,
                    "status": "queued",
                    "created_at": tx.now,
                    "started_at": None,
                    "ended_at": None,
                    "deadline_at": tx.now + program.execution_policy.run_timeout_seconds,
                    "policy": asdict(program.execution_policy),
                    "bindings": recovery_manifest(program),
                    "lease": None,
                    "workspace": None,
                    "graph_id": new_id(),
                    "invocation_id": new_id(),
                    "root_task_id": None,
                    "tasks": {},
                    "operations": {},
                    "inputs": sorted([*carried, original], key=lambda i: i["session_seq"]),
                    "events": [],
                    "terminal_seq": None,
                    "usage": asdict(RunUsage()),
                    "output": None,
                    "error": None,
                    "partial_results": [],
                    "cleanup_state": "pending",
                    "cleanup_job": None,
                }
                emit(state, "run.accepted")
                tx.put("runs", run_id, state)
                tx.put("sessions", session_id, session)
                tx.put(
                    "inputs",
                    input_id,
                    {
                        "fingerprint": digest({"run_id": run_id, "input": payload}),
                        "run_id": run_id,
                        "delivery_run_id": run_id,
                    },
                )
        if state["status"] not in TERMINAL and run_id not in self._workers:
            self._failures.pop(run_id, None)
            self._workers[run_id] = asyncio.create_task(
                self._drive(program, run_id), name="agentloom:" + run_id
            )
        return RunHandle(self, run_id, state["session_id"], state["input_id"])

    async def _drive(self, program, run_id):
        coordinator = None
        try:
            while True:
                lease = claim(
                    self.store, self.namespace, run_id, self.worker_id, self.lease_seconds
                )
                if lease is not None:
                    break
                if self._load(run_id)["status"] in TERMINAL:
                    return
                # A follower observes the existing owner, then can recover if its
                # lease expires. It never dispatches while that owner is valid.
                await asyncio.sleep(min(0.2, self.lease_seconds / 3))
            coordinator = CommitCoordinator(self.store, self.namespace, lease)
            runner = ExecutionRunner(self, program, lease)
            await runner.run()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._failures[run_id] = (
                exc
                if isinstance(exc, InfrastructureError)
                else InfrastructureError("EXECUTION_DRIVER_FAILED", type(exc).__name__)
            )
            if self._failures[run_id] is not exc:
                self._failures[run_id].__cause__ = exc
        finally:
            if coordinator:
                try:
                    coordinator.release()
                except InfrastructureError as exc:
                    self._failures[run_id] = exc
            self._workers.pop(run_id, None)

    async def _maintenance(self):
        cleanup = CleanupService(self)

        def scan():
            settle_orphaned_stops(self)
            cleanup.scan_once()

        while True:
            work = asyncio.create_task(asyncio.to_thread(scan))
            try:
                await asyncio.shield(work)
            except asyncio.CancelledError:
                # No store/file worker is left running after aclose returns.
                await work
                raise
            except InfrastructureError:
                # A later scan reconciles committed cleanup jobs. Business waiters
                # independently read the store and receive infrastructure failures.
                pass
            await asyncio.sleep(0.05)
