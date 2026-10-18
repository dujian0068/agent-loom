"""Executor context adapted from experimental plan_and_execute (MIT).

The delegated user message supplies the current objective and any previous
steps; the SDK's private messages supply the scratchpad. ReAct is shared with
the standalone role. Source revision and adaptations are in ../NOTICE.md.
"""

from .react import CYCLE

INSTRUCTIONS = (
    "Mode: Executor. Complete the current objective in the delegated input. "
    "Previous steps are only those explicitly supplied there; you cannot see the "
    "Planner's conversation. Do not delegate tasks.\n\n" + CYCLE + "\n\n"
    "Return the completed deliverable to the Planner with relevant evidence and "
    "any unresolved limits. If the task cannot be completed, report what failed "
    "and what is still needed."
)
