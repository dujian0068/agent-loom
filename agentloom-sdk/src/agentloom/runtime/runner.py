"""One lease-scoped execution driver, rebuilt from committed facts on takeover."""

import asyncio
import time

from ..configuration.json_documents import canonical_document as canonical
from ..configuration.json_documents import document_digest as digest
from ..context import history_records, record
from ..context.results import RESULT_READ_TOOL, ResultReadHandler
from ..errors import AgentLoomError, InfrastructureError
from ..events import EventBusImpl, HandlerBinding, HookHandler
from ..prompts.assembly import PROMPT_RECORD_KIND
from ..storage import TERMINAL, CommitCoordinator, verify_lease
from ..types import new_id
from .agent_engine import AgentGraphEngine
from .calls import CallDriver, failure, success
from .control import emit, finalize_run, run_sequence
from .handlers import ModelHandler, SkillHandler, ToolHandler


def new_task(
    role_key,
    definition,
    content,
    variables=None,
    *,
    parent_operation=None,
    history=(),
    include_input=True,
    system_prompt=None,
):
    task_id = new_id()
    text = content if isinstance(content, str) else canonical(content)
    records = [record({"role": "system", "content": definition.render(variables)}, fixed=True)]
    if system_prompt is not None:
        records.append(
            record(
                {"role": "system", "content": system_prompt["content"]},
                fixed=True,
                protected=True,
                kind=PROMPT_RECORD_KIND,
            )
        )
    records.extend(history)
    if include_input:
        records.append(record({"role": "user", "content": text}, protected=True, fixed=True))
    for sequence, entry in enumerate(records, 1):
        entry["context_seq"] = sequence
    return {
        "task_id": task_id,
        "role_key": role_key,
        "parent_operation": parent_operation,
        "status": "ready",
        "stage": "prepare",
        "round": 0,
        "calls": [],
        "candidate": None,
        "result": None,
        "error": None,
        "context": {
            "context_id": new_id(),
            "records": records,
            "revision": len(records),
            "snapshot": None,
            "episodes": {},
        },
    }


class ExecutionRunner:
    def __init__(self, runtime, program, lease):
        self.runtime, self.program, self.lease = runtime, program, lease
        self.run_id = lease.run_id
        self._prompts = (self.state().get("prompt_contract") or {}).get("roles", {})
        self.coordinator = CommitCoordinator(
            runtime.store, runtime.namespace, lease, runtime.checkpoint_callback
        )
        self.bus = EventBusImpl()
        hooks = HookHandler(program.hooks, self)
        handlers = {"model.generate": ModelHandler(self), "capability.invoke": ToolHandler(self)}
        skill = SkillHandler(self)
        handlers.update({k: skill for k in ("skill.list", "skill.read", "skill.resource.read")})
        handlers["result.read"] = ResultReadHandler(self)
        for event_type, handler in handlers.items():
            self.bus.register_handler(event_type, 1, HandlerBinding(handler))
        for event_type in (*handlers, "executor.delegate"):
            for stage in ("before", "after"):
                self.bus.register_handler(event_type + "." + stage, 1, HandlerBinding(hooks))
        self.bus.freeze()
        self.calls = CallDriver(self)
        self.engine = AgentGraphEngine(self)
        self._slots = asyncio.Semaphore(program.execution_policy.executor_max_parallel)

    def state(self):
        return self.runtime._load(self.run_id)

    def task(self, task_id):
        return self.runtime.store.read_path(self.runtime.namespace, self.run_id, ("tasks", task_id))

    def operation(self, op_id):
        return self.runtime.store.read_path(
            self.runtime.namespace, self.run_id, ("operations", op_id)
        )

    def commit(self, command, mutation):
        return self.coordinator.commit(command, mutation)

    @staticmethod
    def ensure_admission(state):
        if state["status"] == "stopping" or state["status"] in TERMINAL:
            raise AgentLoomError("RUN_STOPPED", category="cancelled")
        if time.time() >= state["deadline_at"]:
            raise AgentLoomError("RUN_TIMEOUT", category="timeout")

    def check_active(self):
        with self.runtime.store.transaction(self.runtime.namespace) as tx:
            state = tx.get("runs", self.run_id)
            verify_lease(state, self.lease, tx.now)
            self.ensure_admission(state)

    def tools(self, role):
        result = self.program.catalogs[role].model_tools()
        if result or role == "planner" and self.program.executors:
            result.append(RESULT_READ_TOOL)
        if role == "planner" and self.program.executors:
            result.append(
                {
                    "name": "delegate_task",
                    "description": "Assign an isolated task to an executor. "
                    + "; ".join(
                        key + ": " + definition.description
                        for key, definition in self.program.executors.items()
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "executor": {"enum": list(self.program.executors)},
                            "task": {},
                            "variables": {"type": "object"},
                        },
                        "required": ["executor", "task"],
                        "additionalProperties": False,
                    },
                }
            )
        return result

    def first_output(self, op_id, attempt_id):
        def receive(s):
            op = s["operations"][op_id]
            attempt = op["attempts"][-1]
            if attempt["attempt_id"] != attempt_id:
                raise InfrastructureError("ATTEMPT_ID_MISMATCH")
            attempt["first_output_at"] = time.time()
            for item in s["inputs"]:
                if item.get("claimed_by") == op_id and item["delivery_state"] == "claimed":
                    item["delivery_state"] = "delivered"

        self.commit("first-output:" + attempt_id, receive)

    async def delegate(self, op_id):
        from .validation import validate_action

        op = self.operation(op_id)
        try:
            arguments = op["request"]["arguments"]
            validate_action(self, op["role_key"], op["request"]["name"], arguments)
            key = arguments["executor"]
            definition = self.program.executors[key]
        except AgentLoomError as exc:
            return failure(exc.to_dict())
        # Only live Executor work owns a slot; response hooks run after it is released.
        async with self._slots:
            self.check_active()
            if op["target_ref"] is None:
                task = new_task(
                    key,
                    definition,
                    arguments["task"],
                    arguments.get("variables"),
                    parent_operation=op_id,
                    system_prompt=self._prompts.get(key),
                )

                def start(s):
                    self.ensure_admission(s)
                    current = s["operations"][op_id]
                    current.update(
                        target_ref=task["task_id"],
                        permit={"epoch": self.lease.epoch},
                        phase="execute",
                        request_hash=digest(current["request"]),
                    )
                    s["tasks"][task["task_id"]] = task

                self.commit("delegate-start:" + op_id, start)
            target = self.operation(op_id)["target_ref"]
            result = await self.engine.run_task(target)
        return (
            success(result["result"])
            if result["status"] == "succeeded"
            else failure(result["error"])
        )

    async def _monitor(self):
        renewed = time.monotonic()
        while True:
            self.check_active()
            if time.monotonic() - renewed >= self.runtime.lease_seconds / 3:
                self.coordinator.renew(self.runtime.lease_seconds)
                renewed = time.monotonic()
            await asyncio.sleep(min(0.02, self.runtime.lease_seconds / 6))

    async def run(self):
        state = self.state()
        if state["status"] == "stopping":
            self.finalize("cancelled", error=AgentLoomError("RUN_STOPPED").to_dict())
            return
        if time.time() >= state["deadline_at"]:
            self.finalize("timed_out", error=AgentLoomError("RUN_TIMEOUT").to_dict())
            return
        scope = self.runtime._files.prepare(
            self.runtime,
            self.program.workspace,
            state["session_id"],
            self.run_id,
            self.lease.epoch,
            guard=self.check_active,
        )
        if state["workspace"] and state["workspace"]["path"] != scope["path"]:
            raise InfrastructureError("WORKSPACE_BINDING_MISMATCH")
        self.commit("environment:" + str(self.lease.epoch), lambda s: s.update(workspace=scope))
        if state["root_task_id"] is None:
            with self.runtime.store.transaction(self.runtime.namespace) as tx:
                previous = sorted(
                    (r for r in tx.list("runs") if r["run_id"] in state["history_sources"]),
                    key=run_sequence,
                )
            role = "planner" if self.program.mode == "plan" else "react"
            history = history_records(
                previous, self.program.context_policy.recent_runs, state["inputs"]
            )
            task = new_task(
                role,
                self.program.definitions[role],
                state["initial_input"]["content"],
                state["initial_input"]["variables"],
                history=history,
                include_input=False,
                system_prompt=self._prompts.get(role),
            )

            def initialize(s):
                self.ensure_admission(s)
                s.update(
                    root_task_id=task["task_id"],
                    tasks={task["task_id"]: task},
                    status="running",
                    started_at=time.time(),
                )
                # The root prepare stage inserts all pending inputs in receipt
                # order, including supplements accepted before this question.
                emit(s, "run.started")

            self.commit("initialize", initialize)
        work = asyncio.create_task(self.engine.run_task(self.state()["root_task_id"]))
        monitor = asyncio.create_task(self._monitor())
        try:
            done, _ = await asyncio.wait((work, monitor), return_when=asyncio.FIRST_COMPLETED)
            if monitor in done:
                await monitor
            result = await work
            if result["status"] == "succeeded":
                value = result["result"]
                self.finalize(
                    "succeeded",
                    output={
                        "kind": "json"
                        if self.program.definition.output_schema is not None
                        else "text",
                        "value": value,
                        "source_ref": {"task_id": result["task_id"]},
                    },
                )
            else:
                self.finalize("failed", error=result["error"])
        except InfrastructureError:
            raise
        except AgentLoomError as exc:
            work.cancel()
            outcomes = await asyncio.gather(work, return_exceptions=True)
            for outcome in outcomes:
                if isinstance(outcome, InfrastructureError):
                    raise outcome
            status = {
                "RUN_STOPPED": "cancelled",
                "RUN_TIMEOUT": "timed_out",
                "LOOP_LIMIT_EXCEEDED": "budget_exhausted",
            }.get(exc.code, "failed")
            self.finalize(status, error=exc.to_dict())
        finally:
            work.cancel()
            monitor.cancel()
            await asyncio.gather(work, monitor, return_exceptions=True)
            if self.state()["status"] not in TERMINAL:
                self.runtime._files.revoke(self.runtime, scope)

    def finalize(self, status, *, output=None, error=None):
        return finalize_run(self.runtime, self.lease, status, output=output, error=error)
