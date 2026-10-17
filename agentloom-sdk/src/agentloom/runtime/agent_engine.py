"""Bind and activate private role instances; the ReactAgent owns its loop."""

from ..public import AgentNode
from ..types import AgentContext, NodeContext


class AgentGraphEngine:
    def __init__(self, runner):
        self.runner = runner
        self._agents = {}

    async def run_task(self, task_id):
        runner = self.runner
        task = runner.task(task_id)
        agent = self._agents.get(task_id)
        if agent is None:
            agent = runner.program.bind_role(task["role_key"])
            agent._context = AgentContext(
                task["context"]["context_id"], task_id, lambda: runner.task(task_id)
            )
            self._agents[task_id] = agent
            if task_id == runner.state()["root_task_id"]:
                runner.program._context = agent.context
        return await AgentNode(agent).activate(None, task["stage"], NodeContext(task_id, runner))
