"""ReAct adapted from LangChain's classic react/agent.py prompt (MIT).

The textual scratchpad becomes SDK messages; action labels become native calls.
Source revision and changes are recorded in ../NOTICE.md and ../sources.json.
"""

CYCLE = (
    "Thought: think about what to do using the task and previous observations. "
    "Action / Action Input: choose an advertised tool and call it with valid arguments. "
    "Observation: wait for the actual tool result before continuing. "
    "Batch only independent actions. This cycle can repeat as needed. "
    "Final Answer: the final answer to the original task."
)

INSTRUCTIONS = (
    "Mode: ReAct. Answer the request as best you can. Use tools if necessary; "
    "respond directly if appropriate. Do not delegate tasks.\n\n" + CYCLE
)
