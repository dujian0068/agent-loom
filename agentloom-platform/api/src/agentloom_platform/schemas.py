from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

Password = Annotated[str, StringConstraints(strip_whitespace=False)]


class DTO(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Login(DTO):
    username: str = Field(min_length=1, max_length=100)
    password: Password = Field(min_length=1, max_length=256)


class AgentCreate(DTO):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=2000)
    mode: Literal["react", "plan_executor"]


class Binding(DTO):
    resource_id: str
    version: int = Field(ge=1)
    members: list[str] = Field(default_factory=list, max_length=100)


class Definition(DTO):
    name: str = Field(default="", max_length=100)
    description: str = Field(default="", max_length=2000)
    prompt: str = Field(default="", max_length=30000)
    capabilities: list[Binding] = Field(default_factory=list, max_length=100)


class Environment(DTO):
    sandbox_enabled: bool = False
    workspace_access: Literal["read_only", "read_write"] = "read_write"
    retention: Literal["run", "session"] = "run"


class ExecutionPolicy(DTO):
    model_calls: int = Field(default=20, ge=1, le=100)
    tool_calls: int = Field(default=50, ge=1, le=200)
    timeout_seconds: int = Field(default=300, ge=10, le=1800)
    max_tasks: int = Field(default=5, ge=1, le=20)


class Draft(DTO):
    description: str = Field(default="", max_length=2000)
    model: Binding | None = None
    definition: Definition = Field(default_factory=Definition)
    executors: dict[str, Definition] = Field(default_factory=dict, max_length=10)
    environment: Environment = Field(default_factory=Environment)
    execution: ExecutionPolicy = Field(default_factory=ExecutionPolicy)

    @field_validator("executors")
    @classmethod
    def executor_keys(cls, value):
        import re

        if any(
            not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,63}", k)
            or k.startswith(("planner", "main", "system"))
            for k in value
        ):
            raise ValueError("Executor key 格式错误或使用了系统保留前缀")
        return value


class Revision(DTO):
    draft_revision: int = Field(ge=1)


class ResourceCreate(DTO):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=2000)
    spec: dict
    credential: Password | None = Field(default=None, min_length=1, max_length=8192, repr=False)


class ModelCatalogRequest(DTO):
    endpoint: str = Field(min_length=1, max_length=2048)
    resource_id: str | None = None
    credential: Password | None = Field(default=None, min_length=1, max_length=8192, repr=False)


class ToolSpec(DTO):
    registry_key: str = Field(min_length=1, max_length=100)


class SkillSpec(DTO):
    files: dict[str, str] = Field(max_length=100)


class ModelSpec(DTO):
    provider: Literal["openai", "test"] = "openai"
    endpoint: str = Field(default="https://api.openai.com/v1", min_length=1, max_length=2048)
    model_id: str = Field(min_length=1, max_length=100)
    max_output_tokens: int = Field(default=4096, ge=128, le=32768)

    @field_validator("model_id")
    @classmethod
    def printable_model_id(cls, value):
        if not value.isprintable():
            raise ValueError("模型 ID 不能包含控制字符或无效 Unicode")
        return value


class McpSpec(DTO):
    url: str
    transport: Literal["http"] = "http"


class DiscoverySelection(DTO):
    discovery_version: int = Field(ge=1)
    members: list[str] = Field(min_length=1, max_length=100)


class Credential(DTO):
    secret: Password = Field(min_length=1, max_length=8192)


class UserInput(DTO):
    client_message_id: str = Field(min_length=1, max_length=100)
    content: str = Field(min_length=1, max_length=30000)


class MemberCreate(DTO):
    username: str = Field(min_length=1, max_length=100)
    password: Password = Field(min_length=12, max_length=256)
    role: Literal["viewer", "editor", "admin"]


class MemberRole(DTO):
    role: Literal["viewer", "editor", "admin"]
