# LangChain / LangGraph 运行接入

更新：2026-10-20。平台采用开源 Python LangChain + LangGraph，直接依赖上游包，不再依赖 agentloom-sdk。本文是待实现的运行合同；当前仅有设计和交互原型，没有已经接通的运行服务。框架能力与平台能力分别验收。

## 1. 装配与执行边界

LangChain 提供模型、工具和 Agent 抽象；LangGraph 提供图执行、状态和检查点。[LangChain 概览](https://docs.langchain.com/oss/python/langchain/overview)、[LangGraph 持久化](https://docs.langchain.com/oss/python/langgraph/persistence)

| 组件 | 责任 |
| --- | --- |
| ReleaseCompiler | 将草稿、资源版本、模型配置及策略冻结为 ExecutionSnapshot |
| AssemblyService | 按快照构造模型客户端、角色工具、中间件和可执行图 |
| RuntimeGateway | 平台内部执行适配边界，封装图启动、检查点查询、恢复及结果归一 |
| ExecutionHost | 队列、单写者、输入收件箱、停止命令、运行期限、进程与恢复扫描 |
| LangChain / LangGraph | Agent 循环、受控图路由、角色状态和检查点 |
| WorkspaceService / SandboxGateway | 授权文件视图、隔离执行、撤销写入和清理 |

RuntimeGateway、ExecutionHost 等名称是平台计划模块，不是 LangChain 自带接口。HTTP 层不直接把 `ainvoke` 作为长请求，也不向前端暴露原始图状态。

### 两种应用模式

- **ReAct：**使用 `langchain.agents.create_agent`，显式传入模型、工具、Prompt、状态 Schema 和中间件。入口没有 Executor 列表。
- **Plan：**使用 LangGraph `StateGraph` 定义 Planner → 校验计划 → Executor 子图 → 汇总／重新规划 → 最终输出。专业 Executor 可复用 `create_agent`，每次委派有独立状态、稳定任务 ID 和固定角色授权。模型输出必须经过结构化校验后才能形成委派，不能执行任意生成的图或 Python。
- Plan 沿用应用共享模型和策略、角色独立 Prompt／能力的产品约定；通用默认 Executor 关闭。并行数、最大委派次数、停止和预算由根 Run 统一限制。

这属于本平台的 Plan 实现方案，不是 `create_agent(mode="plan")` 这样的上游开关。[上游工作流模式](https://docs.langchain.com/oss/python/langgraph/workflows-agents)

### 快照与进程生命周期

快照固定模式、Prompt、输入输出 Schema、角色资源版本、MCP 成员与参数定义、模型 endpoint／model_id、凭据引用、预算、环境模板、图版本、状态 Schema 版本、依赖锁文件和后端镜像摘要。恢复必须能重建原图及依赖；凭据可按授权轮换，不能悄悄换模型、资源、图版本或重置截止时间。

Worker 管理连接池、模型和 MCP 客户端的生命周期，活动状态按 Run 隔离。checkpointer 使用 `langgraph-checkpoint-postgres` 的 PostgreSQL 实现；生产不用内存 Saver。框架、模型集成、MCP 适配及可选 Deep Agents 分别锁版本，升级前验证原检查点兼容性。普通 API 进程不装配模型或运行图。

## 2. Session、Run 与检查点

| 身份 | 平台约定 |
| --- | --- |
| session_id | 一个用户在一个应用下的私有会话，跨多轮提问稳定 |
| run_id | 一次逻辑任务；固定 release_id、快照、初始输入、总预算和 deadline_at |
| graph_thread_id | 首版每个 Run 分配一个稳定、内部使用的 LangGraph thread_id；故障恢复沿用，下一轮分配新的值 |
| task_id / checkpoint_ns | 同一 Run 内的委派和子图位置；稳定关联，重试不新建同一逻辑任务 |
| input_id / command_id | 平台生成并持久化的输入／控制身份，用于重试去重 |

LangGraph 的 thread 可以承载连续对话；本平台选择按 Run 分配 thread，避免发布改变图结构后把旧检查点直接交给新图。跨轮聊天由 SessionHistoryService 按策略选取正式用户消息和已完成的公开答案，为新图生成输入；不搬运 Executor 私有消息、原始推理、旧工具调用或失败的半成品。历史截取边界和输入 ID 清单随新 Run 固定。

应用存续期间正式输入和公开答案保留；模型实际使用的上下文可以按窗口裁剪。单个 Run 的内部消息、计划、子任务结果和输入采用标记以图检查点为准；跨轮展示和输入路由以平台记录为准。两者不是可互相覆盖的同一份状态。

## 3. 新请求与故障恢复

1. API 认证并校验应用、会话、输入、资源授权及幂等键；按应用 → Session 顺序进入短事务。
2. 同一事务创建 RunRecord、InputSubmission、DispatchIntent 和 HTTP 回执，固定当前发布及 graph_thread_id；返回 `202`。通知只是加速，队列表扫描保证不丢任务。
3. Worker 重新检查准入，取得该 Run 的单写者资格，按快照装配图；先通过输入节点保存初始输入及稳定消息 ID，再推进模型调用。
4. 图检查点确认接入后回填 `accepted`；回填失败时，从同一 thread 的检查点对账。没有检查点时才使用原输入初始化，已有现场不得再次追加原输入。
5. 图完成后，ExecutionHost 从已持久化结果写入平台终态、公开结果和终态事件；若中间崩溃，恢复扫描只完成对账，不重新跑已完成的图。

同一 Run 只有一个图写者；同一 Session 只有一个活动或预留 Run。首版每个活动 Run 使用可单独终止的执行子进程，Worker 负责派发，Supervisor 记录进程与执行域身份。数据库领取使用 owner、generation、lease_until，但 **租约超时本身不保证旧进程已停止，也不能单靠 thread_id 防并发**。首版同机 Supervisor 必须确认旧进程及其沙箱写入已撤销，再授予接管；未确认则保持恢复阻塞。提交平台结果时检查 generation。若未来允许并存旧进程，须额外实现检查点和环境写入的 fencing，不能直接升级为跨机多写者。

恢复按原图版本、thread 和子图命名空间读取已提交状态，通过上游恢复入口继续；不强制某个 Executor 从头执行，不创建新 Run。有效 owner 仍在时只观察。进程关闭属于基础设施停机，不自动转为用户停止。

检查点不能保证外部写操作恰好一次。业务 Tool／MCP 调用通过 OperationLedger 记录稳定 operation_id、参数摘要和结果引用；支持幂等键的目标复用原键，只读调用可按策略重试。外部动作已发生但回执未知时，先查询目标结果；不能确认且不支持幂等的动作进入 `needs_attention`，禁止盲目重放。中断恢复可能重新执行节点中的代码，也必须遵守这条规则。[检查点](https://docs.langchain.com/oss/python/langgraph/checkpointers)、[中断重入规则](https://docs.langchain.com/oss/python/langgraph/interrupts)

任务领取、检查点写入、外部服务调用不假设存在跨系统事务。阶段回执和恢复对账是 P0 必须验证的工程合同。

## 4. 执行中补充

LangChain／LangGraph 的消息输入和 `interrupt` 不等于一个可并发追加的聊天收件箱。本平台自行实现 InputInbox；不在图正在运行时从另一个请求对同一 thread 调用 `ainvoke` 或直接修改 messages。

- 初始输入可包含声明的业务字段和变量；活动补充只允许非空文本，不覆盖原 Prompt／变量，不接收未接入的附件。
- API 按 Session 的提交序号持久保存补充。唯一图写者在下一次模型决策前读取；ReAct 交给主 Agent，Plan 只交给 Planner，不广播 Executor。
- 输入节点／中间件把稳定消息 ID、`ingested_input_ids` 和消息内容一起写进检查点；确认持久化后，平台才显示“已接收”。对应模型响应及 `consumed_input_ids` 持久化后，才显示“本轮已采用”。
- 若图已写入而平台回执未更新，恢复时对账；不重复添加消息。执行中没有下一次模型决策时，补充不能被标成已采用。
- 结束时先对账检查点，再在 Session 事务中关闭本轮输入归属，把未采用输入标为 `pending_next_run`。与终态竞争的提交由该事务确定归属。
- 下一次用户主动提问时，将待下轮输入按原 input_id 和提交顺序纳入新 Run。保留交接记录，不复制为新的用户消息，也不自动启动下一轮。

`accepted` 表示运行侧持久接入，`delivered` 表示已由模型决策采用，二者都不代表工具成功或最终回答完成。后台补充与恢复依赖以上自定义节点／中间件，必须经过故障测试后才能开放。

## 5. 停止、发布和删除

API 在事务中保存 StopCommand 并关闭该 run_id 的准入；即使图尚未初始化也能阻止迟到分派。已登记、尚未启动的 Run 可在确认没有写者／执行域后转为 cancelled。活动 Run 由控制扫描通知 owner，在模型／工具调用边界检查停止，取消本机等待并请求沙箱终止。

LangGraph `interrupt()` 用于在指定位置暂停并等待恢复输入，**不作为“立即杀掉正在执行的任务”的接口**。本机协程取消也不保证远端 MCP 已撤销副作用。只有本地执行、执行域及未决操作已按停止合同核实后，才显示已停止；无法确认时保持 stopping 并提供诊断。没有业务 Worker 时仍可保存意图，由独立控制／维护循环收束；控制服务也不可用时不承诺即时终态。

发布／删除先关闭应用 gate，再对所有旧版本关联 Run（含预留 Run）保存停止和准入关闭事实。候选版本切换必须等待终态与必要清理确认。清理有独立任务和重试状态，不把清理失败改写成回答失败。删除通过受控 checkpointer 清理接口和平台 repository 完成，保留阻止迟到任务复活的最小墓碑。完整顺序见[生命周期](storage-lifecycle.md)。

## 6. 状态、事件与结果

平台 RunRecord 管理排队、执行权、停止意图、业务终态和清理状态；图检查点管理可恢复的内部现场。RuntimeGateway 只从检查点确认的数据生成进度与结果，不把平台 UI 缓存写回图。

上游流事件不是平台可续读事件库。ExecutionHost 将公开事件写入 RunEvent 表，分配 Run 内单调 seq，按稳定来源键去重；HTTP SSE 使用这个 seq，而非内存 callback 序号。终态和公开结果同事务提交，迟到进度不能覆盖终态。进程中断时允许缺失非关键实时进度，但最终状态和输入采用结果必须能从检查点对账。

最终答案只来自已完成图的指定输出。原始 messages、模型推理、凭据、未筛选的 Tool 返回值和 Executor 私有材料不直接透传。首版提供阶段和完整答案；上游虽有 token streaming，产品暂不承诺逐 token 展示。

## 7. 从旧 SDK 方案调整的内容

| 原设计 | 当前平台方案 |
| --- | --- |
| AgentLoom / AgentBuilder / RuntimeEngine | AssemblyService + LangChain Agent／LangGraph 图 + ExecutionHost |
| sdk_run_id / sdk_session_id | 平台 run_id / session_id，另有内部 graph_thread_id |
| FileRuntimeStore | PostgreSQL checkpointer + 平台运行、输入和事件表 |
| SDK stop / inputs.append | 平台停止命令、InputInbox、受控图边界和环境控制 |
| SDK HookSet / EventBus | 受信 middleware、图节点及公开事件投影，重新定义失败策略 |
| WorkspaceBinding.persistent_sandbox | 独立的 sandbox_enabled、workspace_access、retention 配置 |
| SDK 默认预算和恢复协议 | 平台版本化策略 + 上游检查点合同 + 本平台故障验收 |

旧设计不能只替换包名继续使用。agentloom-sdk 作为独立交付物保留；它的代码、测试、文件检查点及未来 Workflow 计划均不构成本平台实现依赖。
