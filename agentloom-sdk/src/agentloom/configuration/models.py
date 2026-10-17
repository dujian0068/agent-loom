"""Local, side-effect-free configuration validation."""

from __future__ import annotations

import copy
import math
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from ..errors import ConfigurationError, RequestError
from .json_values import FrozenDict, canonical, freeze, json_copy
from .schemas import check_schema, validate_schema


def number(value, name, low, high, *, integer=False, open_low=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigurationError("INVALID_CONFIGURATION", field_path=name)
    if (
        (integer and not isinstance(value, int))
        or value > high
        or (value <= low if open_low else value < low)
        or not math.isfinite(value)
    ):
        raise ConfigurationError("INVALID_CONFIGURATION", field_path=name)


@dataclass(frozen=True)
class RetryPolicy:
    enabled: bool = False
    max_attempts: int = 2
    delay_seconds: float = 2

    def __post_init__(self):
        if type(self.enabled) is not bool:
            raise ConfigurationError("INVALID_CONFIGURATION", field_path="retry.enabled")
        number(self.max_attempts, "retry.max_attempts", 1, 20, integer=True)
        number(self.delay_seconds, "retry.delay_seconds", 0, 300)


@dataclass(frozen=True)
class ExecutionPolicy:
    loop_max: int = 100
    run_timeout_seconds: float = 3600
    parallel_enabled: bool = False
    executor_max_parallel: int = 2
    model_first_output_timeout_seconds: float = 60
    model_stream_idle_timeout_seconds: float | None = None
    tool_timeout_seconds: float = 10
    retry: RetryPolicy = field(default_factory=RetryPolicy)

    def __post_init__(self):
        number(self.loop_max, "loop_max", 1, 100000, integer=True)
        number(self.executor_max_parallel, "executor_max_parallel", 1, 128, integer=True)
        for name, maximum in (
            ("run_timeout_seconds", 86400),
            ("model_first_output_timeout_seconds", 3600),
            ("tool_timeout_seconds", 3600),
            ("model_stream_idle_timeout_seconds", 3600),
        ):
            value = getattr(self, name)
            if value is not None or name != "model_stream_idle_timeout_seconds":
                number(value, name, 1, maximum)
        if type(self.parallel_enabled) is not bool or not isinstance(self.retry, RetryPolicy):
            raise ConfigurationError("INVALID_CONFIGURATION")


@dataclass(frozen=True)
class ContextPolicy:
    recent_runs: int = 2
    trigger_ratio: float = 0.8
    output_reserve_tokens: int | None = None
    safety_margin_tokens: int | None = None
    summary_target_tokens: int | None = None
    compaction_prompt_addendum: str = ""

    def __post_init__(self):
        number(self.recent_runs, "recent_runs", 0, 100, integer=True)
        number(self.trigger_ratio, "trigger_ratio", 0, 1, open_low=True)
        if self.trigger_ratio >= 1:
            raise ConfigurationError("INVALID_CONFIGURATION", field_path="trigger_ratio")
        for name in ("output_reserve_tokens", "safety_margin_tokens", "summary_target_tokens"):
            value = getattr(self, name)
            if value is not None:
                number(value, name, 0 if name == "safety_margin_tokens" else 1, 10**9, integer=True)
        if not isinstance(self.compaction_prompt_addendum, str):
            raise ConfigurationError("INVALID_CONFIGURATION")


@dataclass(frozen=True)
class ModelOptions:
    max_output_tokens: int | None = None
    parameters: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not isinstance(self.parameters, Mapping):
            raise ConfigurationError("MODEL_PARAMETERS_INVALID")
        if self.max_output_tokens is not None:
            number(self.max_output_tokens, "max_output_tokens", 1, 10**9, integer=True)
        object.__setattr__(self, "parameters", freeze(self.parameters))


@dataclass(frozen=True)
class CapabilityBinding:
    binding_key: str
    kind: str
    visible_name: str
    adapter_key: str
    member_key: str | None = None
    config: Mapping = field(default_factory=dict)
    access_scope: Mapping = field(default_factory=dict)

    def __post_init__(self):
        if self.kind not in {"tool", "builtin_tool", "mcp", "skill"}:
            raise ConfigurationError("CAPABILITY_KIND_INVALID")
        for name in ("binding_key", "visible_name", "adapter_key"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name).strip():
                raise ConfigurationError("CAPABILITY_KEY_INVALID", field_path=name)
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,63}", self.visible_name):
            raise ConfigurationError("CAPABILITY_VISIBLE_NAME_INVALID")
        if self.member_key is not None and (
            not isinstance(self.member_key, str) or not self.member_key
        ):
            raise ConfigurationError("CAPABILITY_MEMBER_INVALID")
        if not isinstance(self.config, Mapping) or not isinstance(self.access_scope, Mapping):
            raise ConfigurationError("CAPABILITY_CONFIG_INVALID")
        if set(self.access_scope) - {"paths", "read_only"}:
            raise ConfigurationError("ACCESS_SCOPE_UNSUPPORTED")
        if type(self.access_scope.get("read_only", False)) is not bool:
            raise ConfigurationError("ACCESS_SCOPE_INVALID")
        paths = self.access_scope.get("paths")
        if paths is not None:
            if not isinstance(paths, (list, tuple)):
                raise ConfigurationError("ACCESS_SCOPE_INVALID")
            for path in paths:
                if (
                    not isinstance(path, str)
                    or not path
                    or "\\" in path
                    or "\0" in path
                    or PurePosixPath(path).is_absolute()
                    or ".." in PurePosixPath(path).parts
                    or any(p.startswith(".agentloom") for p in PurePosixPath(path).parts)
                ):
                    raise ConfigurationError("ACCESS_SCOPE_INVALID")
        object.__setattr__(self, "config", freeze(self.config))
        object.__setattr__(self, "access_scope", freeze(self.access_scope))


@dataclass(frozen=True)
class VariableSpec:
    schema: Mapping
    default: Any = None
    required: bool = True

    def __post_init__(self):
        check_schema(self.schema)
        if type(self.required) is not bool:
            raise ConfigurationError("VARIABLE_SPEC_INVALID")
        if not self.required:
            try:
                validate_schema(self.default, self.schema)
            except RequestError as exc:
                raise ConfigurationError("VARIABLE_DEFAULT_INVALID") from exc
        object.__setattr__(self, "schema", freeze(self.schema))
        object.__setattr__(self, "default", freeze(self.default))


@dataclass(frozen=True)
class AgentDefinition:
    name: str
    prompt: str
    description: str = ""
    variables: Mapping = field(default_factory=dict)
    capabilities: tuple[CapabilityBinding, ...] = ()
    input_schema: dict | None = None
    output_schema: dict | None = None

    def __post_init__(self):
        if (
            not isinstance(self.name, str)
            or not self.name.strip()
            or not isinstance(self.prompt, str)
            or not isinstance(self.description, str)
        ):
            raise ConfigurationError("INVALID_DEFINITION")
        if not isinstance(self.capabilities, (tuple, list)) or any(
            not isinstance(binding, CapabilityBinding) for binding in self.capabilities
        ):
            raise ConfigurationError("CAPABILITY_BINDING_INVALID")
        if not isinstance(self.variables, Mapping) or any(
            not isinstance(k, str) or not re.fullmatch(r"[A-Za-z_]\w*", k) for k in self.variables
        ):
            raise ConfigurationError("INVALID_VARIABLES")
        object.__setattr__(
            self,
            "variables",
            FrozenDict(
                {
                    key: value if isinstance(value, VariableSpec) else freeze(value)
                    for key, value in self.variables.items()
                }
            ),
        )
        for name in ("input_schema", "output_schema"):
            object.__setattr__(self, name, freeze(getattr(self, name)))
        object.__setattr__(self, "capabilities", tuple(copy.deepcopy(self.capabilities)))
        for schema in (self.input_schema, self.output_schema):
            check_schema(schema)
        if not isinstance(self.variables, dict):
            raise ConfigurationError("INVALID_VARIABLES")
        for variable in re.findall(r"\{\{\s*([A-Za-z_]\w*)\s*\}\}", self.prompt):
            if variable not in self.variables:
                raise ConfigurationError("PROMPT_VARIABLE_UNDECLARED", field_path=variable)

    def render(self, supplied=None):
        supplied = {} if supplied is None else supplied
        if not isinstance(supplied, Mapping) or set(supplied) - set(self.variables):
            raise RequestError("VARIABLE_UNDECLARED")
        values = {}
        for key, specification in self.variables.items():
            if isinstance(specification, VariableSpec):
                if key not in supplied and specification.required:
                    raise RequestError("VARIABLE_REQUIRED", field_path=key)
                value = json_copy(supplied.get(key, specification.default))
                validate_schema(value, specification.schema, code="VARIABLE_TYPE_INVALID")
            else:
                value = json_copy(supplied.get(key, specification))
                default = json_copy(specification)
                expected = {
                    type(None): "null",
                    bool: "boolean",
                    int: "integer",
                    float: "number",
                    str: "string",
                    list: "array",
                    dict: "object",
                }[type(default)]
                validate_schema(value, {"type": expected}, code="VARIABLE_TYPE_INVALID")
            values[key] = value
        return re.sub(
            r"\{\{\s*([A-Za-z_]\w*)\s*\}\}",
            lambda m: values[m[1]] if isinstance(values[m[1]], str) else canonical(values[m[1]]),
            self.prompt,
        )


@dataclass(frozen=True)
class WorkspaceBinding:
    root: Path | str
    persistent_sandbox: bool = False

    def __post_init__(self):
        if not isinstance(self.root, (str, Path)) or not str(self.root) or "\0" in str(self.root):
            raise ConfigurationError("WORKSPACE_ROOT_INVALID")
        object.__setattr__(self, "root", Path(self.root).absolute())
        if type(self.persistent_sandbox) is not bool:
            raise ConfigurationError("INVALID_CONFIGURATION", field_path="persistent_sandbox")
