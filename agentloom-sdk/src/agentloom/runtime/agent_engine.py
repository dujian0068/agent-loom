"""One persistent ready/wait scheduler for role nodes and logical operations."""

import asyncio

from ..configuration.json_documents import copy_document, document_digest
from ..errors import InfrastructureError
from ..nodes import ReactContinuationV1
from ..nodes.driver import NodeDriver
from ..public import AgentNode
from ..types import AgentContext
from .batches import TASK_TERMINAL, eligible_members, freeze_batch, ready, stable_id, wake_waiters


class AgentGraphEngine:
    def __init__(self, runner):
        self.runner = runner
        self.driver = NodeDriver(runner)
        self._agents, self._jobs = {}, {}

    def _node(self, task_id):
        task = self.runner.task(task_id)
        if task_id not in self._agents:
            agent = self.runner.program.bind_role(task["role_key"])
            agent._context = AgentContext(
                task["context"]["context_id"], task_id, lambda: self.runner.task(task_id)
            )
            self._agents[task_id] = agent
            if task_id == self.runner.state()["root_task_id"]:
                self.runner.program._context = agent.context
        return AgentNode(self._agents[task_id])

    def recover(self):
        def restore(state):
            state.setdefault("graph", {"protocol_version": 1, "ready_sequence": 0})
            state.setdefault("batches", {})
            legacy = {key for key, task in state["tasks"].items() if "continuation" not in task}
            for task in state["tasks"].values():
                if "continuation" not in task:
                    self._upgrade_task(state, task)
                if task["status"] == "running":
                    activation = state.get("activations", {}).get(task.get("activation_id"))
                    if activation:
                        activation["status"] = "abandoned"
                    ready(state, task)
                if (
                    task["task_id"] in legacy
                    and task["status"] in TASK_TERMINAL
                    and task["parent_operation"]
                ):
                    from .completion import failure, success

                    op = state["operations"][task["parent_operation"]]
                    if op["raw"] is None:
                        raw = (
                            success(task["result"])
                            if task["status"] == "succeeded"
                            else failure(task["error"])
                        )
                        op.update(raw=raw, response=copy_document(raw), phase="after")
            wake_waiters(state)

        self.runner.commit("recover-graph:" + str(self.runner.lease.epoch), restore)

    @staticmethod
    def _upgrade_task(state, task):
        # Explicit adapter for the supported pre-protocol v1 execution format.
        phase = {
            "prepare": "prepare_context",
            "call_model": "prepare_model",
            "batch": "wait_batch",
            "validate": "validate_output",
            "completed": "completed",
        }.get(task["stage"])
        if phase is None:
            raise InfrastructureError("CONTINUATION_INVALID")
        op_id = stable_id(state["run_id"], task["task_id"], f"model:{task['round']}")
        op = state["operations"].get(op_id)
        if task["stage"] == "call_model" and op is not None:
            phase = "call_model"
        batch_id = freeze_batch(state, task, op) if task["stage"] == "batch" else None
        if batch_id:
            for member in state["batches"][batch_id]["members"]:
                action_id = stable_id(
                    state["run_id"], task["task_id"], f"action:{task['round']}:{member['ordinal']}"
                )
                action = state["operations"].get(action_id)
                if action is not None:
                    member.update(
                        operation_id=action_id,
                        status="settled" if action["status"] != "pending" else "admitted",
                        response=copy_document(action["response"])
                        if action["status"] != "pending"
                        else None,
                    )
                    if (
                        action["status"] == "pending"
                        and action["event_type"] == "executor.delegate"
                        and action["target_ref"] is None
                    ):
                        member["admission_token"] = stable_id(
                            batch_id, member["ordinal"], "admission"
                        )
        task.update(
            node_revision=0,
            activation_no=0,
            activation_id=None,
            ready_seq=0,
            input={
                "content": state["initial_input"]["content"]
                if task["parent_operation"] is None
                else state["operations"][task["parent_operation"]]["request"]["arguments"]["task"]
            },
            wait_condition=None,
            resume_input_ref=None,
            continuation=ReactContinuationV1(
                phase,
                task["role_key"],
                task["role_key"],
                {
                    "context_id": task["context"]["context_id"],
                    "revision": task["context"]["revision"],
                },
                task["round"],
                model_operation_id=op_id if op else None,
                pending_batch_id=batch_id,
            ).to_dict(),
        )
        if task["status"] not in TASK_TERMINAL:
            ready(state, task)

    def requeue(self, task_id, activation_id):
        def requeue(state):
            task = state["tasks"][task_id]
            if task["activation_id"] == activation_id and task["status"] == "running":
                state["activations"][activation_id]["status"] = "abandoned"
                ready(state, task)

        self.runner.commit("requeue:" + activation_id, requeue)

    async def run_task(self, root_task_id):
        self.recover()
        try:
            while True:
                self.runner.check_active()
                for key, job in list(self._jobs.items()):
                    if job.done():
                        del self._jobs[key]
                        await job  # propagate infrastructure/control failures, never lose them
                state = self.runner.state()
                if state["tasks"][root_task_id]["status"] in TASK_TERMINAL:
                    return state["tasks"][root_task_id]
                self._schedule(state)
                ready_tasks = sorted(
                    (t for t in state["tasks"].values() if t["status"] == "ready"),
                    key=lambda t: (t["ready_seq"], t["task_id"]),
                )
                if ready_tasks:
                    task_id = ready_tasks[0]["task_id"]
                    await self.driver.activate(task_id, self._node(task_id))
                    await asyncio.sleep(0)
                elif self._jobs:
                    await asyncio.wait(
                        tuple(self._jobs.values()),
                        timeout=0.005,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                else:
                    raise InfrastructureError("GRAPH_WAIT_UNRESOLVED")
        finally:
            for job in self._jobs.values():
                job.cancel()
            await asyncio.gather(*self._jobs.values(), return_exceptions=True)
            self._jobs.clear()

    def _schedule(self, state):
        for task in state["tasks"].values():
            if task["status"] != "waiting":
                continue
            condition = task["wait_condition"]
            if condition["kind"] == "operation_completed":
                op_id = condition["ref_id"]
                key = ("operation", op_id)
                if key not in self._jobs:
                    self._jobs[key] = asyncio.create_task(self.runner.calls.advance(op_id))
        for batch in state.get("batches", {}).values():
            if batch["delivery_state"] == "consumed":
                continue
            for ordinal in eligible_members(self.runner.state(), batch):
                self._admit(batch["batch_id"], ordinal)
            batch = self.runner.state()["batches"][batch["batch_id"]]
            for member in batch["members"]:
                key = (batch["batch_id"], member["ordinal"])
                if member["status"] in {"admitting", "admitted"} and key not in self._jobs:
                    self._jobs[key] = asyncio.create_task(self._action(batch, member))

    def _admit(self, batch_id, ordinal):
        def admit(state):
            self.runner.ensure_admission(state)
            batch = state["batches"][batch_id]
            if ordinal not in eligible_members(state, batch):
                return
            member = batch["members"][ordinal]
            member["status"] = "admitting"
            if member["call"]["name"] == "delegate_task":
                member["admission_token"] = stable_id(batch_id, ordinal, "admission")

        self.runner.commit(f"admit:{batch_id}:{ordinal}", admit)

    async def _action(self, batch, member):
        task_id, ordinal = batch["owner_task_id"], member["ordinal"]
        response = await self.runner.calls.action(
            task_id, member["call"], ordinal, self.runner.task(task_id)["round"]
        )

        def settle(state):
            current = state["batches"][batch["batch_id"]]["members"][ordinal]
            current.update(status="settled", response=copy_document(response), admission_token=None)
            wake_waiters(state)

        self.runner.commit(f"settle:{batch['batch_id']}:{ordinal}", settle)

    def start_delegate(self, op_id, prepared):
        def start(state):
            self.runner.ensure_admission(state)
            op = state["operations"][op_id]
            if (
                prepared.operation_id != op_id
                or prepared.effective_request_hash != document_digest(op["request"])
                or op["stages"].get("request", {}).get("outcome") == "denied"
            ):
                raise InfrastructureError("DELEGATE_PREPARATION_INVALID")
            member = next(
                (
                    m
                    for b in state["batches"].values()
                    for m in b["members"]
                    if m["operation_id"] == op_id
                ),
                None,
            )
            if member is None or member["admission_token"] is None:
                raise InfrastructureError("EXECUTOR_ADMISSION_REQUIRED")
            task = copy_document(prepared.task)
            op.update(
                target_ref=task["task_id"],
                permit={"epoch": self.runner.lease.epoch},
                phase="execute",
                request_hash=prepared.effective_request_hash,
            )
            state["tasks"][task["task_id"]] = task
            ready(state, task)
            member["admission_token"] = None

        self.runner.commit("delegate-start:" + op_id, start)

    async def wait_delegate(self, op_id):
        while True:
            self.runner.check_active()
            op = self.runner.operation(op_id)
            if op["raw"] is not None:
                return op["raw"]
            if op["target_ref"] is None:
                raise InfrastructureError("DELEGATE_TARGET_MISSING")
            await asyncio.sleep(0.005)
