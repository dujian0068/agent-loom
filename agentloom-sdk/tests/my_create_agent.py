"""手动接入兼容协议的真实模型；默认回归不会执行此文件。

先在应用环境中配置 AGENTLOOM_MODEL、AGENTLOOM_BASE_URL、
AGENTLOOM_CONTEXT_WINDOW；服务要求鉴权时再配置 AGENTLOOM_API_KEY。
AGENTLOOM_MODEL_PROTOCOL 可选，默认为 chat_completions，也支持 responses。
SDK 不读取环境变量，下面由业务显式把参数传给 PropertiesConfig。

从仓库根目录运行：
    ./.venv/bin/python agentloom-sdk/tests/my_create_agent.py
"""

import asyncio
import os

from agentloom import AgentDefinition, AgentLoom, PropertiesConfig


def require(name):
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"请先设置环境变量 {name}")
    return value


async def main():
    properties = PropertiesConfig(
        {
            "runtime.namespace": "first-agent-live",
            "workspace.root": ".agentloom/work",
            "model.protocol": os.environ.get("AGENTLOOM_MODEL_PROTOCOL", "chat_completions"),
            "model.base_url": require("AGENTLOOM_BASE_URL"),
            "model.api_key": os.environ.get("AGENTLOOM_API_KEY"),
            "model.name": require("AGENTLOOM_MODEL"),
            "model.context_window_tokens": int(require("AGENTLOOM_CONTEXT_WINDOW")),
        }
    )
    definition = AgentDefinition(prompt="准确回答用户的问题，用中文回复。")
    async with AgentLoom(properties) as app:
        agent = app.build(definition)
        result = await agent.ainvoke("用一句话介绍你自己。")
        print("状态:", result.status)
        print("输出:", result.output.value if result.output else result.error)


if __name__ == "__main__":
    asyncio.run(main())
