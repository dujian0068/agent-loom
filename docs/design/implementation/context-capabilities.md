# 上下文、模型适配、能力与执行环境实施规格

> **重建边界（2026-10-15）**：旧业务实现已移除。本页保留为待调整的设计草案；用户、应用、发布与配置版本属于平台，SDK 只接收已解析配置并负责执行与恢复。涉及平台身份、发布绑定及表结构的内容需按 [SDK / Platform 边界](../sdk-platform-boundary.md)重新划分，不直接作为 SDK 的必填契约或已实现功能。

日期：2026-10-15。状态：实施基线，尚未实现。本文规定 DTO、确定算法和适配器验收；不表示已有运行代码符合本规格。

入口见 [实施总览](index.md)。上层约束来自 [上下文生命周期](../context-lifecycle.md)、[能力与环境](../capabilities-hooks.md)、[数据契约](../data-contracts.md)、[领域接口](../domain-interfaces.md)及[存储恢复](../storage-recovery-api.md)。

[调用与 Hook](invocation.md)是 Operation、Attempt、Hook 链、patch 归一及重试决策的唯一实现权威；本文只规定这些流程调用 ContextManager、模型／工具适配器和执行环境时的契约。[图运行](runtime-graph.md)负责 Loop、批次、格式修正后的推进；[持久化](persistence.md)负责 RuntimeCommit、租约、物理表和跨域事务；[配置与 API](catalog-api.md)负责发布、资源 CRUD 和用户输入接收。

## 1. 实现边界与公共类型

- Executor 继承所属 AgentRelease 的主模型；CompactionEpisode 继承当前角色模型，二者没有独立模型选择入口。
- 角色 Prompt、工具目录、模型配置、上下文参数、重试策略及环境策略从冻结 Release 解析；恢复不读取“最新”配置替换它们。
- 撤销授权仍立即生效；固定版本不能保持已撤销的密钥、资源或工作区访问权。
- 每个 React 类 NodeTask 有自己的 RoleContext；同 Invocation 的 Planner／Executor 共享工作文件，不共享私有消息。
- 外层 CapabilityNode 没有隐含 RoleContext；其调用有效值留在 Operation，并通过节点映射进入 GraphState。
- ContextManager、ModelManager、ToolManager、EnvironmentManager 是事件请求的唯一对应消费者；内部 writer、适配器和纯函数不再次进入相同请求链。

下文所有持久 DTO 均带 `schema_version=1`；UUID 使用平台分配的 UUID，时间使用 UTC，大小使用字节，tokens 为非负整数。普通实体revision从1开始；Context.effective_revision和record_head允许初始0。`?` 表示可空；集合默认空集合，不以缺失字段表达不同语义。不可变正文使用规范 JSON／UTF-8 编码后计算 SHA-256。

| 类型 | 完整公共字段与语义 |
| --- | --- |
| `OwnerRef` | `kind:agent|workflow, id:UUID, space_id:UUID`，来自可信发布身份 |
| `PayloadRef` | 公共可辨识union：`{kind:inline,value}` 或 `{kind:blob,owner_ref,blob_id,sha256,size_bytes,media_type}`；采用SD-3结构，不另造全blob版本 |
| `ContextRevisionRef` | `context_id:UUID, revision:int`；revision 从 0 开始，0 是空 manifest |
| `ContextReadAuthority` | `run_id, invocation_id?, node_task_id?, allowed_context_ids[], shared_scope_grants[], source_owner_grants[]`，Runtime 装配，不能从模型 JSON 接收 |
| `SourceDependency` | `source_owner, source_ref, grant_id`；派生结果取全部已消费来源的保守并集 |
| `FrozenBindingRef` | `release_id, node_binding_key, role_key?, model_binding_digest?, resource_manifest_digest` |
| `DomainFailure` | `code, category, message, retryable_transport:false|true, external_outcome:not_started|known|unknown, detail_ref?`；message 已脱敏 |

`retryable_transport` 只能由已实现的错误码／异常类型映射产生，不从字符串猜测；实际是否重试仍由 invocation.md 的冻结 RetryPolicy 决定。必要持久化错误不能伪装为工具成功或 Hook fail-open。

inline内容的owner和权限由承载它的可信实体决定，blob分支保留显式owner；两者都传播来源依赖，hash不是访问令牌。inline／blob阈值沿用persistence.md，读取端必须接受两种分支；下文“写blob”指需外置的大正文，不能把小inline值强制另存一套内容权威。

## 2. 原始记录、有效版本与提交端口

### 2.1 持久 DTO

| DTO | 字段 |
| --- | --- |
| `RoleContext` | `context_id, invocation_id, owner_node_task_id, role_key, record_head:int, effective_revision:int, active_snapshot_id?, last_model_operation_id?` |
| `ContextRecord` | `record_id, context_id, context_seq, run_id, role:user|assistant|tool|system, message_kind, payload_ref, operation_id?, call_id?, origin_input_id?` |
| `ContextMutation` | `mutation_id, context_id, base_revision, committed_revision, source_kind:hook|runtime|user, source_ref, cause_operation_id?, patch_ref, created_at` |
| `EffectiveContextRevision` | `context_id, revision, parent_revision?, record_watermark, mutation_ids[], effective_manifest_ref, commit_ref, created_at` |
| `EffectiveRecordEntry` | `record_id, context_seq, raw_payload_ref, effective_payload_ref, effective_hash, source_refs[], visible_fields[], dependency_refs[]` |
| `EffectiveManifest` | `context_id, revision, entries:EffectiveRecordEntry[], authorized_field_catalog_ref, field_catalog_version` |

ContextRecord 永不更新正文。有效 manifest 按 `(context_seq, record_id)` 排序，保留 raw 引用及已生效变更来源；版本也不可改写。ContextRecord 只代表消息事实，tool 的真实执行状态仍以 Operation 为准。配置 Prompt 的有效引用可以成为稳定投影记录，其修改不能倒写 AgentRelease。

`logical_result_key` 是追加命令中的幂等元数据，保存在提交索引而非另造消息 DTO；同一 `(operation_id, message_kind, logical_result_key)` 只能产生一条记录。没有 operation_id 的输入使用 `(context_id, origin_input_id, message_kind)`；平台配置投影使用 `(context_id, release_id, prompt_slot)`。

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
| `PrepareContext` | `context_id, model_operation_id, frozen_binding_ref, history_cutoff_run_id?, input_cutoff, expected_node_revision` |
| `ContextDraft` | `draft_id, context_id, model_operation_id, source_revision_refs[], input_cutoff, protected_inputs[], ordered_items[], snapshot_candidate_id?, source_view_hash, binding_ref` |
| `MaterializeContext` | `draft_ref, source_revision_refs[], snapshot_id?, final_input_cutoff`；必须显式固定版本 |
| `ModelView` | `view_ref, view_hash, source_revision_refs[], snapshot_id?, messages[], protected_inputs[], tool_catalog_ref, estimate, dependency_refs[]` |
| `FreezeModelRequest` | `operation_id, expected_operation_revision, expected_context_heads[], model_view_ref, effective_parameters_ref, claimed_delivery_ids[], input_cutoff, expected_node_revision, idempotency_key` |
| `FrozenModelRequestRef` | `operation_id, request_ref, request_hash, context_revision_refs[], snapshot_id?, protected_input_ids[], input_cutoff, commit_ref` |
| `ModelFreezeWriteSet` | `operation_id, expected_operation_revision, expected_context_heads[], frozen_request_ref, input_claim_write, source_dependencies[], command_hash`；无独立提交权限 |

上述准备端口无持久状态推进；写出的不可变 blob 尚未被事务引用时都是可回收材料。调用方将 ContextDelta／ContextWriteSet／ModelFreezeWriteSet 放入 `RuntimeCommit`，唯一写入口为 `CommitCoordinator.commit(RuntimeCommit, ExecutionLease)`。普通 ContextPort.append_original 门面也由所属 Manager 准备并提交该写集，不开放数据库 writer 给节点。

`Pipeline.prepare_business_model(ref,scope)` 在 request_hooks 阶段返回前置 Hook 已应用的 `PreparedModelRequest`；尚未最终冻结，不授予出网许可。其字段以 invocation.md 为准，至少关联 `operation_id,effective_payload_ref,effective_payload_hash,context_revision_refs[],hook_cursor_ref,effective_parameters_ref`。ContextManager.materialize_prepared_model 只根据新上下文／摘要重建 messages，逐项保留原 effective_parameters_ref 中的非messages参数；不重新运行已完成 Hook。

NodeDriver 用 `commit_model_ready(RuntimeCommit)` 同事务发布最终请求／hash、InputClaim、LoopTicket预占、execution_permit 和 continuation，成功后 Pipeline 才出网。ModelFreezeWriteSet 不自行扣票据。压缩模型不占业务票据，仍有相同scope控制。

`prepare_accept_model` 要求响应已经是 Operation 的最终有效响应，mutation_key 固定 `model:<operation_id>:accept`，把 assistant 与调用关联一次追加；与票据consume、CallBatch或候选答案phase同事务。Pipeline保存effective期间仅保留当前调用的稳定记录绑定／待接受投影，不提前追加第二条assistant消息。`prepare_batch_delivery` 要求所有ordinal已有最终结果，键固定 `batch:<batch_id>:deliver`，填原反馈槽并生成完整工具协议组，与batch consumed和父phase同事务。Executor失败的PendingFeedbackSlot按runtime-graph.md复用，不另加一条重复错误。

这些端口通过调用篇的原始 Manager 门面装配：`context.materialize/context.mutate/result.read` 属于 internal，不虚构外部 Attempt；`prepare_effect` 返回 `PreparedDomainEffect{effect_kind:read|context_mutation,expected_context_revisions[],effect_ref?,result_ref,effect_hash}`，其写集与raw_saved同事务。`context.compact` 属于recoverable，`prepare_wait` 返回 `RecoverableWaitPlan{wait_kind:compaction_episode,wait_ref,source_operation_id,expected_revision,initial_effects_ref}`，Episode初始状态与Operation.target_ref同事务；块模型调用有自己的Operation／Attempt，Episode完成再桥接外层raw结果。

创建／追加／批次消费在事务内分配连续 context_seq、生成一个新 effective revision；同一次提交中的多条消息只推进一个 revision。对 head 的更新与所有记录、manifest、NodeCheckpoint 和 ScopeCommit 一致提交。重复幂等键且请求 hash 不同返回 `IDEMPOTENCY_CONFLICT`。

### 2.3 与 Hook 提交配合

`StageContextPatch` 不重跑 Hook，也不选择链顺序。其字段是 `hook_invocation_id, operation_id, phase, mapping_schema_ref, normalized_change_ref, normalized_change_hash, original_base_revision_refs[], expected_effective_payload_hash, immutable_fact_refs[]`；规范化集合由 invocation.md 唯一算法生成。它从 `NormalizedPatch{hook_invocation_id,mapping_version,input_payload_hash,original_base_revisions[],writes[],read_set[],authorization_digest}` 转换而来，不另做第二次归一。HookDecision.context_patch仍是单个ContextPatch；payload映射可同时影响主context，因此写集允许多个context。

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
8. 决定合法性、无效 patch、deny 的处理均由 invocation.md 控制；ContextManager 只返回验证结果和原子写集。

跨多个 ContextScope 的写集先全部验证，统一事务发布。revision 冲突时重新读取并重建纯函数写集；相同 mutation_id 已提交则直接返回原修订，不能再应用两次 patch。

普通事务锁序为 **根 Run → Invocation → GraphExecution 按 ID → NodeTask 按 ID → Operation 按 ID → Context 按 ID → 子记录**。涉及会话的输入领取、补充转移和终态命令预先锁 Conversation，再按上述顺序；严禁持有Run后补拿Conversation。待验证大正文在事务前读取和 hash 校验；事务内验证版本／授权前置并发布引用，网络和文件写不放入事务。发布／删除owner屏障与Run停止分事务，不持owner锁等待Run。

## 3. 历史投影与输入投递

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
| `UserInput` | `input_id, session_id, input_seq, origin_run_id?, kind:question|supplement, body_ref, received_at` |
| `UserInputDelivery` | `delivery_id, input_id, target_run_id?, operation_id?, previous_delivery_id?, state:pending|claimed|delivered|carried, context_seq?, first_output_receipt_ref?, delivered_at?` |
| `ProtectedInput` | `input_id?, task_input_ref?, raw_body_ref, raw_body_hash, order_key, origin_run_id?, delivery_id?, protection_kind:root_input|delegated_task` |
| `InputClaim` | `operation_id, input_cutoff, delivery_ids[], protected_inputs[], claim_hash` |

根角色保护本次原问题、已领取 carried 补充和本次正式补充；Executor 保护 Planner 明确交付的任务／约束／材料。保护对象原文、顺序和身份必须出现在每次实际请求，摘要或原文数据库引用不能代替发送正文。

`InputDeliveryService.claim(run_id,operation_id,cutoff,expected_operation_revision)` 只能在根角色下一业务模型请求冻结时执行。读取 `input_seq<=cutoff`、目标 Run 合法且未 delivered 的记录，与请求引用同事务置 claimed；claim 的唯一键为 `(operation_id,input_id)`。实际入口由 Run.root_graph_execution_id → AgentInvocation → 已创建的入口 NodeTask 解析，不新增 Run.root_node_task_id。

`mark_delivered(FirstOutputReceipt)` 同事务插入首输出证据、更新 Attempt.first_output_at，并把该 Operation 的 claimed 记录置 delivered。先 committed receipt 再取消“未送达”判断；本事务不生成 token outbox。重投同 `(attempt_id,first_output)` 回原 receipt；另一个成功 Attempt 不重复推进同输入。

`carry_unconsumed(run_id,terminal_commit)` 将本 Run 未 delivered 的 pending／claimed 补充旧记录置 carried，并创建 `target_run_id=null,state=pending,previous_delivery_id=旧ID` 新记录；唯一键 `(input_id,previous_delivery_id)`。原始问题不是待补充队列的新输入。新 Run 在用户主动提问后绑定这些 pending；不因 pending 自动启动。

API `InputReceipt.delivery_state=pending_next_run` 是上述“旧delivery=carried＋新delivery.target_run_id=null且state=pending”的投影，不是UserInput上的独立可写状态。UserInput保持不可变，所有领取／送达／转交只修改或追加UserInputDelivery；不得另建SessionInput.delivery_state作为第二权威。接收时已无可接收补充的活跃Run，也以target_run_id=null的pending投递形成同一API投影。

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

`ModelToolCall` 字段为 `call_id, provider_call_id?, ordinal, model_name, arguments_ref, arguments_hash`；call_id 是平台主键，provider_call_id 仅作为外部协议关联。

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

有已实现对应 tokenizer 时计量完整适配输入。没有 tokenizer 时，默认平台估算器 `utf8-envelope-v1` 使用完整规范传输输入的 UTF-8 字节数，加 `256 + 32×消息数 + 64×工具数` tokens 的保守封装余量；不将这个估计标成供应商真实 token 数。取值必须包含 system、工具 schema、历史、Hook 增量、保护输入及协议标记。

`Budget` 字段为 `window_tokens:W, output_reserve_tokens:O, safety_margin_tokens:S, input_budget_tokens:B, trigger_ratio:r, trigger_tokens, model_metadata_source, estimator_version`。

```text
O = Agent显式值，否则 min(已知模型最大输出或4096, floor(W/4))
S = Agent显式值，否则 max(256, ceil(W*0.01))
B = W-O-S，必须 > 0
r = Agent显式值，否则 0.80；必须 0 < r < 1
trigger_tokens = floor(r*B)
压缩评估条件 I >= trigger_tokens；发送硬条件 I <= B
```

W 必须来自已核验元数据或用户显式配置；没有 W 发布失败。O 与 S 是平台预留，不是模型能力声明。实际业务请求max_output_tokens默认取min(已知模型上限,O)，且必须满足0<max_output_tokens<=O；压缩请求用T并同样不超过其预算预留。前置Hook修改max_output后重做该校验，超界patch整体无效按fail-open保留原值，不扩大O或变更发布预算。供应商明确 `context_length_exceeded` 返回 `MODEL_CONTEXT_LENGTH_EXCEEDED`，结束对应角色，不对同一请求做网络重试或追加压缩。

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
| `CompactionEpisode` | `episode_id, run_id, owner_node_task_id, triggering_model_operation_id, current_operation_id?, result_snapshot_id?, source_view_hash, source_manifest_ref, source_effective_revisions, release_id, prompt_version, chunk_cursor, accumulated_summary_ref?, input_tokens_estimated, created_at, status:running|succeeded|failed|cancelled, revision` |
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

新 Episode 可以折叠仍适用的旧摘要和增量，但 source manifest 保留传递来源链及合并覆盖。source_owner 依赖取并集；源被删除使相应摘要不可用，不能借重建恢复已删除正文。

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

## 6. 模型适配、首输出与完整响应

### 6.1 冻结绑定及单次调用接口

```python
class ModelAdapter:
    async def discover(self, connection_version_ref, credential_handle) -> list[ModelDescriptor]: ...
    async def stream_once(self, request: ModelRequest, ctx: ModelAttemptContext) -> AsyncIterator[ProviderEvent]: ...

class ModelGateway:
    async def generate_once(self, request_ref: FrozenModelRequestRef,
                            ctx: ModelAttemptContext) -> ModelAttemptResult: ...

class ModelManager:
    async def execute_once(self, operation_ref, attempt_ref, execution_permit) -> RawResultRef: ...
```

`ModelManager` 在执行前核对 execution_permit 与冻结 request_hash；业务模型没有同事务预占的 LoopTicket 不出网。压缩调用无需业务票据，但仍验证 Run／Invocation 控制、lease、授权和有效截止。Gateway／Adapter 的 `once` 禁用 SDK 内部重试；多次 Attempt 仅由调用管道驱动。

| DTO | 字段与约束 |
| --- | --- |
| `ModelDescriptor` | `provider_model_id, context_window_tokens?, max_output_tokens?, supported_features[], metadata_source:provider|verified_preset|user, fetched_at?, profile_version`；未知保持 null |
| `ModelRequest` | `operation_id, request_hash, frozen_binding_ref, model_id, messages[], tools[], output_format_ref?, think_config, max_output_tokens, effective_parameters_ref, source_revision_refs[], protected_input_ids[], purpose:business|compaction` |
| `ModelAttemptContext` | `operation_id, attempt_id, attempt_no, lease_epoch, deadline_at, first_output_timeout_ms, idle_timeout_ms?, credential_handle, cancel_signal, receipt_sink`；凭据不序列化 |
| `ProviderEvent` | `kind:heartbeat|role|text_delta|reasoning_delta|tool_name_delta|tool_arguments_delta|usage|completed|error, provider_index?, provider_call_id?, data, received_monotonic_ns` |
| `FirstOutputReceipt` | `receipt_id, operation_id, attempt_id, kind:text|reasoning|tool_name|tool_arguments|complete_frame, observed_at, has_display_content, provider_request_id?, evidence_hash` |
| `ModelResponse` | `text_blocks[], reasoning_blocks[], tool_calls[], usage?, finish_reason?, completion_evidence_ref, provider_request_id?, raw_transport_ref, complete:true` |
| `ModelResponseCall` | `ordinal, provider_call_id?, model_name, arguments_text, parsed_arguments?, parse_error?`；增加call_id:UUID；平台在raw响应保存事务内一次绑定稳定call_id/ordinal，接受决策仅复用 |
| `ModelAttemptResult` | `status:succeeded|failed|cancelled|unknown, response_ref?, partial_ref?, first_output_receipt_ref?, error?, started_at, ended_at` |
| `PartialModelMaterial` | `attempt_id, text_fragments[], reasoning_fragments[], incomplete_calls[], complete:false, last_received_at, error_ref`；仅内部审计 |

首版只有 `OpenAIChatAdapter` 和 `OpenAIResponsesAdapter` 两种协议实现；供应商 preset 显式固定协议、API／Coding Plan 地址、feature 映射、错误和完成帧映射版本。本文不猜测 GLM、Aliyun、DeepSeek、MiniMax 的 Think 字段或窗口。适配 profile 未实现某项配置时发布返回 `MODEL_FEATURE_UNSUPPORTED`，不得忽略参数后宣称已启用。

`ModelAdapter.discover` 只返回实际查询／核验信息，不能探测时明确失败或保留未知；发布要求的窗口由用户显式提供。普通请求不会在运行中偷偷重新发现或切换 endpoint/model。

### 6.2 Attempt 内聚合状态机

状态为 `awaiting_first_output → receiving → complete`，异常转 `failed|cancelled`。complete 只能由该 profile 的合法完整终帧及关联校验确定；TCP EOF、HTTP 200 或一个 finish_reason 字符串本身不是任意 profile 通用的成功证据。

1. Attempt 意图和发送许可提交后开始连接，首输出计时从实际尝试开始；默认 60000ms，有效截止始终取根／Invocation 较早者。
2. 心跳、空 delta、role 和 usage 不算首输出。非空文本／reasoning／工具名／参数片段首次出现，立即调用 receipt_sink 持久保存证据。
3. 完整合法终帧先于任何内容时，生成 `kind=complete_frame,has_display_content=false` 证据，结束首输出等待并完成送达；不能因此显示不存在的正文。
4. 首个有效输出取消首输出计时；后续流仍受总截止和显式 idle_timeout（默认 null）限制，不继续用一分钟截断整条流。
5. 以 provider_index／provider_call_id 组装本 Attempt 工具片段；索引冲突、已关闭调用继续片段等返回 `MODEL_PROTOCOL_ERROR`。工具参数完整前绝不执行。
6. 完成时聚合 text、reasoning、calls、usage，保留供应商原始 payload。JSON 参数无法解析保留 arguments_text 和 parse_error，交业务解释形成对应无效调用反馈，不发连接重试。
7. 只有完整响应进入 raw_saved／后置 Hook。流中断的 partial_ref 与本 Attempt 绑定，不作为后置成功结果，也不拼入下一 Attempt。
8. receipt 持久化失败视为必要存储故障：停止进一步业务交付，关闭连接，已有片段只作未完成材料；不能继续工具调用。
9. complete 后须保存 raw，再完成后置链与 effective；等待者只读已提交最终响应。客户端没有 ProviderEvent 或 token outbox。

取消、deadline、LEASE_LOST 不可自动重试；HTTP 429、502/503/504、明确 reset／暂时连接超时按调用协议分类。网络结果 unknown 不等于协议失败可无条件重发。后置 Hook 拒绝或故障不重新调用供应商。

`generate_once` 若返回完整响应之后提交失败，重试的是原 raw 保存命令；已持久 raw 的恢复只做后处理。如果进程丢失且无 raw，保留外部结果 unknown，再按明确异常类别与冻结 RetryPolicy 决定，不能因“没有结果”自动重发。

### 6.3 标准输出与修正接口

```python
class OutputValidator:
    def validate(self, candidate_ref, output_schema_ref, schema_version) -> OutputValidation: ...

class OutputValidation:
    valid: bool
    errors: tuple[ValidationIssue, ...]
    validated_output_ref: PayloadRef | None
```

`ValidationIssue` 字段为 `json_pointer, keyword, message, expected_ref?`，稳定按 pointer／keyword 排序；错误说明不带 system Prompt、凭据或其他角色上下文。没有标准格式要求时按角色候选规则验证，不额外要求模型发 finish 工具。

`ContextManager.prepare_output_correction(context_ref,model_operation_id,validation_ref,output_schema_ref)` 返回 ContextDelta；只追加带来源的格式反馈与输出要求，不覆盖原候选答案。幂等键 `format:<model_operation_id>:feedback`。

NodeDriver 在同一提交中保存反馈、consume 已完成决策票据及下一 prepare_context 阶段；下一次修正获得新的业务模型 Operation 和 LoopTicket，使用同一根额度／deadline。没有独立修正次数、重试开关或额外额度；已耗尽直接按图运行协议结束。空完整候选不满足已配输出格式时也走此流程。

## 7. 冻结能力目录、调用结果与按需读取

### 7.1 目录与 Handler

| DTO | 完整字段 |
| --- | --- |
| `CapabilitySpec` | `resource_version_id, capability_key, kind:builtin|hosted|mcp|script, model_name, description, input_schema, output_schema?, handler_ref, credential_binding_ref?` |
| `ResolvedCatalog` | `release_id, binding_key, catalog_hash, capabilities[], model_name_to_key, skill_entries[], authorization_ref, environment_policy_ref` |
| `CapabilityRequest` | `operation_id, resource_version_id, capability_key, arguments` |
| `ToolInvocationContext` | `owner_ref, run_id, graph_execution_id, node_task_id?, invocation_id?, conversation_ref?, role_key?, frozen_binding_ref, workspace_scope_ref?, operation_id, attempt_id, attempt_no, lease_epoch, deadline_at, authorization_ref, cancellation` |
| `ToolResult` | `status:succeeded|failed|unknown, content:ContentBlock[], error?, artifacts:ArtifactRef[], metadata` |
| `ContentBlock` | `type:text|json|artifact_ref`，分别含 `text`、`json_ref`、`artifact_ref`；不能内嵌任意系统路径 |

`CapabilityRegistry.for_role(release_id,role_key)`、`for_node_binding(release_id,binding_key)` 返回该发布冻结目录；`resolve(catalog,model_name)` 只在本目录查找，不搜索全平台同名工具。发布时 model_name 冲突拒绝；运行时缺名返回 `CAPABILITY_NOT_FOUND`，目录 hash 不匹配返回 `CAPABILITY_VERSION_MISMATCH`。

`CapabilityHandler.invoke_once(request,context)->ToolResult` 只执行一次；Manager 校验调用可信范围、最终 schema、凭据撤销及执行许可，再调用原 Handler。Hook allow 不扩大目录。普通工具不创建新 NodeTask；Planner 委派 action 由图运行负责，不能交给普通 handler_ref。

平台生成的对象 input_schema 默认 additionalProperties=false；MCP schema 保留其声明扩展语义。导入收拢许可本地 `$ref`，拒绝外部网络 `$ref`；运行校验不得顺带出网取 schema。

`OperationError` 固定字段为 `code, category:validation|permission|not_found|conflict|transport|rate_limit|timeout|business|protocol|cancelled|unknown, message, phase:connect|send|receive|execute, external_outcome:not_started|known|unknown, provider_code?`。成功没有 error；unknown 必须有 error 且 external_outcome=unknown。

### 7.2 结果预览与 result.read

原始结果全量按 owner 写不可变 blob；后置 Hook 得到完整受限引用，普通模型投影只含有效预览。默认平台预览上限固定为 64KiB UTF-8，遇字符边界向前截断，不增加用户配置项。

`ResultPreview` 字段为 `result_ref, media_type, total_bytes, preview_start, preview_end, text?, structured_ref?, truncated, source_operation_id, effective_result_hash`。区间为 UTF-8 字节半开范围；非文本返回 artifact 元信息，不把二进制解码成聊天文本。预览只表示已实际返回范围，不暗示 Agent 读过全文。

`ResultRead{result_ref,offset:int>=0,limit:int=16384}` 允许 `1..65536`；返回 `ResultSlice{result_ref,offset,end_offset,next_offset?,total_bytes,text,etag}`。要求 offset 是 UTF-8 边界，否则 `INVALID_OFFSET`；limit 截断到边界。超末尾返回空且 next_offset=null。

result.read 根据 owner、调用 NodeTask／角色或显式结果授权验证，并检查 source_owner 尚可用；没有 AgentSession 的外层能力合法使用同一接口。不同 Executor 的 result_ref 不能因同 Invocation 共卷而直接读取。读取本身产生新 Operation／结果记录，响应不暴露 blob 宿主路径。

## 8. Skill 导入、发现与脚本

### 8.1 导入命令与产物

`SkillImportCommand` 为 `import_id,source:upload|git,upload_ref?|git_url?,commit_sha?,space_id,credential_binding_ref?,idempotency_key`。git 必须给完整 commit；提交物包括 `SkillPackage{resource_version_id,content_hash,source_ref,commit_sha?,entries[],files_manifest_ref,import_report_ref}`。

`SkillEntry` 为 `name,description,entry_path,license?,compatibility?,metadata,arguments?,argument_hint?,user_invocable:bool,disable_model_invocation:bool,allowed_tools[],disallowed_tools[],hook_resource_refs[]`。`PackageFile` 为 `relative_path,kind:file|directory,size,sha256?,media_type?`。

导入 driver 在无工作区挂载的专用临时目录执行，输入只接受已授权上传引用或通过网络策略的 HTTPS／SSH URL。限制包展开总大小 100MiB、单文件20MiB、文件数10000；这是第一版防资源耗尽常量，超限以 `RESOURCE_IMPORT_LIMIT` 拒绝，不截断导入。

1. 以 source／commit 和命令 hash 建立导入幂等记录；重复同 key 读原报告，异内容冲突。
2. upload 只展开常规文件／目录，拒绝绝对路径、`..`、NUL、设备、FIFO、硬链接和符号链接；不能让解压器先写越界再清理。
3. git 使用专用 git driver：`git init <staging>`，随后固定 argv 执行 `git -C <staging> -c core.hooksPath=/dev/null -c submodule.recurse=false -c protocol.allow=never -c protocol.https.allow=always -c protocol.ssh.allow=always fetch --no-tags --depth=1 <approved_url> <full_commit>`。
4. driver 设置 `GIT_CONFIG_SYSTEM=/dev/null,GIT_CONFIG_GLOBAL=/dev/null,GIT_TERMINAL_PROMPT=0`，SSH 仅用绑定凭据及管理员 host-key 校验；不加载用户可写 git 配置、不调用任意 protocol helper。
5. 验证 FETCH_HEAD 完整 commit 与输入一致，再用 `git -C <staging> archive --format=tar <commit>` 输出并按同一安全展开器导入；不 checkout、不运行 smudge／安装脚本、不拉 submodule，gitlink 条目报告需独立导入。
6. 搜索各目录 SKILL.md 并逐项解析；同名入口冲突拒绝发布，单仓多 Skill 各自给有效／失败报告，不把非入口目录猜为 Skill。
7. 支持表中元数据先校验，未知行为扩展显式报 `SKILL_UNSUPPORTED_FIELD`；额外普通资源文件保留，不执行。
8. 逐文件排序生成 manifest/hash，写不可变资源包，再以事务发布 ResourceVersion 引用和来源；暂存失败可回收，不暴露半包。

### 8.2 支持与读取

标准 name／description／license／compatibility／metadata 保存。`arguments,argument-hint,$ARGUMENTS,$N` 仅做文本模板替换：`$ARGUMENTS` 是原参数串，`$1..$N` 来自显式 `arguments[]`；未提供的位置渲染为空，不执行 shell、不二次解释模板。

`user-invocable` 决定显式用户使用入口，`disable-model-invocation` 从模型发现清单移除并禁止模型自主激活；显式用户授权的使用记录仍校验角色权限。allowed-tools 与角色目录取交集，disallowed-tools 再排除；Skill 不授予新工具。

`model,effort,context:fork,agent` 拒绝发布；动态 shell 注入和未支持的专属变量／设置拒绝。hooks 只产出待显式绑定资源，不能因读 SKILL.md 就激活 Hook。

`SkillService.list_for_role(binding_ref)->SkillEntrySummary[]` 只返回名称、说明、固定入口和资源版本；`read(SkillRead{resource_version_id,entry_name,relative_path,arguments[]?},scope)->SkillReadResult` 返回 `text,result_ref,resource_version_id,path,rendered_arguments_hash?`。相对引用只能在该 Skill 根内解析。

资源挂载为 `/skills/<name>` 只读，references／assets 按需读取。执行脚本必须单独调用 exec，例如 `argv=["python3","/skills/report/scripts/build.py","--output","/work/report.md"]`；skill.read 不执行脚本、不建业务 NodeTask。

解释器和依赖来自冻结 image_digest；不存在时返回 `EXEC_DEPENDENCY_MISSING`，不得在平台 Runtime 进程 pip install。普通脚本文件访问不逐次发 Hook，外层 exec 负责唯一 Operation 和输出审计。

## 9. MCP 连接、调用与错误归一

### 9.1 连接与方法

固定 MCP profile 为 `2025-11-25`；只支持 stdio 与 Streamable HTTP 的 JSON／SSE，不自动降级独立旧 HTTP+SSE。本文使用现有设计指定版本，不补充未经核验的供应商扩展。

| DTO | 字段 |
| --- | --- |
| `McpConnectionBinding` | `connection_version,transport:stdio|streamable_http,endpoint_ref?|command_ref?,credential_binding_ref,protocol_profile,tool_catalog_ref,authorization_policy_ref` |
| `McpConnectionKey` | `space_id,connection_version,credential_binding_ref,workspace_scope_id,execution_view_key` |
| `McpSessionHandle` | `handle_id,key,run_id,lease_epoch,remote_session_id?,negotiated_version,capabilities_ref,expires_at,state:connecting|ready|closing|closed|failed` |
| `McpCall` | `operation_id,attempt_id,request_id,capability_key,remote_tool_name,arguments,deadline_at` |
| `McpRawResult` | `rpc_result_ref?,protocol_error?,is_error:bool,content_ref?,structured_content_ref?,progress_refs[],remote_request_id` |

`McpClientPool.connect(binding,scope,execution_view,lease)->McpSessionHandle` 完成 initialize／版本校验／initialized 后才 ready；`ping(handle)` 只检查该连接；`call_once(handle,McpCall)->McpRawResult` 是一次实际发送，重连／重试不隐藏于此接口；`close(handle,reason)->CloseReceipt` 幂等关闭。

`list_tools(handle,cursor?)` 分页到 cursor=null，稳定规范化目录并创建新的资源版本；新 list_changed 只产生目录更新通知，不改当前 Release。tools/call 前验证实际服务工具名/schema fingerprint 与冻结目录匹配，不匹配返回 `MCP_CATALOG_MISMATCH`，不临时选择同名新工具。

resources/list、templates/list、resources/read 仅在授权后包装普通读取能力，不自动读全部资源。prompts/list/get 导入可选模板，不能覆盖已有 system／user Prompt。logging／progress 只做脱敏观察；cancelled 不证明外部副作用撤销。

客户端不宣告 sampling、elicitation、tasks；服务端请求这些方法返回 JSON-RPC method-not-found（-32601），不启动隐藏模型或用户暂停。roots/list 只给专用 stdio 受限可见根，真实隔离仍靠挂载。

### 9.2 凭据、分区与失败

stdio 进程由 SandboxGateway 在该(scope,execution_view_key,epoch)专用执行域启动，与普通 exec 进程分离；凭据仅注入该 MCP 服务声明变量。HTTP pool 按 McpConnectionKey 分区，活动句柄再绑定 run_id／epoch；远端 MCP-Session-Id 不用于平台授权。connect与每次call都核验当前角色的execution_view_key/资源挂载digest，不能让同/work的另一角色复用该stdio进程；HTTP会话也按此键隔离roots/授权视图。恢复重建句柄，不能把旧 epoch 连接移交新 Runtime。

OAuth 在配置阶段完成授权码＋PKCE、metadata、audience 和刷新策略；执行期不能交互补权。API Key／Header 使用独立凭据绑定，不传平台登录 token。过期／撤销无法刷新返回 `MCP_AUTH_REQUIRED`，不换账号。

访问平台文件的 MCP 必须使用同 scope 的受限挂载，或短期 `WorkspaceAccessToken{owner_ref,workspace_scope_id,execution_view_key,run_id,lease_epoch,path_prefixes[],operations[],expires_at,audience}`。每次网关请求验证签名和数据库有效执行域；不是只信 token 未过期。第三方不能获得全 owner NFS 根或通用脚本密钥。

| 远端事实 | 归一结果 |
| --- | --- |
| initialize 版本不支持／不合法帧 | protocol / `MCP_PROTOCOL_UNSUPPORTED` 或 `MCP_PROTOCOL_ERROR`；不自动降级 |
| JSON-RPC 参数、方法错误 | validation 或 protocol，保存原 numeric code；不按 transport 重试 |
| `isError=true` 工具结果 | business / `MCP_TOOL_ERROR`，保留内容和已知执行事实 |
| output_schema 对 structuredContent 校验失败 | protocol / `MCP_OUTPUT_SCHEMA_INVALID`，外部执行可能已发生，保留 raw |
| 明确连接 reset、临时 connect timeout、502/503/504 | transport；发送后无法确定则 external_outcome=unknown，由统一 RetryPolicy 决定 |
| HTTP 429 | rate_limit；解析合法 Retry-After，等待规则沿用调用管道 |
| 不可识别远端错误文字 | unknown；不凭“网络”字样转换为可重试异常 |
| 停止／deadline／lease过期 | cancelled／控制错误，禁止再次自动发送 |

annotations 仅展示；只读／幂等声明不参与平台重试准入。远端支持稳定业务幂等键时传 operation_id，但不宣称外部副作用恰好一次。所有 RPC/stdio stderr 日志脱敏并保存到对应 owner。

## 10. WorkspaceScope、挂载与文件生命周期

### 10.1 持久身份和派生路径

`WorkspaceScope` 的完整字段为 `workspace_scope_id,owner_ref,file_root_id,partition_kind:agent_session|workflow_session|invocation,partition_id,run_id,invocation_id?,persistent`。当前 lease_epoch 不存入此 DTO；旧 scope 的存在不代表旧执行权仍有效。

`WorkspaceRegistration` 为 `scope_ref,creation_key,release_id,environment_policy_ref,mount_intent_id,state:registered|ready|cleanup_pending|cleaned,revision`。creation_key=`(owner_ref,partition_kind,partition_id,run_id)`，同键参数不一致返回 `WORKSPACE_BINDING_CONFLICT`。

| 调用位置 | 根与分区 | persistent=true | persistent=false |
| --- | --- | --- | --- |
| AgentSession 直接调用 | `agents/<agent>/sessions/<session>` | `work/` | `runs/<run>/work/` |
| Workflow 外层能力 | `workflows/<workflow>/sessions/<session>` | `work/` | `runs/<run>/work/` |
| input_only／显式历史授权的子 Agent | `agents/<agent>/invocations/<invocation>` | `work/` | `runs/<run>/work/` |

所有路径位于管理员固定的 `/shared/agentloom` 下，由 WorkspaceStore 从数据库可信身份解析；file_root_id 绑定创建时的 owner 根。模型／Hook 不提交宿主路径或 partition_id。父 Workflow 配置不覆盖子 Agent 的 persistent 策略；显式历史授权不改变 invocation 文件分区。

`EnvironmentManager.prepare(EnvironmentRequest,ExecutionScope)` 的 request 字段为 `workspace_scope_ref,environment_policy_ref,image_digest,resource_manifest_ref,expected_registration_revision`，返回 `EnvironmentHandle{workspace_handle,sandbox_handle,resource_mounts[],expires_at}`。先事务登记 scope 和 mount_intent，事务后幂等创建目录并验证根 manifest，再提交 ready；同 ID 重入不生成新目录。

`EnvironmentPolicy` 固定 `persistent,image_digest,exec_timeout_ms,egress_allowlist[],resource_mount_rules[]`；`EgressRule` 为 `kind:domain|cidr,value,ports[]`，空列表表示禁外网。`ResourceMountRule` 为 `resource_version_id,logical_path,read_only=true`，资源写权限不能由调用参数覆盖。执行服务部署配置额外固定 runner_limits_version、tmp_size_bytes 和 max_processes，写入policy_digest；它们是已验收的基础设施参数，不增加Agent用户配置。

`WorkspaceHandle` 为 `handle_id,workspace_scope_id,run_id,lease_epoch,execution_domain_id,authorized_mounts[],issued_at,expires_at`；只存在服务端／受限 SDK 中，不入 GraphState。`authorized_mounts` 每项含 `logical_root:/work|/skills/<name>|/wiki|/resources/<key>,resource_version_ref?,read_only`。

Planner／Executor 使用同 Invocation `/work`，各自只读资源目录按角色授权装配。ExecutionDomain以(scope,execution_view_key,epoch)区分受限执行视图；不同角色的只读挂载不同，禁止用资源并集扩大授权。多个视图可以位于同一Agent沙箱设施内，共用同一/work路径，不为每个Executor复制文件或另造应用沙箱身份；不支持隔离挂载视图的driver必须报ENVIRONMENT_VIEW_UNSUPPORTED。外层节点无 invocation_id 合法，使用 Workflow scope；不同 invocation 即使同一个 Agent 也不能共享可写根。恢复沿用 registration／路径及各execution_view_key，撤权枚举scope下全部角色视图，再重建当前 epoch 的句柄。

同一持久会话目录在新 Run 重新登记 scope、重新授权句柄；上一 Run 的句柄一律失效。单活动 Run 的会话占用协议继续适用。持久保存的是文件，不是容器进程、内存或后来安装的软件。

### 10.2 Artifact 授权和来源

`ArtifactRef` 为 `artifact_id,owner_ref,workspace_scope_id,relative_path,etag,media_type,size,source_operation_id,grant_id?,source_dependencies[],availability:available|expired|source_deleted`。

`grant_artifact(ArtifactGrant{artifact_ref,recipient_scope_ref,operations:[read],expected_etag},scope)` 先验证源仍可读及接收方授权，再登记 grant 和来源依赖；返回接收方只读引用。不同 owner／partition 的完整路径不是授权凭证。

需要物化到接收工作区时，先登记该工作区对源的依赖，再复制到暂存文件、校验 hash、原子发布授权导入文件；文件写在事务外，可读引用发布在事务内。副本继续携带传递 source_owner；脚本产物无法安全拆分时按整个受影响工作区撤销，不能通过复制清除来源。

源是临时工作文件时，ArtifactRef 不延长生命周期，不自动转存最终产物。源清理后访问返回 `ARTIFACT_EXPIRED`；owner 删除后返回 `SOURCE_OWNER_DELETED`，不能从系统 blob 缓存恢复已撤销正文。

### 10.3 清理协议

`CleanupRequest` 为 `run_id,workspace_scope_ids[],reason:run_terminal|owner_deleted,expected_cleanup_revision,idempotency_key`，由 SystemCleanupScope 调用。`CleanupReceipt` 为 `run_id,scope_results[{workspace_scope_id,status:retained|removed|retry_pending,error?}],revocation_receipt_refs[],completed_at?`。

顺序固定：关闭新业务准入 → 收束全部写任务 → 取得每个 scope 的撤销证据 → 运行有界 run.ending 扩展 → 删除登记的全部非持久目录 → 提交 cleanup receipt。某个子 Invocation 完成不得提前清理；根超时不等待扩展 Hook 延长期限。

物理删除仅操作登记的精确 work 目录，拒绝 owner 根、系统 blob、共享资源；重复删除不存在目录视为已完成。中途失败登记 cleanup_pending 继续重试，Run 的终态与 cleanup_state 分开。持久 scope 标 retained，无 TTL。页面关闭、SSE 断开、机器故障不触发临时目录清理。

Agent／Workflow 删除走 catalog-api.md／persistence.md 的 owner 删除作业，不向模型暴露同等权限的 fs.delete。先撤销源和派生可读投影；系统原始记录、上下文与检查点只有 owner 删除才进入对应清理范围。

## 11. 基础文件工具的完整行为

### 11.1 路径、编码与版本

平台文件服务支持 Linux/NFSv4 POSIX，开发可用同契约的本地 POSIX。路径输入仅允许 UTF-8 `/work/...` 或相对 `/work` 的路径；读取另可接受明确获准只读挂载。拒绝 NUL、越根 `..`、设备节点、魔术链接和未授权 mount；规范化不是授权判断。

Linux 文件 driver 以授权根 dirfd 使用 openat2 的 beneath/no-magic-links 约束，禁止越界符号链接；递归 list/glob/grep 不跟随目录符号链接。写操作拒绝符号链接目标及父路径中的符号链接，防止校验后换链。开发 driver 用逐段 dirfd+O_NOFOLLOW 保持同规则；不能只检查字符串前缀。

`FileVersion` 为 `path,kind:file|directory,etag,size,sha256?,modified_at`。普通文件 etag=`sha256(canonical(kind,size,content_sha256))`；目录 etag 为排序直属条目的 `(name,kind,etag)` 的 digest；递归删除检查事务前取得的整棵树 manifest digest。

`FileSlice` 为 `path,etag,offset,end_offset,next_offset?,total_bytes,text`，offset／limit 是 UTF-8 字节；offset 非边界报错，limit 向前收敛到边界。默认 limit=16384，最大65536；检测二进制或无效 UTF-8 返回 `BINARY_FILE`，以 artifact 方式读，不能忽略解码错误。

### 11.2 方法契约

| 方法与输入 | 返回 | 拒绝和确定行为 |
| --- | --- | --- |
| `fs.list{path=".",cursor?,limit=100}` | `entries[{name,path,kind,size,etag}],next_cursor?` | limit 1..1000；按 UTF-8 名字节排序；cursor 固定目录 etag，变化返回 `DIRECTORY_CHANGED` |
| `fs.glob{pattern,root=".",limit=1000}` | `paths[],truncated` | 仅 glob 语法，不执行 shell；排序后取前limit，最大10000；不越授权根 |
| `fs.grep{pattern,paths[],literal=false,limit=1000}` | `matches[{path,line,column,text}],truncated` | 路径、行、列排序；UTF-8文本；正则固定使用无回溯 RE2 语义，超1秒返回 `SEARCH_TIMEOUT` |
| `fs.read{path,offset=0,limit=16384}` | `FileSlice` | 本次先开文件、核对版本再读；读期间版本变化返回 `FILE_CHANGED`，不拼接两版 |
| `fs.write{path,content,mode=create_only,if_match?}` | `FileVersion` | create_only 要不存在且不得有if_match；replace 必须if_match；任何其他组合 `INVALID_WRITE_MODE` |
| `fs.edit{path,old_text,new_text,expected_matches=1,if_match}` | `FileVersion,matched_count` | old_text非空；按非重叠字面匹配计数，必须等于expected_matches≥1；全体替换一次原子发布 |
| `fs.patch{path,unified_diff,if_match}` | `FileVersion,hunks_applied` | 只允许单文件精确hunk，头路径必须等于path；禁止fuzz、偏移猜测、二进制patch、多文件和隐式删除 |
| `fs.mkdir{path,parents=false}` | `FileVersion` | 已有目录幂等返回；同名文件冲突；每段保持授权，不能创建只读根内目录 |
| `fs.move{source,destination,if_match}` | `FileVersion` | 只限当前/work分区，目标须不存在；源版本匹配；不覆盖目标、不跨挂载、不移动分区根 |
| `fs.delete{path,if_match,recursive=false}` | `deleted_path,previous_etag` | 文件须匹配；非空目录recursive必须true并校验树manifest；根与只读资源禁止删除 |

`WorkspaceService.read/write/apply_patch` 实现上述相同语义；ToolManager 只转译 schema，不另写一套文件行为。fs.write/edit/patch 的 content 限制为20MiB；更大材料通过脚本生成文件并返回 artifact，不把无限正文塞入工具 JSON。

每个调用输入先经 schema／scope 校验，再进入文件服务；路径不存在为 not_found，版本或匹配数量冲突为 conflict，越界／只读根为 permission，NFS错误保留发生阶段与外部结果。错误反馈含逻辑相对路径，不包含宿主路径。

### 11.3 并发、原子替换与幂等

WorkspaceService 在 scope 的服务私有 `.locks` 目录使用稳定路径键锁；锁文件不在模型挂载视图内。单文件读写均取得该规范路径的 advisory lock；多路径操作先按规范 UTF-8 路径排序取锁，避免 move 对向死锁。

每个操作先取得工作区树锁：普通文件读写取共享，目录创建／删除／移动取排他；随后取得相关文件锁。文件锁只约束经服务的调用，脚本普通 POSIX 写不会自动遵守；脚本与编辑并行时仍可能产生冲突或未知结果，不能宣称全局可串行化。

原子写步骤固定：取得句柄执行域授权 → 锁内重新读现有 etag → 校验 if_match → 同目录创建 O_EXCL 临时文件 → 写完整正文 → fsync文件 → 再核对执行域许可 → rename发布 → fsync父目录 → 保存服务端 mutation receipt → 返回 FileVersion。

ETag 检查和 rename 位于同一个受撤销控制的执行域中。不能以“写前查过数据库 lease”代替 fencing；调用期间 lease 失效由 SandboxGateway 撤销域、禁止旧写入者继续访问。NFS advisory lock 在共享后端必须经验收，锁服务失效时报错，不能降为无锁覆盖。

`FileMutationCommand` 为 `mutation_id=operation_id,request_hash,workspace_scope_id,run_id,lease_epoch,kind,paths[],expected_etags[],new_content_hash?,staging_ref?`。服务内部持久 journal 含 `state:prepared|published|confirmed|unknown,receipt_ref?,observed_etags[],error?`，不是新的平台 Operation 权威。

重复同 mutation_id／hash 有 confirmed receipt 时直接返回原结果；不同 hash 返回 `IDEMPOTENCY_CONFLICT`。prepared／published 状态恢复先锁原路径、检查暂存及目标证据；目标已是预期内容可补交原 receipt，不覆盖它。证据不能确定，保留 unknown 并返回 `FILE_MUTATION_OUTCOME_UNKNOWN`，不得凭无 receipt 宣称未写。

未决 journal 阻止同服务后续相同路径变更，直到恢复检查结束；脚本不受此 journal 自动防重。fs.move/delete 的恢复只有已持久 receipt 或能证明源/目标身份与预先记录一致时确认，不能把“路径现在不存在”单独当作本操作成功。

文件写和 journal fsync 全在数据库事务外。平台提交的 RuntimeCommit 仅引用原结果／文件 receipt；Operation raw_saved 后不重复写文件。文件已改而平台结果未提交仍属于可能已发生副作用，不承诺数据库与 POSIX 跨系统原子事务。

## 12. SandboxGateway 的运行与撤销驱动

### 12.1 方法与 DTO

```python
class SandboxGateway:
    async def ensure(self, scope, execution_view: ExecutionViewSpec, lease_epoch) -> SandboxHandle: ...
    async def execute(self, handle, command, owner: ExecutionOwnerRef, deadline) -> ExecutionHandle: ...
    async def inspect(self, execution_id) -> ExecutionStatus: ...
    async def terminate(self, execution_id) -> TerminationReceipt: ...
    async def revoke(self, run_id, lease_epoch) -> RevocationReceipt: ...
```

| DTO | 字段 |
| --- | --- |
| `SandboxHandle` | `handle_id,execution_domain_id,workspace_scope_id,run_id,lease_epoch,execution_view_key,image_digest,policy_digest,expires_at` |
| `ExecutionViewSpec` | `execution_view_key,role_binding_ref?,capability_binding_ref?,resource_mount_manifest_ref,resource_mount_digest,image_digest,policy_digest`；受信任装配，key为固定绑定与挂载digest的hash |
| `ExecutionOwnerRef` | `kind:business,operation_id,attempt_no` 或 `kind:hook,hook_invocation_id`；二者严格二选一 |
| `ExecCommand` | `argv?:str[],shell_script?:str,cwd="/work",env_patch:dict[str,str],timeout_ms=10000,stdin_ref?`；argv／shell_script恰选一 |
| `ExecutionHandle` | `execution_id,execution_domain_id,owner_ref,command_hash,state,started_at,deadline_at` |
| `ExecutionStatus` | `execution_id,state:accepted|running|exited|terminating|terminated|unknown,exit_code?,signal?,stdout_ref?,stderr_ref?,started_at?,ended_at?,output_truncated:bool,error?` |
| `TerminationReceipt` | `execution_id,state:terminated|already_exited|unconfirmed,process_group_empty:bool,checked_at,evidence_ref` |
| `RevocationReceipt` | `receipt_id,run_id,lease_epoch,domain_results[],state:revoked|unconfirmed,checked_at,evidence_ref` |
| `DomainRevocationResult` | `execution_domain_id,workspace_scope_id,all_processes_exited,write_handles_closed,mount_access_revoked,token_epoch_revoked,error?` |

Exec 的请求级 deadline=`min(开始时间+timeout_ms,根截止,Invocation截止)`；默认10秒可由冻结用户配置调整。没有返回码但因超时被杀不是 exit_code=0；返回 `EXEC_TIMEOUT`。stdout/stderr全量存blob，模型用各自预览和截断标记。空 env_patch 默认不继承 Runtime 环境，仅服务固定 PATH/LANG；受保护变量和凭据名不能被覆盖。

### 12.2 可实现的驱动命令

生产执行服务实现独立 `agentloom-execd`，通过 mTLS 的控制 API 接收结构化命令；Runtime 不在自己的进程执行用户脚本，也不给容器数据库连接。

| 驱动命令 | 输入 | 输出和失效规则 |
| --- | --- | --- |
| `PUT /v1/domains/{scope_id}/{execution_view_key}/{epoch}` | scope_ref、ExecutionViewSpec、Run授权证明、mount_manifest、expiry | 同(scope,execution_view_key,epoch)幂等返回domain；不同配置hash拒绝；旧epoch不能ensure复活 |
| `POST /v1/domains/{domain_id}/executions` | owner、command、deadline、command_hash | 唯一键`(domain_id,owner)`；相同命令返回原execution；必须先持久accepted意图再启动 |
| `GET /v1/executions/{id}` | 授权的run／scope查询 | 返回持久ExecutionStatus；不重启进程、不把未知当退出 |
| `POST /v1/executions/{id}/terminate` | reason、当前控制授权 | 先阻止后续输入，SIGTERM进程组，最长2秒后SIGKILL，确认整个cgroup空；不能只杀父PID |
| `POST /v1/runs/{run}/epochs/{epoch}/revoke` | 接管或停止授权、期望domain清单hash | 固定撤销所有登记域／文件网关token，核对无漏项后返回RevocationReceipt；不依赖旧Worker配合 |

driver 将 ExecCommand 编译为固定 OCI process spec：argv 直接数组传入；shell_script 作为受控临时文件并执行 `[/bin/sh,<script_path>]`，不把模型参数拼进服务自身 shell 命令。执行镜像必须包含所选解释器；OCI spec 用固定非root UID、独立 mount/PID/network namespace、只读根文件系统、只读资源、唯一/work可写挂载及有界/tmp，不挂宿主socket、服务密钥或整个NFS根。

`cwd` 必须解析为授权挂载内目录；exec 授权只允许此受限域。默认 network namespace 无外网，冻结 egress allowlist 由执行服务防火墙／出口代理强制；域名规则通过代理在请求和重定向时重新校验解析目标，不只在启动时解析一次。

服务持久保存 domain／execution 映射和租约到期时间，心跳续期由可信 Runtime 提交；本地到期自动停止接受调用并撤销域。数据库 lease 失效后的旧网关token、文件句柄和MCP连接全部失效，不能仅停止调度线程而保留可写子进程。

生产写进程必须处于可由执行服务独立控制的 cgroup／挂载域；Runtime Worker 失联时执行服务仍能终止该域。若执行宿主也失联、无法证明NFS挂载已失去写能力，返回 unconfirmed，阻止同可写卷新epoch的ensure；不能用网络超时或epoch数字宣称撤销成功。

新执行权领取与旧域撤销按 persistence.md 接管事务执行；只有 `state=revoked` 且每个domain四项撤销事实为true，才允许新epoch获得写句柄。revoke完成后旧请求即使网络恢复也返回 `ENVIRONMENT_REVOKED`，不会恢复被撤销mount。

### 12.3 环境失败与收束

- accepted 意图已保存但进程未启动：inspect返回accepted；原执行服务按同 execution_id 启动一次，不能由Runtime生成新ID重开。
- 进程已结束但结果未交付：inspect取回原输出；平台保存 raw 后继续后置链，不再次exec。
- 进程存在但不可查询：记unknown，先收束原域；调用管道根据已识别错误和重试策略处理，不能把unknown当作已停止。
- revoke unconfirmed：Run接管保留阻塞诊断，不发新写句柄；停止／清理可继续重试撤销，但不能先删除仍可被写入的目录。
- 只读查看也需要授权；同路径不意味着不同epoch可以复用旧handle。成功 revoke 不承诺撤销已经完成的远端业务副作用。

## 13. 跨模块提交目录

所有 blob／文件／网络操作在事务外；以下“写”均指在 RuntimeCommit 内发布已验证引用和状态。

| 命令 | 事务读取与版本条件 | 一致写集与幂等键 |
| --- | --- | --- |
| `prepare_accept_model` 生成写集 | completed effective响应、原context revision、未接受operation | assistant有效消息、实际调用来源；`model:<operation>:accept`；与ticket.consume、batch或候选阶段同提交 |
| `prepare_batch_delivery` 生成写集 | batch全部结算、ordinal不变、父context／task revision | 原反馈槽、完整tool组、batch consumed、phase；`batch:<batch>:deliver` |
| 输出修正 | 候选关联原model operation、validation hash | 格式反馈和下一阶段；`format:<operation>:feedback`；不发新的隐式Attempt |
| 模型raw/effective | Attempt／Operation revision、原request hash、当前执行许可 | raw／effective引用、Hook状态、最终响应；模型assistant追加留给接受决策提交 |
| workspace登记 | 可信owner固定根、partition／Run关系、release环境配置 | registration、mount_intent；`workspace:<owner>:<partition>:<run>` |
| 文件结果 | 原Operation／Attempt及gateway receipt关联、版本 | raw FileVersion／错误引用；`raw:<attempt>`；不在事务里做rename |
| artifact发布 | 源etag、grant及source_owner仍有效、目标scope归属 | ArtifactRef、grant、来源依赖、可读引用；`artifact:<operation>:<ordinal>` |
| 环境撤销完成 | 根控制／接管身份、预期domain清单 | revocation receipt引用、接管屏障或清理许可；`revoke:<run>:<epoch>` |
| scope清理 | 已有撤销证据、准确登记路径、cleanup revision | scope cleanup_state、重试任务或receipt；`cleanup:<run>:<scope>` |

事务提交回执前不能唤醒成功等待者、执行工具或发下一个模型请求。必要写失败返回 `PERSISTENCE_UNAVAILABLE`；lease失效返回 `LEASE_LOST`，旧worker只能停止，不能自行把本地成功结果推成新head。

## 14. 贯穿样例与可执行验收断言

### 14.1 历史、补充和格式修正

样例 Session 顺序为 R1=succeeded（问题Q1＋补充A1/A2）、R2=failed、R3=cancelled、R4=当前，N=2。投影必须是 R1 的Q1/A1/A2/最终结果/终态，R2和R3的完整根过程/真实终态，再接R4；不加入R1工具细节，不改选R1替代失败轮。

R3 的A3在模型首输出前停止：旧delivery=carried，新pending保留input_id=A3。R4冻结时领取A3和Q4，按接收顺序保护并去重；R3历史位置只留转交标记。R4的空完整帧提交kind=complete_frame，A3=delivered；若输出schema要求`{"answer":string}`，空候选形成格式反馈，下一业务Loop修正，不能把A3再次转交R5。

### 14.2 一次压缩与并发修改

B=100000、O=8192、F=65000、材料20000：触发85000≥80000，H=35000，D=-5000，T=8192。计划三块，第二块提交后故障；恢复保持episode_id、前两块operation_id、cursor=2，只调用第三块。

最后快照发布前，Hook修改被覆盖record X：源hash不符，不设置active_snapshot；展开X等原材料的最新有效值，接回原已固定input_cutoff以内的保护输入再计量。如果I=100001，本次业务模型不出网，返回CONTEXT_WINDOW_EXCEEDED；不能再创建第二Episode。若仅新增未覆盖record Y，原snapshot可以发布，Y增量接回。

### 14.3 文件冲突和租约撤销

Planner／Executor E1、E2共享/work。E1和E2都读取report.md的etag=e0；E1 fs.edit提交e1，E2带e0写入得到FILE_VERSION_CONFLICT，文件保持e1，模型收到冲突后重新读取。两个调用各自有Operation，不按参数相同合并。

Worker A epoch7写进程失联，Worker B准备epoch8。执行服务若不能确认epoch7挂载撤销，revoke返回unconfirmed，B没有写句柄；确认全部进程空、挂载失效、token撤销后才准许epoch8。最后整个Run收束，非持久scope的全部产物删除，系统历史／blob仍保留。

### 14.4 必测断言

| 编号／注入点 | 必须断言 |
| --- | --- |
| C01 N=0、N=2、终态混合 | 每轮全部UserInput保留；最近N按问题顺序且包含全部终态种类；无成功补位 |
| C02 一轮五条补充 | 五个input_id、一个Run；历史裁剪不丢任一补充 |
| C03 Executor读根历史 | 未获授权的materialize返回permission；显式任务输入逐字保留 |
| C04 同一mutation重投 | revision、record_seq只推进一次，原receipt与hash一致 |
| C05 patch并发 | 不相交字段重定位；重叠字段返回CONTEXT_REVISION_CONFLICT；合法deny仍有效 |
| C06 request freeze后更新 | 原FrozenRequest hash不变；下一个Operation读取新revision |
| C07 首输出边界 | 心跳／空delta／usage不计；工具参数非空计；空完整终帧计送达但has_display_content=false |
| C08 首输出与终态竞态 | 两种提交顺序均无既delivered又pending的有效交付，旧epoch不能覆盖 |
| C09 streaming retry | Attempt1 partial与Attempt2正文不拼接；部分tool arguments从不执行 |
| C10 接受模型重复通知 | effective保存不提前append；accept+ticket.consume+batch只提交一次 |
| C11 批次两结果逆序完成 | 父消息按ordinal；未全部结算无下一请求；重复delivery不重复反馈 |
| C12 80%阈值 | threshold-1不压缩，threshold压缩；无材料且I<=B可以发送 |
| C13 固定输入超窗 | 当前原文完整保留；未产生供应商业务Attempt；错误不暴露system |
| C14 压缩第二块后故障 | cursor=2恢复第三块；已完成块无新Attempt、无业务Loop扣款 |
| C15 raw后故障 | 摘要／模型／工具均只做后置处理；不再次执行能力 |
| C16 覆盖材料变化 | 整份snapshot替代失效；不从自然语言摘要删句；旧记录可审计 |
| C17 压缩期间补充 | input_cutoff不刷新；新增input不进入当前请求，在下一Loop领取或终态转交；原已选保护输入不被摘要替代 |
| C18 最后一次格式修正 | 正常完成可成功；需要下一票据才耗尽；没有独立修正额度 |
| C19 大结果 | raw全量hash一致，预览范围准确；result.read新Operation；未读正文不自动注入 |
| C20 Skill恶意包 | ../、绝对路径、链接、git helper、submodule拒绝／显式报告；导入从未执行仓库代码 |
| C21 Skill权限 | allowed-tools不能扩大角色目录；model/context:fork拒绝；read不启动脚本 |
| C22 MCP差错 | isError不是transport；unknown文字不重试；sampling返回不支持；目录变更不热切换 |
| C23 外层文件工具 | 无AgentSession仍解析Workflow scope；输入伪造owner／宿主路径被拒绝 |
| C24 工作区隔离 | read/glob/grep、符号链接、脚本、文件MCP均不能跨Session／Invocation |
| C25 并发edit | 两请求同etag至多一个写成功，另一conflict；结果不由完成速度选择 |
| C26 文件崩溃窗口 | fsync后rename前／rename后receipt前均恢复原mutation；证据不足unknown，不假成功 |
| C27 NFS失联 | 不能降为无锁写；无撤销证据不发新epoch写句柄；旧句柄恢复联网仍被拒绝 |
| C28 子任务完成 | 临时产物仍可被授权父读取；根Run收束前不清理 |
| C29 临时卷清理 |全部登记非持久scope删除，持久scope保留，系统上下文不受影响 |
| C31 同scope不同角色Skill | /work读写共享；execution_view_key不同，各自只能读获授权资源；revoke枚举所有视图 |
| C30 source owner删除 | 父投影／摘要／副本不可继续读取；恢复不能从旧snapshot重新发布正文 |

这些测试使用假模型流、假MCP服务、可故障注入BlobStore及本地／NFS文件driver；验收必须检查持久身份、hash、事务回执和执行次数，不能只检查最终文本是否相似。
