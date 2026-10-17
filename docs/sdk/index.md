# agentloom-sdk

SDK 是可独立引入的 Python 执行底座。当前 `agentloom-sdk 0.1.0a1` 已实现 PropertiesConfig 统一参数、按 AgentDefinition 构建 ReAct / Plan–Executor 及单机文件恢复链路，Workflow 暂不接入。首次使用先读 [SDK 接入指南](integration.md)，当前能力与验证范围见[实现文档](implementation.md)。其余章节保留 **2026-10-16 起的目标设计**，不能将其中全部接口视为已发布能力。

## 1. 职责与依赖

SDK 接收构造参数和运行依赖，自行执行、管理上下文、保存检查点并恢复现场。SDK 不查询平台用户、空间、应用、发布记录，也不需要平台业务库。

公开类型统一为 Agent 接口、它的 ReactAgent／PlanAgent 实现，以及独立的 Workflow。StateGraph 负责构建并编译 Workflow。Node 定义图内可调度单元，不是 Agent 的父类；完整 Agent 通过 AgentNode 适配进入外层图。详见[接口关系](public-api.md#11-公共-agent-接口与图内-node-接口)。

| 模块 | 负责 | 不负责 |
| --- | --- | --- |
| Agent / Workflow | 完整程序的公共执行入口 | 平台应用管理、节点内部激活 |
| Definition / 构造与编译 | 行为、角色、图结构、构造校验 | 平台草稿与发布版本 |
| RuntimeEngine | 组织共享运行服务，托管多次执行，协调宿主启动与关闭 | 节点路由、平台用户／应用和机器分派策略 |
| ExecutionRunner / RunControl | 单次执行的现场、预算、截止、停止和接管 | 跨 ExecutionRecord 的共享可变上下文、另一套节点调度队列 |
| AgentGraphEngine | 轻量角色调度、批次、等待与汇合 | Workflow 的 channels / super-step |
| WorkflowEngine | 通用图编排、状态合并、原生恢复 | Planner 的业务决策与 Agent Loop |
| ReactAgent / AgentNode | Agent 执行 Loop 并持有 AgentContext；节点代理和适配图内调用 | 跨任务共享可变消息、另一套角色 Loop |
| NodeContext | 本次激活的身份、控制与受限服务引用 | 消息保存、资源生命周期、另一套 RuntimeEngine |
| EventBus / HandlerRegistry / EventHandler / HookHandler | 调用关联、有序拦截、结果与通知 | 把观察订阅当作可靠执行者 |
| ContextHandler | 连续记录、有效上下文、历史投影、压缩 | 判断用户输入“有没有用” |
| SkillHandler / ToolHandler | 经总线分别处理 Skill 读取与工具／MCP／脚本调用 | 直接修改 Agent 消息、替代图引擎或自动授予 exec 权限 |
| 能力与环境适配器 | 模型、Tool、MCP、Skill、文件和脚本访问 | 资源商城、上传 UI、平台权限目录 |
| RuntimeStore / CommitCoordinator | 执行事实的一致提交、检查点、接管 | 平台发布与应用删除策略 |

依赖只指向 SDK 内的协议和调用方注入的适配器。默认使用 `FileRuntimeStore(".agentloom/state")`，直接通过本地 JSON 文件保存运行状态、检查点与提交回执；纯内存运行须显式注入 `MemoryRuntimeStore`。SDK 不要求数据库。

## 2. 阅读顺序

直接接入：先按[接入指南](integration.md)运行无凭据脚本，再阅读[当前接口状态](implementation.md#当前接口与交付状态)确认扩展边界。以下顺序用于理解完整目标设计：

1. [行为约定](requirements.md)：默认值和已确认规则。
2. [定义与对象关系](definitions.md)、[对象协作 UML](object-collaboration.md)：配置、节点、实例、类关系和调用时序。
3. [构造、绑定与生命周期](assembly.md)、[公共接口](public-api.md)：精确构造字段、资源注册、RunHandle、输入、停止和重入。
4. [Agent 执行](runtime.md)：Loop、安全点、并行、预算和恢复。
5. [事件总线与 Hook](event-bus.md)：状态机、DTO、调用与一致提交。
6. [上下文与模型](context.md)、[能力与环境](capabilities.md)：模型实际看到什么、文件如何隔离。
   默认工具范围、文件后端与命令执行的替换方式见[默认工具与 SPI](builtin-tools-spi.md)。
7. [检查点与存储](checkpoints.md)、[Workflow 与双引擎](workflow.md)：分别保存现场和可靠衔接。
8. [SDK 验收](verification.md)：独立运行及故障注入。
9. [当前实现与验证记录](implementation.md)：本轮实际交付、分包、运行证据和边界。

## 3. 对平台的保证

调用者负责提供正确配置，SDK 负责执行该配置。平台重新构造 Agent 并交回原 ExecutionRecord 标识即可恢复，不需要知道 Executor 执行到哪一轮。

固定配置、已编译 Workflow 和服务可复用；实际 Agent 对象按任务绑定，由 ReactAgent 持有私有 AgentContext，不能将活动实例交给另一任务。每次执行／接管创建独立的 ExecutionRunner；同一 ExecutionRecord 在有效租约下只有一个执行者。

RuntimeEngine 是这些本机执行的宿主，一个实例可托管多个 ExecutionRecord。共享存储与环境依赖可以复用；单次现场按 ExecutionRecord 装配，Agent 模型、Prompt 和能力集合仍按各自配置固定。范围与资源所有权见[运行时引擎设计](assembly.md#11-runtimeengine-管理的范围)。

SDK 的独立运行与恢复验证不依赖 Platform 源码；当前可运行脚本位于[接入指南](integration.md)，不要求已删除的示例目录存在。平台 API 只能调用 SDK 公共门面，不能通过直接改 SDK 存储文件来“帮助恢复”。

## 4. 尚需实施验证的边界

当前核心 Agent、构建、Hook 和能力接口已有实现与回归；完整机器 Schema、部分查询／回执 DTO、内容授权、独立环境 SPI、可靠订阅及 Workflow 仍有缺口。以[实现状态](implementation.md#当前接口与交付状态)和[目标差异](implementation.md#与完整目标设计的差异)逐项判断，不以设计章节代替验收。不承诺任意 LangGraph 程序或历史检查点原样兼容。
