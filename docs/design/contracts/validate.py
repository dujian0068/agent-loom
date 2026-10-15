"""Check SD-3 design projections and cross-record examples; no runtime or I/O tests."""
from copy import deepcopy
import json
from datetime import datetime
from pathlib import Path

from jsonschema import Draft202012Validator, ValidationError, FormatChecker

BASE = Path(__file__).parent
SCHEMA = json.loads((BASE / "contracts.schema.json").read_text())
EXAMPLES = json.loads((BASE / "examples.json").read_text())
Draft202012Validator.check_schema(SCHEMA)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def validator(contract):
    return Draft202012Validator({
        "$schema": SCHEMA["$schema"], "$defs": SCHEMA["$defs"],
        "$ref": f"#/$defs/{contract}",
    }, format_checker=FormatChecker())


def validate_bundle(bundle):
    require(bundle["design_version"] == "SD-3", "Design version must be explicit")
    fixtures = bundle["fixtures"]
    require(len({f["name"] for f in fixtures}) == len(fixtures), "Fixture names are unique")
    for f in fixtures:
        validator(f["contract"]).validate(f["value"])

    def records(contract):
        return [f["value"] for f in fixtures if f["contract"] == contract]

    def index(contract, key):
        values = records(contract)
        require(len({v[key] for v in values}) == len(values), f"Duplicate {contract} identity")
        return {v[key]: v for v in values}

    definitions = index("AgentDefinition", "definition_id")
    graphs = index("GraphDefinition", "graph_id")
    releases = index("AgentRelease", "release_id")
    workflows = index("WorkflowRelease", "release_id")
    all_releases = {**releases, **workflows}
    runs = index("Run", "run_id")
    invocations = index("AgentInvocation", "invocation_id")
    executions = index("GraphExecution", "graph_execution_id")
    tasks = index("NodeTask", "node_task_id")
    continuations = index("ReactContinuation", "node_task_id")
    operations = index("Operation", "operation_id")
    waits = index("WaitGroup", "wait_id")
    node_checkpoints = index("NodeCheckpoint", "node_checkpoint_id")
    checkpoints = index("GraphCheckpoint", "checkpoint_id")

    for definition in definitions.values():
        roles = definition["roles"]
        require(len({r["role_key"] for r in roles}) == len(roles), "Role keys are unique")
        kinds = [r["kind"] for r in roles]
        if definition["mode"] == "react":
            require(kinds == ["react"], "ReAct contains one role and no Executor")
        else:
            require(kinds.count("planner") == 1 and set(kinds) <= {"planner", "executor"},
                    "Plan contains exactly one Planner and optional Executors")

    nodes_by_graph = {}
    for graph in graphs.values():
        require(graph["engine_kind"] == ("agent" if graph["origin"] == "generated_agent" else "workflow"),
                "Graph origin and engine kind must agree")
        nodes = {n["node_key"]: n for n in graph["nodes"]}
        nodes_by_graph[graph["graph_id"]] = nodes
        require(len(nodes) == len(graph["nodes"]), "Node keys are unique within a graph")
        require(graph["entry_nodes"] and set(graph["entry_nodes"]) <= nodes.keys(), "Invalid graph entry")
        require(len({r["route_key"] for r in graph["routes"]}) == len(graph["routes"]), "Duplicate route key")
        for node in nodes.values():
            require(set(node["allowed_dispatch_targets"]) <= nodes.keys(), "Unknown dispatch target")
            if node["node_type"] in {"react", "executor"}:
                require(not node["allowed_dispatch_targets"], "ReAct and Executor cannot delegate")
        for route in graph["routes"]:
            require(route["source_node_key"] in nodes and set(route["targets"]) <= nodes.keys(), "Unknown route node")
            require(bool(route["targets"]) or route["kind"] == "branch_exit", "Route targets must be explicit")
            if route["kind"] == "dispatch":
                allowed = nodes[route["source_node_key"]]["allowed_dispatch_targets"]
                require(set(route["targets"]) <= set(allowed), "Dispatch exceeds allowed target set")
                require(route["completion_policy"] == "return_to_owner", "Dispatch resumes original owner")
            elif route["kind"] == "fork":
                require(route["completion_policy"] == "activate_branches", "Fork creates branch members")
            elif route["kind"] == "branch_exit":
                require(not route["targets"] and route["completion_policy"] == "close_branch", "Branch exit closes without a node")
            else:
                require(route["completion_policy"] == "activate_target", "Ordinary route activates successor")
        routes = {r["route_key"]: r for r in graph["routes"]}
        require(len({j["join_key"] for j in graph["joins"]}) == len(graph["joins"]), "Duplicate Join key")
        for join in graph["joins"]:
            require(all(target is None or target in nodes for target in [join["success_target_node_key"],join["error_target_node_key"]]), "Unknown Join successor")
            if join["member_scope"] == "branch_scope":
                require(not join["resume_owner"], "Branch Join does not resume completed fork task")
                require(join["fork_route_key"] in routes and routes[join["fork_route_key"]]["kind"] == "fork", "Join requires a fork route")
                require(set(join["region_node_keys"]) <= nodes.keys(), "Unknown branch region node")
                require(join["branch_exit_routes"] and all(k in routes and routes[k]["kind"] == "branch_exit" for k in join["branch_exit_routes"]), "Join requires explicit branch exits")
        for loop in graph["loops"]:
            require(loop["header_node_key"] in loop["body_node_keys"] and set(loop["body_node_keys"]) <= nodes.keys(), "Invalid loop region")
            require(loop["back_route_keys"] and set(loop["back_route_keys"] + loop["exit_route_keys"]) <= routes.keys(), "Unknown loop route")

    for release in all_releases.values():
        graph_ref = release["graph_definition_ref"]
        graph = graphs[graph_ref["id"]]
        require(graph["version"] == graph_ref["version"], "Release must pin graph version")
        nodes = nodes_by_graph[graph["graph_id"]]
        bindings = release["node_bindings"]
        require(len(bindings) == len(nodes) and {b["node_key"] for b in bindings} == nodes.keys(),
                "Every node has exactly one binding")
        roles = {}
        if release["release_id"] in releases:
            ref = release["agent_definition_ref"]
            definition = definitions[ref["id"]]
            require(definition["version"] == ref["version"] and definition["agent_id"] == release["agent_id"],
                    "Agent release definition identity mismatch")
            roles = {r["role_key"]: r for r in definition["roles"]}
        for binding in bindings:
            node = nodes[binding["node_key"]]
            node_type = node["node_type"]
            kind = "role" if node_type in {"react", "planner", "executor"} else (
                "capability" if node_type == "capability" else "agent_release")
            require(binding["target_kind"] == kind, "Node and binding kinds disagree")
            if kind == "role":
                require(binding["target_ref"] in roles and roles[binding["target_ref"]]["kind"] == node_type,
                        "Node and role kinds disagree")
            elif kind == "agent_release":
                require(binding["target_ref"] in releases, "AgentNode must pin an existing AgentRelease")

    for run in runs.values():
        require(datetime.fromisoformat(run["deadline_at"]).tzinfo is not None, "Deadline must carry timezone")
        require(run["loops_consumed"] + run["loops_reserved"] <= run["loop_limit"], "Run budget exceeded")
        require(run["release_ref"] in all_releases, "Unknown Run release")
        expected_kind = "agent_session" if run["release_ref"] in releases else "workflow_session"
        require(run["conversation_ref"]["kind"] == expected_kind, "Wrong conversation domain")
        root_id = run["root_graph_execution_id"]
        if root_id is not None:
            root = executions[root_id]
            require(root["run_id"] == run["run_id"] and root["parent_node_task_id"] is None,
                    "Invalid Run root graph")
            require(root["graph_definition_ref"] == all_releases[run["release_ref"]]["graph_definition_ref"],
                    "Root graph must match pinned release")

    for execution in executions.values():
        require(execution["run_id"] in runs, "Graph must belong to a Run")
        graph_ref = execution["graph_definition_ref"]
        require(graphs[graph_ref["id"]]["version"] == graph_ref["version"], "Graph version mismatch")
        require(execution["engine_kind"] == graphs[graph_ref["id"]]["engine_kind"],
                "Execution cannot switch graph engine")
        if execution["invocation_id"] is not None:
            invocation = invocations[execution["invocation_id"]]
            require(invocation["graph_execution_id"] == execution["graph_execution_id"], "Invocation/graph mismatch")
        if execution["parent_node_task_id"] is not None:
            parent = tasks[execution["parent_node_task_id"]]
            require(parent["run_id"] == execution["run_id"] and parent["graph_execution_id"] != execution["graph_execution_id"],
                    "Child graph requires an external parent in the same Run")

    for invocation in invocations.values():
        require(datetime.fromisoformat(invocation["started_at"]).tzinfo is not None, "Invocation creation must carry timezone")
        release = releases[invocation["release_id"]]
        graph = executions[invocation["graph_execution_id"]]
        require(invocation["agent_id"] == release["agent_id"] and invocation["run_id"] == graph["run_id"],
                "Invocation identity mismatch")
        require(graph["compiled_graph_hash"] == release["compiled_graph_hash"] and
                graph["graph_definition_ref"] == release["graph_definition_ref"], "Invocation graph release drift")
        require(graph["parent_node_task_id"] == invocation["parent_node_task_id"], "Parent reference mismatch")
        if invocation["budget_scope"] == "run":
            require(invocation["parent_node_task_id"] is None, "Only direct invocation aliases root budget")
            require(invocation["local_loop_limit"] is None and invocation["local_deadline_at"] is None, "Root alias must not add a second budget")
        else:
            require(invocation["parent_node_task_id"] is not None and invocation["local_loop_limit"] is not None and invocation["local_deadline_at"] is not None, "Workflow invocation requires a local budget")
            require(datetime.fromisoformat(invocation["local_deadline_at"]) > datetime.fromisoformat(invocation["started_at"]), "Local deadline is fixed after invocation creation")
        require((invocation["control_state"] == "terminal") == (invocation["terminal_reason"] is not None), "Invocation terminal reason mismatch")

    for workspace in records("WorkspaceScope"):
        run = runs[workspace["run_id"]]
        owner = workspace["owner_ref"]
        partition = workspace["partition_kind"]
        if partition == "workflow_session":
            require(owner["kind"] == "workflow" and workspace["invocation_id"] is None, "Workflow work does not require an Agent")
            require(run["conversation_ref"] == {"kind":"workflow_session","id":workspace["partition_id"]}, "Workspace must match workflow conversation")
        else:
            invocation = invocations[workspace["invocation_id"]]
            require(owner["kind"] == "agent" and owner["id"] == invocation["agent_id"] and invocation["run_id"] == workspace["run_id"], "Workspace must match Agent invocation owner")
            require(invocation["workspace_scope_ref"] == workspace["workspace_scope_id"], "Invocation must reference its assigned scope")
            if partition == "invocation":
                require(workspace["partition_id"] == invocation["invocation_id"] and invocation["parent_node_task_id"] is not None, "Input-only work uses invocation partition")
            else:
                require(invocation["parent_node_task_id"] is None and run["conversation_ref"] == {"kind":"agent_session","id":workspace["partition_id"]}, "Agent Session work cannot impersonate a workflow session")

    context_ids = set()
    for task in tasks.values():
        graph = executions[task["graph_execution_id"]]
        require(task["run_id"] == graph["run_id"], "Task cannot cross Run")
        node = nodes_by_graph[graph["graph_definition_ref"]["id"]][task["node_key"]]
        parent_id = task["parent_task_id"]
        if parent_id is not None:
            require(tasks[parent_id]["graph_execution_id"] == task["graph_execution_id"],
                    "Executor task stays in the same graph")
        continuation = continuations.get(task["node_task_id"])
        if continuation:
            require(node["node_type"] in {"react", "planner", "executor"}, "Only React nodes own RoleContext")
            require(continuation["context_id"] not in context_ids, "Role contexts must be private")
            context_ids.add(continuation["context_id"])
            if continuation["phase"] == "call_model":
                model_id = continuation["model_operation_id"]
                require(model_id in operations, "call_model must retain stable model operation")
                require(operations[model_id]["kind"] == "model" and
                        operations[model_id]["node_task_id"] == task["node_task_id"],
                        "Model operation belongs to the same role task")
            if task["status"] == "succeeded":
                require(continuation["phase"] == "completed", "Successful React task must be completed")
    require(continuations.keys() <= tasks.keys(), "Continuation must reference its NodeTask")

    for operation in operations.values():
        require(operation["run_id"] in runs, "Unknown operation Run")
        if operation["node_task_id"] is not None:
            require(tasks[operation["node_task_id"]]["run_id"] == operation["run_id"], "Operation task Run mismatch")
        phase = operation["processing_phase"]
        if phase in {"executing", "raw_saved", "response_hooks"}:
            require(operation["effective_request_ref"] is not None, "Executing call requires effective request")
        if phase in {"raw_saved", "response_hooks"}:
            require(operation["raw_result_ref"] is not None, "Raw result must survive post-processing")
        require((operation["final_response_ref"] is not None) == (phase == "completed"), "Only completed operation has final response")
        if phase == "completed":
            require(operation["status"] in {"succeeded", "failed", "blocked"}, "Completed operation needs terminal status")
        else:
            require(operation["status"] == "pending", "Non-completed operation must remain pending")

    for response in records("EventResponse"):
        if response["status"] == "succeeded":
            require(response["error_code"] is None, "Successful response cannot carry error")
        else:
            require(response["error_code"] is not None, "Failure must explain outcome")
        if response["status"] == "blocked":
            require(response["error_code"] == "HOOK_DENIED", "Hook denial is not transport failure")

    for wait in waits.values():
        owner = tasks[wait["owner_task_id"]]
        members = wait["members"]
        require(len({m["member_id"] for m in members}) == len(members), "Duplicate wait member")
        require(len({m["ordinal"] for m in members}) == len(members), "Duplicate wait ordinal")
        for member in members:
            target, result_id = member["target_ref"], member["result_operation_id"]
            local_outcome = member["local_outcome"]
            if wait["member_scope"] == "branch_scope":
                branch = member["branch_data"]
                require(branch is not None and target is None and result_id is None and local_outcome is None,
                        "Branch member closes through branch state, not a fake operation")
                require(wait["base_state_revision"] is not None and wait["join_key"] is not None, "Branch needs frozen state and Join identity")
                require((member["state"] == "settled") == (branch["terminal_reason"] is not None), "Only branch closure settles member")
                if branch["cursor_task_id"] is not None:
                    task = tasks[branch["cursor_task_id"]]
                    require(task["graph_execution_id"] == owner["graph_execution_id"] and task["branch_ref"]["wait_id"] == wait["wait_id"] and task["branch_ref"]["member_id"] == member["member_id"], "Branch cursor ownership mismatch")
                    require(member["state"] != "settled", "Closed branch cannot keep live cursor")
                continue
            require(member["branch_data"] is None, "Dispatch member must not also be a branch")
            if local_outcome is not None:
                require(member["state"] == "settled" and target is None and result_id is None,
                        "Pre-begin rejection/cancellation settles without a target or operation")
                continue
            if member["state"] == "queued":
                require(target is None and result_id is None, "Queued members must not start eagerly")
                continue
            if member["state"] == "admitting":
                # T_MEMBER_ADMIT can commit before T_BEGIN creates the Operation.
                if result_id is None:
                    require(target is None, "Pre-begin admission cannot have a target")
                    continue
                require(target == {"kind": "operation", "id": result_id},
                        "Admission after begin references the Operation, not an unstarted child")
            require(target is not None and result_id in operations, "Admitted member requires stable references")
            operation = operations[result_id]
            require(operation["node_task_id"] == owner["node_task_id"], "Parent operation owns result pipeline")
            targets = {"operation": operations, "node_task": tasks, "child_graph": executions}
            selected = targets[target["kind"]][target["id"]]
            require(selected["run_id"] == owner["run_id"], "Wait target cannot cross Run")
            if target["kind"] == "operation":
                require(target["id"] == result_id, "Operation target must be the member's result pipeline")
            if member["state"] == "settled":
                require(operation["processing_phase"] == "completed" and operation["final_response_ref"] is not None,
                        "Child completion cannot bypass parent's after-Hook pipeline")
        if wait["delivery_state"] in {"settled", "consumed"}:
            require(all(m["state"] == "settled" for m in members), "Join requires all members settled")

    for checkpoint in checkpoints.values():
        graph = executions[checkpoint["graph_execution_id"]]
        require(checkpoint["covered_commit_seq"] <= graph["scope_commit_seq"], "Snapshot cannot cover future commits")
        require(checkpoint["compiled_graph_hash"] == graph["compiled_graph_hash"], "Checkpoint compiled identity drift")
        require(checkpoint["engine_kind"] == graph["engine_kind"] and
                checkpoint["engine_protocol_version"] == graph["engine_protocol_version"],
                "Checkpoint must use execution's engine and protocol")
        state_ref = checkpoint["engine_state_ref"]
        if checkpoint["engine_kind"] != "agent" or state_ref["kind"] != "inline":
            # The projection checks metadata only; adapters must decode/version-check real payloads.
            continue
        payload = state_ref["value"]
        validator("AgentCheckpointPayload").validate(payload)
        for task_id in payload["ready_task_ids"]:
            require(tasks[task_id]["graph_execution_id"] == graph["graph_execution_id"], "Ready task crosses graph")
        for ref in payload["node_checkpoint_refs"]:
            node_checkpoint = node_checkpoints[ref]
            task = tasks[node_checkpoint["node_task_id"]]
            require(task["graph_execution_id"] == graph["graph_execution_id"], "Node checkpoint crosses graph")
            require(node_checkpoint["scope_commit_seq"] <= checkpoint["covered_commit_seq"], "Node snapshot exceeds graph boundary")
            require(node_checkpoint["node_revision"] <= task["revision"], "Node snapshot revision exceeds head")
        for ref in payload["wait_refs"]:
            require(tasks[waits[ref]["owner_task_id"]]["graph_execution_id"] == graph["graph_execution_id"], "Wait crosses graph")


def value(bundle, name):
    return next(f["value"] for f in bundle["fixtures"] if f["name"] == name)


validate_bundle(EXAMPLES)
positive_count = 0

# Queued members retain identity/ordinal without creating work before admission.
queued = deepcopy(EXAMPLES)
value(queued, "wait")["members"][1].update(state="queued", target_ref=None, result_operation_id=None)
validate_bundle(queued)
positive_count += 1

# The two recovery cuts around begin must be representable without a fake child.
admitting_before_begin = deepcopy(EXAMPLES)
value(admitting_before_begin, "wait")["members"].append({
    "member_id": "M3", "ordinal": 2, "state": "admitting",
    "target_ref": None, "result_operation_id": None,
    "branch_data": None, "local_outcome": None,
})
validate_bundle(admitting_before_begin)
positive_count += 1

admitting_after_begin = deepcopy(admitting_before_begin)
pending_operation = deepcopy(value(admitting_after_begin, "delegate_raw"))
pending_operation.update(operation_id="O-ADMITTING", processing_phase="prepared", status="pending",
                         effective_request_ref=None, raw_result_ref=None, final_response_ref=None)
admitting_after_begin["fixtures"].append({
    "name": "admitting_operation", "contract": "Operation", "value": pending_operation,
})
value(admitting_after_begin, "wait")["members"][-1].update(
    target_ref={"kind": "operation", "id": "O-ADMITTING"}, result_operation_id="O-ADMITTING")
validate_bundle(admitting_after_begin)
positive_count += 1

# Pending covers every non-terminal processing phase, not only active attempts.
for phase in ("prepared", "request_hooks", "executing", "raw_saved", "response_hooks"):
    pending = deepcopy(EXAMPLES)
    value(pending, "delegate_raw").update(
        processing_phase=phase, status="pending",
        raw_result_ref="raw-result" if phase in {"raw_saved", "response_hooks"} else None,
        effective_request_ref=None if phase in {"prepared", "request_hooks"} else "effective-request")
    validate_bundle(pending)
    positive_count += 1

# Rejection and stop before begin settle without inventing an Operation/Executor.
for kind in ("rejected", "cancelled"):
    local = deepcopy(EXAMPLES)
    value(local, "wait")["members"].append({
        "member_id": "M3", "ordinal": 2, "state": "settled",
        "target_ref": None, "result_operation_id": None, "branch_data": None,
        "local_outcome": {"kind": kind, "error_code": "INVALID_ARGUMENT" if kind == "rejected" else "RUN_STOPPED", "error_ref": "M3-error"},
    })
    validate_bundle(local)
    positive_count += 1

# A pre-Hook denial has an Operation but never creates an Executor target.
pre_denied = deepcopy(EXAMPLES)
denied_operation = deepcopy(value(pre_denied, "delegate_raw"))
denied_operation.update(operation_id="O-DENIED", processing_phase="completed", status="blocked",
                        effective_request_ref=None, raw_result_ref=None,
                        final_response_ref="pre-denied-final")
pre_denied["fixtures"].append({"name": "pre_denied_operation", "contract": "Operation", "value": denied_operation})
value(pre_denied, "wait")["members"].append({
    "member_id": "M3", "ordinal": 2, "state": "settled",
    "target_ref": {"kind": "operation", "id": "O-DENIED"}, "result_operation_id": "O-DENIED",
    "branch_data": None, "local_outcome": None,
})
validate_bundle(pre_denied)
positive_count += 1

# Complete effective result, including after-Hook denial, may settle a member.
settled = deepcopy(EXAMPLES)
value(settled, "delegate_raw").update(processing_phase="completed", status="blocked", final_response_ref="blocked-final")
value(settled, "wait")["members"][0]["state"] = "settled"
validate_bundle(settled)
positive_count += 1

for status in ("succeeded", "failed"):
    completed = deepcopy(settled)
    value(completed, "delegate_raw").update(status=status, final_response_ref=f"{status}-final")
    validate_bundle(completed)
    positive_count += 1

last_loop = deepcopy(EXAMPLES)
value(last_loop, "run").update(loops_consumed=100, loops_reserved=0)
validate_bundle(last_loop)
positive_count += 1

# A workflow invocation has a local cap, while its root retains the shared cap.
workflow = deepcopy(EXAMPLES)
value(workflow, "run").update(release_ref="WR1", conversation_ref={"kind":"workflow_session","id":"WS1"}, root_graph_execution_id="WG1")
outer_graph = deepcopy(value(workflow, "graph"))
outer_graph.update(graph_execution_id="WG1", engine_kind="workflow", graph_definition_ref=deepcopy(value(workflow, "workflow_release")["graph_definition_ref"]), compiled_graph_hash="workflow-compiled", invocation_id=None, parent_node_task_id=None)
outer_task = deepcopy(value(workflow, "P1"))
outer_task.update(node_task_id="OUTER_AGENT", graph_execution_id="WG1", node_key="agent_step", parent_task_id=None, continuation_ref=None)
workflow["fixtures"] += [{"name":"outer_graph","contract":"GraphExecution","value":outer_graph}, {"name":"outer_agent","contract":"NodeTask","value":outer_task}]
value(workflow, "graph")["parent_node_task_id"] = "OUTER_AGENT"
value(workflow, "invocation").update(parent_node_task_id="OUTER_AGENT",budget_scope="invocation",local_loop_limit=5,local_deadline_at="2026-10-15T01:01:00Z")
value(workflow, "agent_workspace").update(partition_kind="invocation",partition_id="I1")
workflow["fixtures"].append({"name":"workflow_workspace","contract":"WorkspaceScope","value":{"workspace_scope_id":"W1/WS1/R1","owner_ref":{"kind":"workflow","id":"W1","space_id":"SPACE1"},"file_root_id":"W1_ROOT","partition_kind":"workflow_session","partition_id":"WS1","run_id":"R1","invocation_id":None,"persistent":False}})
validate_bundle(workflow)
positive_count += 1

invalid_count = 0


def reject(name, mutate, *, base=EXAMPLES):
    global invalid_count
    bundle = deepcopy(base)
    mutate(bundle)
    try:
        validate_bundle(bundle)
    except (ValidationError, ValueError, KeyError):
        invalid_count += 1
    else:
        raise AssertionError(f"Invalid case accepted: {name}")


for fixture in EXAMPLES["fixtures"]:
    fixture_name = fixture["name"]
    reject(f"unknown field: {fixture_name}",
           lambda b, n=fixture_name: value(b, n).update(unrecognized=True))
reject("executor model override", lambda b: value(b, "agent")["roles"][1].update(model_ref="other"))
reject("ReAct contains executor", lambda b: value(b, "agent").update(mode="react"))
reject("missing binding", lambda b: value(b, "agent_release")["node_bindings"].pop())
reject("role mismatch", lambda b: value(b, "agent_release")["node_bindings"][1].update(target_ref="main"))
reject("unknown node", lambda b: value(b, "plan_graph")["nodes"][0].update(node_type="magic"))
reject("bad route", lambda b: value(b, "plan_graph")["routes"][0].update(targets=["missing"]))
reject("wrong release graph version", lambda b: value(b, "agent_release")["graph_definition_ref"].update(version=2))
reject("workspace cannot change Agent owner", lambda b: value(b, "agent_workspace")["owner_ref"].update(id="A2"))
reject("workflow work needs workflow conversation", lambda b: value(b, "agent_workspace").update(partition_kind="workflow_session",invocation_id=None,owner_ref={"kind":"workflow","id":"W1","space_id":"SPACE1"}))
reject("invocation work is not an Agent Session alias", lambda b: value(b, "agent_workspace").update(partition_kind="invocation",partition_id="I1"))
reject("branch exit cannot activate a target", lambda b: value(b, "structured_graph")["routes"][2].update(targets=["after"]))
reject("Join must name fork route", lambda b: value(b, "structured_graph")["joins"][0].update(fork_route_key="a_next"))
reject("loop header must be in region", lambda b: value(b, "structured_graph")["loops"][0].update(header_node_key="end"))
reject("root invocation cannot add local budget", lambda b: value(b, "invocation").update(local_loop_limit=5))
reject("local budget needs workflow parent", lambda b: value(b, "invocation").update(budget_scope="invocation",local_loop_limit=5,local_deadline_at="2026-10-15T01:01:00Z"))
reject("invalid creation time", lambda b: value(b, "invocation").update(started_at="tomorrow"))
reject("terminal invocation needs reason", lambda b: value(b, "invocation").update(control_state="terminal"))
reject("dispatch cannot carry branch data", lambda b: value(b, "wait")["members"][0].update(branch_data={"entry_node_key":"research","cursor_task_id":"E1","next_task_sequence":1,"view_write_ids":[],"terminal_reason":None}))
reject("101st Loop", lambda b: value(b, "run").update(loops_consumed=100, loops_reserved=1))
reject("shared private context", lambda b: value(b, "continuation_E2").update(context_id="C2"))
reject("lost model recovery reference", lambda b: value(b, "continuation_E2").update(model_operation_id=None))
reject("child graph drift", lambda b: value(b, "E2").update(graph_execution_id="missing"))
reject("missing raw", lambda b: value(b, "delegate_raw").update(raw_result_ref=None))
reject("premature operation success", lambda b: value(b, "delegate_raw").update(status="succeeded"))
for status in ("prepared", "running", "cancelled", "unknown"):
    reject(f"legacy operation status: {status}", lambda b, s=status: value(b, "delegate_raw").update(status=s))
reject("legacy delegate kind", lambda b: value(b, "delegate_raw").update(kind="delegate"))
reject("blocked before completion", lambda b: value(b, "delegate_raw").update(status="blocked"))
reject("completed cannot remain pending", lambda b: value(b, "delegate_raw").update(
    processing_phase="completed", status="pending", final_response_ref="pending-final"))
reject("prepared cannot claim failure", lambda b: value(b, "delegate_raw").update(
    processing_phase="prepared", status="failed", raw_result_ref=None))
reject("admission cannot invent child before begin", lambda b: value(b, "wait")["members"][-1].update(
    target_ref={"kind": "node_task", "id": "E1"}), base=admitting_before_begin)
reject("begin cannot save only an operation reference", lambda b: value(b, "wait")["members"][-1].update(
    target_ref=None), base=admitting_after_begin)
reject("admission cannot reference an unstarted child", lambda b: value(b, "wait")["members"][-1].update(
    target_ref={"kind": "node_task", "id": "E1"}), base=admitting_after_begin)
reject("admission cannot reference missing operation", lambda b: value(b, "wait")["members"][-1].update(
    target_ref={"kind": "operation", "id": "missing"}, result_operation_id="missing"), base=admitting_after_begin)
reject("admitted requires begin to have committed", lambda b: value(b, "wait")["members"][-1].update(
    state="admitted"), base=admitting_before_begin)
reject("raw cannot settle delegate", lambda b: value(b, "wait")["members"][0].update(state="settled"))
reject("missing result pipeline", lambda b: value(b, "wait")["members"][0].update(result_operation_id=None))
reject("local outcome cannot bypass started operation", lambda b: value(b, "wait")["members"][0].update(state="settled",local_outcome={"kind":"rejected","error_code":"INVALID_ARGUMENT","error_ref":"bad"}))
reject("settled without outcome", lambda b: value(b, "wait")["members"][0].update(state="settled",target_ref=None,result_operation_id=None))
reject("queued cannot carry terminal result", lambda b: value(b, "wait")["members"][0].update(state="queued",target_ref=None,result_operation_id=None,local_outcome={"kind":"cancelled","error_code":"RUN_STOPPED","error_ref":"stopped"}))
reject("local outcome must explain failure", lambda b: value(b, "wait")["members"][0].update(state="settled",target_ref=None,result_operation_id=None,local_outcome={"kind":"rejected","error_code":"","error_ref":"bad"}))
reject("operation target must match result", lambda b: value(b, "wait")["members"][0].update(target_ref={"kind":"operation","id":value(b,"delegate_running")["operation_id"]}))
reject("join before all settled", lambda b: value(b, "wait").update(delivery_state="consumed"))
reject("final response pending", lambda b: value(b, "blocked_response").update(status="pending"))
reject("deny not network error", lambda b: value(b, "blocked_response").update(error_code="NETWORK_ERROR"))
reject("future snapshot", lambda b: value(b, "graph_checkpoint").update(covered_commit_seq=10))
reject("future node snapshot", lambda b: value(b, "node_checkpoint").update(scope_commit_seq=9))
reject("checkpoint compiled drift", lambda b: value(b, "graph_checkpoint").update(compiled_graph_hash="new"))
reject("execution switches engine", lambda b: value(b, "graph").update(engine_kind="workflow"))
reject("checkpoint uses other engine", lambda b: value(b, "graph_checkpoint").update(engine_kind="workflow"))
reject("checkpoint uses other protocol", lambda b: value(b, "graph_checkpoint").update(engine_protocol_version=2))
reject("internal payload contains workflow state", lambda b: value(b, "graph_checkpoint")["engine_state_ref"]["value"].update(channel_versions={}))

print(f"PASS: {len(EXAMPLES['fixtures'])} core fixtures, {positive_count} boundary variants; "
      f"{invalid_count} invalid cases rejected.")
print("Scope: partial design structures and in-memory cross-record invariants. "
      "No database transactions, Hook execution, scheduler, recovery or model calls were tested.")
