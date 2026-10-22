import hashlib
import json
from pathlib import Path

from sqlalchemy import func, select

from .persistence import Application, ChatSession, Release, Resource, ResourceVersion, RunEvent, Secret
from .schemas import Draft
from .security import Problem, require_role

GRAPH_VERSION = "platform-graph-v1"
TERMINAL = {"completed", "failed", "cancelled"}


def run_snapshot(s, run):
    if run.release_id:
        return s.get(Release, run.release_id).snapshot
    snapshot = s.get(ChatSession, run.session_id).preview_snapshot
    if not snapshot:
        raise RuntimeError("Run has neither a release nor a draft preview")
    return snapshot


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def implementation_hash():
    package = Path(__file__).parent
    files = [package.parents[1] / "requirements.lock", package / "runtime.py", package / "tools.py"]
    fingerprint = hashlib.sha256()
    for path in files:
        fingerprint.update(path.name.encode() + b"\0" + path.read_bytes() + b"\0")
    return fingerprint.hexdigest()


def get_app(s, actor_id, app_id, role="viewer"):
    app = s.get(Application, app_id)
    if not app:
        raise Problem(404, "NOT_FOUND", "应用不存在")
    member = require_role(s, actor_id, app.space_id, role)
    if member.role == "viewer" and not app.release_id:
        raise Problem(404, "NOT_FOUND", "应用不存在")
    return app


def app_view(s, app, include_draft=True):
    release = s.get(Release, app.release_id) if app.release_id else None
    result = {
        "id": app.id,
        "space_id": app.space_id,
        "name": app.name,
        "mode": app.mode,
        "revision": app.revision,
        "release_id": app.release_id,
        "release_version": release.version if release else None,
        "lifecycle": app.lifecycle,
        "description": app.draft["description"]
        if include_draft
        else (release.snapshot["draft"]["description"] if release else ""),
    }
    if include_draft:
        result["draft"] = app.draft
    return result


def resolve_binding(s, space_id, binding, allowed_kinds):
    resource = s.get(Resource, binding["resource_id"])
    version = s.scalars(
        select(ResourceVersion).where(
            ResourceVersion.resource_id == binding["resource_id"],
            ResourceVersion.version == binding["version"],
        )
    ).first()
    if not resource or resource.space_id != space_id or not version or resource.kind not in allowed_kinds:
        raise Problem(422, "INVALID_RESOURCE_REFERENCE", "资源引用不存在或不属于当前空间")
    return resource, version


def validate_references(s, app, draft):
    if app.mode == "react" and draft["executors"]:
        raise Problem(422, "REACT_EXECUTORS", "ReAct 应用不能配置专业执行器")
    if draft["model"]:
        resolve_binding(s, app.space_id, draft["model"], {"model"})
    for role in [draft["definition"], *draft["executors"].values()]:
        for binding in role["capabilities"]:
            resolve_binding(s, app.space_id, binding, {"tool", "skill", "mcp"})


def compile_snapshot(s, app, settings):
    draft = Draft.model_validate(app.draft).model_dump()
    validate_references(s, app, draft)
    violations, resources = [], {}

    def error(code, path, message):
        violations.append({"code": code, "path": path, "message": message})

    def resolve(binding, path, kinds):
        resource, version = resolve_binding(s, app.space_id, binding, kinds)
        if not resource.enabled or version.status != "ready":
            error("RESOURCE_NOT_READY", path, f"{resource.name} 尚不可用")
        key = f"{resource.id}:{version.version}"
        resources[key] = {
            "id": resource.id,
            "kind": resource.kind,
            "name": resource.name,
            "version": version.version,
            "description": resource.description,
            "spec": version.spec,
        }
        return resource, version

    if not draft["model"]:
        error("MODEL_REQUIRED", "model", "请选择模型连接")
    else:
        model, version = resolve(draft["model"], "model", {"model"})
        if version.spec["provider"] == "test":
            if not settings.allow_test_model:
                error("TEST_MODEL_DISABLED", "model", "测试模型未开启")
        elif not s.get(Secret, model.id):
            error("CREDENTIAL_REQUIRED", "model", "请先配置模型凭据")
    if not draft["definition"]["prompt"]:
        error("PROMPT_REQUIRED", "definition.prompt", "请填写系统提示词")
    if app.mode == "plan_executor" and not draft["executors"]:
        error("EXECUTOR_REQUIRED", "executors", "Plan 应用至少需要一位专业执行器")
    for role_key, role in [("definition", draft["definition"]), *draft["executors"].items()]:
        names, bound = set(), set()
        for index, binding in enumerate(role["capabilities"]):
            path = f"{role_key}.capabilities.{index}"
            resource, version = resolve(binding, path, {"tool", "skill", "mcp"})
            if resource.id in bound:
                error("DUPLICATE_BINDING", path, "同一角色不能重复选择资源")
            bound.add(resource.id)
            tool_names = []
            if resource.kind == "tool":
                tool_names = [version.spec["registry_key"]]
                if version.spec.get("writes") and draft["environment"]["workspace_access"] == "read_only":
                    error("WORKSPACE_READ_ONLY", path, f"{resource.name} 需要读写工作区")
            elif resource.kind == "mcp":
                available = {m["name"] for m in version.spec.get("members", [])}
                if not binding["members"] or not set(binding["members"]) <= available:
                    error("MCP_MEMBERS_REQUIRED", path, "请选择此固定版本内的 MCP 工具成员")
                tool_names = binding["members"]
            for name in tool_names:
                if name in names or name in {"read_skill", "plan_tasks", "Plan"}:
                    error("TOOL_NAME_CONFLICT", path, f"工具名称冲突：{name}")
                names.add(name)
        if role_key != "definition" and (not role["name"] or not role["prompt"]):
            error("EXECUTOR_INCOMPLETE", f"executors.{role_key}", "执行器需要名称与职责提示词")
    if draft["environment"]["sandbox_enabled"]:
        error("SANDBOX_UNAVAILABLE", "environment.sandbox_enabled", "执行沙箱尚未接入")
    snapshot = {
        "snapshot_version": 1,
        "graph_version": GRAPH_VERSION,
        "state_schema_version": 1,
        "implementation_hash": implementation_hash(),
        "mode": app.mode,
        "space_id": app.space_id,
        "draft": draft,
        "resources": resources,
    }
    return snapshot, sorted(violations, key=lambda v: (v["path"], v["code"]))


def append_event(s, run_id, kind, payload):
    seq = (s.scalar(select(func.max(RunEvent.seq)).where(RunEvent.run_id == run_id)) or 0) + 1
    s.add(RunEvent(run_id=run_id, seq=seq, kind=kind, payload=payload))
    s.flush()
    return seq
