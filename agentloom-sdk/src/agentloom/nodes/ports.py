"""Per-activation services. Every access checks scope and lifetime authority."""

from ..configuration.json_documents import copy_document, freeze_document
from ..errors import RequestError
from ..runtime.batches import stable_id
from ..runtime.handlers import BusinessEvent
from .contracts import InvocationScopeRef, NodeContext, PreparedEffect


class ActivationAuthority:
    def __init__(self, runner, task_id, activation_id):
        self.__check = runner.check_active
        self.__read = lambda: runner.task(task_id)
        self.activation_id = activation_id
        self.active = True

    def check(self):
        if not self.active:
            raise RequestError("NODE_ACTIVATION_CLOSED")
        self.__check()
        task = self.__read()
        if task["activation_id"] != self.activation_id or task["status"] != "running":
            raise RequestError("NODE_ACTIVATION_CLOSED")

    def close(self):
        self.active = False


class ContextPort:
    def __init__(self, authority, read, request):
        self.__authority, self.__read, self.__request = authority, read, request
        self.context = None

    def snapshot(self):
        self.__authority.check()
        return freeze_document(self.__read())

    async def materialize(self):
        self.__authority.check()
        return await self.__request(
            BusinessEvent("context.materialize", "", {"task_id": self.context.task_id}),
            self.context,
        )

    async def prepare(self, kind, **body):
        self.__authority.check()
        return await self.__request(
            BusinessEvent(
                "context.mutate", "", {"task_id": self.context.task_id, "kind": kind, "body": body}
            ),
            self.context,
        )


class ModelPort:
    def __init__(self, authority, task_id, read, prepare, request):
        self.__authority, self.__task_id, self.__read = authority, task_id, read
        self.__prepare, self.__request = prepare, request

    async def prepare(self, loop_index, context_port):
        self.__authority.check()
        records = await context_port.materialize()
        request = self.__request(self.__task_id)
        request.update(
            messages=[r["message"] for r in records], record_ids=[r["record_id"] for r in records]
        )
        op = self.__prepare(self.__task_id, f"model:{loop_index}", "model.generate", request)
        return op["operation_id"], PreparedEffect("begin_model", {"operation": op})

    def result(self, op_id):
        self.__authority.check()
        op = self.__read(op_id)
        if op is None or op["task_id"] != self.__task_id:
            raise RequestError("OPERATION_SCOPE_MISMATCH")
        return freeze_document(op["response"]) if op["status"] != "pending" else None


class CapabilityPort:
    def __init__(self, authority, task_id, read):
        self.__authority, self.__task_id, self.__read = authority, task_id, read

    def batch_id(self, loop_index):
        self.__authority.check()
        return stable_id(self.__task_id, "batch", loop_index)

    def settled(self, batch_id):
        self.__authority.check()
        batch = self.__read(batch_id)
        if batch is None or batch["owner_task_id"] != self.__task_id:
            raise RequestError("BATCH_SCOPE_MISMATCH")
        return all(m["status"] == "settled" for m in batch["members"])


class EnvironmentPort:
    def __init__(self, authority, request):
        self.__authority, self.__request = authority, request
        self.context = None

    async def read(self):
        self.__authority.check()
        return await self.__request(BusinessEvent("environment.read", "", {}), self.context)


def bind_context(runner, task, activation_id):
    task_id = task["task_id"]
    authority = ActivationAuthority(runner, task_id, activation_id)
    context_port = ContextPort(authority, lambda: runner.task(task_id), runner.bus.request)
    environment_port = EnvironmentPort(authority, runner.bus.request)
    context = NodeContext(
        InvocationScopeRef(runner.run_id, runner.state()["graph_id"], task_id, task["role_key"]),
        activation_id,
        runner.lease,
        authority,
        authority,
        ModelPort(
            authority,
            task_id,
            runner.operation,
            runner.calls.prepare_operation,
            runner.calls.model_request,
        ),
        CapabilityPort(
            authority, task_id, lambda bid: copy_document(runner.state()["batches"].get(bid))
        ),
        context_port,
        environment_port,
    )
    context_port.context = environment_port.context = context
    return context
