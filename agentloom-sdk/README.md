# agentloom-sdk

独立的 Python ReAct / Plan–Executor SDK，包名 `agentloom-sdk`，导入名 `agentloom`，当前版本 `0.1.0a1`。实现不依赖 Platform、数据库服务或 Web 服务；默认将运行状态保存到当前目录的 `.agentloom/state`。**本次不接入 Workflow / StateGraph**；设计文档中涉及 Workflow 的接口仍属于后续工作。

首次接入请从 [SDK 接入指南](../docs/sdk/integration.md) 开始：提供无凭据的完整运行脚本、`PropertiesConfig → AgentLoom → build(agent_def)` 主路径、扩展合同和重启恢复步骤。[实现文档](../docs/sdk/implementation.md#当前接口与交付状态)列出当前接口状态、模块职责、验证快照与未实现项。

## 安装

Python 3.11+，本地工作空间适配器支持 POSIX（Linux / macOS）。在仓库根目录执行：

```sh
python -m pip install -e ./agentloom-sdk
# 开发检查及可选 MCP 官方客户端
python -m pip install -e './agentloom-sdk[test,mcp]'
```

## 直接运行

模型窗口由调用者显式提供，不根据模型名称猜测。以下代码使用 Responses 流式协议；`OpenAIChatAdapter` 则使用 Chat Completions 协议。兼容服务可显式指定 `base_url`。

```python
import asyncio
import os
from agentloom import (
    AgentDefinition, AgentLoom, AgentProperties, OpenAIResponsesAdapter,
    PropertiesConfig, RuntimeProperties, ServiceProperties, WorkspaceBinding,
)

async def main():
    properties = PropertiesConfig(
        runtime=RuntimeProperties(namespace="my-app"),
        defaults=AgentProperties(workspace=WorkspaceBinding(".agentloom/work")),
        services=ServiceProperties(
            model=OpenAIResponsesAdapter(
                model=os.environ["AGENTLOOM_MODEL"],
                api_key=os.environ["OPENAI_API_KEY"],
                context_window_tokens=int(os.environ["AGENTLOOM_CONTEXT_WINDOW"]),
            ),
        ),
    )
    async with AgentLoom(properties) as app:
        agent = app.build(AgentDefinition("assistant", "准确回答用户的问题。"))
        handle = await agent.start("说明你的任务", session_id="conversation-1")
        print(handle.run_id)  # 保存此 ID；重启后以原配置 start(run_id=...) 接续
        result = await handle.wait()
        print(result.status, result.output.value if result.output else result.error)

asyncio.run(main())
```

`ScriptedModel` 可用于无凭据的确定性开发；它与真实适配器走相同的运行、Hook 和持久化路径。没有调用方提供的凭据时，测试不会发起付费模型请求。

## 统一参数配置

启动参数统一由 `PropertiesConfig` 注入，并按职责分为四组：

| 分组 | 类型与内容 |
| --- | --- |
| `runtime` | `RuntimeProperties`：namespace、默认文件状态目录、文件锁等待、租约、清理重试 |
| `defaults` | `AgentProperties`：WorkspaceBinding、ExecutionPolicy、ContextPolicy、ModelOptions |
| `services` | `ServiceProperties`：模型、能力目录、Hook、显式存储、环境、检查点回调 |
| `files` | `FileProperties`：文件提供者名称，默认 `local`；可选构造参数 `options` |

`defaults` 和 `services` 必须提供，工作区与模型各有唯一归属；`runtime` 和 `files` 可省略，保持默认文件存储及本地文件工具实现。所有配置在构造时校验并冻结；相对路径按构造时的工作目录固定。模型选项与窗口预算也在启动前校验。`AgentLoom(properties)` 构造没有文件／网络副作用，进入 `async with` 才创建并开启 Runtime，之后用 `app.build(agent_def)` 构建 Agent，用 `app.runtime.runs/inputs/...` 查询和控制。

`runtime.state_directory` 默认 `.agentloom/state`，`store_lock_timeout_seconds` 默认 10；这两项只用于宿主创建的 FileRuntimeStore。显式传 `services.store` 时使用该存储自身配置，并由调用方负责关闭。模型、Hook、能力适配器与环境也是借用的实例，不复制或自动关闭。退出 AgentLoom 只暂停本机推进并保留恢复现场；不会发送用户停止命令。每个 AgentLoom 实例只开启一次，重启使用同参数的新实例。

每个 Runtime／Agent 都读取自己的只读参数组，不依赖全局配置。Agent 的显式覆盖优先于启动默认值，并在构造时冻结：

```python
from dataclasses import replace
from agentloom import ExecutionPolicy

# app 处于 async with 中；只替换这个 Agent 的运行策略。
limited = app.builder.with_options(execution_policy=ExecutionPolicy(loop_max=10))
agent = limited.build(agent_def)

# 也可以传入完整的参数组；未改字段沿用原对象。
custom = replace(properties.defaults, execution_policy=ExecutionPolicy(loop_max=20))
another = app.build(agent_def, properties=custom)

# 派生另一份启动参数，不修改已启动的宿主。
other_properties = properties.with_options(
    runtime=replace(properties.runtime, namespace="another-app"),
)
```

`PropertiesConfig.with_options()` 整组替换 runtime／defaults／services／files；使用 `dataclasses.replace()` 修改组内字段。Builder 的 `with_options()` 支持逐项覆盖，参数组和旧形式的单项参数不能混传，冲突返回 `PARAMETERS_CONFLICT`。配置不包含 `run_id`、用户输入、消息、租约现场或活动上下文；检查点继续只保存原恢复合同要求的固定值，不序列化服务对象。

## 从已有定义构建

AgentDefinition.prompt 只写业务提示词。SDK 按构造模式自动注入 ReAct／Planner／Executor 指令，并提供最终输出 Schema 和委派输入合同；调用方无需编写模式系统 Prompt。模式指令在 Run 创建时保存，后续恢复沿用原快照，详见[模式指令合同](../docs/sdk/assembly.md#25-sdk-模式指令与业务-prompt)。

当前内置模板为 LangChain 适配版 `agentloom-langchain-modes-v2`，三个角色分别维护，使用 SDK 原生工具调用和最终输出校验。[来源、固定版本与适配说明](src/agentloom/prompts/NOTICE.md)、MIT 许可证和来源清单随 wheel 一同分发；运行时无需安装 LangChain 或下载模板。

已有 `AgentDefinition` 时，统一入口直接使用 `app.build(agent_def)`。需要自行管理 Runtime 时，仍可使用 `AgentBuilder`；支持 `properties=AgentProperties(...)`，也保留原关键字构造方式。`model`、`runtime`、`registry` 是调用方已准备的依赖；执行时 RuntimeEngine 需要处于打开状态。

```python
from agentloom import AgentBuilder, ExecutionPolicy, WorkspaceBinding

builder = AgentBuilder(
    model=model,
    workspace=WorkspaceBinding(".agentloom/work"),
    runtime_engine=runtime,
    capabilities=registry,  # 无工具/Skill 时可省略
)
agent = builder.build(agent_def)
result = await agent.ainvoke("分析需求")

# 每次 build 都是新实例，可继续同一会话的新一轮。
next_agent = builder.build(agent_def)
next_result = await next_agent.ainvoke("补充验收条件", session_id=result.session_id)

# 传入 Executor 定义表，构建 PlanAgent。
plan = builder.build(planner_def, executors={"review": reviewer_def})

# 派生独立配置，不修改 builder 或已构建的 Agent。
limited = builder.with_options(execution_policy=ExecutionPolicy(loop_max=10))
limited_agent = limited.build(agent_def)
```

模式规则：省略 `mode` 和 `executors` 创建 ReAct；提供 `executors` 映射（包括 `{}`）创建 Plan。也可以显式 `mode="plan"` 创建无 Executor 的 Plan；`mode="react"` 与任何 Executor 表组合都会报错。Definition 继续只描述行为，不包含模型连接或运行状态。

`with_options()` 接收与 Builder 构造函数相同的依赖关键字，未提供的值继承，提供的值整体替换。可选参数显式传 `None` 恢复 SDK 默认值，例如 `hooks=None` 清空 Hook、`capabilities=None` 清空资源目录；不会把新旧权限合并。必需的模型、工作区和运行时不能清空。

Builder 和直接构造共用本地校验、角色授权及恢复清单。构建不会创建 Run、调用远端发现或自动打开 Runtime；共享适配器和宿主仍由调用方管理。重启后用原配置重新 build，再 `await agent.start(run_id=...)` 即可接续原执行，原构造方式与 Builder 可以互相接续。

## 配置与调用

构造 Agent 只做本地校验，不调用模型、MCP、Hook 或创建工作目录。配置冻结后不可重新赋值。一个活动 Agent 对象只绑定一个 `run_id`；同一会话的新问题需要用相同配置构造新对象。

旧的 `agent.workspace/execution_policy/context_policy/model_options` 和 `runtime.namespace/lease_seconds/cleanup_retry_seconds` 继续可读，实际均转发到相应 `.properties` 参数组，没有另一套可变副本。`RuntimeEngine(properties=RuntimeProperties(...))` 与原 namespace／租约／清理关键字择一使用；ReactAgent／PlanAgent／AgentBuilder 的 AgentProperties 与原工作区／策略关键字也择一使用。

本机取得执行权并激活角色后，`agent.context` 提供所属根角色的只读现场：`messages`、`revision`、`progress` 和 `snapshot()`。PlanAgent 对应 Planner，Executor 的私有上下文不合并到其中；实际执行循环由各 ReactAgent 持有，角色图引擎只负责创建和激活任务实例。只附着其他宿主或已终态执行时不创建活动上下文，使用 RunHandle 查询。

| 接口 | 行为 |
| --- | --- |
| `await agent.start(input, session_id=..., run_id=..., input_id=...)` | 接收并返回 `RunHandle`；省略 ID 时生成。`input_id` 只能与 `run_id` 同时传入 |
| `await agent.start(run_id=...)` | 同配置接回已有执行；已有持有者时附着观察，租约到期后可接管。不存在时报错 |
| `await agent.ainvoke(...)` | 等价于 start 后 wait；返回 `RunResult` |
| `agent.astream(..., after_seq=0)` | 已提交事件流，可按序号重连；不是供应商 token 流 |
| `await handle.wait(timeout_s=...)` | 超时或取消只结束本次等待，后台执行继续 |
| `await handle.stop(command_id=..., reason="user")` | 幂等停止命令；关闭业务准入，收束任务后形成终态 |
| `await runtime.inputs.append(run_id, input_id=..., content=...)` | 下次根角色业务请求领取补充；未送达则带到同会话下一次主动提问 |
| `await runtime.runs.attach(run_id)` | 仅查询／观察，不需要重建 Agent，也不主动恢复业务 |

默认 `ExecutionPolicy`：总 Loop 上限 100、总截止 3600 秒、串行执行、Executor 并行上限 2、模型首输出超时 60 秒、工具超时 10 秒，SDK 自动重试关闭。`parallel_enabled=True` 时同批调用并行，Executor 另受并发上限约束；全批结算后按原顺序反馈模型。

重试保持同一 Operation，增加 Attempt；模型重新决定、输出格式纠正消耗新 Loop；压缩模型请求单独计量。第 100 次可正常完成，只有需要第 101 次时才耗尽。Provider 真实 token 用量缺失时返回未知，不使用估算值冒充用量。

HTTP 限流重试取配置间隔与合法 `Retry-After` 剩余时间的较大值，支持有限非负秒和 HTTP 日期；非法值忽略。服务端时间与最终 `next_attempt_at` 随 Attempt 持久化，重开 Runtime 沿用原时间，等待期间仍受停止和总截止控制。

`AgentInput(content=..., variables=...)` 支持结构化输入；`AgentDefinition.input_schema/output_schema` 使用 JSON Schema 2020-12，仅允许本地 `$ref`。`VariableSpec` 声明必填项、类型及默认值，Prompt 只替换声明的 `{{name}}`，不执行模板代码。

## Plan 与能力分配

`PlanAgent` 的其余构造参数与 `ReactAgent` 相同，额外接收 `executors: dict[str, AgentDefinition]`：

```python
from agentloom import AgentDefinition, ExecutionPolicy, PlanAgent

agent = PlanAgent(
    definition=AgentDefinition("advisor", "分析方案的成本和风险，给出有事实依据的建议。"),
    executors={
        "research": AgentDefinition("research", "收集与方案有关的事实，标注来源。", "收集和分析材料"),
        "review": AgentDefinition("review", "检查交付材料中的问题。", "审查事实与结论"),
    },
    model=model, workspace=workspace, runtime_engine=runtime,
    execution_policy=ExecutionPolicy(parallel_enabled=True, executor_max_parallel=2),
)
```

每次委派生成私有 ReactAgent / Task / Context；模型、Loop、总截止共享，Prompt 和能力按角色分别绑定。无 Executor 时不暴露 `delegate_task`，也不补默认执行者。子任务失败作为单项结果反馈，其他成员继续。

能力必须先注册，再由各自的 Definition 显式绑定。注册不自动授权，Planner 和 Executor 的能力不取并集。例如文件能力：

```python
from agentloom import CapabilityBinding, CapabilityRegistry, file_tools

registry = CapabilityRegistry(tools=(file_tools(),))
definition = AgentDefinition(
    "assistant", "使用授权文件完成任务。",
    capabilities=(
        CapabilityBinding("read", "builtin_tool", "fs_read", "files", "read"),
        CapabilityBinding("write", "builtin_tool", "fs_write", "files", "write"),
    ),
)
# ReactAgent(..., definition=definition, capabilities=registry)
```

| 能力 | 入口与约束 |
| --- | --- |
| 自定义工具 | `CapabilityRegistration` + `CapabilitySpec` + `FunctionToolAdapter`；输入／输出 Schema 校验，提供当前角色的 `ToolInvocationContext.workspace` |
| 文件 | `read/list/stat/write/edit/patch/mkdir/move/delete/glob/grep`；相对路径或 `/work/...`，拒绝越根和符号链接；修改须匹配 etag，list 游标绑定目录版本 |
| 权限范围 | `CapabilityBinding.access_scope={"paths": ["reports"], "read_only": True}`；未支持的 scope 字段在构造时拒绝 |
| MCP | 可选 `McpToolAdapter(url=...)` 或 `command=..., args=...`；显式 `await adapter.discover()` 后冻结选中的成员 Schema，调用前核对实际目录，每个 Attempt 独立会话 |
| Skill | `SkillPackage.from_directory` + `DirectorySkillLoader` + `SkillRegistration`；manifest 校验、只读资源、确定性参数展开，读取不会执行脚本或增加工具权限 |
| 脚本 | `ScriptToolAdapter` 需注入配套的隔离环境和执行网关；独立 Protocol 仍属目标设计，本地文件环境不提供宿主 shell 执行 |

普通工具/MCP/Skill/委派的大结果保存完整有效值，模型收到最多 64 KiB 的 UTF-8 预览；可使用自动提供的 `result_read` 分片读取。引用校验来源、角色和 hash，Planner 不能凭 Executor 的私有引用读取其内部工具正文。

文件后端已支持 [WorkspaceBackend SPI](../docs/sdk/builtin-tools-spi.md#8-python-包注册与配置加载)：第三方包继承抽象基类，实现全部 11 个普通方法，并登记 `agentloom.workspace_backends` entry point。业务安装包后只配置名称，无需手动导入或实例化：

```python
from agentloom import FileProperties

properties = properties.with_options(files=FileProperties(provider="acme", options={}))
```

地址由第三方包决定；可内置，也可通过 options 传入。SDK 加载选中类并复用实例，保留工具 Schema、逐项授权、Hook、重试、恢复和清理流程。默认 local 也实现同一接口。提供者自带根地址时使用 `WorkspaceBinding()`，本地仍须指定 root。SDK 创建的文件提供者由 SDK 关闭；普通方法的超时、内部异步机制和真实存储 fencing 由提供者实现。

完整 EnvironmentProvider／SandboxGateway 协议及真实共享文件／对象存储驱动仍待实现。第三方文件后端与未验证脚本环境的组合会明确拒绝，避免访问不一致的文件视图。

## Hook、上下文和恢复

`HookSet` 由显式 `HookBinding` 和 handler 映射组成，`stage` 为 `request` 或 `response`，按 `ordinal` 顺序执行。`FunctionHookHandler` 返回 `HookDecision`。支持模型、工具、委派、Skill 与 `result.read` 路由，`HookMatcher` 可限制角色、能力绑定和 purpose。

回调输入是 `HookInput` 与 `HookCallContext`，使用 `event.payload_ref/context_refs` 和 `context.reader` 读取固定版本有效内容；记录提供 `context_seq/effective_payload/visible_fields`。原生能力实现 `invoke_once(CapabilityRequest, ToolInvocationContext) -> ToolResult`，函数工具仍返回业务值。可用代码见[扩展接入](../docs/sdk/integration.md#5-工具mcp-与-hook-扩展)。

允许修改的字段由路由白名单控制；原始请求／结果保留，有效修改进入后续上下文。用户本轮原文、角色目标、调用槽位和真实失败状态不可改。异常、超时或无效 patch 留诊断并放行；有效 `deny` 即使携带无效 patch 仍生效。Hook 结果保存和应用分别有幂等提交点；已保存结果不会因重启再次调用 handler。`HookReadPort` 只读当前角色的固定版本，回调结束即过期。

同会话默认注入最近 2 次执行的实际根角色过程，更早轮次保留输入、最终结果和终态；不复制之前已注入的历史，不泄漏 Executor 私有过程。补充按 input_id 去重并记录转交。上下文使用保守 UTF-8 封装预算，压缩采用可恢复的分块摘要，保留原始记录与来源 hash；当前任务输入始终原文保留。

恢复沿用原 Task、Operation、Attempt 和截止时间。已经保存的模型／工具结果只续后置处理；外部调用开始后却没有可靠结果时记录 `CALL_OUTCOME_UNKNOWN`，不盲目重发可能产生副作用的调用。

活动 Run 恢复会核对类型明确的配置清单，区分布尔／数字、VariableSpec／普通默认值，并保留变量默认值的 integer／number 推导。旧清单如果已丢失声明类型且无法无歧义核对，返回带变量路径的 `RECOVERY_BINDING_MISMATCH`；可以用原版本续完，或通过控制接口停止后以新 run_id 发起执行。详见[恢复清单合同](../docs/sdk/assembly.md#24-统一参数配置-propertiesconfig)。合法的 128 层最终 JSON 值也能通过公开事件流及游标回放交付，SDK 事件包装使用独立内部预算。

模型完整响应和 MCP 已知结果在关闭连接前持久化，关闭阶段超时不会触发重复业务调用；关闭失败诊断独立保留。停止／截止仍阻止成功交付，失权仍拒绝旧 owner 写入。自定义能力若在方法内部关闭资源，可在关闭前通过 `context.report_completion(tool_result)` 固定结果，详细合同见[能力接口](../docs/sdk/capabilities.md)。通用模型 usage 事件与 completed 的计数按字段合并，不因空值丢失已知用量。

Runtime 关闭会停止本机推进并保留检查点，不等同于用户停止；注入的 store、HTTP 客户端和其他共享依赖仍由调用者关闭。临时目录在根执行终态后由独立 CleanupService 清理，失败单独重试；`persistent_sandbox=True` 保留会话文件，并使旧执行句柄失效。

后台停止收束与清理按 Run 隔离故障。撤权失败时保持 `stopping`，按 `cleanup_retry_seconds` 重试，继续处理其他 Run；同一存储的新 Runtime 也会遵守已保存的重试时间。`handle.snapshot()` 或 `runtime.runs.get()` 的 `maintenance` 字段按阶段提供最近错误、累计失败次数、失败时间、下次重试时间和恢复时间；普通文件删除错误仍在 `cleanup_error` 中。无法访问存储时输出不含原始异常文本的日志，后续扫描继续尝试。

公开查询入口包括 `runtime.runs/tasks/operations/events/sessions`。`runtime.records.delete(run_id, command_id=...)` 只删除已清理的终态记录；有后续历史引用时须先删除依赖记录，删除后原 run_id 保持关闭。

## 文件持久化

`RuntimeStore` 要求关联状态一致提交，不要求数据库。当前后端可按需要注入：

| 后端 | 用法与范围 |
| --- | --- |
| `FileRuntimeStore(".agentloom/state")` | `RuntimeEngine()` 的默认值，JSON 文件、POSIX 文件锁和原子提交，支持单机多进程恢复 |
| `MemoryRuntimeStore()` | 需显式注入，适合不需要重启恢复的进程内执行 |

统一入口通过 `RuntimeProperties(state_directory="/path/to/state")` 指定状态目录，保持默认文件存储由宿主管理。需要自管存储时注入 `ServiceProperties(store=..., model=...)`；纯内存运行显式传入 `MemoryRuntimeStore()`。直接使用 RuntimeEngine 时同样支持 `properties=RuntimeProperties(...)` 或注入 `store=...`。关闭默认存储只释放句柄，不删除持久状态；显式注入的存储由调用者管理。不同程序可用 namespace 隔离记录，同一 namespace 和目录可由新进程接续。

文件后端目录结构：

```text
.agentloom/state/
  namespaces/<namespace 的 SHA-256>/
    lock                 # 固定文件锁，事务之间不删除
    manifest.json        # 格式版本、提交代数、记录引用及校验和
    objects/<SHA-256>.json  # 不可变 JSON 记录，包括运行状态和提交回执
```

一次提交先保存变更的 JSON 记录并 `fsync`，再原子替换清单并同步目录。运行状态、会话占用、提交回执等关联记录通过同一清单同时生效；在替换之前崩溃只留下未引用文件。读取也持有同一 namespace 的进程间锁，不会读到半次提交。锁等待默认最多 10 秒，可通过 `lock_timeout_s` 设置；不同 namespace 独立加锁。同机进程使用相同宿主时钟计算租约和截止。

读取会校验清单和内容 hash，损坏或未知格式明确报错，不自动回退到旧状态重新调用工具。已发布清单的确认失败时，重试先同步目录并查询原命令回执。被替换的记录正常随提交回收；中断写入的残留可通过 `store.collect_garbage(namespace="my-app")` 在锁内回收，返回删除文件数。

状态目录由 SDK 管理，应与可清理的工作区目录分开；不要在运行时手动修改其中的清单、对象或锁。该后端按 namespace 串行提交，清单包含该 namespace 当前所有记录引用，适用于本地执行；未验证 NFS、跨机器协调或断电恢复。

## 分包

| 包 | 职责 |
| --- | --- |
| `agentloom.public` | PropertiesConfig、AgentLoom、AgentBuilder、统一装配、Agent 门面、RunHandle 与公开投影 |
| `agentloom.configuration` | 冻结配置、Schema／变量校验、恢复清单、RFC 8785 canonical JSON |
| `agentloom.prompts` | LangChain 模板按 roles 分包、原生动作协议、格式合同组装、Run 快照恢复校验 |
| `agentloom.runtime` | 宿主、Runner、角色激活、Agent Loop、调用准入／Attempt、完成回执、恢复核验、停止、输入、查询、清理 |
| `agentloom.events` | 单处理者 EventBus、局部注册、Hook 链和持久阶段 |
| `agentloom.context` | 原始／有效记录、历史投影、预算、压缩、大结果分片 |
| `agentloom.capabilities` | 角色能力目录、Tool、MCP、Skill、文件／脚本适配 |
| `agentloom.environment` | 本地 POSIX 工作区、执行域 fence、版本化文件操作 |
| `agentloom.models` | Chat Completions、Responses、SSE 和确定性模型适配器 |
| `agentloom.storage` | 事务协议、文件／内存后端、提交回执、执行租约 |
| `agentloom.types/errors` | 公共传输值、扩展 Protocol 和稳定错误 |

## 当前实现边界

- Workflow、LangGraph、Platform 本次未接入。
- 文件后端和本地 POSIX 环境只保证单机协作语义；不提供多机 NFS fencing、OCI 容器服务或 `agentloom-execd`。同步自定义 Python 工具和调用方授权的 stdio MCP 服务属于可信代码，不能当作进程沙箱；工具函数不应阻塞事件循环。MCP 不会自动获得工作区挂载或访问令牌。
- 大结果持久化在原子执行快照中，尚无独立 BlobStore、ArtifactRef 跨工作区导入、来源级撤销和外部结果授权；`AgentInput.artifacts` 非空明确报错。独立存储实现可通过 RuntimeStore 协议注入，但需自行证明原子性和共享时钟保证。
- 当前文件 grep 仅支持字面匹配，目录 move 明确拒绝；没有 RE2、NFS mutation journal。读取期间检测文件变化，工作文件已写而运行结果未提交时仍可能是 unknown；两者即使都存于文件系统，也不属于同一次原子提交。
- 公共数据使用冻结 dataclass / JSON 值，尚未发布完整设计中所有机器 Schema、内容授权 DTO 和用户生命周期修改 Hook。`AgentNode` 仅保留内部调度桥，不能据此声称 Workflow 已实现。

完整目标设计见 [SDK 设计](../docs/sdk/index.md)。本轮实现和验证范围见 [实现记录](../docs/sdk/implementation.md)，未实现项不能用设计文档或测试替身视作已经交付。

## 验证

```sh
python -m pytest -q agentloom-sdk/tests agentloom-sdk/qa
ruff check agentloom-sdk/src agentloom-sdk/tests agentloom-sdk/qa
ruff format --check agentloom-sdk/src agentloom-sdk/tests agentloom-sdk/qa
python -m pip wheel ./agentloom-sdk --no-deps -w /tmp/agentloom-wheels
```

测试不依赖 Platform 或真实模型凭据。覆盖持久化提交边界的独立进程退出、双宿主租约、并发与额度、Hook、补充及历史、压缩、文件 fence、真实 MCP stdio 子进程和 HTTP SSE 协议分片。

恢复绑定与公开事件修复后的完整回归为 **1483 passed，0 failed，0 skipped**，覆盖 SDK 1021 项和七轮 QA 462 项，包含独立白盒探针；本轮新增 124 项测试。范围、命令与结果见[实现记录](../docs/sdk/implementation.md#恢复绑定与公开事件边界修复)。测试使用本地确定性模型、协议替身和真实本地子进程，真实模型行为及远端服务仍需按部署环境联调。
