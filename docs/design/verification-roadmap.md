# 实现顺序、设计追踪与验收

> **重建边界（2026-10-15）**：旧业务实现已移除。本页保留为待调整的设计草案；用户、应用、发布与配置版本属于平台，SDK 只接收已解析配置并负责执行与恢复。涉及平台身份、发布绑定及表结构的内容需按 [SDK / Platform 边界](sdk-platform-boundary.md)重新划分，不直接作为 SDK 的必填契约或已实现功能。

**实施细则：[index](implementation/index.md)**。本页保留架构解释；具体方法、字段状态、事务和恢复分支见实施规格。

[总览](overview.md) · [契约](data-contracts.md) · [执行](agent-execution.md) · [存储恢复](storage-recovery-api.md)

版本：SD-3，2026-10-15。状态：架构提案的实施与验收计划，旧运行代码已移除，本文等待按新交付边界实施，下面各场景尚未执行。实例、节点协议和检查点定义见[领域模型](domain-model.md)、[图与节点](graph-node-engine.md)、[检查点协议](checkpoint-protocol.md)及[领域接口](domain-interfaces.md)。Workflow 属于产品目标，本轮不实现编排 UI／运行代码；外层已调整为 LangGraph 源码适配方向，算法相关断言须按[适配决策](architecture-discussion.md#6-workflow-产品范围与-langgraph-源码适配)重新确定。

## 1. 已确认要求如何落实

| 要求／设计工作 | 方案位置 | 必须可验证的结果 |
| --- | --- | --- |
| 配置生成 Agent、固定执行模式 | 领域模型、AgentRelease、发布 API | 创建后 mode 不变；草稿不能运行；一次 Run 引用固定发布版本 |
| 用户编排 Workflow | 产品目标、WorkflowDefinition／Release、源码适配决策 | 支持完整 Agent／Tool／MCP、映射、条件、循环、并行与汇合；发布后页面／API 运行；外层不覆盖 Agent 私有配置 |
| T01 领域对象和状态 | domain-model、data-contracts | AgentDefinition、GraphDefinition、发布绑定、Run、AgentInvocation、GraphExecution、NodeTask、Operation 各有明确身份 |
| T02 模块边界 | overview、agent-execution、runtime-messaging-checkpoints | 图引擎管理节点调度，ReactNode 公共 Loop 供 Planner／Executor 复用；代理、总线和适配器不另建业务 Loop |
| T03 可恢复节点与图协议 | agent-execution、storage-recovery-api | 外层／内层按范围分别保存，图位置与节点阶段可恢复；不序列化 Python 栈 |
| T04 委派和批次 | agent-execution | 同配置两次委派创建两个独立执行；并发数受用户配置约束；全批结果后父角色继续 |
| T05 一致提交 | storage-recovery-api | 已保存结果不因父角色未消费而重复调用或扣额；完成状态与后继可执行事实一致提交，通知丢失可补发 |
| T06 额度和停止 | agent-execution、context-lifecycle | 全 Run 共享 100；第 100 次可正常完成，第 101 次不执行；停止关闭启动入口 |
| T07 跨机恢复 | storage-recovery-api | 保持原 Run、执行实例、调用和上下文身份；旧租约不能提交或继续写卷 |
| T08 上下文和压缩 | context-lifecycle | 历史选择规则不变；Hook 修改进入权威有效上下文，后续 Loop／历史／压缩／恢复可见，旧摘要不覆盖新 patch |
| T09 能力、消息和 Hook | capabilities-hooks、runtime-messaging-checkpoints | EventBus 统一各域通信；分发前有序 await Hook 的校验和修改；Manager 消费获准最终 payload；合法 deny 与失败放行分别处理、必需持久化独立；SDK 无隐式多层重试 |
| T10 文件和环境 | capabilities-hooks、storage-recovery-api | 主子共享本次工作文件，Session 隔离；临时文件 Run 终结后删除 |
| T11 配置与 API | data-contracts、storage-recovery-api | 配置范围为单 Agent；流、输入、停止、发布和删除竞争有确定结果 |
| T12 验收 | 本文 | 契约、阶段、并发、故障和 API 覆盖真实不变量 |
| Agent 删除／暂不过期 | domain-model、storage-recovery-api | 专属记录和文件删除；共享资源保留；正常记录无自动 TTL |

T01–T12 保留为追踪编号，本轮以 SD-3 给出的定义、绑定、任务和实例关系为验收依据；不能继续保留旧 NodeTask／图状态的双重可写来源。设计制品通过不等于运行代码通过。

## 2. 实施阶段与退出条件

本轮交付完整领域与协议设计，不立即按表改运行代码。阶段编号表达后续实施和验证顺序；所有测试均需真实运行后再标记通过。

| 阶段 | 设计／实现内容 | 退出条件 |
| --- | --- | --- |
| M1 领域和配置契约 | Definition、NodeBinding、资源版本、GraphCompiler、稳定身份 | 定义不可变；节点类型、位置、任务分离；编译错误有明确位置；缓存不混用发布绑定 |
| M2 调用与消息边界 | 统一 EventBus、装配代理、请求／回复、有序 HookManager、各域 Manager、观察订阅 | 通信清单完整；获准的最终 payload 才分发；deny 有关联错误回复；原始／有效内容与 Hook 进度可追踪；无双执行或递归路由 |
| M3 图、节点与 Loop | AgentGraphEngine、NodeDriver、ReactContinuation、WaitGroup／Join、CallBatchCoordinator | 内层唯一就绪队列；同一图承载 Planner 和 Executor；安全点与任务身份稳定；全批交付及额度边界通过 |
| M4 上下文与输入交接 | ContextManager、有效视图与 patch、历史投影、用户补充、压缩、格式修正 | Hook 修改持续可见且保留来源；补充不丢；有效版本与消费一致，压缩不消耗业务 Loop |
| M5 统一恢复与环境 | CheckpointStore、raw／effective 结果、Hook 进度、父子引用、一致推进、执行权、工作卷 | raw 保存后不重做外部操作，复用已完成 Hook 决定；宕机接管通过；无新旧写入者竞争 |
| M6 应用 API 与交付 | 发布、Session、输入、低频 SSE、完整结果、停止与删除 | 模型内部流式但不转发 token；Run 终态才交付最终答案；HTTP/SSE 端到端通过 |
| M7 替换和部署 | 新协议接管两种模式；移除旧调度路径；更新运维说明 | 确定性 API 全链路与已配置真实模型关键路径通过，再发布测试环境 |

M3 与 M5 依据同一 NodeOutcome／RuntimeCommit 协议实施，不能实现两套独立可写状态再临时同步。内存适配器可以先验证调度，但持久恢复、接管和 fencing 必须用真实存储与独立 Worker 验证；结构依据检查点及数据契约文档。

M8 为未来外层工作流实现：WorkflowRelease、AgentNode、CapabilityNode、条件／循环／Send／Join 与共享状态 reducer。当前交付 M8 的设计和下方测试计划，不把它列成本轮必须实现的代码。

不同适配器可以并行开发，但进入 M7 时不留下长期“旧 Plan／新 Plan”开关。本文不触发清空数据库或运行部署；实施时按明确授权的目标环境和数据范围操作。

## 3. 图、节点与调用批次测试

| 场景 | 输入 | 关键断言 |
| --- | --- | --- |
| ReAct 直接回答 | 一次模型最终答案 | 一个 AgentInvocation、一个 GraphExecution、一个 React NodeTask 和私有 Context；没有 Executor，不绕开统一图协议 |
| ReAct 多轮工具 | 读文件→工具结果→再次模型→回答 | 每轮同一 node_task_id/context_id；role_loop_index 递增；不新建角色实例 |
| Planner 普通工具 | Planner 只调用 MCP 或本地工具 | 仍在原角色内部循环；不创建子执行器 |
| 两次同名委派 | 同一 Executor 配置接收两个不同任务 | 一个 GraphExecution 中产生两个 Executor NodeTask 及私有 Context；与原 Planner 共三个任务；共享授权工作文件 |
| 重复协作循环 | Planner 委派→汇总→再次委派→回答 | Planner 始终同一实例；每个新委派有新的 call_id 和子执行实例 |
| 未配置 Executor | Plan Agent 没有可委派角色 | 工具列表不含虚构默认 Executor；Planner 可直接用已授权工具完成工作 |
| 串行混合批次 | tool、executor、tool；parallel=false | 严格按模型调用 ordinal 执行；一次响应多个调用不等于允许并行 |
| 并行滚动名额 | E1 慢、E2 快、E3 排队；max=2 | E2 NodeTask 终态和 TaskResult／TaskWrite 提交后释放名额，父结果 Hook 尚未完成也允许 E3 补位；E2 成员仍不得提前结算 |
| 串行结果 Hook 较慢 | E1 结束，后置 Hook 未提交，E2 排队 | 即使 E1 不再占执行名额，E2 仍等待 E1 最终 EventResponse；恢复不改变串行顺序 |
| 并行工具与委派 | 普通工具和多个 Executor 同批 | 按配置执行；Executor 容量独立计数；不引入全局模型并发预算 |
| 一项失败 | 一个工具失败，其他成员成功 | 其余继续；失败关联原 call_id；全批完成后一次交给当前角色 |
| 父角色等待 | Planner 等批次，两个子角色继续循环 | 父 Wait 释放 Driver 推进资源，Executor 等模型／工具期间仍占自己的活跃名额；不存在父占满推进池的死锁 |
| 可恢复 begin | 已准入成员取得 PendingOperationRef，父提交 Wait | dispatch 意图、父等待与恢复键一致提交后才启动子；ref 不当最终结果发给 LLM |
| 串行未启动项 | 混合批次第二成员仍排队 | 不提前 begin 执行后续项；冻结成员身份不要求同时启动全部 Operation |
| begin 前拒绝或取消 | 参数错误，或停止时成员尚未启动 | local_outcome 与 BatchMember 结果、settled 状态一致提交；空目标不阻塞全批结算，恢复不补建 Operation／Executor |
| 前置 Hook 拒绝委派 | Operation 已建立，Executor 尚未创建 | 等待目标为已有 Operation；最终拒绝响应结算，不能要求虚构子任务或用 local_outcome 绕过处理链 |
| 已结束成员重报 | 同一成员完成通知再次到达 | 结果不追加两次、容量不释放两次、父角色不被重复唤醒 |
| 不受支持的委派 | ReAct／Executor 输出不可用的委派调用 | 形成明确错误结果；不绕过模式和角色授权创建实例 |

### 图定义、编译与外层协议验收矩阵

| 范围／场景 | 必须验证的结果 | 实施阶段 |
| --- | --- | --- |
| 图与行为版本组合 | 相同 GraphDefinition 可绑定不同角色；CompiledGraph 缓存包含绑定 hash 与实现版本，不串 Prompt／权限 | M1 |
| 节点端口或绑定不匹配 | 发布前报出 node_key 与 schema／绑定诊断，不运行后才猜类型 | M1 |
| 定义非法路由 | 未注册目标、不明确 Join、无受控循环、越权 AgentRelease 引用编译失败 | M1 |
| 可恢复激活 | Progress 提交才继续；Wait 唤醒同 node_task_id；Complete 不再激活，NodeActivation 可变化 | M3／M5 |
| Planner 委派返回 | 调度只唤醒等待的原 Planner，不经完成回边另建 Planner 任务 | M3 |
| 动态 Send 两次到同节点 | 产生两个 member_id／NodeTask，不产生两个 GraphExecution | M3 |
| 未选条件分支 | 不加入当前 WaitGroup 成员，不造成 Join 永久等待 | M3／M8 |
| reducer 合并 | 内部按冻结批次顺序交付；外层按适配内核声明的稳定更新规则归并，恢复不重复累计 | M3／M8 |
| 无 reducer 多写同字段 | 编译排除或运行明确报冲突，不使用最后完成者覆盖 | M3／M8 |
| 流程结构支持范围 | 按迁移后的能力矩阵验证顺序、条件、循环、并行及汇合；不支持的结构在发布前明确拒绝，不默认继承旧自研拓扑禁令 | M8 |
| 多步骤分支 | fork → (A1→A2) 与 B1 → 汇合；A1+B1 完成不能误报整条A分支结束；A2输入与同级状态可见性按适配内核规则验证 | M8 |
| 条件跳过与失败 | 已创建分支选择退出则显式 skipped 关闭；未选择创建的分支不入成员集合；失败保留已提交写入和错误，其他分支继续 | M8 |
| 循环内再次分派 | 新迭代与旧迭代有稳定可区分身份；迟到通知不误结算新任务，内核字段映射随适配确定 | M8 |
| A1完成交接时宕机 | 已保存A1结果、外层checkpoint／pending writes和A2待执行事实可一致恢复；不重跑A1或漏掉A2 | M5／M8 |
| 外层 AgentNode 调用 ReAct | 外层 1 图＋内层 1 图，AgentInvocation 1 份，外层任务与内层任务分离 | M8 |
| 外层两次同 AgentRelease | 外层 1 图＋内层 2 图，调用键和私有上下文不串用 | M8 |
| 外层回边重访已完成位置 | 创建新 NodeTask，保留旧任务事实；不与 React 内部 Loop 混为一谈 | M8 |
| 父等待子图 | 释放推进资源，子图可取得运行机会，不出现父占满 worker／令牌而等待子图 | M8 |
| 子图完成但父未消费 | 接管只补交付结果，不再创建 AgentInvocation，不重跑内层 | M5／M8 |
| 内外层恢复范围 | 外层引用内部检查点范围，不能将内层 messages 全量复制进外层现场 | M5／M8 |
| 无Agent的外层工具 | 仅CapabilityNode组成的Workflow可在自身文件根执行文件/MCP，InvocationContext不伪造Agent或Session，系统blob归Workflow | M8 |
| 无Session的完整Agent调用 | 同Agent两次input_only调用按各自invocation分区；恢复复用原scope；持久与临时清理分别验证，跨调用需Artifact授权 | M8 |
| 源Agent删除与父投影 | 私有blob与派生父载荷按source_owner依赖清除；外层不保留私有副本，其他Agent和Workflow自身无关数据不误删 | M8 |
| 分支含两个逻辑任务 | A1、A2有各自稳定任务及恢复身份；不将中间任务完成等同分支完成；不强制采用旧wait/member字段布局 | M8 |
| Session 与工作文件授权 | WorkflowSession 不伪装成 Agent Session；ConversationScope 与不同应用文件范围显式授权 | M8 |

M8 是外层验收目标，尚需根据源码适配补齐具体输入和内核级断言；旧的自研分支可见性、排序或字段布局不能直接当作上游行为。图实例计数、权限、根控制、结果复用和父子恢复边界继续保留。本轮不实现外层编排接口、SubgraphNode 编辑器或 LangGraph API 兼容层。

## 4. Loop、错误、上下文和控制

| 场景 | 关键断言 |
| --- | --- |
| 默认配置 | Loop 100、整 Run 一小时、并行关闭、Executor 最大并行 2、自动重试关闭、max_attempts=2（含首次）、重试间隔 2 秒 |
| 第 100 次合法最终回答 | 正常 succeeded；未发第 101 次；没有额外总结调用 |
| 第 100 次仍需继续 | 完成本次已产生的合法状态处理后，新业务 Loop 准备入口返回 LOOP_EXHAUSTED，不先追加压缩请求 |
| 工作流局部额度 | 根100次，A局部5次、B局部20次；A第6次只返回Invocation预算错误，B继续；根耗尽才终结整树 |
| 预占暂满 | 根或局部 consumed 未到limit、reserved占满时等待；票据释放后按固定作用域重试准入，不误报耗尽 |
| 局部截止与接管 | Invocation创建即起算，排队计时；迁移／重试不重置，取根与局部较早截止；已终结调用不得因迟到结果复活 |
| 双范围票据事务 | 根与局部检查在同一事务，统一LoopTicket；失败不能只扣一边、恢复不双扣 |
| 多角色竞争最后票据 | loops_consumed＋loops_reserved 不超 limit；票据按 model_operation_id 幂等预留 |
| 子角色模型失败耗尽最后预留 | 失败反馈槽与 ticket 消费同事务；不会等待全批而阻塞其他成员判断额度；不会提前启动 Planner |
| 格式不合规 | 校验反馈进入同角色下一 Loop；没有独立修正额度 |
| 透明模型连接重试 | 同一 Operation、冻结参数不变、Attempt 增加；有效成功决策只结算一次 Loop |
| 用户关闭自动重试 | 首次连接异常直接进入约定错误处理；底层 SDK 不另行重试 |
| 用户开启重试 | 仅匹配异常及剩余次数允许再次尝试；最大两次执行；等待 2 秒；整 Run deadline 始终有效 |
| LLM 自己重试工具 | 新调用成为新 Operation，独立尝试额度；参数相同不被偷偷去重 |
| 未知错误 | 工具错误交 LLM，根模型不可继续则 Run 失败；不伪称未产生外部副作用 |
| 首输出前失败／超时 | 即使无内容也有异常终态；模型首输出默认 60 秒 |
| 首输出后持续流式 | 适配器内部收流，不发送 token／chunk 事件；不再用首输出 60 秒计时，整 Run 截止仍有效；连接错误按 Agent 配置处理 |
| 片段后重试 | 旧 Attempt 片段单独保存并标未完成，不对外当成功答案；新响应独立组装，不拼接不同尝试 |
| 工具／脚本超时 | 默认 10 秒；受控脚本取消／隔离后才释放写入域；错误按调用规则交回 |
| 最近 N 个已结束 Run | 默认 2，成功／失败／停止都计入，不向前补成功；每 Agent 可配置 |
| 较早历史降详细度 | 所有原始用户提问与补充、最终结果或结束状态保留；内部工具过程省略 |
| 当前上下文压缩 | 原问题和补充不丢；压缩模型继承；默认 Prompt＋用户补充 Prompt；不计业务 Loop |
| 压缩软目标 | B=100000、F=65000、材料20000仍可压缩；60%不足不是失败理由，最终硬预算重新校验 |
| 无可压缩材料 | I处于80%到硬预算之间，记录评估并执行；超过硬预算才失败；同ModelOperation不空压缩循环 |
| 压缩失败／仍超硬窗口 | 提示用户并终止；不做摘要语义质量验收、不删除原始记录 |
| 执行中补充 | Plan 模式只进 Planner 下一次请求；不广播给已有 Executor；ReAct 进入根角色下一次请求 |
| 补充遇到结束 | 已接收但尚未送达模型的输入保留，转下次用户提问；不自动创建 Run |
| 停止与总超时 | 关闭新业务工作；当前Loop中未开始的动作取消，在途调用到安全边界收束；所有写入者终结／隔离后结束与清理 |
| Hook 自身失败 | 处理器异常／超时后继续，用此前有效 payload／context；前面成功的修改不回滚；必要存储、业务 schema 和权限错误不归入此放行规则 |
| 发布时有活动 Run | 停止旧 Run 后切换发布指针；后续新提问用新版本；原文件根和历史不变 |

Loop 计数还要覆盖失败结果是否进入后续上下文的分支。检查实际票据状态与消息记录，不用“模型请求总次数”代替业务 Loop 统计。

## 5. 调用代理、消息与输出验收

| 场景 | 关键断言 |
| --- | --- |
| 普通接口调用 | 装配后模型、工具、MCP 等通过代理接入统一 EventBus；业务调用处不散布消息和检查点代码 |
| 原始处理器执行 | 同一次 Attempt 不因代理装配而执行两遍原始实现；处理器不调用自身代理；不会既本地执行又消息执行 |
| 多类调用审计 | 覆盖模型、Embedding、上下文读取／更新／压缩、能力、Executor、环境和 Runtime 控制；不只统计 Tool 调用 |
| 单请求执行负责人 | 一个请求只由一个能力域 Manager 执行；观察订阅者不能各自重复产生该请求的副作用 |
| 多请求 list | 每项独立关联 ID 和结果；逐项进入既定 Hook／分发流程，按 Agent 配置串行或并行；不会因列表而跳过批次汇合 |
| 多 Hook 顺序 | 每项有序 await；后一项看到前一项的合法修改；未完成校验不能先分发；某项自身失败沿用此前有效状态 |
| 同一Hook双patch冲突 | payload改当前路径为b，context改为c：整项patch无效，不执行b却记c；合法deny仍生效，allow沿用前值 |
| 双patch一致与别名 | 同字段同值仅应用一次；仅context修改当前调用字段也同步payload；对象替换与子字段修改必须能证明一致 |
| 已冻结调用事实 | 已冻结但未发送的请求也不可被后续Hook改写；已执行事实只允许来源注释，重试仍用冻结值 |
| 请求 Hook 修改 | Manager 接收最终校验后的 effective payload；修改若影响模型可见内容则由 ContextManager 保存，不只改临时出网副本 |
| 合法前置 deny | 不调用原能力，不伪造 Attempt 成功；关联原 request 返回 blocked／错误和原因；不通过自动重试绕过 |
| deny 与处理器失败 | 有效 deny 阻止被拒动作；抛异常／超时不等于 deny，按 fail-open 继续；两者审计状态不同 |
| 响应 Hook 有序修改 | 先保存 raw，再处理响应链；effective 结果、有效 context revision 与消费关联一致保存后交付；raw 不提前触发动作 |
| 合法后置 deny | raw 与实际副作用保留，返回关联错误而非原业务结果；不重执行工具，不宣称已撤销，后续模型可见拒绝原因 |
| 可信信封不可修改 | Hook 不可改变类型、关联 ID、causation、权限或可信 scope；不能路由到别的调用或伪造归属 |
| 响应不可丢失 | allow／修改／deny／Hook 自身失败均保留原关联回复；无处理器返回明确错误，不能吞响应或永久等待 |
| 恢复重投 | 同一调用 ID 的 effective 终态已提交则直接复用；只有 raw 时继续原响应链，不重执行外部操作；不重复扣额 |
| LLM 再次发起相同参数 | 创建新调用身份；不通过参数 hash 把新的业务调用误判为恢复重投 |
| 必需提交失败 | raw、Hook 决定、effective 或 context 修订必需提交失败均不可假成功应答；Hook fail-open 不忽略存储故障 |
| patch 自身引入 schema 违规／越权 | 丢弃当前无效项、保留前序有效修改并重新校验；不能携带不合法参数出网 |
| 原输入或最终系统校验失败 | 返回相应业务／Runtime 错误；不能以 Hook fail-open 忽略原输入错误、执行权变化或存储失败 |
| 存储内部调用 | 检查点、Hook 进度和状态提交使用底层存储端口，不再次进入同一 EventBus 处理链 |
| 长文本模型流 | 无论收到多少片段，收齐后保存 raw、经过响应 Hook、保存 effective，再回复一次结果；生命周期事件数量不随 token 数增长 |
| 工具参数分片 | 只在参数完整且合法后执行；中途断流不执行残缺参数 |
| 模型完成但任务未完成 | 工具调用或 Executor 结果继续本次 Loop／图流转；不提前发送 Run 成功终态 |
| 中间模型文本 | 不因文本看似总结或 `finish_reason=stop` 就跳过 Runtime 的决策／输出校验 |
| 子执行器输出 | 完整结果交给父执行，用户 API 不默认输出其全部私有模型上下文 |
| Hook 与观察者区分 | 有序拦截链决定允许／拒绝和修改后再分发；纯通知同总线订阅但不控制；无 hook.execute／逐 token 通知或递归自触发 |
| 后置 Hook 与执行事实 | 响应修改不覆盖实际外部结果／Attempt 状态；原始失败或成功证据可审计，effective 另存 |
| 批次成员被拒绝 | 拒绝结果占据原 call_id／ordinal；其他成员按原策略完成，全批交付后模型看到原因，不留悬挂槽位 |
| 前置拒绝模型请求 | 没有实际首输出不标输入已送达；无法继续时 Runtime 返回原因，不调用被拒模型生成解释 |
| 有效上下文持续可见 | Hook patch 经 ContextManager 提交后，下一 Loop、同 Session 历史投影、压缩输入和恢复视图都反映该修改 |
| 原始与有效内容区分 | 原消息／结果保留；有效视图标 Hook 版本、来源关联与 revision；接口不将修改冒称原始返回 |
| 角色隔离 | 私有 patch 只影响目标角色；跨角色共享字段显式声明 shared scope 与权限，不复制整个私有消息链 |
| 请求已冻结（含尚未发送） | 新 patch 不修改其冻结输入；同一 Operation 重试保持原参数，后续新请求使用新版本，原请求事实仍可核查 |
| 压缩与 patch 竞争 | 压缩基于旧 revision 时提交不能覆盖新 patch；版本冲突显式处理，不能把有效 context 回退 |
| 有效响应／context 一致性 | 不出现响应已消费而修改未保存，也不出现恢复重复追加修改或消费同一结果 |
| 停止消息 | 不被正在等待的长模型请求堵塞；关闭新工作并按既定规则收束 |
| 客户端断开 | 只断开观察订阅；Run 继续，重连按事件序号补读，不能重新请求一次模型 |

Hook 的合法拒绝和上下文修改已确认；它们仍不能伪造执行身份、抹去已发生副作用或重启已结束 Run。未知 Hook 结果按检查点协议的技术建议验证：先查已提交决定，无可靠结果且处理器失联时记录 indeterminate 并继续先前有效状态；不重放任意脚本，不覆盖新状态，不把其当成已返回 allow。上下文版本冲突按明确的 CAS 和增量合并协议验证。

检查点至少一次通知与外部副作用恰好执行一次是不同保证。测试不能用消息去重通过来宣称远程业务天然幂等。

## 6. 故障注入与恢复矩阵

| 故障位置 | 恢复要求 |
| --- | --- |
| blob 已写、DB 引用未提交 | 暂不可见；可复用内容 hash；无引用对象可回收 |
| 请求 Hook 部分决定已提交 | 复用固定链版本、已完成决定及 context revision；继续未完成部分，不重复叠加补丁 |
| 有效请求已冻结、尚未执行 | 恢复原 Operation／effective 请求／票据，按原参数执行；不把 claimed 补充误标 delivered |
| 请求已发送、结果未持久化 | 有受控句柄则接续；否则 unknown；不能直接声称从未执行 |
| raw 完整响应已保存，后置 Hook 未完成 | 不重新调用模型或工具，不回复／发布完成；继续响应链，raw 不等于决策已消费 |
| 某 Hook 已完成且决定已提交 | 复用 allow／deny／修改决定、有效载荷和 context revision，不重执行处理器或重复应用补丁 |
| 合法拒绝已提交、回复未送达 | 补交付原 blocked／错误和原因，不改为再次执行原能力 |
| context patch 已提交、下一 Loop 前宕机 | ContextManager 恢复最新提交 revision，后续 Loop、历史和压缩不回退原始视图 |
| effective 回复和 context 修改提交中宕机 | 一致提交集合全部生效或全部不生效；不能只回放结果而漏掉修改 |
| 旧摘要计算期间提交了新 patch | 旧 revision 的摘要提交被 CAS／版本校验挡住，不静默覆盖新修改 |
| Hook 在途且结果是否生效未知 | 先查 hook_invocation_id 的既有决定；无可靠结果且处理器失联时记 indeterminate，保留前序有效状态继续；不重放任意脚本 |
| effective 完成、回复尚未送达 | 按保存的结果及 context 修订补交付；不重新执行适配器或 Hook，不重复消费 Loop |
| 调用批次已建立、尚未激活 | 使用同一 batch_id/call_id，按原顺序和容量激活 |
| 子执行已创建、父进程崩溃 | 通过稳定父子引用找到原执行范围；不新建第二个实例 |
| 内层已完成、外层未收到回复 | 读取原内层终态与结果，完成外层节点；不重新执行内层 Agent |
| 外层正在执行、内层只完成一部分 | 按内层检查点及已完成任务结果接续原实例；不只重跑外层函数 |
| 节点完成与后继登记之间宕机 | 完成结果、节点状态、后继可执行事实一致提交；恢复后不漏执行后继 |
| 完成事务已提交、推进消息未发送 | 由持久待投递记录补发；重复投递不重复执行后继 |
| 完整检查点早于已完成分支 | 恢复时合并对应范围后续已提交任务结果；旧快照不得覆盖成功事实 |
| 成员 effective 结果已存、父角色未消费 | 不重发外部请求或重做响应 Hook；CallBatchCoordinator 一次性交付 |
| E1 已完成、E2 在运行中机器宕机 | E1 不重跑；E2 恢复原 node_task_id/context_id；父等待原批次 |
| 整批交付事务中宕机 | 未提交则无部分消息可见；已提交则不得重复追加工具结果 |
| 压缩完成一部分 | 按 CompactionEpisode 游标和已有结果续作；无额外业务 Loop |
| ticket／反馈提交中宕机 | 同事务集合全部生效或全部不生效；无孤立扣额或反馈槽 |
| Executor终态已存、父Hook未完成 | 名额已释放、成员未结算；接管不重占名额、不重跑Executor，只继续父后处理 |
| 分支关闭与Join消费竞态 | 全成员关闭、固定序归并、状态revision和后继意图一致提交；同一TaskWrite不被重复合并 |
| 旧 Worker lease 过期后返回 | PostgreSQL 拒绝旧 epoch 提交；不向用户发送它的未持久化终态 |
| 旧脚本仍持有共享卷 | 新 Worker 等待网关撤销确认；不启动第二个写入者 |
| deadline 在准备和提交之间到达 | 不允许成功／新业务提交；允许当前有效 owner 收束和记录超时 |
| 终结和新输入同时发生 | Session 锁决定归属；输入要么进入原 Run 补充，要么进入下一提问，不丢不重复 |
| 删除文件过程中失败 | 保持 deleting；剩余路径可继续清理；不再接收业务 Run |
| NFS 不可用 | 明确存储错误；不偷偷回退本机同名目录 |
| SSE 通知丢失或客户端重连 | 按持久 run_seq 补读；不创建第二个 Run、不重新调用 LLM |

用模拟时钟验证 deadline 和 lease，用可控障碍控制交错，不依赖 sleep 碰概率。核对调用次数、原始／有效参数 hash、raw／effective 结果、Hook 链版本与决定／进度、context patch 来源与 revision、原始消息、票据、graph_execution_id/node_task_id、文件 hash 和事件序号；只检查最后一句“成功”不足以证明恢复正确。

`CheckpointStore` 的装载测试必须验证范围、版本、父子关联、完整检查点和任务结果的一致性。不把对象序列化／反序列化成功当成跨机接管通过，也不把单进程消息总线当成持久队列。旧 RuntimeCheckpoint 不再作为另一份可独立更新的调度真相；统一由 GraphCheckpoint、NodeCheckpoint 和运行记录组合恢复视图。若没有历史状态完整副本，不能声称支持任意历史回放。

## 7. API 全链路验收

通过临时测试账号／token，经 API 创建资源、Skill、两个 Executor 配置和已发布 Agent。确定性 ModelAdapter 提供固定响应、委派、错误与断流，用相同 HTTP/SSE API 跑完整路径；然后切换到已配置的真实模型复跑关键场景。测试结束删除专用 Agent 和 token，保留共享用户资源。

最少覆盖四组任务：

1. **ReAct**：读取 Skill、读写文件、调用 MCP、修正一次错误参数、输出结构化结果；验证只有一个 AgentInvocation／GraphExecution／根 React NodeTask，跨 Loop 身份不变。
2. **Plan–Executor**：Planner 读需求 Skill、记录计划，连续两轮委派同类 Executor；执行器独立上下文、共享工作文件，Planner 按批次收集结果。并行副本发送三个任务，以实际时间线验证最大活跃数 2 和滚动补位。
3. **生命周期**：运行中补充、停止、断流重连、Worker 故障接管、发布版本切换、无沙箱文件清理和 Agent 删除。
4. **EventBus 与 Hook**：通过 API 配置多项有序 Hook，覆盖 allow、前后置 deny、参数／结果／上下文修改及自身异常。核对 Manager 的 effective 请求、Loop 的 effective 响应及后续历史／压缩使用的有效上下文；界面无需介入。在 raw 保存后、Hook 决定提交后、context patch／effective 一致提交前后注入故障，验证已完成决定和修订复用。另模拟并行批次某项被拒、旧摘要与新 patch 竞争，确认原因可见、不悬挂、不回退。未知 Hook 结果按检查点协议的 indeterminate 建议分支进行确定性验收。

测试执行状态通过[领域接口](domain-interfaces.md)中定义的 Run、GraphExecution、NodeTask、CallBatch、Operation 查询接口和 SSE 验证，不依赖页面观察证明行为。还应捕获 API 事件序列，确认不存在 `model.delta`，用户最终答案仅随 Run 终态交付，原始模型流的不同长度不会造成等量业务事件。确定性适配器分别模拟首输出前失败、输出后卡住、残缺工具参数及 effective 结果回复前断线；Hook 处理期间不再使用已解除的模型首输出超时，但仍受 Run 总时限和 Hook 自身时限约束。停止 Worker 的故障注入控制只在测试环境提供，生产 API 不新增任意进程终止权限。

跨机恢复必须在 API 启动 Run 后停止实际 owner，让另一 Worker 接管，并比对运行前后的稳定身份和已完成结果；不能只在同一进程重新创建一个 Python 对象。

## 8. 文档制品校验与交付边界

本轮设计更新检查：

- Markdown 站点构建及内部页面、锚点和资源链接。
- UML／时序／流程说明图的语法和实际 SVG 渲染；这些图是解释工具，不是可执行图接口。
- `contracts/validate.py` 检查 SD-3 的 Definition／NodeBinding／GraphExecution／NodeTask／Checkpoint 契约及结构正反例；校验通过不代表运行调度或恢复通过。事件示例不得继续用 `model.delta` 代表新的公开交付规则。
- 已确认原则和待定实体清晰区分；移除“Hook 仅观察”“修改／deny 未确认”“修改仅临时 payload”的旧规则；未知 Hook 恢复、版本冲突采用本轮协议并明确技术建议身份。raw_saved 不得标成有效交付完成。
- 不同文档中同一实体、默认值、停止规则和恢复责任是否一致。

运行 `npm run docs:build`、`npm run docs:check`，并用安装 jsonschema 的 Python 执行 `docs/design/contracts/validate.py`。文档检查通过只说明设计制品自洽，不替代 M1–M7 的代码、协议及真实模型测试；本次不因更新设计重新构建产品前端。

### 8.1 图表一致性复核记录（2026-10-15）

此前复核共包含 35 张图，其中 3 张旧实现参考图已随旧代码使用说明删除。下表只保留设计草案的复核记录；这些记录不证明 SDK / Platform 新边界已经完成全部图表调整。对象图检查归属、引用和数量，服务图检查依赖和职责，时序图检查调用与恢复边界。

| 文档 | 图数 | 复核结果与主要修正 | 细节依据 |
| --- | --- | --- | --- |
| [总览](overview.md) | 2 | 复核已拆分的设计态／运行态对象图；保持不变 | 领域模型、ID-1/2 |
| [领域模型](domain-model.md) | 4 | AgentNode 经代理；系统 Operation 可无任务；实例图只表达归属 | ID-2 表字典、ID-3 子调用 |
| [图与节点](graph-node-engine.md) | 5 | 普通工具与委派同批汇合；Manager 不调度；后置处理后交付 | ID-3/4 |
| [Agent 执行](agent-execution.md) | 3 | Driver、Pipeline、Coordinator 分工；先冻结父等待，再准入子调用 | ID-3 批次算法 |
| [检查点](checkpoint-protocol.md) | 3 | GraphCheckpoint 可暂为零；调用投影不另建账本；父子恢复经原 Operation | ID-2 快照与接管、ID-3 StartChild |
| [上下文](context-lifecycle.md) | 4 | 只准备写集；固定输入水位；首输出证据；摘要来源失效分支 | ID-5 上下文／压缩 |
| [领域接口](domain-interfaces.md) | 3 | 发布等待实际收束；Manager 只准备计划；公共门面不绕过调用管道 | ID-1 发布、ID-3/4 |
| [能力与 Hook](capabilities-hooks.md) | 1 | 改为服务依赖图，区分 Hook、处理器解析和提交职责 | ID-4 Hook 归一与提交 |
| [事件总线](runtime-messaging-checkpoints.md) | 3 | 区分服务、Operation 状态和模型流；补模型发送门禁 | ID-4 状态机、ID-5 适配器 |
| [存储恢复](storage-recovery-api.md) | 1 | 旧 writer 撤权未确认时停在 recovering，禁止恢复执行 | ID-2 租约协议 |
| [实施入口](implementation/index.md) | 2 | 明确服务依赖及原任务唤醒后的消费写集 | ID-3/5 |
| [调用实施](implementation/invocation.md) | 1 | 区分前置 deny、外部 Attempt、其他执行种类和观察通知 | 本篇状态机与事务目录 |

今后变更一种权威职责或事务边界时，需同步该行所列图和相邻解释。不能只以 Mermaid 能渲染作为语义正确的证明，也不能把此次文档检查当作运行时功能验收。

### 8.2 Workflow 产品目标补充后的适用范围

在上述图表复核后，产品目标补齐 Workflow，并明确选择双引擎：内层 AgentGraphEngine 采用轻量角色调度，外层 WorkflowEngine 将 LangGraph 相关设计逻辑和源码迁入本包适配。两者通过固定调用身份和检查点适配器衔接，不能把公共入口误画成一个统一节点调度器。旧外层分支算法与持久字段不作迁移内核的最终规格，见[源码适配范围](architecture-discussion.md#63-对现有详细规格的影响)。

本次补充[双引擎协议](dual-engine-integration.md)，同步需求、服务依赖图和三处父子调用／恢复时序图，区分内层批次安全点与外层原生快照／pending writes；数据样例增加 engine_kind、协议版本和公共载荷引用。新增故障验收项是“子调用完成但外层 task writes 未保存”“writes 已保存但下一原生快照未生成”，还需验证外层重入不重复启动 Agent、根停止覆盖两个引擎。文档构建、结构和图形检查不等于这两个运行内核或上述故障测试已经实现。

### 8.3 双引擎独立审阅后的修正（2026-10-15）

对基线 `6acb8da` 的独立只读审阅发现两处明确冲突，本轮已修正文档与可执行契约：

| 问题 | 修正与验证范围 |
| --- | --- |
| 串行准入未预留 Executor token，后续创建却强制转换 token | [ID-3 准入算法](implementation/runtime-graph.md#5-批次准入executor-槽位和结果汇合)在串行也同时准备令牌与 begin 意图；重复 advance 不再预留；补充 B05/B06 的接管、deny 和停止断言 |
| Operation／WaitMember 的 Schema、样例与实施状态不一致 | 同步 delegation、pending／blocked、admitting；覆盖 begin 前无引用、begin 后原Operation引用、完整前后置阶段与拒绝终态；拒绝旧枚举、半边关联及提前子任务引用 |

现有 `contracts/validate.py` 仅验证设计投影和内存跨记录约束；补充后覆盖 23 个基础样例、16 个边界正例及 77 个反例。该检查不是数据库恢复或运行时调用测试，不因通过而宣称全部设计已验证。

同时补齐[外层失败映射](dual-engine-integration.md#53-失败取消和重入的确定映射)及[持久提交屏障](dual-engine-integration.md#63-任务写入的持久提交屏障)：区分业务有效失败、基础设施异常、父等待取消和根停止；普通／空输出与动态分派均须等待任务写入确认，ERROR／INTERRUPT 不自动消费平台结果。第 7 节新增的故障矩阵是后续迁移验收要求，本轮没有实现或执行这些外层故障测试。
