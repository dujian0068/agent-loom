# SDK 上下文、历史与压缩

状态：详细设计，尚未实现。SDK 保存原始记录、权威有效修订和本次模型视图；Platform 只负责用户身份与可见范围以及将正式输入交给 SDK。[作用域类型](definitions.md)、[Hook 修改](event-bus.md)和[原子提交](checkpoints.md)是本页依赖。

## 1. 实现边界与公共类型

- Executor 继承所属 AgentDefinition 的主模型；CompactionEpisode 继承当前角色模型，二者没有独立模型选择入口。
- 角色 Prompt、工具目录、模型配置、上下文参数、重试策略及环境策略在 Agent 构造时注入并固定；恢复不读取“最新”配置替换它们。
- 调用方可通过控制接口撤销执行或更新授权服务；SDK 在能力边界检查已注入访问范围，固定配置不能绕过撤销。
- 每个 React 类 NodeTask 有自己的 RoleContext；同 Invocation 的 Planner／Executor 共享工作文件，不共享私有消息。
- 外层 CapabilityNode 没有隐含 RoleContext；其调用有效值留在 Operation，并通过节点映射进入 GraphState。
- ContextManager、ModelManager、ToolManager、EnvironmentManager 是事件请求的唯一对应消费者；内部 writer、适配器和纯函数不再次进入相同请求链。

下文所有持久 DTO 均带 `schema_version=1`；UUID 由 SDK 生成或调用方稳定传入，时间使用 UTC，大小使用字节，tokens 为非负整数。普通实体revision从1开始；Context.effective_revision和record_head允许初始0。`?` 表示可空；集合默认空集合，不以缺失字段表达不同语义。不可变正文使用规范 JSON／UTF-8 编码后计算 SHA-256。

| 类型 | 完整公共字段与语义 |
| --- | --- |
| `ContentScopeRef` | `namespace, scope_key`，来自注入的存储分区和内容范围；不包含平台用户或应用 |
| `PayloadRef` | 公共可辨识union：`{kind:inline,value}` 或 `{kind:blob,content_scope_ref,blob_id,sha256,size_bytes,media_type}`；小值可 inline，大值使用不可变内容引用 |
| `ContextRevisionRef` | `context_id:UUID, revision:int`；revision 从 0 开始，0 是空 manifest |
| `ContextReadAuthority` | `run_id, invocation_id?, node_task_id?, allowed_context_ids[], shared_scope_grants[], source_scope_grants[]`，Runtime 装配，不能从模型 JSON 接收 |
| `SourceDependency` | `source_scope, source_ref, grant_id`；派生结果取全部已消费来源的保守并集 |
| `ExecutionBindingRef` | `program_key, node_binding_key, role_key?`，定位调用方已装配程序的绑定，不执行远程配置查询 |
| `DomainFailure` | `code, category, message, retryable_transport:false\|true, external_outcome:not_started\|known\|unknown, detail_ref?`；message 已脱敏 |

`retryable_transport` 只能由已实现的错误码／异常类型映射产生，不从字符串猜测；实际是否重试仍由 event-bus.md 的冻结 RetryPolicy 决定。必要持久化错误不能伪装为工具成功或 Hook fail-open。

inline内容的owner和权限由承载它的可信实体决定，blob分支保留显式owner；两者都传播来源依赖，hash不是访问令牌。inline／blob阈值沿用checkpoints.md，读取端必须接受两种分支；下文“写blob”指需外置的大正文，不能把小inline值强制另存一套内容权威。

## 2. 原始记录、有效版本与提交端口

### 2.1 持久 DTO

| DTO | 字段 |
| --- | --- |
| `RoleContext` | `context_id, invocation_id, owner_node_task_id, role_key, record_head:int, effective_revision:int, active_snapshot_id?, last_model_operation_id?` |
| `ContextRecord` | `record_id, context_id, context_seq, run_id, role:user\|assistant\|tool\|system, message_kind, payload_ref, operation_id?, call_id?, origin_input_id?` |
| `ContextMutation` | `mutation_id, context_id, base_revision, committed_revision, source_kind:hook\|runtime\|user, source_ref, cause_operation_id?, patch_ref, created_at` |
| `EffectiveContextRevision` | `context_id, revision, parent_revision?, record_watermark, mutation_ids[], effective_manifest_ref, commit_ref, created_at` |
| `EffectiveRecordEntry` | `record_id, context_seq, raw_payload_ref, effective_payload_ref, effective_hash, source_refs[], visible_fields[], dependency_refs[]` |
| `EffectiveManifest` | `context_id, revision, entries:EffectiveRecordEntry[], authorized_field_catalog_ref, field_catalog_version` |

ContextRecord 永不更新正文。有效 manifest 按 `(context_seq, record_id)` 排序，保留 raw 引用及已生效变更来源；版本也不可改写。ContextRecord 只代表消息事实，tool 的真实执行状态仍以 Operation 为准。配置 Prompt 的有效引用可以成为稳定投影记录，其修改不能倒写 AgentDefinition。

`logical_result_key` 是追加命令中的幂等元数据，保存在提交索引而非另造消息 DTO；同一 `(operation_id, message_kind, logical_result_key)` 只能产生一条记录。没有 operation_id 的输入使用 `(context_id, origin_input_id, message_kind)`；构造配置投影使用 `(context_id, program_key, prompt_slot)`。

### 2.2 命令与方法

```python
class ContextManager:
    async def prepare_create(self, cmd: CreateRoleContext) -> ContextDelta: ...
    async def prepare_original(self, cmd: AppendOriginal) -> ContextDelta: ...
    async def prepare(self, cmd: PrepareContext, authority: ContextReadAuthority) -> ContextDraft: ...
    async def materialize(self, cmd: MaterializeContext, authority: ContextReadAuthority) -> ModelView: ...
    async def stage_patch(self, cmd: StageContextPatch, authority: ContextReadAuthority) -> ContextWriteSet: ...
    async def materialize_prepared_model(self, prepared_ref: PreparedModelRequestRef,
                                        new_context_revision: ContextRevisionRef,
                                        snapshot_ref: ContextSnapshotRef | None) -> PreparedModelRequest: ...
    async def prepare_model_freeze(self, cmd: FreezeModelRequest) -> ModelFreezeWriteSet: ...
    async def prepare_accept_model(self, context_ref: ContextRevisionRef,
                                   operation_response: EventResponse,
                                   mutation_key: str) -> ContextDelta: ...
    async def prepare_batch_delivery(self, context_ref: ContextRevisionRef,
                                     batch_id: UUID,
                                     ordinal_results: tuple[BatchMemberOutcome, ...]) -> ContextDelta: ...
    async def prepare_output_correction(self, context_ref: ContextRevisionRef,
                                        model_operation_id: UUID, validation_ref: PayloadRef,
                                        output_schema_ref: PayloadRef) -> ContextDelta: ...
```

| 命令／返回 | 必填字段及含义 |
| --- | --- |
| `CreateRoleContext` | `context_id, invocation_id, owner_node_task_id, role_key, expected_node_revision, idempotency_key`；身份随 NodeTask 初始提交创建 |
| `AppendOriginal` | `context_id, expected_revision, records:OriginalRecordInput[], cause_operation_id?, idempotency_key` |
| `OriginalRecordInput` | `record_id, role, message_kind, payload_ref, operation_id?, call_id?, origin_input_id?, logical_result_key, dependencies[]` |
| `AppendReceipt` | `context_revision_ref, record_refs[], commit_receipt`；重复命令返回原 record_id／seq |
| `ContextDelta` | `context_id, expected_revision, expected_record_head, original_records[], mutations[], next_revision_manifest_ref, source_dependencies[], idempotency_key, command_hash`；只准备写集 |
| `PrepareContext` | `context_id, model_operation_id, execution_binding_ref, history_cutoff_run_id?, input_cutoff, expected_node_revision` |
| `ContextDraft` | `draft_id, context_id, model_operation_id, source_revision_refs[], input_cutoff, protected_inputs[], ordered_items[], snapshot_candidate_id?, source_view_hash, binding_ref` |
| `MaterializeContext` | `draft_ref, source_revision_refs[], snapshot_id?, final_input_cutoff`；必须显式固定版本 |
| `ModelView` | `view_ref, view_hash, source_revision_refs[], snapshot_id?, messages[], protected_inputs[], tool_catalog_ref, estimate, dependency_refs[]` |
| `FreezeModelRequest` | `operation_id, expected_operation_revision, expected_context_heads[], model_view_ref, effective_parameters_ref, claimed_delivery_ids[], input_cutoff, expected_node_revision, idempotency_key` |
| `FrozenModelRequestRef` | `operation_id, request_ref, request_hash, context_revision_refs[], snapshot_id?, protected_input_ids[], input_cutoff, commit_ref` |
| `ModelFreezeWriteSet` | `operation_id, expected_operation_revision, expected_context_heads[], frozen_request_ref, input_claim_write, source_dependencies[], command_hash`；无独立提交权限 |

上述准备端口无持久状态推进；写出的不可变 blob 尚未被事务引用时都是可回收材料。调用方将 ContextDelta／ContextWriteSet／ModelFreezeWriteSet 放入 `RuntimeCommit`，唯一写入口为 `CommitCoordinator.commit(RuntimeCommit, ExecutionLease)`。普通 ContextPort.append_original 门面也由所属 Manager 准备并提交该写集，不开放数据库 writer 给节点。

`Pipeline.prepare_business_model(ref,scope)` 在 request_hooks 阶段返回前置 Hook 已应用的 `PreparedModelRequest`；尚未最终冻结，不授予出网许可。字段使用[调用协议的唯一 DTO](event-bus.md#22-持久调用记录)：以 operation_ref／operation_revision 关联调用，以 candidate_payload_ref／prepared_hash 关联前置处理后的候选，以 request_hook_state_ref、context_revision_refs 和 effective_parameters_ref 固定处理依据，另保存 purpose、protection_manifest_ref、model_budget_ref。候选不等于最终 FrozenModelRequest。ContextManager.materialize_prepared_model 只根据新上下文／摘要重建 messages，逐项保留原 effective_parameters_ref 中的非messages参数；不重新运行已完成 Hook。

NodeDriver 用 `commit_model_ready(RuntimeCommit)` 同事务发布最终请求／hash、InputClaim、LoopTicket预占、execution_permit 和 continuation，成功后 Pipeline 才出网。ModelFreezeWriteSet 不自行扣票据。压缩模型不占业务票据，仍有相同scope控制。

`prepare_accept_model` 要求响应已经是 Operation 的最终有效响应，mutation_key 固定 `model:<operation_id>:accept`，把 assistant 与调用关联一次追加；与票据consume、CallBatch或候选答案phase同事务。Pipeline保存effective期间仅保留当前调用的稳定记录绑定／待接受投影，不提前追加第二条assistant消息。`prepare_batch_delivery` 要求所有ordinal已有最终结果，键固定 `batch:<batch_id>:deliver`，填原反馈槽并生成完整工具协议组，与batch consumed和父phase同事务。Executor失败的PendingFeedbackSlot按runtime.md复用，不另加一条重复错误。

这些端口通过调用篇的原始 Manager 门面装配：`context.materialize/context.mutate/result.read` 属于 internal，不虚构外部 Attempt；`prepare_effect` 返回 `PreparedDomainEffect{effect_kind:read|context_mutation,expected_context_revisions[],effect_ref?,result_ref,effect_hash}`，其写集与raw_saved同事务。`context.compact` 属于recoverable，`prepare_wait` 返回 `RecoverableWaitPlan{wait_kind:compaction_episode,wait_ref,source_operation_id,expected_revision,initial_effects_ref}`，Episode初始状态与Operation.target_ref同事务；块模型调用有自己的Operation／Attempt，Episode完成再桥接外层raw结果。

创建／追加／批次消费在事务内分配连续 context_seq、生成一个新 effective revision；同一次提交中的多条消息只推进一个 revision。对 head 的更新与所有记录、manifest、NodeCheckpoint 和 ScopeCommit 一致提交。重复幂等键且请求 hash 不同返回 `IDEMPOTENCY_CONFLICT`。

### 2.3 与 Hook 提交配合

`StageContextPatch` 不重跑 Hook，也不选择链顺序。其字段是 `hook_invocation_id, operation_id, phase, mapping_schema_ref, normalized_change_ref, normalized_change_hash, original_base_revision_refs[], expected_effective_payload_hash, immutable_fact_refs[]`；规范化集合由 event-bus.md 唯一算法生成。它从 `NormalizedPatch{hook_invocation_id,mapping_version,input_payload_hash,original_base_revisions[],writes[],read_set[],authorization_digest}` 转换而来，不另做第二次归一。HookDecision.context_patch仍是单个ContextPatch；payload映射可同时影响主context，因此写集允许多个context。

`ContextPatch{context_id,base_revision,operations[]}` 的每项为 `ContextPatchOperation{target_record_id,field_key,operation:replace_value|append_annotation,value_ref}`，不得使用消息数组下标。`ContextReadPrecondition` 为 `context_id,expected_revision,record_id?,field_key?,expected_effective_hash?,authorization_digest`；读集覆盖目标字段及影响可见性／授权的字段，而非只覆盖写字段。

`ContextWriteSet` 包含 `read_preconditions[], context_mutations[], new_revision_manifests[], effective_payload_ref, effective_payload_hash, source_dependencies[], validation_diagnostics[]`。Pipeline 的 Hook APPLY 提交增加 `hook_decision_write, hook_cursor_write, expected_operation_revision, expected_node_revision, idempotency_key`，与该写集同事务。`PatchReceipt` 返回 `accepted, context_revision_refs[], effective_payload_ref, diagnostic_ref?, commit_receipt`，是此统一提交的结果。

ContextManager 执行以下校验，任何一个失败都不发布部分写集：

1. 可信 scope 确认 context／record 归属、角色或显式共享字段授权；context_id 本身不是授权。
2. 按 base manifest 的稳定 record_id 和 field_key 定位；不接受模型消息下标、裸 GraphState 键或自造记录身份。
3. 校验原始 UserInput、身份、权限、deadline、额度、Operation 实际执行事实均未被覆盖。
4. 对已冻结请求只允许追加有来源说明；当前尚未冻结调用的有效字段必须与 effective payload 同值。
5. 按注册 schema 校验完整候选结果，再验证保护输入及固定输入预算；可压缩材料超阈值由正常压缩处理。
6. 重查 head；只在目标字段、可见性和授权语义均未变化时将同一写集重定位到新 head，记录原 base 与 relocation 来源。
7. 目标字段发生变化或无法证明未变，返回 `CONTEXT_REVISION_CONFLICT`；不重新执行外部 Hook、不覆盖他人提交。
8. 决定合法性、无效 patch、deny 的处理均由 event-bus.md 控制；ContextManager 只返回验证结果和原子写集。

跨多个 ContextScope 的写集先全部验证，统一事务发布。revision 冲突时重新读取并重建纯函数写集；相同 mutation_id 已提交则直接返回原修订，不能再应用两次 patch。

普通事务锁序为 **根 Run → Invocation → GraphExecution 按 ID → NodeTask 按 ID → Operation 按 ID → Context 按 ID → 子记录**。涉及会话的输入领取、补充转移和终态命令预先锁 Session，再按上述顺序；严禁持有Run后补拿Session。待验证大正文在事务前读取和 hash 校验；事务内验证版本／授权前置并发布引用，网络和文件写不放入事务。

## 3. 历史投影与输入投递

本节的 Session 历史选择和补充领取适用于直接 Agent Run。Workflow 的 AgentNode 使用 input_only 的 Invocation 私有上下文，不能沿父 Run 的 session_id 自动读取历史或领取补充；外层输入与状态映射见[Workflow 上下文边界](workflow.md#41-输入历史与角色上下文)。无 AgentInvocation 的根 Workflow 不调用下文根角色定位逻辑。

### 3.1 查询 DTO 与唯一历史算法

`HistorySelection` 字段为 `session_id, current_run_id, current_question_input_seq, recent_n, ended_runs[], recent_run_ids[], source_revision_refs[], input_watermark`。`recent_n` 是本 Run 冻结配置，默认 2，允许 0。

`HistoryItem` 字段为 `item_id, run_id, kind:user_input|effective_record|terminal_marker|transfer_marker, input_id?, record_id?, source_revision_ref?, payload_ref?, terminal_status?, order_key, protected:bool, source_refs[]`。

`HistoryProjector.select(session_id,current_run_id,recent_n,authority)` 固定当前问题的 input_seq，分页读取它之前、属于同 Session 的全部终态 Run，按各轮原问题 input_seq 升序排序。终态集合固定为 `succeeded, failed, cancelled, timed_out, budget_exhausted`；stopping、queued、running 不计入。

1. 取排序末尾 N 轮为 recent；N=0 时 recent 为空，其余为 older。不得跳过失败、补取成功轮。
2. older 按轮次输出该轮原问题和全部补充，再输出已提交最终有效结果（存在时）及真实终态标记；没有最终结果输出“未形成最终结果”。
3. recent 按其根 React／Planner 的确定有效版本输出全部已提交过程和终态；不注入 Executor 私有模型／工具日志。
4. 当前角色的有效过程单独接入；Executor 只接任务说明、显式约束、材料及自己的过程，不能执行根历史选取器。
5. 逐 input_id 去重：属于当前保护集合时只保留当前原文位置；否则正文置于最后实际 delivered 位置；从未 delivered 的保留最初接收位置；其他位置留来源／转交标记。
6. 同轮 UserInput 按 input_seq 排序；角色消息按 context_seq 排序；并行结果按 batch ordinal，不按完成时间排序。
7. 历史末尾 Run 终态从权威 Run 读，不从消息猜测。故障恢复原 Run 不新增历史轮次。
8. 首次新 Run 不复用旧 Run 的 ContextSnapshot 代替 recent 选取；全量历史分页物化后再按本次预算压缩。

根角色过程包括普通工具、MCP、skill.read、exec、委派摘要、拒绝及有效参数来源；大结果保持当时已见的预览和 result_ref，不自动展开未阅读正文。`result.read` 的后续返回形成新的独立过程记录。

### 3.2 正式输入与保护清单

| DTO | 字段 |
| --- | --- |
| `UserInput` | `input_id, session_id, input_seq, origin_run_id?, kind:question\|supplement, body_ref, received_at` |
| `UserInputDelivery` | `delivery_id, input_id, target_run_id?, operation_id?, previous_delivery_id?, state:pending\|claimed\|delivered\|carried, context_seq?, first_output_receipt_ref?, delivered_at?` |
| `ProtectedInput` | `input_id?, task_input_ref?, raw_body_ref, raw_body_hash, order_key, origin_run_id?, delivery_id?, protection_kind:root_input\|delegated_task` |
| `InputClaim` | `operation_id, input_cutoff, delivery_ids[], protected_inputs[], claim_hash` |

根角色保护本次原问题、已领取 carried 补充和本次正式补充；Executor 保护 Planner 明确交付的任务／约束／材料。保护对象原文、顺序和身份必须出现在每次实际请求，摘要或原文数据库引用不能代替发送正文。

`InputDeliveryService.claim(run_id,operation_id,cutoff,expected_operation_revision)` 只能在根角色下一业务模型请求冻结时执行。读取 `input_seq<=cutoff`、目标 Run 合法且未 delivered 的记录，与请求引用同事务置 claimed；claim 的唯一键为 `(operation_id,input_id)`。实际入口由 Run.root_graph_execution_id → AgentInvocation → 已创建的入口 NodeTask 解析，不新增 Run.root_node_task_id。

`mark_delivered(FirstOutputReceipt)` 同事务插入首输出证据、更新 Attempt.first_output_at，并把该 Operation 的 claimed 记录置 delivered。先 committed receipt 再取消“未送达”判断；本事务不生成 token outbox。重投同 `(attempt_id,first_output)` 回原 receipt；另一个成功 Attempt 不重复推进同输入。

`carry_unconsumed(run_id,terminal_commit)` 将本 Run 未 delivered 的 pending／claimed 补充旧记录置 carried，并创建 `target_run_id=null,state=pending,previous_delivery_id=旧ID` 新记录；唯一键 `(input_id,previous_delivery_id)`。原始问题不是待补充队列的新输入。新 Run 在用户主动提问后绑定这些 pending；不因 pending 自动启动。

API `InputReceipt.delivery_state=pending_next_run` 是上述“旧delivery=carried＋新delivery.target_run_id=null且state=pending”的投影，不是UserInput上的独立可写状态。UserInput保持不可变，所有领取／送达／转交只修改或追加UserInputDelivery；不得另建SessionInput.delivery_state作为第二权威。初次 append 时指定 Run 已终态且此前未接收该 input_id，返回 RUN_ENDED，不保存补充；调用方按新提问协议发起下一 Run。同 input_id 的回执未知重投先查原接收事实，不能把已经转交的输入重新当作新问题。

prepare_context固定本轮input_cutoff，压缩与Hook处理期间不刷新这个水位；新到补充留下一Loop，终态没有下一Loop则转交。冻结只领取已物化且input_seq<=cutoff的投递，逐项核对投影与保护原文，不能临时追加未经过准备流程的输入。Attempt 重试保持原 claim、input_cutoff 和请求 hash，不注入后来输入。

### 3.3 事务与失败

| 边界 | 事务读取／前置 | 事务写入及幂等键 |
| --- | --- | --- |
| 根输入投影追加 | Run 可推进；入口 NodeTask、context head、input_seq 水位和未送达记录 | user_ref、revision、delivery.context_seq；`input-project:<context>:<input>` |
| 请求冻结与领取 | Operation request_hooks 已完成、有效请求 hash、所有 context heads、保护清单、Run／局部控制 | FrozenModelRequestRef、claimed、Operation phase、continuation；`model-freeze:<operation>` |
| 首输出接收 | 当前 lease、Attempt 仍归属原 Operation、claim 引用一致 | receipt、first_output_at、delivered；`first-output:<attempt>` |
| Run 终态转交 | 根 Run 已关闭准入、未 delivered 记录 | carried／pending 链和终态一并提交；`carry:<run>:<input>:<delivery>` |

终态事务和首输出事务以根 Run 锁串行化：先送达则终态不转交；先终态则迟到旧结果不能撤销转交。数据库提交失败时暂停推进，保存可重试提交命令；不得把内存首片段当作已持久送达。权限错误不返回其他角色的 input_id 或正文。

## 4. 模型视图、协议组与预算

### 4.1 视图结构

`ModelMessage` 字段为 `message_id, role, content_blocks[], tool_calls[], tool_call_id?, source_record_refs[], protected_input_refs[], projection_kind:original|effective|summary|protocol_marker`。

`ModelToolCall` 字段为 `call_id, provider_call_id?, ordinal, model_name, arguments_ref, arguments_hash`；call_id 是 SDK 生成的调用身份，provider_call_id 仅作为外部协议关联。

`ProjectionGroup` 字段为 `group_id, kind:message|tool_exchange|terminal_marker, message_ids[], source_record_refs[], complete:bool`。完整 assistant 工具调用及全部结果属于一个 tool_exchange；当前批次未全结算时禁止生成下一业务模型请求。

历史中未完成的工具交互按每个原 call_id 补结构化协议占位，字段为 `executed:false|true|unknown, status:cancelled|unknown|blocked, explanation`。占位不修改 Operation 真相，也不冒充成功输出。未完成模型流只可作为明确标记的审计／错误说明，不成为可执行 assistant 决策。

### 4.2 计量端口

```python
class TokenEstimator:
    def estimate(self, model_binding, transport_request) -> TokenEstimate: ...

class TokenEstimate:
    input_tokens: int
    accuracy: Literal["tokenizer", "estimated"]
    estimator_version: str
    serialized_input_bytes: int
    protocol_overhead_tokens: int
```

有已实现对应 tokenizer 时计量完整适配输入。没有 tokenizer 时，默认 SDK 估算器 `utf8-envelope-v1` 使用完整规范传输输入的 UTF-8 字节数，加 `256 + 32×消息数 + 64×工具数` tokens 的保守封装余量；不将这个估计标成供应商真实 token 数。取值必须包含 system、工具 schema、历史、Hook 增量、保护输入及协议标记。

`Budget` 字段为 `window_tokens:W, output_reserve_tokens:O, safety_margin_tokens:S, input_budget_tokens:B, trigger_ratio:r, trigger_tokens, model_metadata_source, estimator_version`。

```text
O = Agent显式值，否则 min(已知模型最大输出或4096, floor(W/4))
S = Agent显式值，否则 max(256, ceil(W*0.01))
B = W-O-S，必须 > 0
r = Agent显式值，否则 0.80；必须 0 < r < 1
trigger_tokens = floor(r*B)
压缩评估条件 I >= trigger_tokens；发送硬条件 I <= B
```

W 必须来自已核验元数据或用户显式配置；没有 W 构造校验失败。O 与 S 是执行预算预留，不是模型能力声明。实际业务请求max_output_tokens默认取min(已知模型上限,O)，且必须满足0<max_output_tokens<=O；压缩请求用T并同样不超过其预算预留。前置Hook修改max_output后重做该校验，超界patch整体无效按fail-open保留原值，不扩大O或变更固定预算。供应商明确 `context_length_exceeded` 返回 `MODEL_CONTEXT_LENGTH_EXCEEDED`，结束对应角色，不对同一请求做网络重试或追加压缩。

`CompactionPolicy.evaluate(draft,estimate,budget,existing_episode_ref)` 返回 `send|compact|no_material|fail`，携带 `fixed_tokens, compressible_tokens, target_tokens?, error?`。无材料且 I<=B 返回 no_material 并登记本 Operation 已评估；I>B 返回 `CONTEXT_PROTECTED_INPUT_TOO_LARGE`。

## 5. CompactionEpisode 的唯一分块与恢复算法

### 5.1 DTO

| DTO | 字段 |
| --- | --- |
| `ContextSourceRange` | `context_id, from_seq, through_seq`，闭区间；不能把多个 context 的 seq 混为一个水位 |
| `SourceFragment` | `record_id, context_id, source_revision, effective_hash, char_start, char_end, payload_ref, mutation_ids[], input_ids[], previous_snapshot_id?`；字符区间为 Unicode code point 半开区间 |
| `CompactionSourceManifest` | `source_view_hash, source_revision_refs[], source_ranges[], ordered_fragments[], protected_input_ids[], replaced_historical_input_ids[], source_watermarks[], prior_snapshot_refs[], dependencies[]` |
| `CompactionPlan` | `plan_ref, plan_hash, episode_id, source_manifest_ref, prompt_version, custom_prompt_ref?, estimator_version, target_tokens:T, fixed_prompt_ref, chunk_boundaries[], chunk_count` |
| `ChunkBoundary` | `ordinal, fragments[], input_hash, reserved_previous_summary_tokens:T, max_output_tokens:T` |
| `CompactionEpisode` | `episode_id, run_id, owner_node_task_id, triggering_model_operation_id, current_operation_id?, result_snapshot_id?, source_view_hash, source_manifest_ref, source_effective_revisions, model_binding_key, compaction_prompt_ref, chunk_cursor, accumulated_summary_ref?, input_tokens_estimated, created_at, status:running\|succeeded\|failed\|cancelled, revision` |
| `CompactionChunkResult` | `episode_id, ordinal, operation_id, raw_result_ref, effective_summary_ref, summary_hash, estimated_tokens, truncated, commit_ref` |
| `ContextSnapshot` | `snapshot_id, context_id, generation, source_ranges[], source_effective_revisions, protected_input_ids[], summary_ref, tail_after_seq, token_estimate` |
| `SnapshotApplicability` | `snapshot_id, checked_revision_refs[], applicable:bool, invalidated_record_ids[], diagnostic_ref?`；查询结果，不是另一份 head |

Episode 的 plan_ref、plan_hash 与 snapshot 有效性诊断存入其 source manifest／提交扩展记录，不另造可独立推进的 Episode 状态机。`chunk_cursor` 是已完成块数，0 表示未完成任何块。

### 5.2 方法与状态边界

```python
class CompactionService:
    async def plan(self, draft_ref, model_operation_id, lease) -> CompactionEpisodeRef: ...
    async def advance(self, episode_id, expected_revision, lease) -> CompactionProgress: ...
    async def publish(self, episode_id, expected_revision, lease) -> CompactionResult: ...
    async def inspect(self, episode_id, authority) -> CompactionProgress: ...
```

`CompactionProgress` 为 `episode_ref, status, chunk_cursor, chunk_count, pending_operation_ref?, result_snapshot_id?, error?`。`CompactionResult` 为 `episode_id, status, snapshot_id?, applicability, rebuilt_view_ref?, error?`。plan 每个 triggering_model_operation_id 至多创建一个 Episode；重入返回同一 ID，不能通过故障重建绕过唯一性。

`advance` 每次最多推进一个 chunk；先查 purpose_key 对应 Operation，有最终结果则消费，有 raw_saved 则继续后置处理，有待执行意图才由调用管道执行。它不持有数据库事务等待模型。业务 continuation 处于 compact_context，恢复不用还原 Python 栈。

### 5.3 目标与分块

固定材料 F 包括角色 system、工具 schema、当前保护输入、必须保留的合法 Hook 增量、协议尾部及最终摘要封装；与可压缩集合互斥。

```text
H = B-F
P = min(O, 已知供应商输出上限, H)；未知供应商上限不参与 min
C = min(8192, floor(B*0.25), P)
D = floor(B*0.60)-F
T_auto = min(C,D) if D>0 else C
T = min(Agent显式正整数目标,P) if 已配置 else T_auto
```

有材料而 H<=0 返回 `CONTEXT_PROTECTED_INPUT_TOO_LARGE`；T<=0 返回 `CONTEXT_BUDGET_INVALID`。60% 是软目标，摘要成功后只验证硬窗口，不依据语义质量或达到80%与否重做。

1. 冻结 source revision、source manifest、默认摘要 Prompt 版本、用户补充 Prompt、模型绑定、T 和估算器版本。
2. 默认 system 只要求整理事实、目标、约束、结果、未完成项和错误；自定义压缩 Prompt 以独立 user 材料加入，不能替换 system。
3. 输入先按历史轮次、context_seq、record_id 排序，再构造普通消息组或完整工具交互组；当前保护输入绝不加入替换范围。
4. 每块预留默认 system／自定义 Prompt／已获准增量、上一摘要最大 T、当前输出 T、安全边距和转录封装，剩余为材料容量。
5. 对排序下一完整组做加入后的完整估算；可放入就追加。放不下而当前块非空，关闭本块后再试该组。
6. 单组独自放不下时按记录顺序分解；单记录仍放不下，用二分查找选择能够放入的最大 Unicode code point 前缀，fragment 标出 record_id 与字符区间。
7. 每次拆分必须消耗至少一个字符；连一个字符连同封装都放不下，返回 `COMPACTION_FIXED_PROMPT_TOO_LARGE`，不创建空块循环。
8. 所有材料以转录数据发送给压缩模型，不发送孤立 provider tool 消息；块边界和全部 fragment hash 写入固定 plan。
9. 第0块输入本块材料；第k块输入上一已提交有效摘要和第k块材料。模型请求输出上限固定为 T，无工具目录、业务 Skill 或 Executor 权限。
10. 每块模型调用带 `purpose=compaction`，purpose_key 固定 `compaction:<episode_id>:chunk:<ordinal>`；沿用模型连接重试配置，不占 LoopTicket。
11. 已完成 Hook 不因重新估算而重跑；合法 Hook 增量引入后若本块请求无法满足硬窗口，舍弃导致不可容纳固定材料的无效修改；其余处理遵循调用协议，不能偷偷移动已冻结块边界。
12. raw 完整结果先保存，经后置 Hook 和摘要结构校验后提交 chunk result、cursor+1 与 accumulated_summary_ref；同 ordinal 重复消费返回原 receipt。
13. 任一块最终失败或合法 deny，使 Episode failed，返回 `COMPACTION_FAILED`／明确拒绝原因并终结所属完整 AgentInvocation；根停止使 Episode cancelled。

摘要只做完整传输和规定输出结构校验，不对正文增加语义评分；合法完成但达到输出上限的摘要标记 truncated 后接受。空字符串符合摘要输出结构时同样接受，最终原文保护与硬窗口检查仍执行。输出包含未闭合传输帧仍是协议失败，不是可接受截断摘要。

### 5.4 快照发布与失效

全部块完成后，读取当前 context heads、原 active_snapshot_id、manifest 的每个已覆盖 record_id 的有效 hash／mutation 集。只有追加新记录或修改未覆盖内容，不阻止提交原覆盖 snapshot；新增材料稍后接回。

若任一已覆盖记录有效内容改变，保存基于原固定来源的不可变 ContextSnapshot 和 Episode.result_snapshot_id，保留模型成功事实，`status=succeeded`，但不更新 RoleContext.active_snapshot_id；诊断 `SNAPSHOT_SOURCE_CHANGED`，从原 source manifest 展开所有覆盖原材料，以当前有效版本重新物化。该快照只供固定旧版本查询，不用于新视图。该 Operation 已用过唯一 Episode，最终仍超 B 就 `CONTEXT_WINDOW_EXCEEDED`，不重新调用摘要模型。

来源未变时，同事务插入 ContextSnapshot、关联 Episode.result_snapshot_id、替换 RoleContext.active_snapshot_id、写 continuation 与 ScopeCommit。generation=该 context 已提交 generation+1；唯一键 `(context_id,generation)` 和 `(triggering_model_operation_id)` 分别保护快照序列与 Episode 数量。

后续 materialize 按“适用摘要＋未覆盖记录／更正＋保护输入”组装。覆盖判断使用稳定 record_id、有效 hash、mutation 集和 source revision；不能仅按最大 seq 跳过消息。一次记录版本只替代一次。旧摘要失效是整份替代范围失效，不在摘要自然语言中删除单句。

新 Episode 可以折叠仍适用的旧摘要和增量，但 source manifest 保留传递来源链及合并覆盖。source_scope 依赖取并集；源被删除使相应摘要不可用，不能借重建恢复已删除正文。

最终接回本次prepare_context固定水位以内的保护输入，再完整计量；之后到达的补充不加入本请求。I<=B 才冻结业务请求；即使仍超过80%也继续本次；I>B 明确终结，无第二次压缩。

### 5.5 提交与恢复表

| 提交 | 读取及 CAS 前置 | 写集／唯一键 |
| --- | --- | --- |
| 创建 Episode | 原业务 Operation 尚未发送且无 episode；context 与 NodeTask revision | Episode、plan引用、continuation；`compact-plan:<model_operation>` |
| 创建块意图 | Episode running、cursor=k、无已完成k | `purpose_key=compaction:<episode>:chunk:<k>` 的 Operation／Attempt意图 |
| 消费块 | cursor=k、块最终有效响应、Operation.completed | ChunkResult、cursor=k+1、summary_ref；`compact-chunk:<episode>:<k>` |
| 发布快照 | cursor=chunk_count、来源 hash、旧 snapshot、context heads、lease | snapshot／适用诊断、Episode结果、context指针；`compact-publish:<episode>` |
| 恢复已冻请求 | FrozenModelRequestRef 与 hash 全部存在可读 | 不写新 Episode、不重跑前置链；继续原 Operation |

blob 已写未提交是孤儿；不得据此跳过 cursor。块 raw 已提交则仅恢复后处理；第二块已提交后宕机从第三块继续。快照不存在或引用不可读返回 `PERSISTENCE_UNAVAILABLE`，不凭模型重新生成当作原结果。


## 6. 历史与压缩的边界示例

一次提问及其正式补充属于一个 Run；故障接管沿用原 Run，不增加历史轮次。最近 N 次指当前 Run 之前最近 N 次已终结提问，成功、失败、用户结束都算，不向前补找成功轮次。较早记录保留全部用户输入（含补充）、最终结果及结束状态。Executor 私有过程不会自动进入 Planner 历史，只进入委派结果。

无最终结果明确标记未完成。工具协议组必须完整，历史被中断的调用使用未完成占位，不能将占位写回真实 Operation 成功状态。大结果保留实际交付给模型的预览和可查引用，不声称模型读过全部正文。所有历史投影读取有效修订，不能绕过 Hook 变更重新读取 raw 内容。

当前问题、正式补充、Executor 任务约束由代码原样保护，压缩成功不代表可以省略它们。模型重试使用同一冻结输入，不把新补充塞进重试 Attempt。SDK 不判断用户输入“是否有用”。

### 6.1 历史、补充和格式修正

样例 Session 顺序为 R1=succeeded（问题Q1＋补充A1/A2）、R2=failed、R3=cancelled、R4=当前，N=2。投影必须是 R1 的Q1/A1/A2/最终结果/终态，R2和R3的完整根过程/真实终态，再接R4；不加入R1工具细节，不改选R1替代失败轮。

R3 的A3在模型首输出前停止：旧delivery=carried，新pending保留input_id=A3。R4冻结时领取A3和Q4，按接收顺序保护并去重；R3历史位置只留转交标记。R4的空完整帧提交kind=complete_frame，A3=delivered；若输出schema要求`{"answer":string}`，空候选形成格式反馈，下一业务Loop修正，不能把A3再次转交R5。

### 6.2 一次压缩与并发修改

B=100000、O=8192、F=65000、材料20000：触发85000≥80000，H=35000，D=-5000，T=8192。计划三块，第二块提交后故障；恢复保持episode_id、前两块operation_id、cursor=2，只调用第三块。

最后快照发布前，Hook修改被覆盖record X：源hash不符，不设置active_snapshot；展开X等原材料的最新有效值，接回原已固定input_cutoff以内的保护输入再计量。如果I=100001，本次业务模型不出网，返回CONTEXT_WINDOW_EXCEEDED；不能再创建第二Episode。若仅新增未覆盖record Y，原snapshot可以发布，Y增量接回。

## 7. 上下文验收断言

编号与能力验收表共同追踪；所有场景使用可控假模型和可故障注入的执行存储。

| 编号／注入点 | 必须断言 |
| --- | --- |
| C01 N=0、N=2、终态混合 | 每轮全部UserInput保留；最近N按问题顺序且包含全部终态种类；无成功补位 |
| C02 一轮五条补充 | 五个input_id、一个Run；历史裁剪不丢任一补充 |
| C03 Executor读根历史 | 未获授权的materialize返回permission；显式任务输入逐字保留 |
| C04 同一mutation重投 | revision、record_seq只推进一次，原receipt与hash一致 |
| C05 patch并发 | 不相交字段重定位；重叠字段返回CONTEXT_REVISION_CONFLICT；合法deny仍有效 |
| C06 request freeze后更新 | 原FrozenRequest hash不变；下一个Operation读取新revision |
| C08 首输出与终态竞态 | 两种提交顺序均无既delivered又pending的有效交付，旧epoch不能覆盖 |
| C12 80%阈值 | threshold-1不压缩，threshold压缩；无材料且I<=B可以发送 |
| C13 固定输入超窗 | 当前原文完整保留；未产生供应商业务Attempt；错误不暴露system |
| C14 压缩第二块后故障 | cursor=2恢复第三块；已完成块无新Attempt、无业务Loop扣款 |
| C16 覆盖材料变化 | 整份snapshot替代失效；不从自然语言摘要删句；旧记录可审计 |
| C17 压缩期间补充 | input_cutoff不刷新；新增input不进入当前请求，在下一Loop领取或终态转交；原已选保护输入不被摘要替代 |
| C30 source scope删除 | 父投影／摘要／副本不可继续读取；恢复不能从旧snapshot重新发布正文 |
