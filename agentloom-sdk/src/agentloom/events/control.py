"""Settle saved call Hook facts without invoking user code during root control."""

from ..errors import AgentLoomError
from .patches import apply_saved_hook


def _matches(binding, operation, stage):
    if binding["event_type"] != operation["event_type"] or binding["stage"] != stage:
        return False
    matcher = binding["matcher"]
    return all(
        not matcher.get(key) or value in matcher[key]
        for key, value in (
            ("role_keys", operation["role_key"]),
            ("purposes", operation["purpose"]),
            ("capability_binding_keys", operation.get("binding_key")),
        )
    )


def settle_saved_hooks(state, operation, reason):
    """Apply each saved decision once; return the authoritative deny stage, if any."""
    denied = None
    for stage in ("request", "response"):
        chain = sorted(
            (b for b in state["bindings"]["hooks"] if _matches(b, operation, stage)),
            key=lambda b: b["ordinal"],
        )
        stage_denied = operation["stages"].get(stage, {}).get("outcome") == "denied"
        for binding in chain:
            key = stage + ":" + binding["binding_key"]
            hook = operation["hooks"].get(key)
            if hook is None:
                hook = operation["hooks"][key] = {
                    "status": "prepared",
                    "result": None,
                    "diagnostic": None,
                }
            if hook["status"] == "result_saved":
                apply_saved_hook(state, operation["operation_id"], key)
                hook["execution_status"] = "completed"
            if hook["status"] == "started":
                hook.update(
                    status="indeterminate",
                    execution_status="unknown",
                    patch_status="none",
                    result=None,
                    diagnostic=AgentLoomError("HOOK_OUTCOME_UNKNOWN").to_dict(),
                    control_reason=reason,
                )
            elif hook["status"] == "prepared":
                hook.update(
                    status="applied",
                    execution_status="skipped_control",
                    patch_status="none",
                    result=None,
                    control_reason=reason,
                )
            if (hook.get("result") or {}).get("decision") == "deny":
                stage_denied = True
        if stage_denied:
            denied = denied or stage
            operation["stages"][stage] = {"outcome": "denied"}
        elif stage not in operation["stages"]:
            operation["stages"][stage] = {"outcome": "cancelled", "error": reason}
    return denied
