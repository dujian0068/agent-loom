import asyncio
import hashlib
import json
from pathlib import PurePosixPath
from urllib.parse import urlsplit

import yaml
from cryptography.fernet import Fernet
from pydantic import ValidationError
from sqlalchemy import select

from .persistence import Resource, ResourceVersion, Secret
from .schemas import McpSpec, ModelSpec, SkillSpec, ToolSpec
from .security import Problem

TOOL_CATALOG = [
    ("read_file", "读取文件", "读取工作区内的 UTF-8 文本", "files", True, False),
    ("ls", "列出目录", "列出授权工作区中的文件", "files", True, False),
    ("stat", "查看文件信息", "查看文件类型与大小", "files", True, False),
    ("glob", "查找文件", "按路径模式查找文件", "files", True, False),
    ("grep", "搜索文件内容", "按文本搜索工作区文件", "files", True, False),
    ("write_file", "写入文件", "创建或覆盖工作区文件", "files", True, True),
    ("edit_file", "替换文本", "精确替换文件中的一处文本", "files", True, True),
    ("mkdir", "新建目录", "创建工作区目录", "files", True, True),
    ("move", "移动文件", "移动工作区内文件", "files", True, True),
    ("delete", "删除文件", "删除指定工作区文件", "files", True, True),
    ("patch", "应用文件补丁", "补丁适配器待实现", "files", False, True),
    ("write_todos", "任务清单", "更新当前角色的任务清单", "execution", True, False),
    ("exec", "执行命令与脚本", "沙箱服务尚未接入", "execution", False, True),
    ("task", "子 Agent 委派", "在 Plan 应用中配置专业执行器", "orchestration", False, False),
] + [
    (key, name, "外部连接适配器待接入", "integrations", False, False)
    for key, name in [
        ("web-search", "联网搜索"),
        ("web-extract", "网页正文提取"),
        ("browser", "浏览器操作"),
        ("sql-query", "数据库查询"),
        ("code-interpreter", "远端代码解释器"),
        ("gmail-search", "搜索邮件"),
        ("gmail-send", "发送邮件"),
        ("calendar-list", "读取日历"),
        ("calendar-create", "创建日历事件"),
        ("drive-search", "检索云盘文件"),
    ]
]
TOOL_MAP = {row[0]: row for row in TOOL_CATALOG}


def initialize_catalog(s, space_id):
    for key, name, description, category, ready, writes in TOOL_CATALOG:
        resource = Resource(space_id=space_id, kind="tool", name=name, description=description)
        s.add(resource)
        s.flush()
        s.add(
            ResourceVersion(
                resource_id=resource.id,
                version=1,
                status="ready" if ready else "unavailable",
                spec={"registry_key": key, "category": category, "writes": writes},
            )
        )


def latest_version(s, resource_id):
    return s.scalars(
        select(ResourceVersion)
        .where(ResourceVersion.resource_id == resource_id)
        .order_by(ResourceVersion.version.desc())
    ).first()


def resource_view(s, resource):
    versions = list(
        s.scalars(
            select(ResourceVersion)
            .where(ResourceVersion.resource_id == resource.id)
            .order_by(ResourceVersion.version.desc())
        )
    )
    return {
        "id": resource.id,
        "kind": resource.kind,
        "name": resource.name,
        "description": resource.description,
        "enabled": resource.enabled,
        "credential_configured": s.get(Secret, resource.id) is not None,
        "versions": [{"version": v.version, "status": v.status, "spec": v.spec} for v in versions],
    }


def validate_url(value):
    url = urlsplit(value)
    if (
        url.scheme not in ("http", "https")
        or not url.hostname
        or url.username
        or url.password
        or url.fragment
    ):
        raise Problem(422, "INVALID_ENDPOINT", "请输入不含认证信息的 HTTP(S) 地址")
    if url.hostname in ("169.254.169.254", "metadata.google.internal"):
        raise Problem(422, "INVALID_ENDPOINT", "不能使用实例元数据地址")


def validate_spec(kind, spec, settings):
    try:
        if kind == "tool":
            item = ToolSpec.model_validate(spec)
            row = TOOL_MAP.get(item.registry_key)
            if not row or not row[4]:
                raise Problem(422, "TOOL_UNAVAILABLE", "只能注册已实现的受信工具")
            return {**item.model_dump(), "category": row[3], "writes": row[5]}, "ready"
        if kind == "model":
            item = ModelSpec.model_validate(spec)
            validate_url(item.endpoint)
            if item.provider == "test" and not settings.allow_test_model:
                raise Problem(422, "TEST_MODEL_DISABLED", "部署未开启测试模型")
            return item.model_dump(), "ready"
        if kind == "mcp":
            item = McpSpec.model_validate(spec)
            validate_url(item.url)
            return item.model_dump(), "pending"
        if kind == "skill":
            item = SkillSpec.model_validate(spec)
            if "SKILL.md" not in item.files or sum(len(v.encode()) for v in item.files.values()) > 1_500_000:
                raise Problem(422, "INVALID_SKILL", "技能包必须包含 SKILL.md，总大小不能超过 1.5 MB")
            for name in item.files:
                path = PurePosixPath(name)
                if path.is_absolute() or ".." in path.parts or "\\" in name or str(path) != name:
                    raise Problem(422, "INVALID_SKILL_PATH", "技能包只能包含规范的相对文件路径")
            content = item.files["SKILL.md"]
            if not content.startswith("---\n"):
                raise Problem(422, "INVALID_SKILL", "SKILL.md 必须包含 YAML 元信息")
            try:
                metadata = yaml.safe_load(content.split("---", 2)[1])
            except yaml.YAMLError:
                raise Problem(422, "INVALID_SKILL", "SKILL.md 元信息不是有效 YAML")
            if not isinstance(metadata, dict) or not metadata.get("name") or not metadata.get("description"):
                raise Problem(422, "INVALID_SKILL", "技能元信息需要 name 和 description")
            if set(metadata) - {"name", "description", "license", "compatibility", "metadata"}:
                raise Problem(
                    422, "SKILL_PROFILE_UNSUPPORTED", "当前只支持说明与只读资源，不支持权限或执行扩展"
                )
            digest = hashlib.sha256(json.dumps(item.files, sort_keys=True).encode()).hexdigest()
            return {**item.model_dump(), "digest": digest, "metadata": metadata}, "ready"
    except ValidationError as exc:
        raise Problem(
            422,
            "INVALID_RESOURCE",
            "资源配置格式错误",
            [
                {"path": ".".join(map(str, e["loc"])), "message": e["msg"], "code": e["type"]}
                for e in exc.errors()
            ],
        )
    raise Problem(422, "INVALID_KIND", "未知资源类型")


def decrypt_secret(s, settings, resource_id):
    secret = s.get(Secret, resource_id)
    return Fernet(settings.secret_key.encode()).decrypt(secret.ciphertext.encode()).decode() if secret else ""


async def discover_mcp(spec, secret):
    import re

    from jsonschema import Draft202012Validator
    from langchain_mcp_adapters.client import MultiServerMCPClient

    connection = {"url": spec["url"], "transport": "http"}
    if secret:
        connection["headers"] = {"Authorization": f"Bearer {secret}"}
    client = MultiServerMCPClient({"service": connection})
    tools = await asyncio.wait_for(client.get_tools(), timeout=20)
    if len(tools) > 100:
        raise Problem(422, "MCP_TOO_MANY_TOOLS", "此连接暴露超过 100 个工具")
    members = [
        {
            "name": tool.name,
            "description": tool.description,
            "schema": tool.args_schema
            if isinstance(tool.args_schema, dict)
            else tool.args_schema.model_json_schema(),
        }
        for tool in tools
    ]
    names = set()
    for member in members:
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", member["name"]) or member["name"] in names:
            raise Problem(422, "MCP_TOOL_NAME_INVALID", "MCP 工具名称重复或不符合模型工具命名要求")
        names.add(member["name"])
        schema = member["schema"]
        Draft202012Validator.check_schema(schema)
        pending = [schema]
        while pending:
            item = pending.pop()
            if isinstance(item, dict):
                if "$ref" in item and not item["$ref"].startswith("#"):
                    raise Problem(422, "MCP_REMOTE_SCHEMA", "工具 Schema 只能引用自身定义")
                pending.extend(item.values())
            elif isinstance(item, list):
                pending.extend(item)
    return members
