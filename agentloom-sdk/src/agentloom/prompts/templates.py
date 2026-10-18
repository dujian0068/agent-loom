"""Versioned LangChain adaptations; the immutable registry contains no runtime state."""

from types import MappingProxyType

from .protocol import INSTRUCTIONS as COMMON
from .roles.executor import INSTRUCTIONS as EXECUTOR
from .roles.planner import DELEGATION, LOCAL_EXECUTION
from .roles.planner import INSTRUCTIONS as PLANNER
from .roles.react import INSTRUCTIONS as REACT

__all__ = ["COMMON", "DELEGATION", "LOCAL_EXECUTION", "ROLES", "TEMPLATE_VERSION"]

TEMPLATE_VERSION = "agentloom-langchain-modes-v2"

ROLES = MappingProxyType({"react": REACT, "planner": PLANNER, "executor": EXECUTOR})
