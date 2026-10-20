"""Check persisted execution facts before takeover can dispatch external work."""

from uuid import NAMESPACE_URL, uuid5

from ..configuration.json_documents import document_digest as digest
from ..configuration.json_values import MAX_JSON_INTEGER
from ..errors import InfrastructureError, RequestError
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
        _validate_graph(state)
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
    accepted_records = set()
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
            awaiting_consumption = (
                ticket == "reserved"
                and complete
                and tasks[op["task_id"]].get("continuation", {}).get("model_operation_id") == op_id
                and tasks[op["task_id"]]["status"] not in {"succeeded", "failed", "cancelled"}
                and (op["status"] == "succeeded" or op["task_id"] != root)
            )
            _require(
                (ticket == "reserved") != complete or awaiting_consumption, "operation.ticket_state"
            )
        if ticket in {"reserved", "consumed"}:
            counts["loops_" + ticket] += 1
        if ticket == "consumed":
            kind = "decision" if op["status"] == "succeeded" else "error_feedback"
            _require(op.get("consumption_kind") == kind, "operation.consumption_kind")
            _require(kind == "decision" or op["task_id"] != root, "operation.feedback_scope")
        if "accepted_record_id" in op:
            identity = (op["task_id"], op["accepted_record_id"])
            accepted = [
                r
                for r in tasks[op["task_id"]]["context"]["records"]
                if r["record_id"] == op["accepted_record_id"]
            ]
            _require(
                business
                and op["status"] == "succeeded"
                and ticket == "consumed"
                and identity not in accepted_records
                and len(accepted) == 1
                and accepted[0]["message"].get("role") == "assistant",
                "operation.accepted_record_id",
            )
            accepted_records.add(identity)
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


def _validate_graph(state):
    from ..nodes import PayloadRef
    from ..nodes.validation import validate_continuation
    from .batches import TASK_TERMINAL, executor_slots, satisfied

    graph = state.get("graph")
    if not graph:
        return  # pre-node-protocol v1 format has its explicit migration adapter
    if type(graph.get("protocol_version")) is not int or graph["protocol_version"] != 1:
        raise InfrastructureError("CHECKPOINT_VERSION_UNSUPPORTED")
    for task_id, task in state["tasks"].items():
        try:
            validate_continuation(task["continuation"], task, state=state)
            if task.get("resume_input_ref") is not None:
                PayloadRef.from_dict(task["resume_input_ref"], owner_id=task_id)
        except RequestError as exc:
            if exc.field_path == "node.protocol_version":
                raise InfrastructureError("NODE_PROTOCOL_VERSION_UNSUPPORTED") from exc
            _require(False, exc.field_path or "node.resume_input_ref")
        _require(type(task["node_revision"]) is int and task["node_revision"] >= 0, "node.revision")
        _require(
            type(task["activation_no"]) is int and task["activation_no"] >= 0, "node.activation_no"
        )
        if task["status"] == "waiting":
            _require(
                task["wait_condition"] is not None and task["resume_input_ref"] is not None,
                "node.wait",
            )
            satisfied(state, task["wait_condition"])
        if task["activation_id"]:
            activation = state.get("activations", {}).get(task["activation_id"])
            _require(
                activation is not None and activation["task_id"] == task_id,
                "node.activation_binding",
            )
        if task_id in state.get("task_results", {}):
            result = state["task_results"][task_id]
            _require(
                task["status"] in TASK_TERMINAL
                and result["status"] == task["status"]
                and digest(result["result"]) == digest(task["result"])
                and digest(result["error"]) == digest(task["error"]),
                "node.task_result",
            )
    _require(executor_slots(state) <= state["policy"]["executor_max_parallel"], "batch.capacity")
    for batch_id, batch in state.get("batches", {}).items():
        _require(batch_id == batch["batch_id"] and batch["generation"] == 1, "batch.identity")
        task = state["tasks"][batch["owner_task_id"]]
        decision = state["operations"][batch["decision_operation_id"]]
        _require(
            decision["task_id"] == task["task_id"]
            and decision["ticket"] == "consumed"
            and decision["consumption_kind"] == "decision",
            "batch.decision",
        )
        for ordinal, member in enumerate(batch["members"]):
            _require(member["ordinal"] == ordinal, "batch.ordinal")
            if member["operation_id"]:
                op = state["operations"][member["operation_id"]]
                _require(
                    op["task_id"] == task["task_id"]
                    and op.get("source_call_id") == member["call"]["call_id"],
                    "batch.operation_binding",
                )
                _require(
                    not op["target_ref"] or member["admission_token"] is None,
                    "batch.converted_token",
                )
            if member["status"] == "settled":
                _require(
                    member["response"] is not None and member["admission_token"] is None,
                    "batch.settlement",
                )
