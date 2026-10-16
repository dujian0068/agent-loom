# Agent 设计约定

更新：2026-10-16。已确认需求按 SDK 和 Platform 分开维护；本页负责导航和跨层约束，不再重复多份相互漂移的字段定义。当前为目标设计，新实现尚未完成。

## 一、产品目标与基本概念

AgentLoom 交付高代码 SDK 与低代码 Platform。

- SDK 提供 ReactAgent、PlanAgent 和 Workflow API；ReactAgent、PlanAgent 实现公共 Agent 接口。SDK 拥有执行、上下文、Hook、能力及恢复。
- SDK 的执行宿主统一命名为 RuntimeEngine（运行时引擎），管理运行服务及活动执行；同一引擎可托管多个 Agent／Workflow 的多次执行。
- Platform 管理用户、空间、资源与应用，以配置构建并发布 SDK 程序，通过网页及 HTTP 使用。
- Workflow 允许编排多个完整 Agent、Tool／MCP 和业务逻辑，支持映射、条件、循环、并行、汇合与状态更新。
- 内层 AgentGraphEngine 负责有限角色调度；外层 WorkflowEngine 迁入并适配 LangGraph 的设计与源码；两者都属于 SDK，共用执行基础设施。

Agent 应用、可执行 Agent 对象、角色定义、NodeTask 和 GraphExecution 不是同一对象。具体见 [SDK 定义](sdk/definitions.md)及[平台领域模型](platform/domain-model.md)。

接口层区分两类职责：Agent 面向调用者，统一使用完整智能体；Node 面向图引擎，约定节点的激活和结果。具体 Agent 类负责构造，Agent 接口不负责创建其他 Agent。2026-10-16 确认 AgentNode 同时实现 Agent 和 Node，内部持有 `agent: Agent`：公共 Agent 方法代理内部程序，图内激活接续已有任务，不另起根执行。Loop 由内部 ReactAgent 实现；Planner／Executor 均复用它，若设专用节点子类则继承 AgentNode，只表达角色绑定与接入约束。

工程设计将 Workflow 保持为独立可执行类型，由 StateGraph 构建并编译；它可以只编排工具和函数，不要求具备 Agent 的模型与聊天语义。AgentNode 通过组合把完整 Agent 接入 Workflow，也可承载内层角色。内层 Node 与外层 WorkflowNodeAdapter 保留各自的调度合同，检查点仍通过 SDK 的统一设施衔接。最新关系及独立调用／图内执行边界见[Node、角色与执行实例](sdk/object-collaboration.md#3-node角色与执行实例)。

RuntimeEngine 组织共享基础设施、托管执行、装配本次现场并协调收尾。ExecutionRecord 是一次执行的持久记录；ExecutionRunner 是取得执行权后驱动该次执行的本机对象，负责运行现场及所属图引擎的协调。节点调度仍由两套图引擎完成，模型、文件与沙箱操作由对应适配器执行。模型与能力配置按 Agent 固定，上下文、预算、事件总线等活动状态按一次执行隔离。详见[运行时引擎范围](sdk/assembly.md#11-runtimeengine-管理的范围)及[对象作用域](sdk/object-collaboration.md#11-作用域与生命周期的读法)。

2026-10-16 补充上下文归属：`NodeContext` 是图内任务身份、执行控制和受限服务引用，不保存模型消息，也不另建 RuntimeEngine。执行中的 ReactAgent 持有自己的 `AgentContext`，包括消息与有效修订、Loop 进度、待处理调用和压缩引用；Planner 与每次 Executor 委派各有私有实例。共享的是配置、获准的文件和基础设施。AgentNode 只持有并代理 Agent，不复制上下文。消息与进度由统一检查点设施保存，恢复时重建 Agent 和 NodeContext，沿用原逻辑身份。

事件协作采用三个接口：HandlerRegistry 负责注册／查询／冻结，EventBus 负责发送，EventHandler 负责处理。EventBusImpl 实现前两个；组合式 HookHandler 同时实现三个接口，能够注册 RecordHookHandler 等末端处理器。子处理器实现 EventHandler，按本地固定顺序处理；注册关系不自动向父级回退。

模型／工具调用的前后阶段由 ReactAgent Loop 经 NodeDriver 发出业务事件，告知“准备调用什么／调用已结束”；发送方不调用 Hook 专用方法，也不判断是否配置 Hook。EventBus 只按注册表路由，ModelManager／ToolManager 只处理实际业务调用，不代发外围 before／after。需要修改或拒绝的阶段必须等待 StageResult，再继续执行或交付；普通观察订阅不改变执行。原始结果先保存，再由 Loop 发 after，且模型完整流式响应只发一次，不逐 token 发送。调用设施保留 Attempt 超时和配置允许的重试，阶段恢复由原 Loop／节点驱动器负责，不另设 InvocationPipeline。HookHandler 沿用原 Operation 保存决定、有效修改及游标，修改进入授权范围的有效上下文；不生成另一套执行、额度或检查点设施。接口关系与类型合同见[对象 UML](sdk/object-collaboration.md#4-eventbushandlerregistry-与-eventhandler)和[调用设计](sdk/event-bus.md#10-三个接口与组合关系)。

## 二、已确认行为的权威位置

| 需求 | 文档 |
| --- | --- |
| 模式、默认值、Loop、重试、停止、历史范围 | [SDK 行为约定](sdk/requirements.md) |
| Agent、EventBus、Node、Hook、Skill 等对象协作 | [SDK 对象级 UML](sdk/object-collaboration.md) |
| 构造参数、角色绑定、资源注册及对象生命周期 | [SDK 装配设计](sdk/assembly.md) |
| RuntimeEngine 的职责、共享服务与单次现场的范围 | [运行时引擎](sdk/assembly.md#11-runtimeengine-管理的范围) |
| 公共构造、运行、补充、停止和恢复接口 | [SDK 公共接口](sdk/public-api.md) |
| 两套引擎、父子执行和原生 pending writes | [Workflow 与双引擎](sdk/workflow.md) |
| Hook修改／拒绝及失败放行 | [事件总线与调用](sdk/event-bus.md) |
| 最近 N 轮、原始记录、有效上下文、Loop内压缩 | [上下文](sdk/context.md) |
| Skill、MCP、文件工具、共享工作区与沙箱 | [能力与环境](sdk/capabilities.md) |
| 原子检查点、故障接管、执行权与结果去重 | [检查点](sdk/checkpoints.md) |
| 模型Key、文件夹／Git导入、应用创建与发布 | [平台配置](platform/configuration.md) |
| 发布停止旧ExecutionRecord、删除应用及文件、暂不过期 | [平台存储与生命周期](platform/storage-lifecycle.md) |

历史最近轮数和执行参数都可按单个 Agent 配置。第 100 次 Loop 正常，第 101 次禁止。补充是正式用户输入，不因历史裁剪而消失。Hook 失败不阻断，合法拒绝可阻止执行；修改由权威上下文保存。模型流内部接收，完整模型响应后发事件，最终结果随 ExecutionRecord 终态交付。

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
