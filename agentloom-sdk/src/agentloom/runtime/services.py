"""Public read/control facades. Only these trusted commands bypass a business lease."""

from ..configuration.json_documents import document_digest as digest
from ..errors import AgentLoomError, RequestError
from ..public.handles import RunHandle
from ..public.validation import identity, pagination
from ..storage import TERMINAL
from ..types import new_id
from .control import request_stop, run_sequence


def input_receipt(state, item):
    return {
        "input_id": item["input_id"],
        "session_id": state["session_id"],
        "run_id": item.get("origin_run_id", state["run_id"]),
        "acceptance": "accepted",
        "delivery_state": item["delivery_state"],
        "session_seq": item["session_seq"],
        "commit_ref": state["revision"],
    }


class RunService:
    def __init__(self, runtime):
        self.runtime = runtime

    async def attach(self, run_id):
        self.runtime.ensure_open()
        state = self.runtime._load(run_id)
        return RunHandle(self.runtime, state["run_id"], state["session_id"], state["input_id"])

    async def get(self, run_id):
        self.runtime.ensure_open()
        state = self.runtime._load(run_id)
        # Private contexts, provider requests, environment handles and credentials stay internal.
        keys = (
            "run_id",
            "session_id",
            "input_id",
            "status",
            "output",
            "error",
            "usage",
            "created_at",
            "started_at",
            "ended_at",
            "cleanup_state",
            "terminal_seq",
            "revision",
        )
        return {
            **{key: state[key] for key in keys},
            "cleanup_error": state["cleanup_job"]["error"] if state["cleanup_job"] else None,
            "maintenance": state.get("maintenance", {}),
            "tasks": [
                {k: task[k] for k in ("task_id", "role_key", "parent_operation", "status", "stage")}
                for task in state["tasks"].values()
            ],
        }

    async def stop(self, run_id, *, command_id, reason="user"):
        runtime = self.runtime
        runtime.ensure_open()
        run_id, command_id = identity(run_id, "run_id"), identity(command_id, "command_id")
        if not isinstance(reason, str):
            raise RequestError("INVALID_STOP_COMMAND")
        fingerprint = digest({"run_id": run_id, "reason": reason})
        with runtime.store.transaction(runtime.namespace) as tx:
            saved = tx.get("commands", command_id)
            if saved:
                if saved["fingerprint"] != fingerprint:
                    raise RequestError("IDEMPOTENCY_CONFLICT")
                return saved["receipt"]
            status = request_stop(tx, run_id, AgentLoomError("RUN_STOPPED").to_dict())
            receipt = {
                "run_id": run_id,
                "command_id": command_id,
                "admission_closed": True,
                "status_at_commit": status,
                "commit_ref": new_id(),
            }
            tx.put("commands", command_id, {"fingerprint": fingerprint, "receipt": receipt})
            return receipt


class InputCommands:
    def __init__(self, runtime):
        self.runtime = runtime

    async def append(self, run_id, *, input_id, content):
        runtime = self.runtime
        runtime.ensure_open()
        run_id, input_id = identity(run_id, "run_id"), identity(input_id, "input_id")
        if not isinstance(content, str) or not content.strip():
            raise RequestError("INPUT_INVALID")
        fingerprint = digest({"run_id": run_id, "content": content, "kind": "supplement"})
        with runtime.store.transaction(runtime.namespace) as tx:
            existing = tx.get("inputs", input_id)
            if existing:
                if existing["fingerprint"] != fingerprint:
                    raise RequestError("INPUT_CONFLICT")
                target = tx.get("runs", existing["delivery_run_id"])
                if target is None:
                    raise RequestError("INPUT_SOURCE_DELETED")
                item = next(i for i in target["inputs"] if i["input_id"] == input_id)
                return input_receipt(target, item)
            state = tx.get("runs", run_id)
            if state is None:
                raise RequestError("RUN_NOT_FOUND")
            if state["status"] in TERMINAL or state["status"] == "stopping":
                raise RequestError("RUN_ENDED")
            session = tx.get("sessions", state["session_id"])
            session["seq"] += 1
            item = {
                "input_id": input_id,
                "origin_run_id": run_id,
                "text": content,
                "delivery_state": "pending",
                "record_id": None,
                "session_seq": session["seq"],
                "kind": "supplement",
            }
            state["inputs"].append(item)
            state["revision"] += 1
            tx.put("runs", run_id, state)
            tx.put("sessions", state["session_id"], session)
            tx.put(
                "inputs",
                input_id,
                {"fingerprint": fingerprint, "run_id": run_id, "delivery_run_id": run_id},
            )
            return input_receipt(state, item)


class QueryServices:
    def __init__(self, runtime, kind):
        self.runtime, self.kind = runtime, kind

    async def list(self, run_id, *, after=0, limit=100):
        self.runtime.ensure_open()
        pagination(after, limit)
        state = self.runtime._load(run_id)
        if self.kind == "tasks":
            entries = [
                {
                    key: t[key]
                    for key in (
                        "task_id",
                        "role_key",
                        "status",
                        "stage",
                        "parent_operation",
                        "result",
                        "error",
                    )
                }
                for t in state["tasks"].values()
            ]
        else:
            entries = state["events"]
        page = entries[after : after + limit]
        return {
            "items": page,
            "next_cursor": after + len(page) if after + len(page) < len(entries) else None,
        }


class EventService(QueryServices):
    async def read(self, run_id, *, after_seq=0, limit=100):
        self.runtime.ensure_open()
        pagination(after_seq, limit)
        if after_seq > len(self.runtime._load(run_id)["events"]):
            raise RequestError("EVENT_CURSOR_AHEAD")
        return await self.list(run_id, after=after_seq, limit=limit)


class OperationService:
    def __init__(self, runtime):
        self.runtime = runtime

    async def get(self, run_id, operation_id):
        self.runtime.ensure_open()
        operation = self.runtime._load(run_id)["operations"].get(
            identity(operation_id, "operation_id")
        )
        if operation is None:
            raise RequestError("OPERATION_NOT_FOUND")
        return {
            key: operation[key]
            for key in ("operation_id", "task_id", "event_type", "status", "phase", "target_ref")
        }


class SessionService:
    def __init__(self, runtime):
        self.runtime = runtime

    async def history(self, session_id, *, after=0, limit=100):
        self.runtime.ensure_open()
        pagination(after, limit)
        session_id = identity(session_id, "session_id")
        with self.runtime.store.transaction(self.runtime.namespace) as tx:
            records = sorted(
                (r for r in tx.list("runs") if r["session_id"] == str(session_id)),
                key=run_sequence,
            )
        items = [
            {k: r[k] for k in ("run_id", "status", "output", "error", "created_at")}
            for r in records[after : after + limit]
        ]
        return {
            "items": items,
            "next_cursor": after + len(items) if after + len(items) < len(records) else None,
        }

    async def inputs(self, session_id):
        self.runtime.ensure_open()
        session_id = identity(session_id, "session_id")
        with self.runtime.store.transaction(self.runtime.namespace) as tx:
            items = {}
            for state in sorted(tx.list("runs"), key=run_sequence):
                if state["session_id"] == str(session_id):
                    for item in state["inputs"]:
                        items[item["input_id"]] = {
                            **input_receipt(state, item),
                            "content": item["text"],
                        }
        return sorted(items.values(), key=lambda i: i["session_seq"])


class RecordService:
    def __init__(self, runtime):
        self.runtime = runtime

    async def delete(self, run_id, *, command_id):
        runtime = self.runtime
        runtime.ensure_open()
        run_id, command_id = identity(run_id, "run_id"), identity(command_id, "command_id")
        fingerprint = digest({"delete": run_id})
        with runtime.store.transaction(runtime.namespace) as tx:
            saved = tx.get("commands", command_id)
            if saved:
                if saved["fingerprint"] != fingerprint:
                    raise RequestError("IDEMPOTENCY_CONFLICT")
                return saved["receipt"]
            state = tx.get("runs", run_id)
            if state is None:
                raise RequestError("RUN_NOT_FOUND")
            if state["status"] not in TERMINAL or state["cleanup_state"] != "succeeded":
                raise RequestError("RECORD_NOT_QUIESCENT")
            if any(i["delivery_state"] == "pending_next_run" for i in state["inputs"]):
                raise RequestError("PENDING_INPUTS_EXIST")
            if any(
                run_id in other["history_sources"]
                for other in tx.list("runs")
                if other["run_id"] != run_id
            ):
                raise RequestError("RECORD_REFERENCED", "Delete dependent execution records first")
            from ..checkpoints.journal import delete_owned

            delete_owned(tx, run_id)
            tx.delete("runs", run_id)
            for receipt in tx.list("commits"):
                if receipt["run_id"] == run_id:
                    tx.delete("commits", run_id + ":" + receipt["command"])
            tx.put("admission", run_id, {"closed": True, "deleted": True})
            receipt = {"run_id": run_id, "command_id": command_id, "deleted": True}
            tx.put("commands", command_id, {"fingerprint": fingerprint, "receipt": receipt})
            return receipt
