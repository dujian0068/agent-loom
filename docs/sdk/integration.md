# SDK 接入指南

本页面向当前 `agentloom-sdk 0.1.0a1`，以仓库中的 Python 公共接口为准。支持独立 ReAct、Plan–Executor 和单机文件恢复；默认不需要数据库。Workflow / StateGraph 暂不接入。实现状态、分包和回归证据见[实现文档](implementation.md)，完整目标合同见[装配设计](assembly.md)和[公共接口设计](public-api.md)。

## 1. 安装与最小运行

要求 Python 3.11+。默认文件后端和本地工作区适配器支持 Linux / macOS。以下安装命令在仓库根目录执行；这里没有假设已发布到公共包仓库。

```sh
python -m pip install -e ./agentloom-sdk
# 需要 MCP 时安装可选依赖
python -m pip install -e './agentloom-sdk[mcp]'
```

下面是可以直接保存并运行的完整脚本，无需模型凭据。它完成一次调用，关闭宿主后以同一个 run_id 重新读取结果，验证文件持久化和终态重开。

```python
import asyncio
from uuid import uuid4

from agentloom import (
    AgentDefinition,
    AgentLoom,
    AgentProperties,
    ModelResponse,
    PropertiesConfig,
    RuntimeProperties,
    ScriptedModel,
    ServiceProperties,
    WorkspaceBinding,
)


async def main():
    model = ScriptedModel(lambda request: ModelResponse("接入成功"))
    definition = AgentDefinition("assistant", "准确回答用户的问题。")
    properties = PropertiesConfig(
        runtime=RuntimeProperties(namespace="integration-demo"),
        defaults=AgentProperties(workspace=WorkspaceBinding(".agentloom/work")),
        services=ServiceProperties(model=model),
    )
    run_id, input_id, session_id = (str(uuid4()) for _ in range(3))

    async with AgentLoom(properties) as app:
        agent = app.build(definition)
        handle = await agent.start(
            "检查接入", run_id=run_id, input_id=input_id, session_id=session_id
        )
        result = await handle.wait()
        print(result.run_id, result.status)
        print(result.output.value if result.output else result.error)

    async with AgentLoom(properties) as reopened:
        result = await reopened.build(definition).ainvoke(run_id=run_id)
        print("重开结果：", result.status, "模型调用次数：", len(model.requests))


asyncio.run(main())
```

预期两次结果都是 `succeeded`，模型只调用一次。运行记录位于当前目录的 `.agentloom/state`，工作文件位于独立的 `.agentloom/work`。此脚本验证已终态记录的重开；非终态接管的条件见[恢复与故障处理](#7-恢复与故障处理)。

## 2. 参数配置与生命周期

启动时传入一份 `PropertiesConfig`，由各模块读取各自的只读参数组。`defaults` 和 `services` 必填，`runtime` 和 `files` 可省略。

| 参数组 | 字段与默认值 | 归属 |
| --- | --- | --- |
| `runtime: RuntimeProperties` | `namespace="default"`、`state_directory=".agentloom/state"`、`store_lock_timeout_seconds=10`、`lease_seconds=30`、`cleanup_retry_seconds=30` | 宿主、默认文件存储和后台维护 |
| `defaults: AgentProperties` | 必填 `workspace: WorkspaceBinding`；可选 `execution_policy`、`context_policy`、`model_options`，省略时使用对应 Policy 默认值 | 每个 Agent 的固定参数 |
| `services: ServiceProperties` | 必填 `model`；可选 `capabilities`、`hooks`、`store`、`environment`、`checkpoint_callback` | 调用方注入的服务实例 |
| `files: FileProperties` | `provider="local"`、`options={}` | 文件实现选择；SDK 负责加载及生命周期 |

配置构造只校验并冻结参数；相对路径在构造时固定为绝对路径。进入 `async with AgentLoom(properties)` 才打开宿主并创建默认状态目录。`app.build()` 只装配 Agent，`start()` 才接收一次执行。

在 Web 服务中，应让 AgentLoom 的生命周期覆盖整个应用或 Worker，接收请求时为每个新 Run 调用一次 `app.build()`。不要在 `start()` 返回后立即退出宿主作用域，否则本机后台推进会暂停。一个 Agent 实例绑定一个 Run；同会话的新问题需要新的 Agent 实例。

退出宿主会暂停本机推进并保留检查点，不等于用户停止。每个 AgentLoom 实例只开启一次；重启创建新实例。SDK 关闭自己创建的文件提供者和默认文件存储，注入的 store、模型客户端、环境和其他共享服务仍由调用方管理。共享适配器须支持调用方启用的并发。

下面及后续装配片段中的 `properties` 指已构造的参数配置，`app` 指处于 `async with` 内的宿主，`definition` 指已有 AgentDefinition。

```python
from dataclasses import replace
from agentloom import ExecutionPolicy

# 派生单个 Agent 的策略；启动默认值不变。
limited = app.builder.with_options(execution_policy=ExecutionPolicy(loop_max=10))
agent = limited.build(definition)

# 修改参数组中的字段，再整组替换；新宿主使用这份配置。
other_properties = properties.with_options(
    runtime=replace(properties.runtime, namespace="another-app"),
)
```

`PropertiesConfig.with_options()` 整组替换；`dataclasses.replace()` 修改组内字段。Builder 的 `with_options()` 支持单项覆盖。参数组与旧形式的同组关键字混传会报 `PARAMETERS_CONFLICT`。run_id、输入、变量值、消息和租约现场属于每次执行，不放入启动参数配置。

显式注入 `services.store` 后，`runtime.state_directory/store_lock_timeout_seconds` 不再配置该存储；使用注入对象自己的配置和关闭方式。文件锁等待接受有限非负数，`0` 表示不等待，非法值返回 `STORE_LOCK_TIMEOUT_INVALID`。

## 3. 接入真实模型

当前提供 `OpenAIResponsesAdapter` 和 `OpenAIChatAdapter`。调用者明确提供模型名、凭据和上下文窗口，不根据名称自动推断，也没有统一的 `ModelAdapter.discover()`。以下片段在创建 AgentLoom **之前**执行；环境变量由应用准备，SDK 不会自动读取 `.env`。

```python
import os
from dataclasses import replace
from agentloom import OpenAIResponsesAdapter

model = OpenAIResponsesAdapter(
    model=os.environ["AGENTLOOM_MODEL"],
    api_key=os.environ["OPENAI_API_KEY"],
    context_window_tokens=int(os.environ["AGENTLOOM_CONTEXT_WINDOW"]),
    base_url=os.environ.get("AGENTLOOM_BASE_URL", "https://api.openai.com/v1"),
)
properties = properties.with_options(
    services=replace(properties.services, model=model),
)
```

Chat Completions 服务改用 `OpenAIChatAdapter`。`ModelOptions.max_output_tokens` 和 `parameters` 放在 `AgentProperties` 中；当前适配器会在构造时拒绝不支持的选项。注入 `httpx.AsyncClient` 时由调用方关闭；未注入时适配器自行管理每次连接。

自定义模型实现 `descriptor: ModelDescriptor` 和 `stream_once(request, context)`，产出 `ProviderEvent`。完整结果使用 `ProviderEvent("completed", ModelResponse(...).to_dict())`；仅有文本增量不算完成。`usage` 事件中的 `input_tokens/output_tokens` 是累计计数，逐字段合并到 completed，null／缺字段不清除已知值，0 合法。计数须为 `0..2**53-1` 的整数，布尔值不合法；未知统计不会被估算值替代。

## 4. 从定义构建 ReAct 和 Plan

业务方只填写 `AgentDefinition.prompt` 的业务要求。`app.build(definition)` 自动使用 ReAct 指令；提供 executors 或指定 `mode="plan"` 后，SDK 自动配置 Planner 指令，每次委派的子任务自动使用 Executor 指令。无需另写系统 Prompt 或在 PropertiesConfig 注入模式模板。声明的最终输出 Schema，以及 Planner 所需的 Executor 任务和变量合同，都会随模式指令提供给模型。

例如“整理财务报告，使用中文说明结论”就是业务提示词，不需要附加“你是 Planner，请使用 delegate_task”。工具和 Executor 仍按定义显式授权，SDK 不凭空添加业务能力。完整规则见[模式指令装配](assembly.md#25-sdk-模式指令与业务-prompt)。

内置模式模板采用 LangChain 来源的 SDK 适配版 `agentloom-langchain-modes-v2`。ReAct 与 Executor 使用行动／观察循环；Planner 根据实际执行结果更新计划、继续委派或汇总。执行采用原生工具调用，最终答案遵守声明的 output_schema；业务方无需实现 Action 文本解析、额外规划工具或终止标记。模板来源及改动见[说明](../../agentloom-sdk/src/agentloom/prompts/NOTICE.md)。升级 SDK 只影响新 Run；恢复沿用接受 Run 时保存的模板版本和正文。

`app.build(definition)` 创建 ReAct。传入 Executor 定义表创建 Plan；共享模型、运行预算和宿主，各角色保留自己的 Prompt、能力与上下文。

```python
from agentloom import AgentDefinition

planner = AgentDefinition("advisor", "分析方案的成本和风险，给出有事实依据的建议。")
reviewer = AgentDefinition("reviewer", "检查论据是否支持结论，指出缺失事实。", description="审查事实与结论")
plan = app.build(planner, executors={"review": reviewer})
result = await plan.ainvoke("检查方案中的风险")
```

省略 `mode/executors` 为 ReAct；提供 `executors`（包括 `{}`）或显式 `mode="plan"` 为 Plan。`mode="react"` 与 Executor 表冲突。空 Executor 表不会生成默认执行者，也不暴露 `delegate_task`。

已有自己管理的 RuntimeEngine 时可使用 `AgentBuilder(model=..., properties=..., runtime_engine=...)`；直接构造 ReactAgent／PlanAgent 也继续支持。三个入口共用装配校验和恢复合同。`AgentInput(content=..., variables=...)` 用于结构化输入与已声明变量；输入／输出 Schema 在 AgentDefinition 中定义。当前非空 `AgentInput.artifacts` 明确拒绝。

## 5. 工具、MCP 与 Hook 扩展

### 工具注册与角色授权

注册资源后还须在 Definition 中显式绑定。下面创建一个接收 JSON 参数、返回 JSON 值的函数工具，并从当前宿主派生 Builder：

```python
from agentloom import (
    AgentDefinition, CapabilityBinding, CapabilityRegistration,
    CapabilityRegistry, CapabilitySpec, FunctionToolAdapter,
)


async def echo(arguments, context):
    context.check_active()
    return {"text": arguments["text"]}


registry = CapabilityRegistry(tools=(
    CapabilityRegistration(
        adapter_key="local",
        adapter=FunctionToolAdapter(echo),
        members=(CapabilitySpec(
            capability_key="echo", name="echo", description="返回输入文本",
            input_schema={
                "type": "object", "properties": {"text": {"type": "string"}},
                "required": ["text"], "additionalProperties": False,
            },
        ),),
    ),
))
tool_definition = AgentDefinition(
    "assistant", "使用已授权工具处理请求。",
    capabilities=(CapabilityBinding(
        binding_key="echo", kind="tool", visible_name="echo",
        adapter_key="local", member_key="echo",
    ),),
)
tool_agent = app.builder.with_options(capabilities=registry).build(tool_definition)
```

`adapter_key` 选择注册对象，`member_key` 选择该对象的成员，`visible_name` 是模型看到的工具名。Planner 和各 Executor 分别绑定；注册目录不自动授予任何角色权限。函数工具返回业务值即可；同步函数必须快速返回，不能依赖它隔离阻塞 I/O。

原生扩展实现 `async invoke_once(request: CapabilityRequest, context: ToolInvocationContext) -> ToolResult`，返回 `ToolResult.from_value(value)` 或合法信封。旧 `invoke(member, arguments, context)` 仍可兼容，但异常后不会回退再调用另一种签名。工具已返回非法 JSON 时记录协议失败和 completed 事实；函数执行中抛错不自动等同于已完成。

需要在方法内部关闭资源的原生适配器，可以在取得完整结果后、关闭前同步调用 `context.report_completion(tool_result)`。该回调进行同样的结果／输出 Schema 校验并持久化首个结果；校验与提交异常须向上传播，不能吞掉后继续交付。本次派发结束后回调过期，不能用于后台迟到回填。没有内部关闭步骤的工具正常返回即可。详细规则见[能力合同](capabilities.md)。

报告失败可使用 `context.report_completion(error=error)`，error 为 AgentLoomError。合法失败提交后，该调用会抛出原异常，可在 finally 中关闭资源；external_outcome="known" 会保留 completed 事实并阻止重试，unknown／not_started 保留原语义。非法错误字段或循环返回值会成为完成后的协议失败，无需给异常添加私有属性。

JSON 边界材料最多 128 层容器，标量深度为 0；工具结果计算完整 ToolResult 信封，因此单个 inline JSON 值最多 124 层。超限会保存 `CAPABILITY_RESULT_INVALID / protocol / completed`，让模型收到失败反馈，不保存无法读取的深嵌套材料。直接抛出的 AgentLoomError 同样验证错误字段；非法错误被替换为 `ADAPTER_ERROR_INVALID / protocol / unknown`，不会因 NaN 等字段导致整个 Run 序列化失败。详细计数与恢复边界见[能力结果合同](capabilities.md#2-冻结能力目录调用结果与按需读取)。

### MCP、文件和 Skill

| 能力 | 当前接入方式 |
| --- | --- |
| MCP | 安装 `mcp` extra，构造 `McpToolAdapter(url=...)` 或 `command=..., args=...`；调用方显式 `await adapter.discover()`，从返回目录选择并冻结 CapabilitySpec，再按工具方式注册和绑定。discover 会连接外部服务，build 不会自动 discover；调用时核对目录，每个 Attempt 独立会话 |
| 文件 | `CapabilityRegistry(tools=(file_tools(),))` 后按成员名绑定 read／write 等能力；使用 `context.workspace` 获得当前执行域的受限文件服务，路径范围由 binding.access_scope 控制 |
| Skill | `SkillPackage.from_directory`、`DirectorySkillLoader`、`SkillRegistration` 注册只读包，角色显式绑定；读取文本不执行脚本，也不授予额外工具权限 |
| 脚本 | ScriptToolAdapter 需要调用方提供配套隔离环境与网关；默认 LocalEnvironment 不提供宿主 shell 执行。设计中的独立环境／沙箱 Protocol 尚未完整交付 |

大结果保留完整有效值，模型默认收到最多 64 KiB 的预览，并通过自动提供的 `result_read` 按授权分片读取。Planner 不能使用 Executor 的私有结果引用。

### Hook 输入与读取

Hook 接收 `HookInput` 和 `HookCallContext`，通过受限 reader 读取有效载荷和固定版本上下文：

```python
from agentloom import FunctionHookHandler, HookBinding, HookDecision, HookSet


async def inspect_request(event, context):
    payload = await context.reader.read_payload()
    page = await context.reader.read_context(event.context_refs[0], limit=16)
    print(context.metadata.role_key, len(payload["messages"]), len(page.records))
    return HookDecision("allow")


hooks = HookSet(
    bindings=(HookBinding("inspect", "model.generate", "request", "inspect"),),
    handlers={"inspect": FunctionHookHandler(inspect_request)},
)
hooked_agent = app.builder.with_options(hooks=hooks).build(definition)
```

`event.payload_ref/context_refs`、记录的 `context_seq/effective_payload/visible_fields` 均遵守只读合同。`read_context()` 返回 `HookRecordPage(records, next_seq)`；用 `after_seq=next_seq` 继续分页，返回 None 时结束。reader 在回调结束后过期。

request／response Hook 按 ordinal 执行，返回 `HookDecision` 的 allow／deny 及白名单 patch。非法 patch、普通异常和超时会保留诊断并放行；合法 deny 不因附带非法 patch 而失效。Hook 因此不能替代宿主的身份验证和资源授权。当前没有 ScriptHookHandler 或生命周期修改 Hook。

## 6. 执行、补充、停止与查询

以下入口均要求所属 Runtime 仍打开。业务终态通过 RunResult 返回；使用 `result.status` 判断结果，再读取 `output` 或 `error`。

| 操作 | 当前接口与语义 |
| --- | --- |
| 新执行 | `await agent.start(input, run_id=..., input_id=..., session_id=...)`；返回 RunHandle 表示已接收，不代表已完成。input_id 只能随明确的 run_id 和输入提供 |
| 等待 | `await handle.wait(timeout_s=...)`；返回 RunResult。WaitTimeout／取消等待只结束观察，后台执行继续 |
| 直接等待结果 | `await agent.ainvoke(...)` 等于 start 后 wait |
| 事件 | `handle.events(after_seq=...)` 或 `agent.astream(..., after_seq=...)` 返回已提交 RunEvent；游标为排他的非负序号。这不是供应商 token 流，断开观察不停止业务 |
| 补充 | `await app.runtime.inputs.append(run_id, input_id=..., content=...)`；仅接受非空文本，下次根角色请求领取。未送达输入可转交到同会话下一次主动提问 |
| 停止 | `await handle.stop(command_id=..., reason="user")`；幂等持久化停止意图，然后 wait 观察收束 |
| 无 Agent 停止 | `await app.runtime.runs.stop(run_id, command_id=...)`；也可提前关闭尚未初始化的 run_id 准入 |
| 只观察 | `await app.runtime.runs.attach(run_id)` 返回新 RunHandle，不主动接管业务 |

默认总 Loop 为 100，总截止 3600 秒，模型首输出超时 60 秒，流空闲超时关闭，工具超时 10 秒，自动重试关闭。并行默认关闭；开启后 Executor 默认最多并行 2 个。需要重试时显式设置 `ExecutionPolicy(retry=RetryPolicy(enabled=True, ...))`；重试属于同一 Operation 的新 Attempt，停止、总截止和失权不可重试。

HTTP／MCP 的合法 Retry-After 与配置间隔取较大值，等待时间持久化，重启不重新计算整段等待。仅资源关闭超时不重新执行已完成调用；外部服务侧的副作用幂等仍需由该服务和调用协议保证，SDK 的完成回执不构成跨服务事务。

查询当前返回 JSON 投影；不要按目标文档假设已有正式 RunSnapshot、Page 或 Receipt 类型：

| 查询／删除 | 返回形态 |
| --- | --- |
| `runs.get(run_id)`、`handle.snapshot()` | Run 状态 dict，包含 cleanup_state、cleanup_error、maintenance |
| `tasks.list(run_id, after=0, limit=100)` | `{items, next_cursor}` |
| `operations.get(run_id, operation_id)` | 单项调用状态 dict |
| `events.read(run_id, after_seq=0, limit=100)` | `{items, next_cursor}`，事件含稳定 seq |
| `sessions.history(session_id, after=0, limit=100)` | `{items, next_cursor}` |
| `sessions.inputs(session_id)` | 输入列表，当前无分页 |
| `records.delete(run_id, command_id=...)` | 删除回执 dict；要求终态、清理成功、无待转交输入及后续历史引用 |

这些都是 async 方法。RunResult 的 partial_results 不公开内部模型碎片、reasoning 或 Executor 私有审计材料。Run 终态不表示清理已完成；按 snapshot 的 cleanup_state 判断清理，维护失败会保存诊断并重试。

### 文件提供者接入

文件工具可以整体切换到已安装的第三方实现。它必须继承 `agentloom.WorkspaceBackend`，实现 read、write、list、mkdir、stat、edit、patch、move、delete、glob、grep 全部 11 个普通方法。每个方法接收 `WorkspaceCallContext` 和原工具参数，返回 [contracts.py](../../agentloom-sdk/src/agentloom/workspace/contracts.py) 中定义的普通 DTO 字典；无需实现 async 接口。

第三方包的登记：

```toml
[project.entry-points."agentloom.workspace_backends"]
acme = "acme_files:AcmeWorkspaceBackend"
```

安装该包后，业务在原配置中加入 files；不创建 AcmeWorkspaceBackend 对象：

```python
from dataclasses import replace
from agentloom import FileProperties, WorkspaceBinding

properties = properties.with_options(
    files=FileProperties(provider="acme", options={}),
    defaults=replace(properties.defaults, workspace=WorkspaceBinding()),
)
```

SDK 使用 `AcmeWorkspaceBackend(**options)` 构造实例。地址可内置，options 可以为空；其余模型、能力注册和 AgentDefinition 保持原样。仍需用 `file_tools()` 注册目录，并用 `CapabilityBinding(kind="builtin_tool", adapter_key="files", member_key="read", ...)` 等逐项授权，选择后端不会自动扩大 Agent 权限。

未配置 files 时使用 local，WorkspaceBinding.root 继续必填。提供者名不存在、重复登记、缺方法或签名不兼容会明确报错，构造失败不会回退到本地。SDK 仅导入选中类；同一宿主复用实例，并在运行和维护任务收束后关闭。close 失败可以重试 aclose，借用的其他服务不受影响。

恢复时必须保留提供者名、发行包版本和 options。检查点保存这些绑定及 options 的类型敏感摘要，不保存原始参数；更换配置会拒绝非终态恢复。第三方内部地址或行为变化应发布新版本。长同步调用的超时、提交前执行权核验及存储 fencing 由提供者实现，SDK 不替实现包建立异步或线程执行机制。完整注册、返回值、生命周期与错误合同见[文件 SPI](builtin-tools-spi.md#8-python-包注册与配置加载)。

## 7. 恢复与故障处理

接入方保存 namespace、run_id、session_id、初始 input_id 及原装配配置。重启时保持状态目录、工作区根、Definition、策略、模型规格、能力与 Hook 绑定一致，并重建对应的服务实现。SDK 不替调用方保存 Python 处理器或客户端连接；服务地址和实现版本仍由接入方固定。

```python
# 放在新进程的异步入口中；properties、definition 按原配置重新构造。
from agentloom import AgentLoom

async with AgentLoom(properties) as app:
    agent = app.build(definition)
    handle = await agent.start(run_id=saved_run_id)
    result = await handle.wait()
```

Plan 恢复还须传回原 Executor 表。已终态返回保存结果；有效 owner 存在时只附着观察；owner 租约到期后才可接管。恢复不重置总截止和 Loop 用量。仅有 Runtime 启动不会自动重建全部 Agent，非终态业务接续仍需按原配置调用 start；停止和过期执行的后台收束由 Runtime 维护任务处理。

当前清单保留变量声明类型，并用规范 JSON 核对配置，`true` 与 `1`、VariableSpec 与同字段的普通默认字典都不相同。旧清单若缺少类型证据且无法无歧义核对，会返回 `RECOVERY_BINDING_MISMATCH` 及具体变量路径；可以使用原版本续完，或通过控制接口停止后用新 run_id 发起任务。不要手动补写清单绕过检查。合法的 128 层最终 JSON 值可通过 wait、events／astream 和游标回放交付，事件封装不挤占业务材料的深度预算。

初始接收回执丢失时，使用相同 run_id、input_id、session_id 和原输入重投；不能先生成新 ID。补充和停止命令也须保持原 input_id／command_id。同一 ID 对应不同内容会被拒绝。

| 故障或结果 | 接入方处理 |
| --- | --- |
| ConfigurationError | 在打开业务入口前修正参数、Schema、模型选项或绑定 |
| RequestError | 按 code 处理输入、实例已绑定、会话占用、幂等冲突或恢复配置不一致 |
| WaitTimeout | 再次等待或 attach 观察；只有明确的用户停止才发送 stop |
| RunResult 非 succeeded | 读取持久化的业务状态和 error；不要把失败终态当作 Python 调用成功的答案 |
| InfrastructureError／LeaseLost | 暂停本机推进，保留检查点；恢复依赖可用后用原配置和 run_id 重开。失权的旧执行者不得继续写入 |
| CALL_OUTCOME_UNKNOWN | 外部调用已经开始但没有可靠完成记录；恢复不自动补发该调用，不把 unknown 当作未执行 |

合法模型 completed 和 MCP 已知响应在关闭流／会话前保存，接管者复用 raw 并继续后处理。关闭诊断不覆盖已完成结果；保存结果或提交回执失败按基础设施故障传播。停止与总截止仍阻止成功交付，失权仍拒绝旧 owner 写入。

默认 FileRuntimeStore 使用 JSON 对象、POSIX 文件锁和原子清单，支持单机多进程协作；MemoryRuntimeStore 需要显式注入且不能跨进程恢复。状态目录与可清理工作区分开，保持 SDK 文件清单与对象完整，不手动改写检查点。当前没有 SQLite 或 PostgreSQL 后端，不承诺 NFS／跨机协调及任意历史版本迁移。

## 8. 当前范围与验证

本版本尚未提供独立 ContentStore／ArtifactRef 输入授权、完整查询 DTO、模型发现协议、完整环境／沙箱 SPI 及真实共享／对象存储驱动、可靠订阅 ACK／outbox、脚本 Hook 和 Workflow。目标设计中的同名类不能直接作为本版本的导入清单；完整状态见[实现文档](implementation.md#当前接口与交付状态)。

接入验证至少覆盖：启动参数构造、角色权限绑定、一次真实业务调用、同 run_id 重开、停止／等待超时区别、维护状态查询。确定性脚本用于检查本地装配；真实模型端点和 MCP 服务需按部署环境另行联调。

本次全部剩余改动统一提交前的完整回归为 **1537 passed，0 failed，0 skipped**，包含 SDK 1075 项与七轮 QA 462 项，独立白盒探针一并执行。源码摘要、测试范围和完整日志见[验证记录](implementation.md#验证口径)。SDK 与独立文件提供者的安装包另通过 10 个进程验证场景，见[安装包验证](../../agentloom-sdk/qa/evidence/full-workspace-publish-20261018/wheel-summary.json)。MiniMax 示例检查了命令行入口，未执行真实远端模型调用。

本页 7 个 Python 代码块已通过本地验证，另验证停止、补充、等待超时和查询入口，共 8 个场景；真实模型片段仅验证构造，其余执行使用确定性模型，没有远端服务调用。[文档代码与验证结果](../../agentloom-sdk/qa/evidence/docs-integration-20261017/summary.json)独立保存，不计入上述 SDK 回归数量。
