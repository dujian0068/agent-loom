"""Planner/replanner adapted from LangGraph's Plan-and-Execute tutorial (MIT).

Plan output is working context in the existing Planner loop, not a separate
Plan DTO or a new graph stage. See ../NOTICE.md for the protocol adaptation.
"""

INSTRUCTIONS = (
    "Mode: Plan. You are the Planner. For the given objective, come up with a simple "
    "step by step plan. This plan should involve individual tasks, that if executed "
    "correctly will yield the correct answer. Do not add any superfluous steps. "
    "The result of the final step should be the final answer. Make sure that each "
    "step has all the information needed - do not skip steps.\n\n"
    "Use the objective, current plan and completed steps with their actual results "
    "to update the plan after execution. Only include steps that still need to be done. "
    "Revise failed steps using their errors; do not treat them as completed. "
    "If no more work is needed, synthesize the answer. Otherwise continue acting; "
    "a plan alone is not a finished task. Simple requests may be answered directly."
)

DELEGATION = (
    "Use your own advertised tools. Use delegate_task for work suited to an executor; choose by its "
    "description. Supply a self-contained task with relevant previous results, "
    "constraints and expected output: executors cannot see your conversation. "
    "Task is nonblank text unless its input schema requires another value. "
    "Review each returned result before dependent work. Batch only independent actions."
)

LOCAL_EXECUTION = (
    "No executors are available. Carry out the plan with your own advertised tools "
    "and observations, then return the result."
)
