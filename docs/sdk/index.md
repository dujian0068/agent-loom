# agentloom-sdk

SDK 是可独立引入的 Python 执行底座，提供 ReAct、Plan–Executor 和 Workflow。本文及本目录是 **2026-10-16 目标设计**，当前尚无可运行实现。公共入口名称是接口草案，不表示已经发布 Python 包。

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
| ReactNode / PlannerNode / ExecutorNode | 共用可恢复 Loop，各自维护 continuation | 独立用户／应用生命周期 |
| EventBus / Pipeline / HookManager | 调用关联、有序拦截、结果与通知 | 把观察订阅当作可靠执行者 |
| ContextManager | 连续记录、有效上下文、历史投影、压缩 | 判断用户输入“有没有用” |
| 能力与环境适配器 | 模型、Tool、MCP、Skill、文件和脚本访问 | 资源商城、上传 UI、平台权限目录 |
| RuntimeStore / CommitCoordinator | 执行事实的一致提交、检查点、接管 | 平台发布与应用删除策略 |

依赖只指向 SDK 内的协议和调用方注入的适配器。内存后端支持演示；需要进程故障恢复时必须配置具备相应一致性保证的持久后端。PostgreSQL 是可选的参考适配，不是导入 SDK 的前提。

## 2. 阅读顺序

1. [行为约定](requirements.md)：默认值和已确认规则。
2. [定义与对象关系](definitions.md)、[对象协作 UML](object-collaboration.md)：配置、节点、实例、类关系和调用时序。
3. [构造、绑定与生命周期](assembly.md)、[公共接口](public-api.md)：精确构造字段、资源注册、RunHandle、输入、停止和重入。
4. [Agent 执行](runtime.md)：Loop、安全点、并行、预算和恢复。
5. [事件总线与 Hook](event-bus.md)：状态机、DTO、调用与一致提交。
6. [上下文与模型](context.md)、[能力与环境](capabilities.md)：模型实际看到什么、文件如何隔离。
7. [检查点与存储](checkpoints.md)、[Workflow 与双引擎](workflow.md)：分别保存现场和可靠衔接。
8. [SDK 验收](verification.md)：独立运行及故障注入。

## 3. 对平台的保证

调用者负责提供正确配置，SDK 负责执行该配置。平台重新构造 Agent 并交回原 ExecutionRecord 标识即可恢复，不需要知道 Executor 执行到哪一轮。

构造的程序可以复用，运行状态不能保存在共享程序对象上。每次执行／接管创建独立的 ExecutionRunner；同一 ExecutionRecord 在有效租约下只有一个执行者。

RuntimeEngine 是这些本机执行的宿主，一个实例可托管多个 ExecutionRecord。共享存储与环境依赖可以复用；单次现场按 ExecutionRecord 装配，Agent 模型、Prompt 和能力集合仍按各自配置固定。范围与资源所有权见[运行时引擎设计](assembly.md#11-runtimeengine-管理的范围)。

平台删除整个目录后，SDK 的三个示例仍须能运行。平台 API 只能调用 SDK 公共门面，不能通过直接改 SDK 存储表来“帮助恢复”。

## 4. 尚需实施验证的边界

直接 Agent 的构造、调用／重入、RunHandle、HookSet 和能力注册已补充详细合同；仍需补齐机器 Schema、查询及删除 DTO、扩展端口的机器合同、Workflow compile／输入／原生存储映射及生命周期 Hook，再进入实现验证，见[整理复核记录](../reorganization-review.md)。不承诺任意 LangGraph 程序或历史检查点原样兼容。
