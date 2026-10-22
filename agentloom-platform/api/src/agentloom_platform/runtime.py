"""All LangChain/LangGraph construction is isolated in this module."""

import asyncio
import hashlib
import json
import time
from contextlib import asynccontextmanager
from typing import Annotated, TypedDict

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.structured_output import ToolStrategy
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage, message_to_dict, messages_from_dict
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import StructuredTool
from langchain_core.utils.function_calling import convert_to_openai_tool
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from pydantic import BaseModel, Field
from sqlalchemy import select

from .applications import GRAPH_VERSION, append_event, implementation_hash
from .persistence import Application, ChatSession, Input, Membership, Operation, Resource, Run, User, uid
from .resources import decrypt_secret
from .tools import Workspace, skill_tool


class RuntimeBlocked(Exception):
    pass


class TestModel(BaseChatModel):
    """Opt-in deterministic integration-test model, never a production fallback."""

    tool_names: list[str] = Field(default_factory=list)
    executor_keys: list[str] = Field(default_factory=list)

    @property
    def _llm_type(self):
        return "platform-test"

    def bind_tools(self, tools, **kwargs):
        return self.model_copy(
            update={"tool_names": [convert_to_openai_tool(t)["function"]["name"] for t in tools]}
        )

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        if "Plan" in self.tool_names:
            message = AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": uid(),
                        "type": "tool_call",
                        "name": "Plan",
                        "args": {
                            "tasks": [
                                {"executor": key, "instruction": "整理用户问题并给出结论"}
                                for key in self.executor_keys[:2]
                            ]
                        },
                    }
                ],
            )
        else:
            last = next((m.content for m in reversed(messages) if isinstance(m, HumanMessage)), "")
            message = AIMessage(content="测试模型已执行：" + str(last)[:300])
        return ChatResult(generations=[ChatGeneration(message=message)])


class Control(AgentMiddleware):
    def __init__(self, db, run_id, generation, snapshot, role):
        self.db, self.run_id, self.generation = db, run_id, generation
        self.snapshot, self.role = snapshot, role
        self.tool_lock = asyncio.Lock()

    def check(self, counter=None):
        with self.db.session(write=True) as s:
            run = s.get(Run, self.run_id)
            session = s.get(ChatSession, run.session_id)
            app = s.get(Application, session.app_id)
            membership = s.get(Membership, (session.owner_id, app.space_id))
            owner = s.get(User, session.owner_id)
            if run.generation != self.generation or run.status != "running" or app.lifecycle != "active":
                raise RuntimeBlocked("任务执行权已关闭")
            if not membership or not owner or not owner.enabled or time.time() >= run.deadline_at:
                raise RuntimeBlocked("授权已撤销或运行已超时")
            if session.preview_snapshot and membership.role not in {"admin", "editor"}:
                raise RuntimeBlocked("草稿测试权限已撤销")
            for resource in self.snapshot["resources"].values():
                current = s.get(Resource, resource["id"])
                if not current or not current.enabled:
                    raise RuntimeBlocked("运行资源已停用")
            if counter:
                limit = self.snapshot["draft"]["execution"][counter]
                if getattr(run, counter) >= limit:
                    raise RuntimeBlocked("运行预算已耗尽")
                setattr(run, counter, getattr(run, counter) + 1)

    async def awrap_model_call(self, request, handler):
        self.check("model_calls")
        return await handler(request)

    async def awrap_tool_call(self, request, handler):
        async with self.tool_lock:
            self.check()
            # A provider's tool_call_id is scoped to its assistant message, not
            # the entire Run. The persisted message id also survives recovery.
            origin = next(
                (
                    message
                    for message in reversed(request.state["messages"])
                    if isinstance(message, AIMessage)
                    and any(call["id"] == request.tool_call["id"] for call in message.tool_calls)
                ),
                None,
            )
            if origin is None or not origin.id:
                raise RuntimeBlocked("工具调用缺少持久化的来源消息")
            identity = json.dumps(
                [origin.id, request.tool_call["id"], request.tool_call["name"], request.tool_call["args"]],
                sort_keys=True,
                ensure_ascii=False,
            )
            call_id = self.role + ":" + hashlib.sha256(identity.encode()).hexdigest()
            with self.db.session(write=True) as s:
                previous = s.get(Operation, (self.run_id, call_id))
                if previous:
                    if previous.status == "completed":
                        return messages_from_dict([previous.result])[0]
                    raise RuntimeBlocked("工具操作结果未确认，已阻止自动重放；需要人工核对")
            # Reserve the budget before recording an operation as dispatched.
            # A rejected call has no uncertain external outcome to reconcile.
            self.check("tool_calls")
            with self.db.session(write=True) as s:
                s.add(Operation(run_id=self.run_id, call_id=call_id, status="pending"))
                append_event(
                    s,
                    self.run_id,
                    "tool",
                    {
                        "call_id": call_id,
                        "role": self.role,
                        "name": request.tool_call["name"],
                        "status": "running",
                    },
                )
            result = await handler(request)
            if not isinstance(result, ToolMessage):
                raise RuntimeBlocked("工具返回了不支持的状态修改")
            with self.db.session(write=True) as s:
                operation = s.get(Operation, (self.run_id, call_id))
                operation.status, operation.result = "completed", message_to_dict(result)
                append_event(
                    s,
                    self.run_id,
                    "tool",
                    {
                        "call_id": call_id,
                        "role": self.role,
                        "name": request.tool_call["name"],
                        "status": "failed" if result.status == "error" else "completed",
                    },
                )
            return result


class PlanTask(BaseModel):
    executor: str
    instruction: str = Field(min_length=1, max_length=10000)


class Plan(BaseModel):
    tasks: list[PlanTask] = Field(min_length=1, max_length=20)


class State(TypedDict, total=False):
    messages: Annotated[list, add_messages]
    input_id: str
    ingested: bool
    tasks: list[dict]
    task_index: int
    results: list[str]
    result: str


def text_answer(result):
    message = result["messages"][-1]
    if not isinstance(message, AIMessage) or message.tool_calls:
        raise RuntimeBlocked("模型没有产生完整答案")
    if isinstance(message.content, str):
        return message.content
    return "\n".join(
        block["text"] for block in message.content if isinstance(block, dict) and block.get("type") == "text"
    )


def build_graph(snapshot, model, role_tools, saver=None, controls=None):
    draft, controls = snapshot["draft"], controls or {}
    roles = {"planner": draft["definition"], **draft["executors"]}
    agents = {}
    for key, role in roles.items():
        tools, skill_prompt = role_tools.get(key, ([], ""))
        agents[key] = create_agent(
            model,
            tools=tools,
            system_prompt=role["prompt"] + skill_prompt,
            middleware=[controls[key]] if key in controls else [],
            name=key,
        )
    graph = StateGraph(State)
    graph.add_node("ingest", lambda state: {"ingested": True})
    graph.add_edge(START, "ingest")

    if snapshot["mode"] == "react":

        async def react(state):
            response = await agents["planner"].ainvoke({"messages": state["messages"]})
            return {"result": text_answer(response)}

        graph.add_node("react", react)
        graph.add_edge("ingest", "react")
        graph.add_edge("react", END)
    else:
        planner_prompt = draft["definition"]["prompt"] + role_tools.get("planner", ([], ""))[1]
        planner_prompt += "\n请将任务拆分为顺序执行的步骤，只能选择以下 Executor key：\n"
        planner_prompt += json.dumps(
            {
                key: {"name": value["name"], "description": value["description"]}
                for key, value in draft["executors"].items()
            },
            ensure_ascii=False,
        )
        planner = create_agent(
            model,
            tools=role_tools.get("planner", ([], ""))[0],
            system_prompt=planner_prompt,
            response_format=ToolStrategy(Plan),
            middleware=[controls["planner"]] if "planner" in controls else [],
            name="plan",
        )

        async def plan(state):
            response = await planner.ainvoke({"messages": state["messages"]})
            result = response["structured_response"]
            tasks = result.model_dump()["tasks"]
            if len(tasks) > draft["execution"]["max_tasks"] or any(
                t["executor"] not in draft["executors"] for t in tasks
            ):
                raise RuntimeBlocked("计划包含未授权执行器或超过任务上限")
            return {"tasks": tasks, "task_index": 0, "results": []}

        graph.add_node("plan", plan)
        graph.add_edge("ingest", "plan")

        def next_task(state):
            return (
                "finish"
                if state["task_index"] >= len(state["tasks"])
                else ("executor_" + state["tasks"][state["task_index"]]["executor"])
            )

        for key in draft["executors"]:

            def make_executor(role_key):
                async def execute(state):
                    task = state["tasks"][state["task_index"]]
                    # Executor private messages stay in its child checkpoint namespace.
                    context = "\n".join(state["results"])[-30000:]
                    response = await agents[role_key].ainvoke(
                        {
                            "messages": [
                                HumanMessage(content=task["instruction"] + "\n前序公开结果：\n" + context)
                            ]
                        }
                    )
                    return {
                        "results": [*state["results"], text_answer(response)],
                        "task_index": state["task_index"] + 1,
                    }

                return execute

            name = "executor_" + key
            graph.add_node(name, make_executor(key))
            graph.add_conditional_edges(
                name, next_task, ["finish", *["executor_" + k for k in draft["executors"]]]
            )
        graph.add_conditional_edges(
            "plan", next_task, ["finish", *["executor_" + k for k in draft["executors"]]]
        )

        async def finish(state):
            response = await agents["planner"].ainvoke(
                {
                    "messages": [
                        *state["messages"],
                        HumanMessage(
                            content="请根据以下执行器结果完成最终答复：\n" + "\n\n".join(state["results"])
                        ),
                    ]
                }
            )
            return {"result": text_answer(response)}

        graph.add_node("finish", finish)
        graph.add_edge("finish", END)
    return graph.compile(checkpointer=saver)


def validate_graph(snapshot):
    from langgraph.checkpoint.memory import InMemorySaver

    build_graph(snapshot, TestModel(executor_keys=list(snapshot["draft"]["executors"])), {}, InMemorySaver())


@asynccontextmanager
async def checkpointer(url):
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

    serde = JsonPlusSerializer(
        allowed_msgpack_modules=[
            ("agentloom_platform.runtime", "Plan"),
            ("agentloom_platform.runtime", "PlanTask"),
        ]
    )
    if url.startswith("sqlite:///"):
        import aiosqlite
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

        async with aiosqlite.connect(url.removeprefix("sqlite:///"), timeout=30) as conn:
            saver = AsyncSqliteSaver(conn, serde=serde)
            await saver.setup()
            yield saver
    else:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        async with AsyncPostgresSaver.from_conn_string(url, serde=serde) as saver:
            # Distinct run processes can start together on a new installation.
            await saver.conn.execute("SELECT pg_advisory_lock(781205321)")
            try:
                await saver.setup()
            finally:
                await saver.conn.execute("SELECT pg_advisory_unlock(781205321)")
            yield saver


async def assemble(settings, db, run, snapshot, saver):
    if snapshot["graph_version"] != GRAPH_VERSION or snapshot["implementation_hash"] != implementation_hash():
        raise RuntimeBlocked("运行依赖版本不匹配，拒绝使用新图恢复旧任务")
    Control(db, run.id, run.generation, snapshot, "planner").check()
    draft, resources = snapshot["draft"], snapshot["resources"]
    model_resource = resources[f"{draft['model']['resource_id']}:{draft['model']['version']}"]
    model_spec = model_resource["spec"]
    with db.session() as s:
        secret = decrypt_secret(s, settings, model_resource["id"])
    if model_spec["provider"] == "test":
        if not settings.allow_test_model:
            raise RuntimeBlocked("测试模型未开启")
        model = TestModel(executor_keys=list(draft["executors"]))
    else:
        from langchain_openai import ChatOpenAI

        model = ChatOpenAI(
            model=model_spec["model_id"],
            base_url=model_spec["endpoint"],
            api_key=secret,
            max_tokens=model_spec["max_output_tokens"],
            timeout=60,
            max_retries=0,
        )
    scope = run.id if draft["environment"]["retention"] == "run" else run.session_id
    workspace = Workspace(
        settings.data_dir / "workspaces" / scope, draft["environment"]["workspace_access"] == "read_write"
    )
    local_tools = workspace.tools()
    role_tools, controls = {}, {}
    for role_key, role in {"planner": draft["definition"], **draft["executors"]}.items():
        selected, skills = [], []
        controls[role_key] = Control(db, run.id, run.generation, snapshot, role_key)
        for binding in role["capabilities"]:
            resource = resources[f"{binding['resource_id']}:{binding['version']}"]
            if resource["kind"] == "tool":
                key = resource["spec"]["registry_key"]
                if key == "write_todos":

                    def make_todos(role_name):
                        def write_todos(todos: list[str]) -> str:
                            """Publish a short task checklist for this Agent role."""
                            if len(todos) > 30 or sum(map(len, todos)) > 4000:
                                raise ValueError("任务清单过长")
                            with db.session(write=True) as s:
                                append_event(s, run.id, "todos", {"role": role_name, "todos": todos})
                            return "任务清单已更新"

                        return StructuredTool.from_function(write_todos)

                    selected.append(make_todos(role_key))
                elif key in local_tools:
                    selected.append(local_tools[key])
                else:
                    raise RuntimeBlocked("工具实现尚未接入")
            elif resource["kind"] == "skill":
                skills.append(resource)
            elif resource["kind"] == "mcp":
                from langchain_mcp_adapters.client import MultiServerMCPClient

                controls[role_key].check()
                with db.session() as s:
                    credential = decrypt_secret(s, settings, resource["id"])
                connection = {"url": resource["spec"]["url"], "transport": "http"}
                if credential:
                    connection["headers"] = {"Authorization": f"Bearer {credential}"}
                client = MultiServerMCPClient({"service": connection})
                tools = await asyncio.wait_for(client.get_tools(), timeout=20)
                controls[role_key].check()
                expected = {t["name"]: t for t in resource["spec"]["members"]}
                available = {t.name: t for t in tools}
                for name in binding["members"]:
                    if name not in expected or name not in available:
                        raise RuntimeBlocked("MCP 固定工具成员已变化")
                    schema = available[name].args_schema
                    schema = schema if isinstance(schema, dict) else schema.model_json_schema()
                    if schema != expected[name]["schema"]:
                        raise RuntimeBlocked("MCP 工具参数已变化，请重新发现并发布")
                    selected.append(
                        available[name].model_copy(update={"description": expected[name]["description"]})
                    )
        skill_prompt = ""
        if skills:
            reader, skill_prompt = skill_tool(skills)
            selected.append(reader)
        role_tools[role_key] = (selected, skill_prompt)
    return build_graph(snapshot, model, role_tools, saver, controls)


async def execute(settings, db, run, snapshot):
    with db.session() as s:
        item = s.scalar(select(Input).where(Input.run_id == run.id))
        past = list(
            s.scalars(
                select(Run)
                .where(
                    Run.session_id == run.session_id,
                    Run.created_at < run.created_at,
                    Run.status == "completed",
                )
                .order_by(Run.created_at)
            )
        )[-10:]
        history = []
        for previous in past:
            question = s.scalar(select(Input).where(Input.run_id == previous.id))
            history.extend(
                [
                    HumanMessage(content=question.content, id=question.id),
                    AIMessage(content=previous.result, id=previous.id),
                ]
            )
        initial = {
            "messages": [*history, HumanMessage(content=item.content, id=item.id)],
            "input_id": item.id,
        }
    async with checkpointer(settings.checkpoint_url) as saver:
        graph = await assemble(settings, db, run, snapshot, saver)
        config = {"configurable": {"thread_id": run.thread_id}, "recursion_limit": 250}
        existing = await graph.aget_state(config)
        if existing.values.get("ingested"):
            with db.session(write=True) as s:
                saved = s.get(Input, item.id)
                if saved.status != "accepted":
                    saved.status = "accepted"
                    append_event(s, run.id, "input", {"input_id": item.id, "status": "accepted"})
        if existing.values.get("result") and not existing.next:
            return existing.values["result"]
        stream_input = None if existing.values else initial
        async for state in graph.astream(stream_input, config, stream_mode="values", durability="sync"):
            if state.get("ingested"):
                with db.session(write=True) as s:
                    saved = s.get(Input, item.id)
                    if saved.status != "accepted":
                        saved.status = "accepted"
                        append_event(s, run.id, "input", {"input_id": item.id, "status": "accepted"})
            if state.get("tasks"):
                with db.session(write=True) as s:
                    append_event(
                        s,
                        run.id,
                        "plan",
                        {
                            "tasks": [
                                {
                                    "executor": task["executor"],
                                    "instruction": task["instruction"],
                                    "status": "completed" if i < state.get("task_index", 0) else "pending",
                                }
                                for i, task in enumerate(state["tasks"])
                            ]
                        },
                    )
        final = await graph.aget_state(config)
        if final.next or "result" not in final.values:
            raise RuntimeBlocked("图尚未产生持久化的最终结果")
        return final.values["result"]
