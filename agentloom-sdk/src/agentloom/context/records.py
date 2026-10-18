from ..configuration.json_documents import canonical_document as canonical
from ..configuration.json_documents import copy_document
from ..configuration.json_documents import document_digest as digest
from ..errors import AgentLoomError
from ..types import new_id


def record(message, *, protected=False, fixed=False, kind="message", record_id=None, raw=None):
    return {
        "record_id": record_id or new_id(),
        "raw": copy_document(message if raw is None else raw),
        "message": copy_document(message),
        "protected": protected,
        "fixed": fixed,
        "kind": kind,
    }


def append(context, message, **kwargs):
    entry = record(message, **kwargs)
    entry["context_seq"] = len(context["records"]) + 1
    context["records"].append(entry)
    context["revision"] += 1
    return entry


def materialize(context):
    records = context["records"]
    snapshot = context.get("snapshot")
    by_id = {r["record_id"]: r for r in records}
    if snapshot and all(
        rid in by_id and digest(by_id[rid]["message"]) == hashed
        for rid, hashed in snapshot["sources"].items()
    ):
        selected = [
            r
            for r in records
            if r["kind"] != "summary" and r["record_id"] not in snapshot["sources"]
        ]
        index = 0
        while index < len(selected) and selected[index]["message"]["role"] == "system":
            index += 1
        selected.insert(index, by_id[snapshot["record_id"]])
    else:
        selected = [r for r in records if r["kind"] != "summary"]
    return copy_document(selected)


def revise_call_arguments(context, original_records, call_id, arguments):
    """Revise one effective call slot without changing its raw decision or siblings."""

    def find(records):
        for entry in records:
            for call in entry["message"].get("tool_calls", []):
                if call["id"] == call_id:
                    return call
        raise AgentLoomError("HOOK_CALL_RECORD_MISSING")

    original = find(original_records)
    current = find(context["records"])
    # Concurrent hooks on other slots of the same assistant message may commit.
    # Only the actual semantic read set must remain unchanged.
    if current != original:
        raise AgentLoomError("CONTEXT_REVISION_CONFLICT")
    current["function"]["arguments"] = canonical(arguments)


def history_records(runs, recent_n, current_inputs=()):
    """Terminal runs, including failures; never inject Executor-private records."""
    result = []
    recent_ids = {r["run_id"] for r in runs[-recent_n:]} if recent_n else set()
    protected_ids = {item["input_id"] for item in current_inputs}
    positions = {}
    for run in runs:
        for item in run["inputs"]:
            key = item["input_id"]
            if key not in positions or item["delivery_state"] == "delivered":
                positions[key] = run["run_id"]

    def input_message(run, item):
        key = item["input_id"]
        if key in protected_ids or positions[key] != run["run_id"]:
            return {
                "role": "user",
                "content": f"[Input {key} was transferred; its original text appears at its delivery position.]",
            }
        return {"role": "user", "content": item["text"]}

    for run in runs:
        root = run["tasks"].get(run.get("root_task_id"))
        if root and run["run_id"] in recent_ids:
            by_record = {item["record_id"]: item for item in run["inputs"] if item.get("record_id")}
            messages, projected = [], set()
            for entry in root["context"]["records"]:
                if (
                    entry["kind"] in {"summary", "history", "terminal_marker"}
                    or entry["message"]["role"] == "system"
                ):
                    continue
                item = by_record.get(entry["record_id"])
                if item:
                    messages.append(input_message(run, item))
                    projected.add(item["input_id"])
                else:
                    messages.append(copy_document(entry["message"]))
            # Accepted input remains history even if no subsequent model turn ran.
            messages.extend(
                input_message(run, item)
                for item in run["inputs"]
                if item["input_id"] not in projected
            )
            # Fill every missing result in an interrupted assistant tool exchange.
            repaired = []
            pending = []
            for message in messages:
                if message["role"] != "tool" and pending:
                    repaired.extend(_missing_results(pending, run))
                    pending = []
                repaired.append(message)
                if message["role"] == "assistant":
                    pending = [c["id"] for c in message.get("tool_calls", [])]
                elif message["role"] == "tool" and message["tool_call_id"] in pending:
                    pending.remove(message["tool_call_id"])
            repaired.extend(_missing_results(pending, run))
            for message in repaired:
                result.append(record(message, protected=message["role"] == "user", kind="history"))
        else:
            for item in run["inputs"]:
                result.append(record(input_message(run, item), protected=True, kind="history"))
            if run["output"]:
                value = run["output"]["value"]
                result.append(
                    record(
                        {
                            "role": "assistant",
                            "content": value if isinstance(value, str) else canonical(value),
                        },
                        kind="history",
                    )
                )
        result.append(
            record(
                {"role": "user", "content": f"[Previous execution ended: {run['status']}]"},
                kind="terminal_marker",
            )
        )
    return result


def _missing_results(ids, run):
    from .history_facts import interrupted_call

    return [
        {
            "role": "tool",
            "tool_call_id": call_id,
            "content": canonical(interrupted_call(run, call_id)),
        }
        for call_id in ids
    ]
