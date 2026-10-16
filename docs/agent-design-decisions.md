# Agent 设计约定

更新：2026-10-16。已确认需求按 SDK 和 Platform 分开维护；本页负责导航和跨层约束，不再重复多份相互漂移的字段定义。当前为目标设计，新实现尚未完成。

## 一、产品目标与基本概念

AgentLoom 交付高代码 SDK 与低代码 Platform。

- SDK 提供 ReAct Agent、Plan–Executor Agent 和 Workflow API；拥有执行、上下文、Hook、能力及恢复。
- Platform 管理用户、空间、资源与应用，以配置构建并发布 SDK 程序，通过网页及 HTTP 使用。
- Workflow 允许编排多个完整 Agent、Tool／MCP 和业务逻辑，支持映射、条件、循环、并行、汇合与状态更新。
- 内层 AgentGraphEngine 负责有限角色调度；外层 WorkflowEngine 迁入并适配 LangGraph 的设计与源码；两者都属于 SDK，共用执行基础设施。

Agent 应用、可执行 Agent 对象、角色定义、NodeTask 和 GraphExecution 不是同一对象。具体见 [SDK 定义](sdk/definitions.md)及[平台领域模型](platform/domain-model.md)。

## 二、已确认行为的权威位置

| 需求 | 文档 |
| --- | --- |
| 模式、默认值、Loop、重试、停止、历史范围 | [SDK 行为约定](sdk/requirements.md) |
| Agent、EventBus、Node、Hook、Skill 等对象协作 | [SDK 对象级 UML](sdk/object-collaboration.md) |
| 构造参数、角色绑定、资源注册及对象生命周期 | [SDK 装配设计](sdk/assembly.md) |
| 公共构造、运行、补充、停止和恢复接口 | [SDK 公共接口](sdk/public-api.md) |
| 两套引擎、父子执行和原生 pending writes | [Workflow 与双引擎](sdk/workflow.md) |
| Hook修改／拒绝及失败放行 | [事件总线与调用](sdk/event-bus.md) |
| 最近 N 轮、原始记录、有效上下文、Loop内压缩 | [上下文](sdk/context.md) |
| Skill、MCP、文件工具、共享工作区与沙箱 | [能力与环境](sdk/capabilities.md) |
| 原子检查点、故障接管、执行权与结果去重 | [检查点](sdk/checkpoints.md) |
| 模型Key、文件夹／Git导入、应用创建与发布 | [平台配置](platform/configuration.md) |
| 发布停止旧Run、删除应用及文件、暂不过期 | [平台存储与生命周期](platform/storage-lifecycle.md) |

历史最近轮数和执行参数都可按单个 Agent 配置。第 100 次 Loop 正常，第 101 次禁止。补充是正式用户输入，不因历史裁剪而消失。Hook 失败不阻断，合法拒绝可阻止执行；修改由权威上下文保存。模型流内部接收，完整模型响应后发事件，最终结果随 Run 终态交付。

## 三、层间不可破坏的约束

Platform → SDK 单向依赖。SDK 不要求平台数据库、应用和发布记录；平台装配好参数后构造 Agent。SDK 负责恢复，平台不会理解每个 Executor 的 continuation，也不直接写 SDK 状态表。

两层对照、版本责任与调用数据见[职责边界](sdk-platform-boundary.md)。

## 四、详细设计与实施的剩余工作

2026-10-16 对重组版本逐项复核发现了跨层环境绑定、图与正文的执行职责、DTO 和计票不一致，并已修正；有效 Workflow 约定也已补回。详见[整理复核记录](reorganization-review.md)。这表示职责拆分得到校正，不表示详细设计已全部闭合。

直接 Agent 的构造、能力注册、HookSet、start／ainvoke／astream、RunHandle 和重入行为已有细化方案，见[装配设计](sdk/assembly.md)与[公共接口](sdk/public-api.md)。这些是工程接口方案，不改变已确认的产品规则，也不表示功能已实现。

仍需补齐的是工程合同：SDK 机器可验证 Schema／样例、公共查询／删除 DTO 和扩展端口机器合同、Workflow compile 与输入映射、原生 Saver 的逐方法／存储映射以及生命周期 Hook。Workflow 的追加输入接收点不能靠直接 Agent 的补充协议代替。

完成合同后还需要以下实施验收：

- LangGraph 源码迁入范围、许可证保留和版本基线；native task identity、pending writes 及保存屏障适配。
- Skill／MCP／模型 profile 的实际兼容清单，以实现测试声明，不宣称全量兼容。
- RuntimeStore、共享文件与沙箱驱动的跨进程提交、撤权、故障和恢复能力。
- SDK 独立示例和平台 API 的分层验收。

测试矩阵见 [SDK 验收](sdk/verification.md)与[平台验收](platform/verification.md)。
