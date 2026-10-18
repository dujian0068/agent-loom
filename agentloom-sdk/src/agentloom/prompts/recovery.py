"""Validate saved instructions without rendering the installed template again."""

from ..errors import InfrastructureError
from .assembly import PROMPT_CONTRACT_VERSION, PROMPT_RECORD_KIND, profile_for_role


def _require(condition, field):
    if not condition:
        raise InfrastructureError(
            "PERSISTENCE_INVARIANT_BROKEN",
            "Committed prompt contract disagrees: " + field,
            category="infrastructure",
            phase="recover",
            field_path="prompt_contract." + field,
        )


def validate_prompt_state(state):
    """Legacy Runs keep their original prompts, including children created on resume."""
    if "prompt_contract" not in state:
        _require(
            not any(
                r["kind"] == PROMPT_RECORD_KIND
                for task in state["tasks"].values()
                for r in task["context"]["records"]
            ),
            "missing",
        )
        return
    contract = state["prompt_contract"]
    _require(isinstance(contract, dict), "type")
    _require(
        type(contract.get("schema_version")) is int
        and contract["schema_version"] == PROMPT_CONTRACT_VERSION,
        "schema_version",
    )
    roles = contract.get("roles")
    _require(
        isinstance(roles, dict) and set(roles) == set(state["bindings"]["definitions"]), "roles"
    )
    for role, prompt in roles.items():
        _require(isinstance(prompt, dict), role)
        _require(prompt.get("profile") == profile_for_role(state["mode"], role), role + ".profile")
        for field in ("template_version", "content"):
            _require(
                isinstance(prompt.get(field), str) and bool(prompt[field].strip()),
                role + "." + field,
            )
    by_task = {}
    for task_id, task in state["tasks"].items():
        records = task["context"]["records"]
        selected = [r for r in records if r["kind"] == PROMPT_RECORD_KIND]
        _require(len(selected) == 1, "record_count")
        entry = selected[0]
        expected = {"role": "system", "content": roles[task["role_key"]]["content"]}
        _require(entry["fixed"] is True and entry["protected"] is True, "record_flags")
        _require(entry["message"] == entry["raw"] == expected, "record_content")
        _require(
            records.index(entry) == 1 and records[0]["message"]["role"] == "system",
            "record_position",
        )
        snapshot = task["context"].get("snapshot")
        _require(not snapshot or entry["record_id"] not in snapshot["sources"], "summary_source")
        by_task[task_id] = entry
    for op in state["operations"].values():
        if op["event_type"] != "model.generate" or op["purpose"] != "business":
            continue
        entry, request = by_task[op["task_id"]], op["request"]
        ids = request["record_ids"]
        _require(
            ids.count(entry["record_id"]) == 1 and len(ids) == len(request["messages"]),
            "request_records",
        )
        _require(
            request["messages"][ids.index(entry["record_id"])] == entry["message"],
            "request_content",
        )
