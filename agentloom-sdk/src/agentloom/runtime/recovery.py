"""Check persisted execution facts before takeover can dispatch external work."""

from uuid import NAMESPACE_URL, uuid5

from ..configuration.json_documents import document_digest as digest
from ..configuration.json_values import MAX_JSON_INTEGER
from ..errors import InfrastructureError
from ..prompts.recovery import validate_prompt_state
from .usage import TOKEN_KEYS


def _require(condition, field):
    if not condition:
        raise InfrastructureError(
            "PERSISTENCE_INVARIANT_BROKEN",
            "Committed execution facts disagree: " + field,
            category="infrastructure",
            phase="recover",
            field_path=field,
        )


def validate_recovery(state):
    """Validate projections; never mint tickets or silently repair a checkpoint.

    Both an unfinished attempt and a completed call awaiting Loop consumption
    are valid crash boundaries. Neither authorizes a second external dispatch.
    """
    try:
        _validate(state)
        validate_prompt_state(state)
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise InfrastructureError(
            "PERSISTENCE_INVARIANT_BROKEN",
            "Malformed execution checkpoint",
            category="infrastructure",
            phase="recover",
        ) from exc


def _validate(state):
    tasks, operations, usage = state["tasks"], state["operations"], state["usage"]
    root = state["root_task_id"]
    _require(root in tasks if root is not None else not tasks and not operations, "root_task_id")
    _require(digest(state["policy"]) == digest(state["bindings"]["execution_policy"]), "policy")
    counts = dict.fromkeys(
        (
            "loops_reserved",
            "loops_consumed",
            "business_model_attempts",
            "compaction_model_attempts",
            "tool_attempts",
        ),
        0,
    )
    tokens, known, model_attempts, identities, sources = {}, 0, 0, set(), set()
    for task_id, task in tasks.items():
        _require(task_id == task["task_id"], "task.task_id")
        _require(task["role_key"] in state["bindings"]["definitions"], "task.role_key")
        _require(type(task["round"]) is int and task["round"] >= 0, "task.round")
        parent = task["parent_operation"]
        if task_id == root:
            _require(parent is None, "root.parent_operation")
        else:
            _require(parent in operations, "task.parent_operation")
            op = operations[parent]
            _require(
                op["event_type"] == "executor.delegate"
                and op["target_ref"] == task_id
                and op["request"]["arguments"]["executor"] == task["role_key"],
                "task.parent_binding",
            )
        ancestors, current = {task_id}, task
        while current["parent_operation"] is not None:
            owner = operations[current["parent_operation"]]["task_id"]
            _require(owner not in ancestors, "task.parent_cycle")
            ancestors.add(owner)
            current = tasks[owner]
        records = task["context"]["records"]
        _require(len({r["record_id"] for r in records}) == len(records), "context.record_id")
        _require(
            all(
                "context_seq" not in entry
                or type(entry["context_seq"]) is int
                and entry["context_seq"] == seq
                for seq, entry in enumerate(records, 1)
            ),
            "context.sequence",
        )

    for op_id, op in operations.items():
        _require(op_id == op["operation_id"] and op["task_id"] in tasks, "operation.identity")
        _require(op["role_key"] == tasks[op["task_id"]]["role_key"], "operation.role_key")
        purpose = (op["task_id"], op["purpose_key"])
        _require(purpose not in identities, "operation.purpose_key")
        identities.add(purpose)
        source = op.get("source_call_id")
        if source:
            _require((op["task_id"], source) not in sources, "operation.source_call_id")
            sources.add((op["task_id"], source))
        complete = op["status"] != "pending"
        _require(op["status"] in {"pending", "succeeded", "failed", "blocked"}, "operation.status")
        _require(complete == (op["phase"] == "completed"), "operation.phase")
        if complete:
            _require(
                op["response"] and op["response"]["status"] == op["status"], "operation.response"
            )
        for key, hook in op["hooks"].items():
            _require(key.split(":", 1)[0] in {"request", "response"}, "hook.stage")
            _require(
                hook["status"]
                in {
                    "prepared",
                    "started",
                    "result_saved",
                    "applied",
                    "indeterminate",
                },
                "hook.status",
            )
            if (
                hook["status"] in {"result_saved", "applied"}
                and hook.get("execution_status") != "skipped_control"
            ):
                _require(
                    isinstance(hook["result"], dict)
                    and hook["result"].get("decision") in {"allow", "deny"},
                    "hook.decision",
                )
            if hook.get("contract"):
                _require(hook["contract"].get("schema_version") == 1, "hook.contract_version")
        model = op["event_type"] == "model.generate"
        business = model and op["purpose"] == "business"
        ticket = op["ticket"]
        _require(ticket in {None, "reserved", "consumed", "released"}, "operation.ticket")
        _require(business or ticket is None, "operation.ticket_scope")
        if ticket:
            _require((ticket == "reserved") != complete, "operation.ticket_state")
        if ticket in {"reserved", "consumed"}:
            counts["loops_" + ticket] += 1
        if ticket == "consumed":
            kind = "decision" if op["status"] == "succeeded" else "error_feedback"
            _require(op.get("consumption_kind") == kind, "operation.consumption_kind")
            _require(kind == "decision" or op["task_id"] != root, "operation.feedback_scope")
        if op["permit"]:
            _require(not business or ticket is not None, "operation.ticket_missing")
            _require(op["request_hash"] == digest(op["request"]), "operation.request_hash")
        if op["target_ref"]:
            _require(
                op["event_type"] == "executor.delegate"
                and tasks[op["target_ref"]]["parent_operation"] == op_id,
                "operation.target_ref",
            )
        attempts = op["attempts"]
        _require(not attempts or op["permit"], "attempt.permit")
        _require(len(attempts) <= state["policy"]["retry"]["max_attempts"], "attempt.limit")
        for index, attempt in enumerate(attempts, 1):
            expected_id = str(uuid5(NAMESPACE_URL, op_id + ":attempt:" + str(index)))
            _require(
                attempt["attempt_no"] == index and attempt["attempt_id"] == expected_id,
                "attempt.identity",
            )
            _require(
                attempt["status"] in {"started", "succeeded", "failed", "unknown", "cancelled"},
                "attempt.status",
            )
            if attempt["status"] == "started":
                _require(
                    index == len(attempts) and not complete and op["raw"] is None,
                    "attempt.unfinished",
                )
            key = (
                (
                    "compaction_model_attempts"
                    if op["purpose"] == "compaction"
                    else "business_model_attempts"
                )
                if model
                else "tool_attempts"
            )
            counts[key] += 1
            if model:
                model_attempts += 1
                result = (attempt.get("result") or {}).get("result")
                material = (result.get("usage") or {}) if isinstance(result, dict) else {}
                for key in TOKEN_KEYS:
                    _require(
                        material.get(key) is None
                        or (type(material[key]) is int and 0 <= material[key] <= MAX_JSON_INTEGER),
                        "attempt.usage." + key,
                    )
                full = all(type(material.get(k)) is int for k in TOKEN_KEYS)
                # Old checkpoints only accounted attempts with both counters known.
                accounted = attempt.get("usage_accounted", full)
                if accounted:
                    for key in TOKEN_KEYS:
                        if material.get(key) is not None:
                            tokens[key] = tokens.get(key, 0) + material[key]
                    known += int(full)
    for key, value in counts.items():
        _require(type(usage[key]) is int and usage[key] == value, "usage." + key)
    _require(
        counts["loops_reserved"] + counts["loops_consumed"] <= state["policy"]["loop_max"],
        "usage.loop_max",
    )
    for key in TOKEN_KEYS:
        _require(
            (usage[key] is None or type(usage[key]) is int and 0 <= usage[key] <= MAX_JSON_INTEGER)
            and usage[key] == tokens.get(key),
            "usage." + key,
        )
    _require(state.get("token_usage_known_attempts", 0) == known, "usage.known_attempts")
    _require(
        type(usage["token_counts_complete"]) is bool
        and usage["token_counts_complete"] == (model_attempts > 0 and known == model_attempts),
        "usage.token_counts_complete",
    )
    for seq, event in enumerate(state["events"], 1):
        _require(event["seq"] == seq and event["run_id"] == state["run_id"], "event.sequence")
