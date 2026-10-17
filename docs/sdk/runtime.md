# SDK Agent 执行与轻量图调度

状态：详细设计，尚未实现。AgentGraphEngine 仅推进 ReAct／Plan 角色任务；Workflow 编译与 super-step 见[工作流](workflow.md)。平台不会调用这些内部阶段接口。

RuntimeEngine（运行时引擎）负责 SDK 的运行服务和执行托管，详细构造及生命周期见[装配设计](assembly.md#11-runtimeengine-管理的范围)。本文描述它装配的单次执行现场和图内算法；节点收到的 NodeContext 只包含当前任务身份、控制和已有服务的受限引用；ReactAgent.context 才保存角色的消息和 Loop 进度。

## 1. 所有权、配置与共同类型

内部 Agent 的 **AgentGraphEngine** 是调度权威；本文内部算法中简称 GraphEngine。NodeDriver 是其执行组件，CallBatchCoordinator 只准备批次变更。外层由独立 WorkflowEngine 推进；两个内核共用根 ExecutionRecord 的租约、控制、调用与存储，不共用图 ready 队列。恢复按 engine_kind 分派，整体接管执行树；详细边界见[双引擎协议](workflow.md)。注册表保存不可变配置和工厂；活动 AgentNode／ReactAgent 按任务创建，ReactAgent 持有私有 AgentContext，其中 continuation 是可恢复执行位置。SDK 执行事实均通过 `CommitCoordinator.commit(RuntimeCommit, ExecutionLease)` 提交，不得让 BudgetService、批次协调器或恢复器单独提交同一事实。

固定默认值：业务 Loop 合计 100；ExecutionRecord 从创建起 3600 秒；并行关闭；开启后每个 Planner 批次最多 2 个活跃 Executor；自动重试关闭，开启时同 Operation 共 2 次 Attempt、间隔 2 秒；工具单次 10 秒；模型首有效输出 60 秒。Executor 和压缩继承主模型；压缩不占业务票据。有效截止取根与适用 Invocation 局部截止的最小值，排队、Hook、压缩、重试与恢复均计时。

```python
Id = str                             # 服务端 UUID；示例短 ID 仅用于文档
Revision = int                       # 普通实体初始1；Context effective revision/record_head可0；实际变更递增
JsonValue = None | bool | int | float | str | list | dict
GraphRef = GraphExecutionRef
TaskRef = NodeTaskRef
Phase = Literal["prepare_context", "prepare_model", "compact_context", "call_model",
                "interpret_response", "wait_batch", "consume_results", "validate_output", "completed"]

@dataclass(frozen=True)
class ExpectedVersions:
    run_revision: Revision | None     # 控制门以锁内最新值检查；不要求所有独立调用共用旧 revision
    graph_revisions: dict[Id, Revision]
    task_revisions: dict[Id, Revision]
    operation_revisions: dict[Id, Revision]
    context_revisions: dict[Id, Revision]

@dataclass(frozen=True)
class WaitCondition:
    kind: Literal["batch_settled", "budget_available", "operation_completed", "child_completed"]
    ref_id: Id                       # WaitGroup / budget scope / Operation / child invocation
    generation: int                  # 固定批次代次；budget 使用申请记录的代次
    observed_revision: Revision

@dataclass(frozen=True)
class ReactContinuationV1:
    protocol_version: Literal[1]
    phase: Phase
    role_key: str
    role_binding_ref: ExecutionBindingRef
    context_revision_ref: ContextRevisionRef
    role_loop_index: int             # 已创建业务模型 Operation 的最大序号；初始 0
    current_loop_ticket_id: Id | None
    model_operation_id: Id | None
    pending_batch_id: Id | None
    pending_operation_refs: tuple[PendingOperationRef, ...]
    compaction_ref: Id | None
    supplement_delivery_watermark: int
    projection_ref: PayloadRef | None
    accepted_decision_ref: PayloadRef | None
    validation_feedback_ref: PayloadRef | None
    final_output_ref: PayloadRef | None
```

`NodeTask` 额外保存 `ready_seq:int、resume_input_ref:PayloadRef?、activation_no:int`；ready_seq 由所属图提交分配，不使用进程时间排序。`resume_input_ref` 保存结果引用和 WaitCondition 的 generation，不保存 Future。`CallBatch.delivery_state=unconsumed|consumed`；成员 `status=queued|admitting|admitted|settled`。`admitting` 只表示准入命令已提交、正在 begin／前置处理，不能当作 Executor 已启动。WaitMember 同步使用对应状态；成员集合、ordinal、call_id 一经冻结不可改动。

所有系统 ID 初次由服务端分配并随创建意图提交；重试按稳定逻辑键查回 ID，不按参数 hash 合并调用。相同键与不同载荷返回 `IDEMPOTENCY_CONFLICT`。通知仅提供“重新读取事实”的提示，不能携带覆盖当前状态的权威值。


## 2. Agent 图构造与校验

构造输入为当前 AgentDefinition，Plan 另接收 Executor key → AgentDefinition 映射，以及已注入策略、模型、CapabilityRegistry 和 HookSet；生成 GraphDefinition 与 NodeBinding。Plan 的入口 ReactAgent 使用 PlanAgent.definition，Executor ReactAgent 使用 executors[key]。无需平台发布记录或发布 hash。SDK 只校验自己的节点接口、恢复格式与静态配置，调用方负责恢复时提供原配置。

- ReAct：恰好一个 react 入口，无 Executor；普通工具在入口的批次中调用。
- Plan：恰好一个 planner 入口，零到多个 executor 类型；只有 Planner 可委派；同类型多次委派产生不同 NodeTask。
- mode 由 ReactAgent／PlanAgent 构造类型确定；定义内部没有模式开关或角色列表。
- role_key 从 NodeBinding 派生：直接 ReAct 为 react，Planner 为 planner，Executor 为定义表的 key；key 规则见[最小配置](definitions.md#2-最小配置结构)。node_key、模型可见能力名分别在所属范围内唯一。
- Planner／Executor 节点均绑定 ReactAgent；具体职责由节点位置和委派权限确定。所有行为配置都用 AgentDefinition；模型、执行与上下文策略由所属完整 Agent 注入，不为 Executor 创建另一份额度。
- 输入输出 Schema 自包含，无远程引用；Prompt 变量声明完整。
- 运行策略和上下文预算在构造时验证，不能在第一个模型请求才发现缺模型窗口。
- 不将 plan_steps 编译为节点或依赖边。计划内容可更新，已登记 CallBatch 的成员集不可更新。
- GraphExecution 保存 engine_kind、engine_protocol_version 和结构位置 key；恢复时重新构造程序，未知协议或缺少必需节点时返回明确错误，不猜测替代实现。
- 可执行对象、客户端和 callable 不写进检查点；内存图与持久执行现场通过稳定 key 对应。

Workflow 使用迁入的编译器；不再并行维护另一套通用 fork／branch cursor／Join 算法。

## 3. NodeOutcome、Driver 和激活状态机

```python
@dataclass(frozen=True)
class Progress:
    continuation: ContinuationRef
    effects: tuple[PreparedEffect, ...]  # 只允许本阶段的强类型 RuntimeCommit 子命令
@dataclass(frozen=True)
class Wait:
    continuation: ContinuationRef
    condition: WaitCondition
    resume_input_ref: PayloadRef
    effects: tuple[PreparedEffect, ...]
@dataclass(frozen=True)
class Complete:
    output_ref: PayloadRef
    continuation: ContinuationRef
    writes: tuple[PreparedTaskWrite, ...]
@dataclass(frozen=True)
class Fail:
    error: RuntimeErrorRecord
    partial_result_ref: PayloadRef | None
    accounting: LoopAccountingOutcome | None
NodeOutcome = Progress | Wait | Complete | Fail
ActivationStream = AsyncGenerator[NodeOutcome, CommitReceipt]

class Node(Protocol):
    def activate(self, input: NodeInput, continuation: ContinuationRef | None,
                 node_context: NodeContext) -> ActivationStream: ...
class NodeDriver(Protocol):
    async def activate(self, task: TaskRef, claim: ExecutionLease) -> ActivationReceipt: ...
class GraphEngine(Protocol):
    async def start(self, request: CreateGraphExecution, scope: ExecutionScope) -> GraphRef: ...
    async def dispatch(self, request: DispatchTasks, scope: ExecutionScope) -> DispatchReceipt: ...
    async def drive(self, run_id: Id, claim: ExecutionLease) -> RunHandle: ...
    async def notify(self, hint: CommittedEvent, claim: ExecutionLease) -> None: ...
    async def request_stop(self, scope: ControlScope, reason: StopReason,
                           claim: ExecutionLease) -> StopReceipt: ...
```

`RuntimeErrorRecord` 固定字段为 `{code:str, message:str, scope_kind:run|invocation|task, scope_id:Id, source_operation_id:Id?, details_ref:PayloadRef?}`；message 已脱敏，控制优先级由 code/scope 决定，不按错误文案猜测。`activate` 只能被 GraphEngine 调用。前置：任务 ready，所属图未终态、scope 允许推进、ABI 匹配、lease 有效；提交 ActivateTask 后才创建生成器。后置：返回值是最后已提交 receipt；任何终结 outcome 后不再调用生成器。`dispatch` 只登记授权位置、专属输入、稳定成员键，不直接启动协程；`notify` 幂等重新读取相关事实，不修改模型输入。`start` 要求父子关联或根初始化已授权，提交初始图与入口意图后返回。

| 当前 Task.status | 输入／条件 | 原子结果 | 后续 |
| --- | --- | --- | --- |
| ready | ActivateTask，CAS 命中 | running、新 activation_no/id、固定 start revision | 创建或恢复任务私有 Agent，重建 NodeContext 和 generator |
| running | Progress | running、revision+1、NodeCheckpoint/effects | receipt 传回 generator 后继续 |
| running | Wait，条件未满足 | waiting、wait/resume 引用、activation ended | 释放 Driver worker |
| running | Wait，提交时条件已满足 | ready、同一 wait/resume 引用、ready_seq、activation ended | 重新入队；避免丢唤醒 |
| waiting | 条件已满足且 scope 开放 | ready、持久 resume 引用、ready_seq | 新 activation，原 task/context |
| running | Complete | succeeded、唯一 TaskResult/后继意图 | 释放 Executor 槽、通知父管道 |
| running | Fail | failed、唯一 TaskResult/错误路由 | 父成员错误或 Invocation 收束 |
| ready/running/waiting | 控制收束 | 先关 scope；静默后 cancelled/failed | 保留原因，不再业务激活 |
| succeeded/failed/cancelled | 重复结果／通知 | 返回原结果，无 revision 变化 | 不再执行 Node |

NodeContext 由 Driver 按每次激活创建，字段为 `scope: InvocationScopeRef`、`activation_id`、`lease: ExecutionLease`、`context_read_authority: ContextReadAuthority`、只读 `control`，以及按授权装配的 `model_port`、`capability_port`、`context_port`、`environment_port` 和可选 `delegation_port`。访问端口时仍验证执行权；对象随激活结束失效。它不含 messages、continuation 或客户端所有权，也不能写入检查点。所有端口引用 ExecutionRunner 已装配的服务，不再创建一份服务实例。

`AgentNode.activate` 将恢复输入交给内部 Agent 的可恢复执行入口。ReactAgent 在接入时装载 `AgentContext`：continuation 与输入任务身份必须一致；后续阶段从 `self.context.continuation` 读取，准备新的候选写集，收到提交回执后才刷新已提交视图。AgentContext 的消息头和进度通过一致任务现场恢复；不得用旧内存 messages 覆盖已提交 Hook 修改。NodeOutcome 继续携带准备发布的进度引用，AgentNode 不维护第二份状态。

Driver 唯一实现选用 async generator 的 `asend(receipt)` 握手，禁止“yield 之后立即自行继续”的后台任务：

```python
async def activate(task_ref, lease):
    start = await commits.commit(plan_activate(task_ref), lease)
    state = await reader.load_task_bundle(task_ref)         # 一致任务、消息修订与已提交进度
    node = driver.bind_task(state.binding, state)          # 使用注册工厂；缓存仅属于本Runner
    node_context = services.for_activation(state, lease)  # 只引用当前有效的受限服务
    stream = node.activate(state.input, state.continuation, node_context)
    try:
        outcome = await anext(stream)
        while True:
            validate_outcome(state, outcome)              # phase、写集、所属身份、引用完整性
            prepared = await engine.prepare_outcome(state, outcome)  # 纯路由/映射；不持锁
            receipt = await commits.commit(prepared, lease)
            state = receipt.task_view
            if isinstance(outcome, (Wait, Complete, Fail)):
                await stream.aclose()
                return ActivationReceipt(receipt)
            outcome = await stream.asend(receipt)          # 只有已提交 Progress 可以继续
    except CommitConflict as conflict:
        await stream.aclose()
        return await reconcile_committed_or_requeue(task_ref, conflict.commit_key, lease)
    except (LeaseLost, PersistenceUnavailable):
        await stream.aclose()                             # 不伪造业务 Fail；由接管续原事实
        raise
    except StopAsyncIteration:
        return await fail_contract(task_ref, "NODE_MISSING_TERMINAL_OUTCOME", lease)
    except Exception as exc:
        return await fail_contract(task_ref, "NODE_IMPLEMENTATION_FAILED", lease, sanitize(exc))
```

`validate_outcome` 拒绝越权状态写、不可序列化对象、跨角色 context、未提交 result 引用，以及 completed phase 搭配 Progress。`CommitConflict` 单独处理：先关闭旧 generator，按提交键查 receipt；已提交且任务终态则返回原 receipt，否则从已提交 continuation 将原任务重新排入 ready。未提交则重读并重新准备原阶段。不得盲目重放旧 generator 的副作用。取消必须由已持久控制信号触发，进程异常不是用户停止。

## 4. ReAct 各阶段的确定推进

以下阶段函数中的 `c` 是 `agent.context.continuation` 的已提交视图；`node_context` 只提供当前激活的控制与服务。共同入口：每次从准备阶段开始新业务模型前先检查 root/local 已消费额度、停止和截止；耗尽时不先压缩。已有最后一张已消费决策的动作不做“consumed==limit”拒绝，只检查 scope 是否已经关闭、停止及截止。Operation 的阶段推进由原 Loop／NodeDriver 提供；登记、路由和已许可的 Attempt 执行使用 EventBusImpl，以下 `prepare_*` 返回可供同一 RuntimeCommit 提交的强类型 effect；它们不能偷偷推进节点状态。

| 阶段 | 必需引用／入口事实 | 唯一成功输出与下一阶段 |
| --- | --- | --- |
| prepare_context | 本 task context；上批已 consumed 或首次输入 | 固定 projection/ref/revision/补充读取水位 → prepare_model |
| prepare_model | projection；尚无本轮 operation 或已有同 purpose operation | 固定 operation/role_loop_index，发 before 并等待 StageResult → compact_context 或 call_model |
| compact_context | 当前 operation、compaction_ref、请求有效 revision | 压缩提交新 revision、重组并硬窗口验证 → call_model |
| call_model | effective 请求冻结、ticket 已预留或可申请 | 原始结果保存后发 after，结算 completed EventResponse → interpret_response；非终态等待同 operation |
| interpret_response | completed effective 响应，未接受过该 operation | 接受决策+消费 ticket+冻结 batch → wait_batch；无动作 → validate_output |
| wait_batch | pending_batch_id、固定 WaitGroup | 全成员 settled → consume_results；否则 Wait |
| consume_results | 全成员结果引用、未 consumed | 一次 ContextDelta+consumed+清空本轮引用 → prepare_context |
| validate_output | 已接受候选+已消费 ticket | 本地 schema 合规 → completed；不合规反馈+清空本轮引用 → prepare_context |
| completed | final_output_ref | Complete；不请求模型或能力 |

阶段发送与处理合同见[Loop 发出阶段事件](event-bus.md#6-loop-发出阶段事件总线执行主请求)。同一 Loop 内的模型、工具和 Executor 委派都有各自 Operation；成员阶段可以在父任务 wait_batch 期间推进。推进一个成员的 after 不等于进入下一次模型 Loop，仍须等待全批 settled。NodeDriver 对同 Operation 合并激活，防止父等待和成员恢复各发一套事件。

### 4.1 prepare_context 与 prepare_model

1. 检查根控制，再检查 Invocation 控制；返回控制错误时走第 8 节。对新 Loop，任何层 consumed==limit 就持久关闭对应 scope。reserved 暂满仍可读取已有上下文，但不得运行压缩，先登记 budget_available 等待。
2. 直接 Agent ExecutionRecord 的根 ReAct/Planner 领取顺序水位以内的 pending UserInputDelivery；Executor 和 Workflow 内 AgentInvocation 不领取根补充。后者只使用 input_only 输入和本次私有过程。直接 Agent 此时只是选入 projection，冻结请求时才 claimed；ContextHandler 按构造策略 recent_runs=2、角色私有范围、已提交 effective revision 生成 projection，同 input_id 去重。已发生的上下文 Hook 修改通过重新读取 authoritative revision 生效。
3. 以 `(task_id,"context-prepare",role_loop_index+1,source_revision,input_watermark)` 形成准备键，提交 projection_ref/context revision/水位和 phase=prepare_model。准备期间新到补充留下一轮；不能补写到已经冻结请求。
4. 新模型 Operation 的 purpose_key 固定 `business-model:{role_loop_index+1}`。不存在时在 Progress 同事务保存 prepared Operation 意图、递增角色序号、写 model_operation_id；存在则复用，不递增。压缩 Operation 使用不同 purpose，不增加此序号。
5. 调用 `NodeDriver.prepare_business_model(op_ref, scope) -> PreparedModelRequest | FinalResponseRef`，由 Loop 显式发 model.generate.before 并等待已提交 StageResult；Operation 仍保持 before_call，等待模型执行许可。该方法是 NodeDriver 内部方法，不是总线隐藏调用 Hook。前置 deny 作为模型失败按角色范围处理；Hook 自身错误按调用规范 fail-open。请求与 context 的有效 revision 必须一致，不允许 Driver 从原 projection 覆盖 Hook 结果。
6. ContextHandler 用 Hook 后有效请求计算软阈值和硬窗口。触发压缩则创建/复用 `compaction:{model_operation_id}` 的 CompactionEpisode，保存 compaction_ref，Progress 到 compact_context；不触发则到 call_model。没有工具调用、网络等待或 Hook 在存储提交锁内发生。

### 4.2 compact_context 与 call_model

```python
async def compact_context(c, node_context):
    check_new_loop_control(c.task_scope)                     # 耗尽先终结，不白做压缩
    prior = await compactions.inspect(c.compaction_ref, node_context.context_read_authority)
    episode = await compactions.advance(c.compaction_ref, prior.episode_ref.revision, node_context.lease)
    if episode.pending_operation_ref:
        return Wait(c, operation_condition(episode.pending_operation_ref), resume_ref(episode), ())
    if episode.status == "cancelled": return Fail(error=current_control_error(node_context.scope))
    if episode.status == "failed":
        return Fail(error="COMPACTION_FAILED", scope="invocation")
    if episode.chunk_cursor < episode.chunk_count:
        return Progress(c, effects=())                    # 安全点后再推进下一块
    result = await compactions.publish(c.compaction_ref, episode.episode_ref.revision, node_context.lease)
    candidate = await driver.prepare_business_model(c.model_operation_id, node_context.scope)
    if is_final_response(candidate): return Progress(phase="interpret_response", effects=(reference(candidate),))
    rebuilt = await contexts.materialize_prepared_model(candidate.ref, current_context_revision(c), result.snapshot_id)
    if rebuilt.over_hard_limit:
        return Fail(error="CONTEXT_WINDOW_EXCEEDED", scope="invocation")
    return Progress(phase="call_model", context_revision_ref=rebuilt.revision,
                    effects=(rebuilt.request_binding_effect,))

async def call_model(c, node_context):  # 阶段伪代码：yield用于commit-ack，return表示下一outcome
    op = await invocations.get(c.model_operation_id)
    if op.completed:
        return Progress(phase="interpret_response", effects=(reference(op.response),))
    if not op.request_frozen:
        candidate = await driver.prepare_business_model(op.ref, node_context.scope)
        if is_final_response(candidate): return Progress(phase="interpret_response", effects=(reference(candidate),))
        prepared = await contexts.materialize_prepared_model(candidate.ref, current_context_revision(c), applicable_snapshot(c))
        admission = await budget.prepare_reserve_loop(c.task_ref, op.ref)
        if admission.wait:
            return Wait(c, admission.condition, admission.resume_ref, ())
        if admission.control_error:
            return Fail(admission.error)
        yield Progress(c.with_ticket(admission.ticket), effects=(
            prepared.freeze_request, prepared.claim_inputs, admission.reserve_ticket,
            prepared.execution_permit))
        # receipt 后才能出网；事务内二次检查 revision、lease、root/local 控制与容量
    progress = await driver.advance_call(op.operation_id, node_context.lease)
    # 原Loop在raw保存后发model.generate.after；等待时保存阶段并释放激活
    # 未结束只返回可恢复 operation wait；完整响应已提交才推进
    response = await invocations.get_result(op.ref)
    return Wait(c, operation_condition(op), resume_ref(op), ()) if response is None else \
           Progress(phase="interpret_response", effects=(reference(response),))
```

压缩的内部冲突、覆盖范围与增量接回算法由[上下文设计](context.md)规定；本模块恢复固定 episode，不创建第二个压缩节点或调度器。阶段伪代码中的 `Progress(phase=...)` 是构造新 continuation 和 effects 的简写，实际对象严格使用第 3 节签名；`Fail(error=...,scope=...)` 同样封装为 RuntimeErrorRecord，不接受未声明 kwargs。压缩失败在任何角色都终结所属 Invocation。已有冻结请求不可热改；最终冻结时 revision 冲突要重建未发请求，不重复已提交请求 Hook；同业务 Operation 最多一个 CompactionEpisode，已有 episode 成功后不能为了换源 revision 再压一次。旧摘要不适用时按上下文契约重建视图，仍超硬窗口即失败；没有已有 episode 时才按软阈值判断首次压缩。`commit_model_ready` 在锁内发现容量被其他任务预占时以 `BUDGET_ADMISSION_CHANGED` 拒绝本次未提交效果；Driver 重读后落 budget Wait，不能把准备阶段的 granted 当成已授予。该码按 CommitConflict 分支处理。`EventBusImpl.schedule_resume(operation_id) -> None` 只提示原调用方驱动器；`NodeDriver.advance_call` 续发原 before／after，`EventBusImpl.execute_or_resume` 只推进已许可的主 Handler 与唯一 Attempt/重试循环。提示丢失由持久待处理事实扫描补齐，不使等待父任务占 Driver worker。

### 4.3 interpret_response、wait_batch、consume_results

1. 读取 EventResponse，先检查是否已经接受该 operation；已接受就读取其下一 phase，不再次 append、consume 或分派。failed/blocked 不构造空 ModelDecision，按第 7 节角色失败会计处理。
2. normalizer 必须得到合法完成证据及完整消息结构；重复 provider_call_id、无法定位参数片段为 `MODEL_PROTOCOL_ERROR`。该检查在 ModelGateway completed 前已执行，响应 Hook 修改后的有效结果还须再次验证；已有 completed 结果不退回新 Attempt。合法终帧但正文和 actions 皆空属于完整的空候选，交 validate_output 处理，不能与传输协议损坏混为一谈。
3. 仅按当前角色冻结目录解释动作名称。每项复用raw结果提交时固定的 ordinal 和服务端 call_id；未知名称、未授权、单项参数 JSON/schema 错误成为 rejected 成员，其错误含原 provider_call_id/ordinal，不影响其他合法项。文本与动作共存时文本只是过程消息。
4. 调用 `ContextHandler.prepare_accept_model(context_ref, operation_response, mutation_key="model:{operation_id}:accept") -> ContextDelta`，它不独立提交；同 RuntimeCommit 保存消息引用、accepted_decision_ref、ticket consumed、business_loop_no、phase。有动作同时创建 CallBatch/WaitGroup，全部成员及本地 rejected 结果一次冻结；此事务不调用 begin。无动作固定 final_output_ref 并进入 validate_output。
5. 有 batch 则 yield Wait，条件为 `(batch_settled,wait_id,generation)`；冻结 batch 时已提交父等待事实/准入意图，故响应恰好先结算也不会丢唤醒。GraphEngine 在同一事实上执行第 5 节准入。
6. 恢复 wait_batch 时，依据所有成员是否 settled 计算，不能根据最后一条通知推断；全部 settled 就 Progress 到 consume_results，否则再次 Wait 原 wait_id，不建立新 batch。
7. consume_results 按 ordinal 读取唯一 effective EventResponse 或 local_outcome。`ContextHandler.prepare_batch_delivery(context_ref, batch_id, ordinal_results) -> ContextDelta` 生成 mutation_id=`batch:{batch_id}:deliver`：填充原模型消息的结果槽，读取实际有效参数/结果，替换同一 pending feedback 槽。提交 ContextDelta、batch consumed、task 的消费键/水位及 prepare_context phase；同事务不得留下“上下文追加了但 consumed 未写”。
8. 清空 current_loop_ticket_id/model_operation_id/pending_batch_id/accepted_decision_ref/compaction_ref/final_output_ref，保留 role_loop_index、context revision 与补充水位。旧引用仍在不可变历史；无新模型调用前不产生新票据。

### 4.4 validate_output

无动作且 effective 正文为空／纯空白时返回输出校验错误 EMPTY_MODEL_OUTPUT；其他候选无 output_schema 时按自由文本验证，有 schema 时按配置编码解析，再执行本地冻结 schema 验证。无效 JSON 也是输出校验错误，不是供应商网络重试。诊断使用 OutputValidator 的 ValidationIssue（json_pointer／keyword／message／expected_ref?），最多 20 个，按 pointer／keyword 排序，单项说明最多 512 字符；完整原候选仍保留。

通过则直接 Complete（continuation.phase=completed），生成唯一 TaskResult；不通过则以 `format:<model_operation_id>:feedback` 保存修正反馈 ContextDelta、phase=prepare_context，并清空本轮引用。原候选已在接受响应时保存，票据也已 consumed；此处不得重复追加原候选或再次 consume。修正必须申请下一张票据，不能直接再次执行旧 Operation。根最终交付还须通过 ExecutionRecord 终态事务；Executor Complete 只给父委派管道 raw 结果，不直接给用户答案。完成与新输入竞态由输入篇规定的 Session/ExecutionRecord 事务仲裁；本模块不得让迟到 Complete 覆盖已经关闭的 scope。

## 5. 批次准入、Executor 槽位和结果汇合

```python
@dataclass(frozen=True)
class BatchPolicy:
    batch_key: str                     # business-model Operation 对应的稳定逻辑键
    parallel_enabled: bool
    executor_max_parallel: int         # >=1；并行关闭时仍按串行闸门
@dataclass(frozen=True)
class AdmissionToken:
    token_id: Id
    batch_id: Id
    member_id: Id
    state: Literal["reserved", "converted", "released"]
    child_task_id: Id | None
@dataclass(frozen=True)
class PreparedBatch:
    batch_id: Id
    wait_id: Id
    generation: int
    effects: tuple[PreparedEffect, ...]
class CallBatchCoordinator(Protocol):
    async def freeze(self, owner: TaskRef, decision: AcceptedModelDecision,
                     policy: BatchPolicy) -> PreparedBatch: ...
    async def plan_settlement(self, batch: CallBatchRef, member_id: Id,
                              response: FinalResponseRef) -> PreparedMemberSettlement: ...
    async def plan_delivery(self, batch: CallBatchRef,
                            context: ContextRevisionRef) -> PreparedBatchDelivery: ...
class GraphEngineBatchPort(Protocol):
    async def advance_batch(self, batch: CallBatchRef,
                            claim: ExecutionLease) -> AdmissionReceipt: ...
    async def commit_start(self, request: PreparedDelegate,
                           claim: ExecutionLease) -> DispatchReceipt: ...
    async def register_executor(self, request: PreparedDelegate,
                                claim: ExecutionLease) -> DispatchReceipt: ...
```

freeze 前置为同一未接受且有动作的模型决策，后置仅准备全部成员与 WaitGroup；同 model_operation_id 只能一批，接受决策、消费票据与批次创建同事务提交。plan_settlement 只接受该成员原 Operation 的最终响应或经校验的 local_outcome，后置包含成员结果、settled 与可能的父唤醒。plan_delivery 前置全体 settled，后置带 ordinal 排序的结果与一次性 ContextDelta。CallBatchCoordinator 不提供任意事件的 freeze_requests；EventBusImpl.request_many(batch_ref,scope) 仅推进／等待已有模型批次，必须核对 owner、已消费的 decision 票据和保存策略。普通函数／Workflow 不伪造模型来源，使用其所属引擎的调度和单项请求。

上述 Coordinator 准备方法均不建协程、不提交状态。GraphEngine 的 advance_batch／commit_start／register_executor 则是受信命令门面，经统一 CommitCoordinator 提交后返回回执；不是第二个 writer。commit_start 按 PreparedDelegate.target 判别调用 StartExecutor 或 StartChild；register_executor 是 executor 分支的专用入口，两者复用同一提交实现，不各提交一次。

Executor **活跃数**严格等于此 batch 下已经创建并准入、尚无终态 TaskResult 的 Executor NodeTask 数；waiting 子任务仍活跃。单独的 admission token 是防止并发前置 Hook 越过上限的内部预留，不记为活跃实例；queued 成员没有 token。开启并行时保持 `active_executor_count + reserved_admission_tokens <= executor_max_parallel`。token 不按 TTL 自动释放：结果未知时恢复原准入流程，不能因为进程死了便再放另一项。

GraphEngine 对每个批次采用以下唯一算法；任何重复 advance 都在相同锁集合中重读，不依赖本地 Semaphore：

```python
def plan_admissions(batch, root, invocation, active_tasks, tokens):
    if scope_closed_or_expired(root, invocation):
        return cancel_unbegun_members(batch)              # 无 Operation 的成员才用 local_outcome
    remaining = [m for m in batch.members if m.status != "settled"]  # ordinal 已冻结
    if not remaining:
        return wake_owner_once(batch.wait_id, batch.generation)
    if not batch.parallel_enabled:
        first = remaining[0]
        if first.status != "queued":
            return []
        effects = reserve_admission_token(first) if first.action_kind == "executor" else []
        return effects + begin_intent(first)              # 同一准入事务，串行也需要令牌
    free = batch.executor_max_parallel - count_active(active_tasks) - count_reserved(tokens)
    effects = []
    for m in remaining:                                  # 固定 ordinal 扫描，不由完成快慢重排
        if m.status != "queued":
            continue
        if m.action_kind == "executor":
            if free == 0:
                continue                                 # 普通工具仍可启动，避免被满槽位阻塞
            effects += reserve_admission_token(m)
            free -= 1
        effects += begin_intent(m)
    return effects
```

`reserve_admission_token` 和 `begin_intent` 都只返回写集，不自行提交。选中 Executor 时，无论串行还是并行，T_MEMBER_ADMIT 都将令牌、成员 queued→admitting、服务端 call_id/purpose_key 及待 begin 意图放在同一事务；令牌唯一键为 `(batch_id,member_id)`。普通工具不创建 Executor 令牌。串行上一成员未 settled 不准入下一成员，因此最多一项占用／预留；重复 advance 看到 admitting 后不再预留。

提交后才由调用设施 begin，返回 PendingOperationRef；重复投递意图复用同 Operation。begin 从可信主体+purpose_key 取准入意图，在同一 CommitOperation 中创建 `processing_phase=prepared,status=pending` 的 Operation，并写 BatchMember.operation_id、WaitMember.result_operation_id 和 operation target；之后由原 Loop 的动作成员阶段发 before；总线不自动发此事件，begin 也不授权创建 Executor。T_MEMBER_ADMIT 已提交而 T_BEGIN 未提交时，BatchMember／WaitMember 都为 admitting，调用和目标引用均为空；此现场合法，恢复只能补投原 begin 意图，不能退回 queued 或另建令牌。T_BEGIN 提交后、子任务创建前仍为 admitting，target_ref 指向原 Operation。快速完成也已有父成员关联；begin 回执丢失按原 key 取回同关联。串行有任意 admitting/admitted 成员时禁止 begin 后续成员，普通工具与 Executor 共用此闸门。

委派前置链完成后，Loop 经总线 prepare_graph 调用 GraphExecutionHandler 纯准备 PreparedDelegate；此时 Operation 仍为 before_call，无 permit、无 child。`register_executor` 读取 batch/member/admission token/control、StageResult 与提案引用的版本；验证目标是本图允许 executor binding、最终输入已通过授权/schema，再在 StartExecutor 一笔提交中冻结请求、保存 permit 与 executing、创建 child Task/context、保存父 Operation 和 WaitMember 的 child 引用、将 token reserved→converted、登记 ready 意图。此时 active+1/token−1；child 的 graph_execution_id/invocation_id 与 Planner 相同。不能为 Executor 调 AgentInvocationService.start，也不能先独立冻结许可再创建目标。

StartExecutor 未提交时可按原 Operation 重做纯准备；已提交时按 target_ref 接回原任务，不能重建。graph Operation 的 executing 必须已有唯一 target 和 permit；缺任一项报 GRAPH_TARGET_MISSING／PERSISTENCE_INVARIANT_BROKEN，不让恢复器猜测并补建目标。前置后停止或版本冲突发生在提交前时，原子启动被拒绝，仍无 child 和 permit。

前置 deny 不创建 child；已有 Operation 形成 blocked 响应后，target_ref 指向 Operation，提交 member settled 和 token released。参数在 begin 前拒绝则保存 local_outcome、无 Operation/token/child。停止时尚未 begin 的成员同样 local_outcome cancelled；已经 begin 必须沿原 Operation 结算，不能删除引用改为本地取消。admitting 但 begin 未落盘时，停止事务先关闭 begin 意图并释放 token，迟到 begin 必须重新检查 scope。

子任务终态事务同时保存 TaskResult 和父 delegate Operation 的 raw-result 关联，token 已 converted，不另扣减 token；active 投影自然减一。并行立即可补位，串行仍等本成员最终 effective 响应完成。后置 Hook 慢、拒绝、失败处理与原 Operation 关联；member 仅在最终响应提交后 settled。最后一个成员结算事务登记原 owner ready；owner 不因单项失败提前恢复模型。

空 actions 走最终输出路径，不创建批次；request_many 不接受原始事件列表或显式空列表，也不创建无模型来源的批次。每个成员只有一个响应槽；批次输入顺序不依赖调用完成顺序。批次 consumed 仅一次，重复通知返回原 delivery receipt。

## 6. 就绪队列与公平推进

AgentGraphEngine 的内存 ready heap 是内部 Agent 已提交任务的投影，按 `(ready_seq,graph_execution_id,node_task_id)` 排序；每个任务最多一个当前 heap 项。同一 ExecutionRecord 的多个内层 Agent 图可以由该引擎统一公平推进，**外层 Workflow 的原生任务不加入此 heap**，由 WorkflowEngine 自己调度。ExecutionRunner 仅分配根控制和驱动各所属引擎，不建立第三份任务队列。内层每次 drain 先处理已提交结果/控制事件，再准入批次，再激活 ready 任务；每个 task coroutine 在一次已提交 Progress 后 `await asyncio.sleep(0)` 交还事件循环。此操作不创建新 NodeActivation，也不改变业务顺序。

Driver worker 只限制短阶段推进的本机资源，不用于业务 Executor 配额；等待 operation、批次、预算、子图时提交 Wait 并释放 worker。Invocation 管道的网络任务不持 Driver worker。不能让父 Planner 持本机所有 worker 等待子任务。配置 parallel_enabled 只控制同批成员准入；Executor 内部模型/工具仍按该 Invocation 固定执行规则运行。

唤醒采用“提交等待时再查谓词＋结果提交时登记 ready”双向检查：若结果先提交，Wait 事务直接产生 ready；若 Wait 先提交，结果事务唤醒；两个事务按相同父 task/graph 锁串行。已 ready 或 terminal 的重复 wake 是 no-op。预算唤醒按持久申请序 `(request_seq,task_id)` 重新检查，每次只授权满足两级容量的前项；释放或消费任一票据都触发重查，控制事件优先。没有忙循环轮询模型/工具状态。

## 7. LoopTicket：单账本、根与局部约束

```python
@dataclass(frozen=True)
class LoopAdmission:
    kind: Literal["granted", "wait", "control_error"]
    ticket: LoopTicket | None
    condition: WaitCondition | None
    error: RuntimeErrorRecord | None
    effects: tuple[PreparedEffect, ...]
class BudgetService(Protocol):
    async def prepare_reserve_loop(self, owner: TaskRef,
                                   model_operation: OperationRef) -> LoopAdmission: ...
    async def prepare_settle_loop(self, ticket: LoopTicketRef,
                                  outcome: LoopAccountingOutcome) -> PreparedBudgetEffect: ...
    async def check_deadline(self, scope: ExecutionScope) -> ControlDecision: ...
```

`LoopTicket` 补充 `consumption_kind:decision|error_feedback|null`：reserved/released 为 null，consume 同事务写明完整决策或失败反馈；无 Attempt 的拒绝反馈必须为 error_feedback。这些是现有 reserve_loop/settle_loop 门面的事务准备接口，预算权威仍为 LoopTicket 账本。真正 grant 必须随 `commit_model_ready` 在 Coordinator 锁内重算，准备时的 admission 不是可出网凭证。TaskRef 解析 run/invocation，不接受调用方传入 limit/deadline。local 限制仅 budget_scope=invocation 存在；直接 Agent budget_scope=run，不重复限制一遍。

```python
def validate_and_reserve(tx, task, op, lease):
    root, local = lock_and_read_root_invocation(tx, task)   # 所有后续锁遵守第 9 节
    validate_lease(root, lease, tx.now)
    old = lookup_ticket(op.operation_id)
    if old is not None:
        return old                                       # 既有事实；执行仍另查 control/permit
    if root.is_closing or now() >= root.deadline_at:
        return persist_root_close(root.reason_or_timeout)
    if root.consumed == root.limit:
        return persist_root_close("RUN_LOOP_EXHAUSTED")
    if local and (local.is_closing or now() >= local.deadline_at):
        return persist_local_close(local.reason_or_timeout)
    if local and local.consumed == local.limit:
        return persist_local_close("INVOCATION_LOOP_EXHAUSTED")
    if root.consumed + root.reserved == root.limit or local_reserved_full(local):
        return persist_budget_wait(task, op)
    ticket = new_ticket(op, task, state="reserved", business_loop_no=None)
    increment_reserved(root, local)                       # 同一票据的事务投影，不独立扣款
    return ticket
```

`now()` 在整笔事务使用存储后端提供的同一个 `tx.now`；本地文件后端在取得文件锁后采样宿主时钟，数据库适配器可使用数据库时钟。同准入点根错误优先于局部错误。consume 仅允许 reserved→consumed，递减 reserved、递增 consumed，business_loop_no 取根 consumed 的新值；release 仅 reserved→released。终态票据重复同 outcome 返回原 receipt；consume↔release 冲突报 `LOOP_TICKET_STATE_CONFLICT`，禁止重新分配。始终断言两级 `0<=consumed+reserved<=limit`。

| 情况 | 票据与关联事实 | 控制结果 |
| --- | --- | --- |
| 完整 effective 决策被接受 | consume + 模型接受 ContextDelta + phase/batch | 允许本决策动作，包括最后一次 |
| 输出不合格式 | 原票据保持 consumed，修正反馈提交 | 下一 Operation 另申请；无额外修正池 |
| 同模型透明重试 | 复用 reserved 与冻结请求 | 新 Attempt 仍查停止/截止/permit |
| 根 ReAct/Planner 模型最终 failed/blocked | release + 角色失败 + Invocation close | 直接 Agent 结束 ExecutionRecord；外层交失败 |
| Executor 模型最终 failed/blocked | 已有ticket则consume；无ticket按下述失败反馈准入；与子 TaskResult/父 PendingFeedbackSlot同提交 | 父批次仍等全体；错误只占该模型的一张票 |
| 无完整决策且控制停止、不再反馈 | release + 取消/未完成事实 | 不启动动作，不补总结 |
| 原决策已经消费，随后用户停止 | 保留 consumed | 取消尚未执行动作，不回滚额度 |

模型前置 deny 可能发生在 model-ready 之前，此时没有 ticket。根角色失败直接收束 Invocation，无票据就没有 release 动作。Executor 需要给父角色反馈时，对**原 model_operation_id**执行同一两级预算准入：可用则同事务创建 reserved 票据并立即以 consumption_kind=error_feedback consume、保存 TaskResult/PendingFeedbackSlot；不创建 execution_permit 或 Attempt。仅 reserved 暂满则保存 budget Wait，恢复只处理已存 blocked 响应，不重跑 Hook；实际耗尽或 scope 关闭优先走控制收束，不再承诺父业务反馈。该事务的净 reserved 增量为 0，角色序号不增加，失败票据幂等键仍为原 model_operation_id。

Executor 错误反馈槽键为 `(parent_context_id,batch_id,member_id)`；记录 error_ref、source_model_operation_id、状态 pending/finalized。该槽先形成将来的父反馈承诺并结清票据，待父委派后置链完成后用同槽 effective 结果定稿；父模型仍只在批次全部 settled 后读取。这样失败 Executor 不持最后一张 reserved 票据卡住兄弟。若父 scope 已关闭且不再反馈，改为 release，与失败提交在同一锁内决定。

在 99 consumed+1 reserved 时另一角色申请返回 wait；旧票据 release 后可获第 100 张，consume 后下一申请触发根耗尽。第 100 张消费本身**不关闭 scope**，否则会错误取消其合法工具动作。任何其他角色真正要求新模型并观察 consumed==100 时才持久触发整树耗尽；因此已有动作也可能被该控制终结收束，这是根控制优先的确定结果。

## 8. 停止、失败与恢复

### 8.1 作用域收束

控制错误带 `scope_kind=run|invocation、scope_id、reason、observed_at、control_revision`。调用方停止/根超时/根耗尽作用于 ExecutionRecord；局部超时、局部耗尽、根角色模型失败及任意角色压缩失败作用于所属 Invocation；直接 Agent Invocation 收束映射为整个 ExecutionRecord 结束。普通工具失败和 Executor 模型失败是父批次响应，不取消其他成员。

收束严格按以下步骤执行：

1. 短事务锁控制范围并固定首个终结原因，关闭新 Loop、未启动 action、Executor、Attempt 的门；同一竞争中以事务先提交者为准，迟到成功不能改写。根截止检查早于局部检查。
2. 取消未 begin 成员、本机 ready 意图和未 converted token；已存在 Operation 保留原引用，由总线调用流程保存已取得事实及取消结果。所有业务后置 Hook 在关闭 scope 下不得发起新业务调用。总线调用流程使用control_finalize_operation将未启动Hook记skipped_control并结清Operation；不是卡在raw_saved等一条不能运行的后链。
3. 向已开始模型/工具/脚本发送支持的取消信号，登记 WriterBarrier；不把取消本机 Future 当成外部写入已结束。不可取消外部操作按剩余尝试/scope 时间收束，未确认结果记录 unknown；环境写能力由代次 fence 撤销。
4. 对当前无完整决策且不反馈票据 release；接受过决策的保持 consumed。所有子任务达到终态或其写权限已确认撤销后，保存 scope 终态、唯一结果与父通知；根终态同时处理补充交接及 cleanup intent，需预锁 Session。
5. 根全部写入者静默后才清理临时工作文件。Invocation 局部结束不清理全 ExecutionRecord 共享目录。cleanup_state 独立，清理失败只重试清理，不重新打开业务。

控制期间允许的提交仅限：记录已取得 raw/effective 事实、Hook 已有决定/失联故障、取消/错误结算、票据 release/已有决策 consume、输入交接、WriterBarrier、终态与清理。禁止新 dispatch、模型准备、Attempt 及普通完成后继。真实迟到结果可以记审计，但不得让 cancelled task 成功或重新唤醒终态 scope。

### 8.2 恢复算法

```python
async def recover_run(run_id, new_lease):
    latest_control = await controls.read(run_id)           # 不使用旧快照 control_ref 作授权
    if latest_control.terminal:
        return latest_control.result
    await fences.revoke_previous_writer_epoch(run_id, new_lease.epoch)
    bundles = await checkpoints.load_run_consistently(run_id, new_lease)
    validate_hashes_abis_protocols(bundles)                 # 失败禁止任何外部调用
    folded = restore_by_engine_adapter(bundles)            # agent / workflow 各用自身格式；固定读水位
    verify_ticket_projections(folded)                      # 不发新票据修补错误余额
    for graph in sorted(folded.graphs, key=lambda g: g.id):
        if graph.engine_kind == "workflow":
            register_workflow_recovery(graph)              # 原生 checkpoint + pending writes；此处不重新创建任务
            continue
        for task in sorted(graph.tasks, key=lambda t: t.id):
            if task.terminal:
                restore_result_only(task)
            elif scope_is_closing(task) or scope_expired(task):
                enqueue_control_settlement(task)
            elif task.status == "running":
                persist_old_activation_abandoned_and_ready(task, new_lease)
            elif task.status == "waiting":
                reevaluate_saved_condition(task)           # 满足则ready，否则原wait
            else:
                restore_ready_intent(task)
        rebuild_active_slots_and_admission_tokens(graph)  # 从Task/令牌事实，非WaitMember数
        enqueue_unfinished_operation_dispatchs(graph)      # 复用原Operation；只补必要阶段
        enqueue_unconsumed_batches_and_join_intents(graph)
    return await runtime.drive_registered_engines(run_id, new_lease)
```

`load_run_consistently` 为持久化层聚合读取能力，不另建状态存储；跨 scope 先固定一致读水位。旧 running Activation 记录为 abandoned 后新建 Activation，task/context/graph/invocation/op/ticket 均不变。未完成调用的具体恢复交[总线调用流程](event-bus.md)：prepared 继续前置，raw_saved 只续后置，completed 直接复用；executing 无可靠结果按已识别异常策略处理，缺记录本身不能猜成可重试网络错误。

| 错误码 | 检测点 | 保存／恢复分支 |
| --- | --- | --- |
| GRAPH_INVALID / NODE_ABI_UNAVAILABLE | 构造/启动/恢复 | 保存可定位诊断；不执行节点；补齐调用方原依赖后接管原现场 |
| NODE_PROTOCOL_UNSUPPORTED | continuation 解码 | 禁止猜字段；运行失败并保留现场供诊断 |
| NODE_OUTCOME_INVALID / NODE_IMPLEMENTATION_FAILED | Driver | 保存 TaskResult 失败；按编译错误路由或 Invocation 规则处理 |
| REVISION_CONFLICT | Coordinator | 先查幂等 receipt；未提交则重读并重新准备，禁止旧请求出网 |
| LEASE_LOST | 任意推进提交 | 旧 Worker 停止提交/出网，关闭本机句柄；新 lease 接管 |
| PERSISTENCE_UNAVAILABLE | 必要提交 | 关闭本机新工作门，不返回未持久成功；存储恢复后按原 key 查证 |
| MODEL_PROTOCOL_ERROR / MODEL_CALL_FAILED | 模型最终响应 | 根角色 release并收束Invocation；Executor反馈槽+consume |
| COMPACTION_FAILED / CONTEXT_WINDOW_EXCEEDED | 压缩/最终窗口检查 | 收束所属Invocation，不追加总结模型 |
| RUN_LOOP_EXHAUSTED / RUN_TIMED_OUT / RUN_STOPPED | 控制门 | 整树停止；无用户恢复入口 |
| INVOCATION_LOOP_EXHAUSTED / INVOCATION_TIMED_OUT | 局部门 | 仅本调用静默后失败，父读取原结果，兄弟继续 |
| LOOP_TICKET_STATE_CONFLICT / RECOVERY_INVARIANT_BROKEN | 恢复/结算 | 停止业务推进并报告系统失败；不得改账或重跑任务掩盖 |

## 9. RuntimeCommit 的读写集合、锁序与幂等

事务总序为 `Session（仅触及会话时）→root ExecutionRecord→Invocation 按 ID→Graph 按 ID→NodeTask 按 ID→Operation 按 ID→Context 按 ID→子记录按类型/ID`。通常 RuntimeCommit 从 ExecutionRecord 开始；终态/补充交接必须预先声明 Session，禁止拿 ExecutionRecord 后临时补锁 Session。跨图、父子任务、父反馈 Context 均先收集完整集合再按序加锁。平台发布和删除任务只通过公共控制接口请求停止，不参与 SDK 提交事务。

所有下表提交都额外读最新 lease/epoch/存储后端时钟/control，写 ScopeCommit 与所需 outbox；表内“读”含 expected_revision 和相应引用归属。“任务终态”同时结束 Activation，state/result/next intent 不得拆开。纯映射和 schema 在锁外基于冻结版本计算；锁内只重验版本、约束和写事实，不等待 LLM、工具、Hook 或业务工作文件 IO；存储后端自身的读写与同步在提交边界内完成。

| RuntimeCommit 场景 | 事务读集合 | 原子写集合 | 稳定幂等键 |
| --- | --- | --- | --- |
| ActivateTask | graph/task/旧 activation | task running、activation、scope seq | `activate:{task}:{ready_seq}:{lease_epoch}` |
| SaveNodeProgress | task/有关 op/context | continuation、NodeCheckpoint、对应 PreparedEffect | `progress:{task}:{from_revision}:{next_phase}` |
| commit_model_ready（CommitOperation 子效果） | run/local/task/op/context、输入delivery | 冻结payload/hash/revision、ticket reserved、输入claimed、execution_permit、continuation | `model-ready:{operation}` |
| AcceptModelDecision | task/op/ticket/context | 模型接受ContextDelta、ticket consume、batch+WaitGroup或final候选、continuation | `accept-model:{operation}` |
| Freeze/准入批次成员 | graph/owner/batch/成员/active任务/token | admitting、token、begin意图 | `admit:{batch}:{member}` |
| BatchBegin（CommitOperation） | graph/owner/admit intent/member | prepared Operation、batch operation_id、wait result_operation_id/operation target | `begin:{trusted_owner}:{purpose_key}` |
| StartExecutor | graph/owner/delegate op/member/token、允许的StageResult、纯启动提案、上下文版本及控制 | 最终请求/hash、permit/executing、唯一child task+context、父关联、token converted、ready意图 | `executor:{batch}:{member}` |
| CompleteExecutor | graph/child/parent op/member、父context若模型失败 | child终态/结果/写、delegate raw关联、必要反馈槽/票据结算、补位意图 | `task-result:{child}` |
| SettleMember（CommitOperation 子效果） | op/batch/member/owner/context | 最终EventResponse、member settled/结果、释放token、父ready意图 | `member-result:{batch}:{member}` |
| ConsumeBatch | owner/batch/all结果/context | ContextDelta、consumed、消费水位、phase | `consume-batch:{batch}` |
| CompleteTask | graph/task/parent operation | 终态/TaskResult、父结果或根完成意图 | `task-result:{task}` |
| StartChild | 父graph/task/op、允许的StageResult、纯启动提案、上下文版本及控制 | 最终请求/hash、permit/executing、父wait、唯一子invocation/graph/root task、关联和启动意图 | `agent-child:{parent_task}:{logical_call_key}` |
| CompleteChild | 两图/child invocation/parent op | 子终态、父op raw关联、后处理意图 | `invocation-result:{child_invocation}` |
| 控制收束/根终态 | Session如需要、run/调用树/writer barrier | control、取消/票据、终态、补充交接、cleanup intent | `close:{scope}:{control_revision}` / `terminal:{run}` |

`RuntimeCommit` 的 read_set 必须完整声明其 PreparedEffect 所触及实体；Coordinator 以 `expected revisions + stable input hash` 校验。一个逻辑提交键只对应一种内容；lease_epoch、提交时间和纯重投传输字段不进入业务效果 hash，已存在 receipt 先于重复执行检查返回，但旧 lease 不因此获得新执行权。任务结果键与持久后继意图键使恢复不会多创建下一任务。

批次、wait、ticket、admission token、结果消费等都是所在 graph/task/op 的从属记录，不是另一套锁根或调度 authority。读 active Executor/task 与 token 必须在批次准入的同一 graph 锁范围，防止 phantom 超额；持久化篇负责具体索引/隔离级别。事务失败时所有 effect 均不可见；Blob 先持久后提交引用，未提交 blob 可回收但不视作成功。

## 10. 具体输入输出与可执行验收断言

以下输入/输出中的 `op/ticket/batch/task` 均为服务端分配，测试使用固定 ID 工厂。网络模型使用脚本化 FakeModel；Hook、Blob、Coordinator 和时钟均可在精确切点注入故障。断言使用实际记录数量、状态和出网次数，不能只匹配日志文本。

```json
{
  "input": {
    "task": "P1", "graph": "G1", "invocation": "I1", "role_loop_index": 1,
    "model_operation": "M1", "ticket": "L1", "parallel_enabled": false,
    "effective_model_response": {"text": "先查两份材料", "tool_calls": [
      {"id": "provider-a", "name": "research", "arguments": {"task": "核对甲"}},
      {"id": "provider-b", "name": "read_file", "arguments": {"path": "notes.txt"}},
      {"id": "provider-c", "name": "missing", "arguments": {}}
    ]}
  },
  "after_accept": {
    "task": "P1", "phase": "wait_batch", "ticket_state": "consumed", "business_loop_no": 1,
    "batch": "B1", "wait": "W1", "graph_count": 1, "invocation_count": 1,
    "members": [
      {"call_id": "C0", "ordinal": 0, "kind": "executor", "status": "queued"},
      {"call_id": "C1", "ordinal": 1, "kind": "tool", "status": "queued"},
      {"call_id": "C2", "ordinal": 2, "kind": "rejected", "status": "settled", "code": "CAPABILITY_NOT_FOUND"}
    ]
  },
  "after_full_delivery": {
    "task": "P1", "phase": "prepare_context", "role_loop_index": 1, "batch_consumed": true,
    "results": [
      {"call_id": "C0", "status": "succeeded", "text": "甲已核对"},
      {"call_id": "C1", "status": "failed", "code": "FILE_NOT_FOUND"},
      {"call_id": "C2", "status": "failed", "code": "CAPABILITY_NOT_FOUND"}
    ]
  }
}
```

此例断言：初始接收响应不启动工具；准入后先创建 E1，仍只有 G1/I1；E1 TaskResult 后父 Hook 未完时 `read_file.begin_count==0`；C0 settled 后 C1 才 begin；C2 从未有 Operation；P1 下一次模型 Operation 只能在 B1 consumed 以后创建，context 中每个 call_id 恰一个结果槽。

| 编号／输入与故障切点 | 必须断言 |
| --- | --- |
| C01：非法／保留 Executor key、能力名冲突、ReAct 传 Executor 表或 Plan 传活动实例 | 构造返回可定位诊断，不生成可执行程序；外部调用数=0 |
| N01：Progress 提交前注入 DB failure | generator 的下一行计数=0，外部 begin_count 不增加；接管从原 phase |
| N02：Progress commit成功、receipt返回前宕机 | 恢复任务revision只增加一次，ContextMutation/Operation唯一；原 receipt 可查 |
| N03：Wait提交前结果已完成／Wait提交后结果才完成 | 两种顺序均最终 ready，同 task ID，两次重复通知不多建 activation |
| R01：ReAct 两个工具轮次再最终答复 | Invocation=1、Graph=1、NodeTask=1、business tickets consumed=3，工具Operation=2 |
| R02：Hook将请求扩到窗口上限外 | 未压缩/最终硬检查通过之前 Attempt=0、permit=false；失败只收束所属Invocation |
| R03：模型含文本和半截 tool参数；随后流失败 | 工具执行数=0；重试片段分attempt；无过程文本被标最终答案 |
| R04：模型完整输出3次schema失败，第4次合规 | consumed=4、role_loop_index=4、最终输出只来自第4次；失败候选不拼接 |
| B01：串行 E1、T2、E3，E1父结果Hook阻塞 | E1终态后active=0但T2.begin=0；E1settled后T2开始；无第二图 |
| B02：并行max=2，E1/E2/E3，E1父Hook阻塞 | E1/E2先active=2；E1终态后E3启动，active<=2；owner仍waiting |
| B03：并行前置Hook同时通过/一个deny/创建child前宕机 | 任意时active+reserved<=2；deny令牌released且无child；恢复未converted令牌不多发名额 |
| B04：同批一项失败，其余成功，重复member通知3次 | 所有结果ordinal不变，单项失败不取消兄弟，consume mutation仅1条 |
| B05：默认串行Executor，准入后、begin前宕机并重复advance | 恰一reserved token、恰一begin意图；admitting无Operation合法；接回原键创建一次child并converted，不多预留 |
| B06：串行Executor前置deny；另案在begin前停止 | 前者原Operation blocked且token released；后者原意图关闭、token released、local_outcome cancelled；两者均无child |
| L01：根99 consumed+1 reserved，第二task申请 | 返回budget wait而非exhausted；旧票据release后新票据可reserved；consume后下一申请根关闭 |
| L02：上限100，第100次最终输出；另案第100次工具后请求第101次 | 前者succeeded；后者工具可执行但第101模型Attempt=0，ExecutionRecord budget_exhausted，无总结调用 |
| L03：root100/local A5/B20，A已5、root仍20 | A新申请只关闭I_A，B可继续；root满时一次控制关闭全部，账本投影精确一致 |
| L04：Executor模型失败持最后reserved，兄弟等票据；另案前置deny无ticket | 首案子失败/反馈槽/consume原子提交，reserved=0；后案无Attempt且一张票据直接结算，暂无容量只等待已存拒绝，控制优先，不消费空票据 |
| S01：串行A已发B未发，stop提交后模型迟到动作 | B.begin_count=0，迟到动作均不执行；A结果真实记录，终态原因不可被成功覆盖 |
| S02：一个Executor退出而另一个脚本仍写文件 | cleanup_started=false；全树writer退出或fence确认后才true；局部终态不清理根 |
| F01：E1完成E2执行中宕机；raw存而Hook未完成再宕机 | E1执行数不增加，E2复用身份；raw已存工具invoke_once不增加，只补原Hook链 |
| F02：旧epoch迟到commit、旧checkpoint含running | LEASE_LOST，当前context/revision/额度不变；新机器不重置deadline |
| W01：外层AgentNode子图终态后父未消费宕机 | child invocation/graph数各1；父等effective Hook，不能从raw直接完成 |
| W02：外层同一step中A、B，A成功writes已提交、B未完成时故障 | 原生checkpoint＋pending writes恢复A，不重跑其能力；B接回原Operation；后继在原生step提交屏障后推进，不要求自研branch cursor |

阶段参数化故障注入覆盖每个 RuntimeCommit 的“前、事务中、成功后通知前”三点：统一断言 `(TaskResult唯一、Operation身份唯一、ticket无重复consume、batch交付唯一、后继不丢失、终态不重开)`。外层测试先作为规格测试数据及纯编译/规划器验收保存，运行器实现前不得把其列为已通过产品能力。

通用 Workflow 的条件、循环、reducer 与分支汇合按[Workflow 适配](workflow.md)验收，不继续使用旧自研结构化 fork／branch cursor 算法的测试条件。
