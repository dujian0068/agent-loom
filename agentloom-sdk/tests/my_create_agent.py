"""新建一个 agent 并接真实模型(自己填地址 + key)。

地址和 API Key 全从环境变量读,不写进文件。在会话里用 ! 前缀设置(shell 不会记录到
Claude 的会话历史之外),或写到你的 shell 配置里:

    ! export AGENTLOOM_MODEL=你的模型名
    ! export AGENTLOOM_API_KEY=你的key
    ! export AGENTLOOM_BASE_URL=https://your-endpoint/v1
    ! export AGENTLOOM_CONTEXT_WINDOW=128000

跑法(在仓库根目录):
    ./.venv/bin/python examples/run_first_agent_live.py

协议选择:
- Chat Completions 兼容端(DeepSeek / 通义 / 智谱 / Kimi / 自建 vLLM 等)用 OpenAIChatAdapter。
- OpenAI 原生 Responses 协议端,把下面 OpenAIChatAdapter 换成 OpenAIResponsesAdapter 即可。
"""
import asyncio
import os
from uuid import uuid4

from agentloom import (
    AgentDefinition,
    AgentLoom,
    AgentProperties,
    PropertiesConfig,
    RuntimeProperties,
    ServiceProperties,
    WorkspaceBinding,
    OpenAIChatAdapter,
)


def require(name):
    # AGENTLOOM_API_KEY 缺失时回退 OPENAI_API_KEY,方便复用已有变量。
    value = os.environ.get(name) or os.environ.get(
        "OPENAI_API_KEY" if name == "AGENTLOOM_API_KEY" else ""
    )
    if not value:
        raise SystemExit(
            f"请先设置环境变量 {name}:在会话输入 ! export {name}=... 设置后再运行"
        )
    return value


async def main():
    # 1) 真实模型:自己填的调用地址 + key。
    #    若是 OpenAI 原生 Responses 端,把 OpenAIChatAdapter 改成 OpenAIResponsesAdapter。
    model = OpenAIChatAdapter(
        model=require("AGENTLOOM_MODEL"),
        api_key=require("AGENTLOOM_API_KEY"),
        base_url=require("AGENTLOOM_BASE_URL"),
        context_window_tokens=int(require("AGENTLOOM_CONTEXT_WINDOW")),
    )

    # 2) 新建 agent 的定义(省 executors = ReAct;传 executors = Plan)。
    definition = AgentDefinition("assistant", "准确回答用户的问题,用中文回复。")

    # 3) 统一装配:runtime / defaults(workspace 必填) / services(model 必填)。
    properties = PropertiesConfig(
        runtime=RuntimeProperties(namespace="first-agent-live"),
        defaults=AgentProperties(workspace=WorkspaceBinding(".agentloom/work")),
        services=ServiceProperties(model=model),
    )

    # 4) 宿主 -> build -> 一次执行(ainvoke = start + wait)。
    run_id, session_id = str(uuid4()), str(uuid4())
    async with AgentLoom(properties) as app:
        agent = app.build(definition)
        result = await agent.ainvoke(
            "用一句话介绍你自己。", run_id=run_id, session_id=session_id
        )
        print("状态:", result.status)
        print("输出:", result.output.value if result.output else result.error)


if __name__ == "__main__":
    asyncio.run(main())