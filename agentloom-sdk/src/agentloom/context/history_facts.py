"""Honest protocol placeholders for results an interrupted batch never delivered."""


def execution_fact(operation):
    """Prefer durable external evidence over a later control/Hook disposition."""
    for material in (operation.get("raw"), operation.get("response")):
        if material and material.get("execution_fact") == "completed":
            return "completed"
    if operation.get("raw"):
        return operation["raw"]["execution_fact"]
    if operation.get("attempts") or operation.get("target_ref"):
        return "unknown"
    return "not_started"


def interrupted_call(run, call_id):
    operation = next(
        (
            op
            for op in run["operations"].values()
            if op.get("source_call_id") == call_id and op["task_id"] == run["root_task_id"]
        ),
        None,
    )
    fact = execution_fact(operation) if operation else "not_started"
    blocked = operation and operation["status"] == "blocked"
    executed = {"completed": True, "not_started": False, "unknown": "unknown"}[fact]
    explanation = {
        "completed": "A completed result was committed, but this batch feedback was not delivered.",
        "not_started": "This call was not started before the execution ended.",
        "unknown": "The call may have executed; no complete result was committed.",
    }[fact]
    return {
        "status": "blocked" if blocked else ("unknown" if fact == "unknown" else "cancelled"),
        "executed": executed,
        "explanation": explanation,
    }
