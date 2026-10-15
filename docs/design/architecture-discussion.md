# SD-3 架构决策与后续事项

> **重建边界（2026-10-15）**：旧业务实现已移除。本页保留为待调整的设计草案；用户、应用、发布与配置版本属于平台，SDK 只接收已解析配置并负责执行与恢复。涉及平台身份、发布绑定及表结构的内容需按 [SDK / Platform 边界](sdk-platform-boundary.md)重新划分，不直接作为 SDK 的必填契约或已实现功能。

2026-10-15。状态：**本轮完整架构提案，供整体评审，尚未实现。** 本文件替换此前开放问题清单；用户确认的产品行为保存在[Agent 设计约定](../agent-design-decisions.md)，本轮技术选择在以下记录中列明。已给出方案的问题不再保留为“尚未设计”。

## 1. 需求与技术建议的区别

已确认需求包括：配置化 Agent、ReAct／Plan–Executor、用户编排 Agent／工具的 Workflow、Node／ReactNode／PlannerNode／ExecutorNode 行为复用、私有上下文与共享工作文件、可配置预算、集中且分范围的检查点、统一 EventBus、可允许／拒绝／修改的 Hook，以及模型整段结果交付。2026-10-15 补充：外层图的状态、同步与检查点计划复制并适配 LangGraph 源码，影响范围见第 6 节。

用户本轮要求进一步完成 Definition、Node、检查点、实例 UML、交互和领域接口，并为未来外层工作流完整设计。下面的具体类型、提交协议和调度策略是据此提出的工程方案，不冒称用户此前逐字段确认。它们作为一套方案评审，不因写入就代表已有实现。

## 2. 对前轮问题的处理

| 原议题 | 本轮方案 | 依据 |
| --- | --- | --- |
| Q01 领域边界 | 应用发布、图编排、运行执行、资源、上下文、环境与恢复基础设施分别维护事实 | [领域模型](domain-model.md) |
| Q02 Agent 定义 | Agent 是应用；AgentDefinition 是版本化行为；RoleDefinition 为内部组成 | [定义 UML](domain-model.md) |
| Q03 图定义 | GraphDefinition 独立身份与版本；生成图和用户编排图共用契约 | [图与 Node](graph-node-engine.md) |
| Q04 节点绑定 | NodeDefinition 是结构位置；Release 保存 NodeBinding，固定角色／能力／完整 AgentRelease 引用 | [数据契约](data-contracts.md) |
| Q05／Q06 节点职责 | ReactNode 完整任务、多轮 Loop；NodeDriver 消费 Progress／Wait／Complete／Fail | [执行协议](graph-node-engine.md) |
| Q07 实例身份 | Run、AgentInvocation、GraphExecution、NodeTask、NodeActivation 分开；旧 AgentExecution 仅投影 | [实例 UML](domain-model.md) |
| Q08 调度与恢复 | 内部保持单机调度和动作批次协议；外层采用源码适配方向；恢复身份及可靠后继交接保持一致 | [检查点](checkpoint-protocol.md)、第 6 节 |
| Q09 上下文与文件 | GraphState、RoleContext、Continuation 各有所有者；同 Agent 内角色共享授权文件 | [上下文](context-lifecycle.md)、[领域关系](domain-model.md) |
| Q10 发布与版本 | AgentRelease／WorkflowRelease 固定定义、绑定、编译身份和资源清单；发布交接不热改 Run | [领域接口](domain-interfaces.md) |
| Q11 调用边界 | 统一 EventBus＋Manager；begin 返回持久等待引用，唯一最终 EventResponse；有序 Hook | [领域接口](domain-interfaces.md)、[消息设计](runtime-messaging-checkpoints.md) |
| Q12 外层工作流 | WorkflowDefinition／Release、AgentNode／CapabilityNode、条件循环和 Join；本轮只设计 | [外层设计](graph-node-engine.md) |
| Q13 可恢复节点 | NodeCheckpoint／ReactContinuation、公共 NodeDriver 和 CommitCoordinator；不恢复 Python 栈 | [检查点协议](checkpoint-protocol.md) |

## 3. 关键取舍

| 编号 | 选择 | 理由与代价 |
| --- | --- | --- |
| A01 | 双引擎：内层 AgentGraphEngine，外层 WorkflowEngine | 各自维护调度语义，共用调用与恢复基础设施；同一图范围只有一个调度权威 |
| A02 | Executor 是同一内层图的任务，完整 AgentNode 才创建内层图 | 避免每个角色都附带一层重复图状态；任务仍有完整恢复能力 |
| A03 | Definition、绑定、编译产物分开 | 可复用结构并校验绑定；发布元数据较多但身份清晰 |
| A04 | 内部保留 ready queue＋动作批次；外层按 LangGraph 源码适配 | 旧外层局部 Join 算法暂作对照，不与 Pregel 的状态可见性混用；见第 6 节 |
| A05 | GraphState 与 RoleContext 分开 | 防止角色消息共享泄漏；跨域提交需版本关联 |
| A06 | 集中持久化、快照＋任务增量 | 并行成功项可复用；需要一致读取水位、lease 和 outbox |
| A07 | 调用代理与公共 Driver 低侵入 | 业务方法不用自行发消息／保存；自定义 Node 仍必须实现可恢复状态契约 |
| A08 | Hook 合法拒绝生效，故障 fail-open | 遵守已确认产品规则；核心权限和必要存储不能依赖用户 Hook |
| A09 | 未知 Hook 结果先查询，无法确认则记 indeterminate 并放弃未提交修改 | 与 fail-open 一致且不盲重放；无法撤销已发生的 Hook 外部副作用 |
| A10 | 外层 Session、策略、文件授权显式建模 | 多 Agent 组合不等于共用一个 Agent 的私有历史／文件根；外层产品参数后续实现 |
| A11 | 外层明确支持顺序、条件、循环、并行和汇合，具体拓扑能力由适配验证确定 | 旧稿“分支内禁止再次 fork”等限制来自自研方案，不再直接作为产品约束；不支持的结构仍须发布前拒绝 |
| A12 | 双patch归一、冲突整项修改无效、合法deny独立判断 | 工具实参与模型有效记录同源，避免不同视图表达相互矛盾的执行 |
| A13 | 单份LoopTicket同时约束根与Invocation | 局部上限、deadline与失败传播明确；暂时被预留占满是等待而非耗尽 |
| A14 | 60%为压缩软目标；最终可用窗口为硬边界 | F超过软目标仍按剩余空间摘要，不能把目标未达成误报超窗 |
| A15 | Executor终态释放名额、父结果Hook后结算成员 | 执行容量与交付条件分离；串行仍等待完整成员结束 |
| A16 | 停止关闭新动作，在途调用收束到安全提交点 | 不为完成一整轮而继续启动排队动作；根结束且写入者静默后才清理 |
| A17 | 类型化OwnerRef／WorkspaceScope覆盖外层工具与无Session调用 | Workflow自身拥有文件根与系统内容，Agent子调用按Invocation分区；授权和恢复不依赖伪造Session，删除沿来源依赖清理 |
| A18 | begin 前拒绝／取消使用本地终结结果 | 无 Operation 的成员也能确定结算；已开始的调用仍须经过最终响应链，前置 Hook deny 不伪造 Executor |

LangGraph 的源码事实与本平台取舍分开记录，固定代码基线在[图引擎](graph-node-engine.md)和[检查点](checkpoint-protocol.md)。当前尚未复制源码或接入依赖；后续外层按第 6 节的源码适配方向推进，不能把计划写成已实现或已兼容。

## 4. 已细化的实施方案与剩余验证

[实施规格](implementation/index.md)已补齐Hook字段白名单和patch算法、注册/ABI与编译过程、持久表/索引/事务、快照频率、调用状态、模型门控、文件冲突和执行域撤销。它们不再列为未设计项。

独立审阅还促成以下明确化：统一预算公式；raw模型结果保存时固定调用槽；批次按成员全结算判断就绪；补充固定prepare水位；无Worker也可提交停止意图；控制关闭时后置Hook有skipped_control路径；Release持久固定编译产物引用；共享/work与角色只读资源视图分别建模。

内部 Agent 剩余工作是实现和证明：生成生产DDL/DTO/OpenAPI、索引及容量压测、多进程故障注入、真实模型与MCP API测试、NFS和执行域撤权验收。外层编辑器与执行入口仍后置；ID-3 的旧外层编译／调度／恢复算法需先按第 6 节完成源码适配映射，再成为外层实施基线。不能把“已写明旧方案”当作新选型的详细设计已经完成。

## 5. 本轮评审与验证边界

建议先按[总览](overview.md)检查领域和实例关系，再核对[节点协议](graph-node-engine.md)及[检查点](checkpoint-protocol.md)，最后看[接口](domain-interfaces.md)和[验收计划](verification-roadmap.md)。

文档构建、链接、Mermaid 和结构样例校验用于发现设计制品错误；不能代替后续事务、并行、宕机接管和真实模型 API 验收。外层已有领域与交互设计；此次技术方向调整后，状态与持久化实现映射需要补齐。

## 6. Workflow 产品范围与 LangGraph 源码适配

2026-10-15 的新增输入是：Workflow 编排必须出现在产品目标和基本概念中；其外层图状态管理、同步等能力计划复制 LangGraph 源码后适配。**这是后续实现方向，本次只更新文档，尚未导入上游代码。**

### 6.1 保留的产品和领域边界

- 用户配置 ReAct／Plan–Executor Agent，也可编排完整 Agent、Tool、MCP，形成独立 Workflow。流程结构、数据映射和可选路由由工作流发布固定；Planner 计划文本不自动成为外层图。
- AgentDefinition 描述行为，GraphDefinition 描述结构，Release 固定绑定。Agent 的内部 Executor 仍是角色任务；只有完整 AgentNode 调用创建新的 AgentInvocation 和内部 GraphExecution。
- Workflow 的 GraphState、Agent 的私有模型上下文、节点恢复位置分别管理。多次调用同一发布不共享可变上下文；文件通过已有授权范围和 ArtifactRef 传递。
- Run 是根控制范围。停止、截止、预算、租约和删除规则不会因为接入外层引擎而失效。外层不能绕过能力调用管道及 Hook，内层不能重复执行父图已登记的调用。
- 内外层各有自己的检查点命名空间，由平台统一管理读取、授权和清理；不要求快照内部字段完全相同。外层必须能按稳定调用键接回原内层实例。

**本轮已明确选择双引擎，不再将引擎数量列为待定。** 内层 AgentGraphEngine 保留有限的角色调度和动作批次；外层 WorkflowEngine 将 LangGraph 相关设计逻辑及源码迁入 AgentLoom 包中适配，不以直接引用外部 `langgraph` 包作为方案。两者通过 AgentNode 的稳定调用关联协作，通过各自 CheckpointAdapter 使用统一提交与存储基础设施。具体流程、载荷判别和 Saver 映射见[双引擎与检查点适配](dual-engine-integration.md)。**同一 GraphExecution 的调度与状态更新只能有一个权威来源**，禁止两套调度器同时推进同一张图。

### 6.2 拟复用的能力与平台负责的部分

以下依据本地源码提交 `afbe7e48b17f62e139b5c6337ab934549e6dc73b`，迁移开始前再固定实际导入版本和文件范围。

| 能力 | 源码依据 | 适配责任 |
| --- | --- | --- |
| 图定义、状态 schema、节点连接和编译 | [StateGraph](https://github.com/langchain-ai/langgraph/blob/afbe7e48b17f62e139b5c6337ab934549e6dc73b/libs/langgraph/langgraph/graph/state.py#L131) | 将已发布 GraphDefinition 和节点绑定编译到内核；发布与资源权限仍属 AgentLoom |
| 状态更新与并发同步 | [Pregel 执行阶段](https://github.com/langchain-ai/langgraph/blob/afbe7e48b17f62e139b5c6337ab934549e6dc73b/libs/langgraph/langgraph/pregel/main.py#L460)、[apply_writes](https://github.com/langchain-ai/langgraph/blob/afbe7e48b17f62e139b5c6337ab934549e6dc73b/libs/langgraph/langgraph/pregel/_algo.py#L233) | 评估 channels、reducer、super-step、条件／动态分派作为一组运行语义，不只复制一个合并函数 |
| 快照及任务级已完成写入 | [Checkpoint／CheckpointTuple](https://github.com/langchain-ai/langgraph/blob/afbe7e48b17f62e139b5c6337ab934549e6dc73b/libs/checkpoint/langgraph/checkpoint/base/__init__.py#L93)、[BaseCheckpointSaver](https://github.com/langchain-ai/langgraph/blob/afbe7e48b17f62e139b5c6337ab934549e6dc73b/libs/checkpoint/langgraph/checkpoint/base/__init__.py#L199) | 映射 scope、执行权、pending writes 和平台内容引用；与 Operation、Hook、父子调用事实衔接 |
| 完整 Agent 与能力步骤 | AgentLoom 的 AgentNode／CapabilityNode 协议 | 通过平台调用管道执行，保留授权、重试、有效结果与私有上下文边界；不以预置 Agent Loop 替换现有产品行为 |
| 源码和依赖维护 | [上游依赖清单](https://github.com/langchain-ai/langgraph/blob/afbe7e48b17f62e139b5c6337ab934549e6dc73b/libs/langgraph/pyproject.toml)、[源码许可](https://github.com/langchain-ai/langgraph/blob/afbe7e48b17f62e139b5c6337ab934549e6dc73b/LICENSE) | 记录导入版本、文件、修改和测试；随导入保留版权与许可；明确依赖保留／适配范围 |

本地 Pregel 实现采用分阶段推进：本步运行中的节点看不到同一步其他节点尚未归并的 channel 更新，更新供后续步骤读取。旧稿则采用“fork 基线＋分支私有写前缀，整条分支结束才 Join”。**两种可见性规则不同，不能同时作为同一外层执行的规则。** LangGraph 快照里的 `channel_versions/versions_seen` 也不能直接改名成平台的 `scope_commit_seq/context_revision`。上述映射须按实际运行语义设计并验证。

### 6.3 对现有详细规格的影响

| 现有内容 | 本次状态 | 后续处理 |
| --- | --- | --- |
| Agent 两种模式、公共 Loop、Operation／Hook、模型上下文和环境 | 内层采用 AgentGraphEngine；能力和控制协议保持 | 不以外层 Pregel 调度替换内部 Loop／批次，验证外层调用不会绕开共同边界 |
| Workflow 应用、版本绑定、AgentNode 身份和父子结果衔接 | 保持领域需求和协议目标 | 映射到导入内核的节点／任务／检查点身份，不重复创建子调用 |
| 外层 branch_scope、BranchCursor、loop_path、配对 Join 与分支视图算法 | 旧自研候选，不再作为外层最终编码指令 | 对照 LangGraph 调度语义重新确定内部结构；不直接把禁嵌套 fork 等旧限制变成产品限制 |
| 外层 TaskWrite／ScopeCommit 表结构和事务步骤 | 当前为自研协议草案 | 明确内核 checkpoint／pending writes 与平台一致提交的衔接；不能让 Saver 和 Coordinator 各自维护相互冲突的权威状态 |
| 外层验收矩阵与契约样例 | 产品不变量保留，算法特定断言待重基线 | 保留结果不丢失、已完成不重复执行、父子身份稳定、授权和控制等断言；重写依赖旧分支可见性或排序的断言 |

这一状态说明同时适用于图引擎第 7.3–7.6 节、ID-3 第 10 节、数据契约及持久化文档中的外层分支结构。内部 Agent 的 dispatch_batch 不因外层选型变更而改成 Pregel step；业务 Loop、图 super-step 和检查点提交序号依然分别计量。

### 6.4 外层开始编码前需完成的映射

1. 固定源码导入范围、必要依赖、保留的运行语义及对上游修改的测试覆盖。
2. 按已确认的双引擎边界固定 GraphDefinition／GraphExecution 的 engine_kind，完成外层内核任务与平台 NodeTask 的稳定映射，不能在恢复时换引擎。
3. 按[失败与重入映射](dual-engine-integration.md#53-失败取消和重入的确定映射)实现 AgentNode／CapabilityNode 适配：业务 failed／blocked 作为完成结果进入错误路由，本机等待取消不停止子调用，根停止禁止后继；原生恢复始终接回原 Operation，不叠加原生业务重试。
4. 实现 WorkflowCheckpointAdapter，将原生快照和 pending writes 经 CommitCoordinator 写入统一存储；按[提交屏障](dual-engine-integration.md#63-任务写入的持久提交屏障)等待全部必要写入，包括普通非 Delta 输出、空输出和动态分派。完成父消费与原生任务 writes 的一致提交，测试“子完成但父未消费”“写入保存但快照尚未生成”和保存失败禁止越过屏障。
5. 基于适配内核公布流程结构、并发和状态可见性规则，再更新对应 Schema、DDL 和验收断言。先完成最小 API 验证，不以画布演示替代协议验证。

这份清单限定后续工程工作，不要求用户现在选择底层字段或代码文件，也不把导入上游源码等同于已获得完整 AgentLoom 工作流产品。
