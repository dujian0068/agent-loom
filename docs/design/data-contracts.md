# SD-3 数据结构与一致性契约

> **重建边界（2026-10-15）**：旧业务实现已移除。本页保留为待调整的设计草案；用户、应用、发布与配置版本属于平台，SDK 只接收已解析配置并负责执行与恢复。涉及平台身份、发布绑定及表结构的内容需按 [SDK / Platform 边界](sdk-platform-boundary.md)重新划分，不直接作为 SDK 的必填契约或已实现功能。

本页链接的 `contracts.schema.json`、样例与校验脚本保留为原 SD-3 平台和运行时的混合设计投影；通过它们不代表 SDK 公共契约已确定。

**实施细则：[persistence](implementation/persistence.md)**。本页保留架构解释；具体方法、字段状态、事务和恢复分支见实施规格。

日期：2026-10-15。**目标设计，供评审，尚未实施。** 本篇统一设计态、运行态和恢复态字段；取代旧 SD-2 的 AgentExecution 独立状态模型。完整的领域关系、节点接口、恢复提交分别见[领域 UML](domain-model.md)、[图与 Node](graph-node-engine.md)、[检查点协议](checkpoint-protocol.md)、[领域接口](domain-interfaces.md)。

**外层适用范围更新：** Workflow 的应用、发布、调用身份与隔离规则继续有效；外层 branch_scope、BranchCursor、loop_path、TaskWrite 和快照内部字段仍是自研候选，须按[LangGraph 源码适配](architecture-discussion.md#6-workflow-产品范围与-langgraph-源码适配)完成映射后再定稿。现有外层 Schema 样例不代表迁移格式已经确定；内部 Agent 契约不因此改变。

## 1. 通用值类型和身份

| 类型 | 结构／约束 |
| --- | --- |
| ID | 系统生成的不透明字符串，生产采用 UUID／ULID；示例 R1、G1 只用于阅读，模型不生成内部身份 |
| DefinitionRef | `{id, version, content_hash}`；引用不可变版本，Graph／Agent 类型由外层字段确定 |
| PayloadRef | `{kind:inline, value:JSON}` 或 `{kind:blob, blob_id, sha256, size_bytes}`；不把任意宿主路径作为可信 blob |
| SchemaRef | 内联 JSON Schema 或固定版本引用；不执行 schema 中的远程代码／任意网络引用 |
| ContextRevisionRef | `{context_id, revision}`；指向已提交有效版本，非“读取最新”的别名 |
| OwnerRef | `{kind:agent 或 workflow, id, space_id}`；由发布和执行归属解析，不接受调用参数指定 |
| WorkspaceScope | `{workspace_scope_id, owner_ref, file_root_id, partition_kind, partition_id, run_id, invocation_id?, persistent}`；partition_kind 为 agent_session／workflow_session／invocation；Runtime 另绑定当前有效 lease，不能从旧 scope 恢复授权 |
| RuntimeIdentity | `{space_id, run_id, graph_execution_id?, node_task_id?, invocation_id?, lease_epoch}`；Runtime 注入 |
| ExecutionLease | 根 Run 的拥有者、epoch、有效期；提交时重新验证，不由旧检查点恢复为有效 |
| Version / revision / seq | 分别用于不可变内容版本、CAS 修订和提交／消息顺序；不复用一个字段表达全部 |
| 时间 | UTC 时间戳用于截止和审计；进程内超时使用单调时钟；跨机以持久 deadline 为准 |

JSON 只保存数据及固定实现引用；不存 Python callable、协程、连接、明文凭据或模型可伪造的权限对象。结构校验之外，还需要跨记录外键、权限、版本和事务不变量。

## 2. 行为定义与角色

```text
Agent
  agent_id, space_id, name, description, mode(react|plan_executor)
  file_root_id, current_draft_revision, published_release_id?, lifecycle

AgentDefinition
  schema_version, definition_id, version, agent_id, content_hash
  mode, model:ModelBinding, roles:RoleDefinition[]
  execution:ExecutionPolicy, context:ContextPolicy
  environment:EnvironmentPolicy, hooks:HookBinding[]

RoleDefinition
  role_key, kind(react|planner|executor)
  prompt:PromptSpec, variables:VariableSpec[]
  input_schema?, output_schema?, capabilities:CapabilityBinding[]
```

RoleDefinition 无 agent_id／独立发布指针；它随 AgentDefinition 版本一起冻结。Executor 不含独立 model 字段，不继承主角色完整 Prompt 或私有历史；能力选择各自明确保存。

| 值对象 | 必需语义 |
| --- | --- |
| ModelBinding | connection_ref、credential_slot_ref、model_id、provider_profile_version、Think 配置、模型窗口及来源；Key 由凭据服务解析 |
| PromptSpec | 默认／自定义 system 内容或固定 PromptVersion 引用、变量绑定、可选输入输出约束 |
| CapabilityBinding | 资源版本、能力键、模型可见名称、参数 schema、授权边界、运行配置；Tool／MCP 不因绑定成为图节点 |
| HookBinding | hook_version、trigger、matcher、order、timeout、config；可改字段来自受信任挂点 schema |
| ExecutionPolicy | loop_max=100、run_timeout_seconds=3600、parallel_enabled=false、executor_max_parallel=2、model_first_output_timeout_seconds=60、tool_timeout_seconds=10、retry |
| RetryPolicy | enabled=false、max_attempts=2（包含首次）、delay_seconds=2；只限制 Runtime 自动尝试 |
| ContextPolicy | recent_runs=2、trigger_ratio=0.8、output_reserve_tokens?、safety_margin_tokens?、summary_target_tokens?、compaction_prompt_addendum |
| EnvironmentPolicy | persistent_sandbox、file_root_id、execution_profile_ref；授权路径由服务端解析 |

既有预算公式、输入保护和压缩流程保留在[上下文设计](context-lifecycle.md)。模型元信息未知时要求用户填写，不用默认猜测冒充供应商规格。

## 3. 图定义、节点与绑定

```text
GraphDefinition
  schema_version, graph_id, version, content_hash, origin, engine_kind(agent|workflow)
  input_schema, output_schema, state_fields:StateFieldDefinition[]
  nodes:NodeDefinition[], routes:RouteDefinition[], joins:JoinDefinition[], loops:LoopDefinition[]
  entry_nodes[], finish_rule, compiler_requirements

NodeDefinition
  node_key, node_type, implementation_version
  binding_slot, input_schema, output_schema, input_mapping, output_mapping
  read_set[], write_set[], continuation_schema_ref
  allowed_dispatch_targets[]

NodeBinding
  node_key, target_kind(role|capability|agent_release), target_ref
  authorized_input_mapping, authorized_output_mapping

StateFieldDefinition
  key, value_schema, initial_value?, merge(single_writer|reducer)
  reducer_ref?, visibility, allowed_writer_nodes[]

RouteDefinition
  route_key, source_node_key, kind(direct|conditional|dispatch|fork|branch_exit)
  targets[], predicate_ref?, input_mapping, completion_policy

JoinDefinition
  join_key, member_scope(dispatch_batch|branch_scope), settle_policy(all_settled), output_mapping
  resume_owner, success_target_node_key?, error_target_node_key?
  success_input_mapping?, error_input_mapping?
  fork_route_key?, region_node_keys[], branch_exit_routes[]

LoopDefinition
  loop_key, header_node_key, body_node_keys[], back_route_keys[], exit_route_keys[]

LoopFrame
  loop_key, loop_instance_id, iteration
```

不同节点需要的绑定类型由 NodeTypeRegistry 声明：React 类绑定 role，CapabilityNode 绑定 capability，AgentNode 绑定 agent_release。引用内部节点必须带所属图版本，不能凭相同 node_key 跨图解析。

`direct/conditional` 完成边触发新的下游任务；`dispatch` 将专属输入发送给允许的节点位置。Planner 的委派结果由 `resume_owner=true` 的 WaitGroup 回交原 NodeTask，不通过普通边创建一个新 Planner。

旧自研外层候选采用[结构化流程范围](graph-node-engine.md)：fork 成员覆盖整条分支，LoopFrame 记录循环实例和迭代；交叉 Join、跨区域回边及分支内再次 fork 在该候选中被限制。这些结构约束和字段不是 Workflow 产品要求，外层迁移映射完成后需更新；稳定运行身份和恢复不重复执行的要求保留。

图共享状态只存业务值和授权结果引用。分支读取冻结基线与自身已提交 TaskWrite 的有序视图，不看兄弟尚未汇合的写入。分支关闭后 Join 才按成员序、任务序、写序归并；写入引用须有相同 branch_ref 且不超过冻结读取水位。字段合并为纯函数 reducer，固定版本、固定任务顺序，不调用 LLM／网络。无明确 Join 的并行同字段写拒绝或报告版本冲突；不按最快完成的任务决定结果。

### 发布包

```text
AgentRelease
  schema_version, release_id, agent_id, release_no
  agent_definition_ref, graph_definition_ref, node_bindings[]
  resource_manifest[], compiled_descriptor_ref, compiled_graph_hash, compiler_version, node_abi_manifest
  content_hash, published_at

WorkflowDefinition
  definition_id, version, workflow_id, file_root_id
  input_schema, output_schema, root_execution_policy, environment_policy

WorkflowRelease
  release_id, workflow_id, workflow_definition_ref, graph_definition_ref
  node_bindings[], dependency_manifest[], compiled_descriptor_ref, compiled_graph_hash
```

WorkflowRelease 引用具体 AgentRelease，而非 agent_id 的“最新版本”。运行解析成 `ResolvedRelease`，这是只读 DTO，不另存一份可独立变更的 Role／Model 配置。CompiledGraph 是编译产物，CompiledGraphDescriptor 是它的可序列化描述，不是第三种业务定义。

双引擎下，编译描述固定 `engine_kind` 与 `engine_protocol_version`，并保存对应引擎的专属描述；内层模板使用 agent，外层工作流使用 workflow。本文旧外层 routes／loops／branch 字段不强制作为迁移内核格式。编译与恢复须按类型分派，不得更换引擎解释既有现场；详见[类型及映射](dual-engine-integration.md#4-定义与运行身份)。

### 生成 Plan 图的简例

```json
{
  "graph_id": "agent-plan-template",
  "version": 1,
  "engine_kind": "agent",
  "nodes": [
    {"node_key": "planner", "node_type": "planner", "binding_slot": "main"},
    {"node_key": "research", "node_type": "executor", "binding_slot": "researcher"}
  ],
  "entry_nodes": ["planner"],
  "routes": [
    {"route_key": "delegate_research", "source_node_key": "planner",
     "kind": "dispatch", "targets": ["research"], "completion_policy": "return_to_owner"}
  ],
  "joins": [
    {"join_key": "action_batch", "member_scope": "dispatch_batch",
     "settle_policy": "all_settled", "resume_owner": true}
  ],
  "finish_rule": "entry_completed_and_no_live_descendants"
}
```

这是省略 schema 和映射的展示片段，非完整提交 DTO。同一 research 位置可以接受两份任务输入；不会动态新增两个 NodeDefinition。Planner 没有 Executor 时仍可调用自身工具、返回最终结果，图不会产生永久等待空集合的 Join。

## 4. Run、完整 Agent 调用和图运行

| 记录 | 核心字段 | 不变量 |
| --- | --- | --- |
| Session | session_id、agent_id、active_run_id?、input_seq | 一会话归属一个 Agent，最多一活跃 Run |
| WorkflowSession | workflow_session_id、workflow_id、active_run_id? | 与 Agent Session 分开；外层设计对象 |
| Run | run_id、conversation_ref、release_ref、root_graph_execution_id?、status、deadline_at、loop_limit、loops_consumed、loops_reserved、lease_epoch、revision、cleanup_state | 所有内层共用根控制；queued 可未装配根图 |
| AgentInvocation | invocation_id、run_id、agent_id、release_id、graph_execution_id、parent_node_task_id?、conversation_scope、workspace_scope_ref、budget_scope、local_loop_limit?、started_at、local_deadline_at?、control_state、terminal_reason?、result_ref? | 一次完整 Agent 调用对应一个内层图；Executor 不是新 Invocation；局部额度只收紧根控制 |
| GraphExecution | graph_execution_id、run_id、release_ref、engine_kind、engine_protocol_version、graph_definition_ref、compiled_graph_hash、invocation_id?、parent_node_task_id?、status、state_revision、head_checkpoint_id?、scope_commit_seq | 工作流外层可无 invocation_id；图身份和所属引擎跨恢复固定 |

`AgentInvocation.budget_scope=run` 用于直接 Agent Run，local_loop_limit／local_deadline_at 为 null，复用根限制；工作流内调用用 `invocation`，创建时固定局部上限与 `started_at + Agent timeout` 的局部截止。有效截止为根与局部截止的较早值。所有角色 LoopTicket 带 invocation_id，局部计数由同一流水按归属汇总，不是另一套可独立扣减的账本。局部停止仅影响所属调用；根控制优先且影响整树。

RunStatus：`queued / running / stopping / succeeded / failed / cancelled / timed_out / budget_exhausted`。停止先关闭新工作，全部写任务收束后提交终态；cleanup_state 单独记录清理结果。没有用户 pause／resume 接口，恢复是内部接管。

`conversation_ref` 是 Agent Session 或 WorkflowSession 的带类型引用。子 AgentInvocation 默认 conversation_scope 为本次调用输入，不自动新建 Session 或读取其他用户历史。直接 Agent Run 的 Invocation 引用所属 Session；后续提问从 ContextManager 读取历史，不继续旧已完成图。

### 4.1 文件与系统内容的类型化归属

WorkspaceScope 由 EnvironmentManager 按 `(owner_ref, partition_kind, partition_id, run_id)` 幂等创建；输入来自可信发布和运行身份。直接 Agent 调用用 agent_session 分区；工作流外层用 workflow_session 分区；工作流内部完整 Agent 调用默认使用 invocation 分区，不能伪造 Agent Session。相同 Invocation 的 Planner／Executor 共享该 scope；不同 Invocation 即使引用同一 Agent 也保持分区独立。显式历史授权不自动改变工作文件分区。

持久模式的会话分区可以跨 Run 使用同一工作目录，但每个 Run 重新获得受租约保护的 WorkspaceScope／句柄；旧 Run 的句柄不因路径相同继续有效。Invocation 分区持久模式只保证该调用产物保留，不自动关联未来调用。恢复沿用原 scope 身份、挂载根和策略，重新装配当前执行权，禁止用“无Session”回退到全 Agent 根目录。

外层 CapabilityNode 的 owner 是 Workflow，无 invocation_id 或 Agent Session 仍可取得其合法 WorkspaceScope；内层 Agent 的 owner 是 Agent，不能自动继承外层目录。跨 scope 文件使用显式 ArtifactRef 授权，完整路径和身份不是授权凭证。临时目录均等整个根 Run 收束后清理，持久目录归所属 Agent／Workflow 删除流程管理。

系统内容索引采用 `(owner_ref, sha256)`；角色原始内容属 Agent，外层图与工具内容属 Workflow。父持有子结果引用或派生投影时登记 source_owner 依赖；删除源 Agent 时同步移除父投影中的相关载荷并保留无内容的删除标记，不通过复制改属规避删除。共享资源版本仍在独立资源域中维护。

派生数据的来源依赖由映射／上下文／Artifact 服务对已消费输入取保守并集；不得靠模型声明或文本猜测。导入工作区的跨 owner 文件先登记依赖，无法从脚本产物中安全分离源内容时，整体撤销受影响内容，细则见[存储归属](storage-recovery-api.md#12-系统内容归属与跨-owner-依赖)。

## 5. 节点任务与激活

```text
NodeTask
  node_task_id, run_id, graph_execution_id, node_key, binding_ref
  dispatch_id?, parent_task_id?, branch_ref?, loop_path:LoopFrame[]
  input_ref, input_state_view:{base_state_revision, write_ids[]}
  status(ready|running|waiting|succeeded|failed|cancelled)
  current_activation_id?, continuation_ref?, result_ref?, revision

BranchRef
  wait_id, member_id, task_sequence

NodeActivation
  activation_id, node_task_id, activation_no, lease_epoch
  start_node_revision, started_at, ended_at?, outcome?

ReactContinuation
  protocol_version, phase, role_key, context_revision_ref
  role_loop_index, current_loop_ticket_id?, model_operation_id?
  pending_batch_id?, compaction_ref?, supplement_delivery_watermark
  final_output_ref?
```

阶段统一为：`prepare_context / prepare_model / compact_context / call_model / interpret_response / wait_batch / consume_results / validate_output / completed`。失败、取消由 NodeTask.status 表达；不重复创建另一套角色状态机。读取视图可投影为 AgentExecutionView，不能独立写它。

NodeActivation 的 `outcome` 为 Progress／Wait／Complete／Fail 等推进事实；Progress 不代表任务完成。实现对象的 `activate()` 返回 ActivationStream，元素为 NodeOutcome；这是内部控制信号流，不是供应商 token 流。

NodeDriver 读取 NodeCheckpoint 重建 continuation，每个 Progress 经过提交确认后才能继续；Wait 固定等待条件后释放激活；再次唤醒原任务时携带持久结果引用。待调用 Operation 的身份在调用前固定，恢复不会因换 Activation 而生成新业务调用。

## 6. 批次、调用和额度

| 记录 | 核心字段 |
| --- | --- |
| CallBatch | batch_id、owner_node_task_id、model_operation_id、loop_ticket_id、members[]、parallel_enabled、executor_max_parallel、delivery_state、revision |
| BatchMember | call_id、ordinal、action_kind(tool/executor/rejected)、target_key、input_ref、operation_id?、child_node_task_id?、result_ref?、status |
| WaitGroup | wait_id、owner_task_id、member_scope、join_key?、dispatch_id?、generation、base_state_revision?、loop_path[]、members[]、policy、settled_members[]、delivery_state |
| WaitMemberRef | member_id、ordinal、state、target_ref?（kind 为 operation／node_task／child_graph）、result_operation_id?、local_outcome?；排队未准入可无目标，调用启动后固定引用 |
| BranchMember | WaitMember 的 branch_scope 变体；branch_data 含 entry_node_key、cursor_task_id?、next_task_sequence、view_write_ids[]、terminal_reason?；member_id 即分支身份，无父调用 Operation 要求 |
| LoopTicket | ticket_id、run_id、invocation_id、node_task_id、model_operation_id、role_loop_index、business_loop_no?、state(reserved/consumed/released) |
| Operation | operation_id、run_id、graph_execution_id、node_task_id?、purpose_key、kind、purpose、status、processing_phase、请求／结果／Hook／有效上下文引用、retry_policy、revision |
| Attempt | attempt_id、operation_id、attempt_no、lease_epoch、status、started_at、first_output_at?、ended_at?、provider_request_id?、error?、raw_result_ref? |

CallBatch 表达一次模型的动作集合；WaitGroup 表达引擎怎样等待，不再建立第二个 Executor 调度器。CallBatchCoordinator 负责成员账本和结果组装，GraphEngine 唯一决定节点调度与可用槽位。工具由对应 Manager 执行，完成事件结算原 WaitMember。

dispatch_batch 的 WaitGroup 先冻结成员身份，串行只准入当前成员；不能为填满引用提前启动全部调用。begin 返回 PendingOperationRef，不是最终响应；父等待、分派／启动意图先一致提交再执行。子 NodeTask／GraphExecution 完成后，仍需把 raw 结果交父委派 Operation 的后置 Hook；result_operation_id 的最终 EventResponse 已提交，成员才结算。

BatchMember.status 与 WaitMember.state 同步为 `queued|admitting|admitted|settled`。admitting 表示已保存准入意图：begin 前 `target_ref/result_operation_id` 均为空，begin 后二者同时指向原 Operation；尚未创建子任务，不能指向 NodeTask／Graph。admitted 必须有固定调用／目标关联；settled 必须有最终响应或合法 local_outcome。串行 Executor 同样先预留唯一 admission token，再开始前置处理；不能省掉令牌而直接进入要求 reserved→converted 的创建路径。

在 begin 前被参数／授权校验拒绝，或因停止而尚未启动的成员，使用 `local_outcome={kind:rejected|cancelled,error_code,error_ref}`。CallBatch 的成员结果和 WaitMember 的 settled 状态同事务提交，此时 target_ref 与 result_operation_id 均为 null，不虚构 Operation 或 Executor。queued 成员没有 local_outcome；已创建 Operation 的成员必须经它的最终响应结算，不能删引用后改用 local_outcome 绕过处理链。前置 Hook deny 已有 Operation、没有子任务时，target_ref 指向该 Operation（kind=operation），result_operation_id 与其相同；完成后保存拒绝响应即可结算。所有本地错误保留原 call_id／ordinal 并进入相应结果槽；已停止 Run 只保存终态，不再启动下一 Loop。

branch_scope 成员不能靠入口任务完成来结算：A1 完成时，A1 结果、分支视图、cursor 移交 A2 及 A2 启动意图一致提交；显式出口、不可处理失败或取消才关闭整条分支。普通分支 target_ref／result_operation_id 为 null，使用 branch_data 定位；after-Hook 规则只适用于确实有父委派 Operation 的成员。

Executor 活跃名额由已准入且非终态的 NodeTask 投影；TaskResult／TaskWrite 与终态提交后释放，父结果 Hook 未完成仍阻止成员结算。并行可补位，串行必须等成员结算后才准入下一项。

成员按 ordinal 固定；全部成功／失败／取消结算后才进入下一业务 Loop。普通工具失败不取消同批其他成员；Run 控制终结则停止新增成员并明确取消未执行项。空批次立即结算。

Operation 阶段：`prepared / request_hooks / executing / raw_saved / response_hooks / completed`。前置拒绝可以没有 effective_request 和 Attempt；后置拒绝必须保留已经执行的请求、hash 与 raw 结果。完成状态和最终响应一起保存。一个 Operation 对应零到多个 Attempt；委派控制 Operation 自身不产生外部 Attempt。

Operation.kind 固定为 `model|tool|context|environment|delegation`；非 completed 阶段 status 一律为 pending，completed 使用 succeeded／failed／blocked 并保存 final_response_ref。控制取消是 failed 加 cancelled 类错误，执行是否未知由 Attempt／execution_fact 表达，不另造 Operation.cancelled／unknown 状态。阶段、结果状态与执行事实不能互相替代；部分契约样例也必须遵守这些已纳入字段的取值。

恢复沿用 operation_id 和 purpose_key；自动重试增加 Attempt，LLM 再次提出相同参数产生新 Operation。第 100 次获准 Loop 正常完成，第 101 次不能启动；LoopTicket 的预留／消费必须与接受决策或有效错误反馈关联提交，根与 Invocation 约束在同一事务验证；预留暂占满返回等待，已消费满且需要新 Loop 才返回相应范围的耗尽错误。压缩不占业务额度。

## 7. 上下文和 Hook 的数据边界

```text
RoleContext
  context_id, invocation_id, owner_node_task_id, effective_revision

ContextMutation
  mutation_id, context_id, base_revision, committed_revision
  source_kind(hook|runtime|user), source_ref, cause_operation_id?
  patch_ref, created_at

HookDecision
  decision(allow|deny), reason?, payload_patch?, context_patch?

HookProcessingState
  operation_id, phase(request|response), chain_version_ref
  next_handler_index, committed_decision_refs[], effective_payload_ref
  base_context_revision, effective_context_ref, pending_handler_ref?

EventResponse
  request_id, operation_id?, cause_id?, status(succeeded|failed|blocked)
  result_ref?, error?, effective_context_ref?
```

已 begin 的 EventResponse.request_id 指向该 Operation 权威 EventRequest 的 event_id，operation_id 必须存在；HTTP 传输请求 ID 另作追踪。重投按可信主体＋purpose_key 复用该调用，不能靠参数相等合并业务调用。事件信封、PendingOperationRef、CommittedEvent 和任务局部 EventFrame 的职责见[事件总线专篇](runtime-messaging-checkpoints.md#12-事件请求和结果的结构)；这些是调用／通知结构，不新增调度状态权威。

原始记录只追加；有效上下文包含来源明确的修改，供当前处理器、后续 Loop、历史、压缩和恢复共同读取。`context_patch` 只能指向挂点授权范围，不能因拿到 context_id 就跨角色修改。已有请求冻结后保持原输入；新请求再读取更新。

同一 Hook 的两类 patch 先归一为一份规范语义变更集合；同字段同值合并、冲突整体丢弃本项 patch。effective payload 与 effective context 从同一集合生成；合法 deny 独立判定，不因 patch 冲突被吞掉。已冻结调用的实际事实只能附注，不能改写成另一次执行。

Hook 合法 deny 返回 blocked，前置未执行、后置不能回滚副作用。处理器异常／超时／非法修改按 fail-open，必要提交失败不放行。已提交的决定、游标和修订恢复复用；只有 raw_saved 时恢复后置处理，不再调用模型或工具。

未知 Hook 结果的本版默认见[检查点协议](checkpoint-protocol.md)：先查可取回的决定；无法确认时记录 indeterminate，放弃未提交修改，按 fail-open 沿用最后有效值，不盲目重放外部 Hook。它不推翻已提交 deny，也不声称撤销 Hook 外部副作用。

## 8. 检查点结构索引

详细字段和事务见[检查点协议](checkpoint-protocol.md)，这里固定各类对象的关系和名称。

| 对象 | 定位字段 | 载荷 |
| --- | --- | --- |
| GraphCheckpoint | graph_execution_id、engine_kind、engine_protocol_version、checkpoint_id、checkpoint_seq、previous_checkpoint_id、covered_commit_seq | 公共信封＋engine_state_ref；内层为角色／批次现场，外层为原生快照及一致读取的 pending writes，见双引擎协议 |
| WorkflowTaskBinding | graph_execution_id、origin_engine_checkpoint_id、engine_task_id、engine_task_path | 指向稳定 node_task_id；恢复接回同一调用，不能按节点名复用不同业务任务 |
| NodeCheckpoint | node_task_id、node_checkpoint_id、node_revision、scope_commit_seq | activation、continuation_ref、输入、已消费结果和等待引用 |
| TaskResult | task_result_id、node_task_id、dispatch_id? | 节点终态、有效输出／错误引用 |
| TaskWrite | write_id、task_result_id、state_key、dispatch_ordinal、write_ordinal；经 NodeTask 关联 branch_ref／task_sequence | 待加入图状态的值及固定 reducer；pending 是状态，不是另一个实体 |
| ScopeCommit | graph_execution_id、scope_commit_seq、idempotency_key、lease_epoch | 一致提交的变更引用索引 |
| OutboxRecord | event_id、run_id、scope_commit_seq、delivery_state | 提交后通知／调度唤醒，不是唯一执行现场 |

RuntimeCommit 是发给 CommitCoordinator 的提交命令；ScopeCommit 是落盘的事实索引。CheckpointStore 的事务写端口由协调器使用，业务节点不单独调用 put／put_writes 形成不一致状态。GraphCheckpoint 不是摘要 ContextSnapshot，前者恢复执行，后者缩短模型输入。

`TaskWrite` 表示内层／旧自研方案的写集，不是外层原生 pending writes 的存储别名。WorkflowCheckpointAdapter 保留引擎写入规则，通过 `CommitWorkflowCheckpoint / CommitWorkflowTaskWrites` 提交；公共信封、恢复包和父子结果消费见[检查点适配](dual-engine-integration.md#6-统一检查点管理保留两种现场)。

## 9. 跨记录不变量

1. NodeTask、Operation、Context、GraphExecution 必须属于同一授权 Run／AgentInvocation；跨图引用需要显式 parent/child 关系。
2. 节点类型、绑定类型、role.kind、模式及 schema 相容；发布完整后才能启动。
3. `dispatch_batch` 同一 `(graph_execution_id, dispatch_id, member_id)` 只创建一次成员目标 NodeTask；`branch_scope` 则按 `(wait_id, member_id, task_sequence)` 唯一。A1 与 A2 共享 wait/member，但 task_sequence 分别为 0／1，是不同任务；相同 node_key 不构成重复任务。
4. NodeTask 终态只有一个 TaskResult；重复通知返回同一结果，不重复推进后继。
5. 根与局部均满足 `consumed + reserved <= limit`；局部由同一 LoopTicket 流水按 invocation_id 汇总，原子预占／结算不可只更新一边。
6. Hook effective 结果、上下文修订、Operation 完成和响应引用一致；raw_saved 不可直接当成功交付。
7. 节点终态、后继意图、WaitGroup／Join 消费和相应状态更新需要协调提交；通知失败可补送。
8. 所有推进验证 lease_epoch；旧 Worker 不能用历史快照覆盖停止、截止或新拥有者进度。
9. 快照指向不可变内容；历史快照读取不能偷偷换成最新 context、最新 Role 或最新结果。
10. 会话历史不等于恢复入口；新 Run 使用历史投影，故障继续原执行身份。
11. BranchMember 一次只持有一个就绪／运行 cursor；NodeTask 继承 branch_ref 与 loop_path，入口任务完成不等于整条分支关闭。
12. Join 成员集和 generation 在 fork 时冻结；同图同 fork_route_key 生成单调 generation，恢复不会重新分配。
13. 分支视图的不可变写引用只能指向本分支已提交的 TaskWrite；每条写入在最终 Join 合并恰好消费一次。

## 10. 可执行文档样例

[Schema](contracts/contracts.schema.json)、[结构样例](contracts/examples.json)和[验证脚本](contracts/validate.py)覆盖本轮核心 Definition、绑定、实例和快照结构及跨引用样例。它们用于检查设计文档自洽，不是生产 ORM，也不证明恢复、并行或真实模型调用已经通过。

正式实现仍需提供完整 DTO、持久映射、版本升级及事务／故障注入测试。无需兼容当前未上线原型的旧表或检查点，但运行期间固定版本与恢复一致性仍必须遵守；“不兼容旧实现”不等于可以在一次新 Run 中途换定义。
