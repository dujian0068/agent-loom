# LangChain prompt adaptations

Template version: `agentloom-langchain-modes-v2`.

These templates contain adaptations of MIT-licensed LangChain project materials.
The original copyright notices and licenses are included in `LICENSE.langchain`.
`sources.json` records immutable upstream revisions, file hashes and sections. Both
files are included in the installed wheel. Upstream source is consulted at
development time only; constructing or restoring an Agent never fetches prompts.

## Sources and changes

| SDK module | Pinned source | Adaptation |
| --- | --- | --- |
| `roles/react.py` | [LangChain classic ReAct](https://github.com/langchain-ai/langchain/blob/bd5b134d876ef47858beef27bd29c81eeda03293/libs/langchain/langchain_classic/agents/react/agent.py) | Retains the thought/action/input/observation/final-answer cycle. The input is the SDK user message, tools are the advertised schemas, and prior actions/results are SDK messages. The phase labels describe control flow; they are not a required textual output format. |
| `roles/planner.py` | [LangGraph Plan-and-Execute tutorial, archived revision](https://github.com/langchain-ai/langgraph/blob/23961cff61a42b52525f3b20b4094d8d2fba1744/docs/docs/tutorials/plan-and-execute/plan-and-execute.ipynb) | Retains the initial planning criteria and replanning from the objective, plan and actual step results. Both instructions run inside the existing Planner tool loop. Advertised executors are invoked through `delegate_task`; an empty executor catalog selects local execution instructions. |
| `roles/executor.py` | [LangChain experimental executor](https://github.com/langchain-ai/langchain-experimental/blob/f10d5c3247fb102d4d45d29a35a0266760288542/libs/experimental/langchain_experimental/plan_and_execute/executors/agent_executor.py) | Maps the current objective and explicitly supplied previous results to the delegated input, and the scratchpad to the child's own messages. Adds the shared ReAct cycle and SDK requirements for an isolated child report. |
| `protocol.py` and message assembly | [LangChain native tool-calling agent](https://github.com/langchain-ai/langchain/blob/bd5b134d876ef47858beef27bd29c81eeda03293/libs/langchain/langchain_classic/agents/tool_calling_agent/base.py) | Uses provider-native tool calls and real tool messages. A response with no calls proceeds to the SDK's final-output validator. Private reasoning is not required in public output. |

## SDK-specific contracts

Business instructions remain in `AgentDefinition.prompt`, independently of these
SDK-owned instructions. The SDK adds role instructions and declared input/output
schemas. Executor business prompts and default variable values remain private.

The adapters do not add text-action parsers, `Observation` stop sequences,
`TERMINATE`, `final_answer`, `write_todos`, or `<END_OF_PLAN>` markers. They do not
implement LangGraph's separate `Plan`/`Response` model calls or a persistent plan
board. Planning and result review are instructions within the current SDK Planner
loop; tool permission, execution, result feedback, output validation and budgets
are enforced by that loop. Workflow remains outside this implementation.

Instructions and their version are frozen for every role when a Run is accepted.
Restored Runs use their saved content, including children not yet created. A v1
snapshot is not rewritten into v2; Runs without a prompt snapshot retain their
legacy layout. Hook edits and compaction cannot replace SDK instructions.
