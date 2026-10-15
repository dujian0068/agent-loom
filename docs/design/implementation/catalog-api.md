# ID-1 配置、发布与应用 API 实施规格

> **重建边界（2026-10-15）**：旧业务实现已移除。本页保留为待调整的设计草案；用户、应用、发布与配置版本属于平台，SDK 只接收已解析配置并负责执行与恢复。涉及平台身份、发布绑定及表结构的内容需按 [SDK / Platform 边界](../sdk-platform-boundary.md)重新划分，不直接作为 SDK 的必填契约或已实现功能。

2026-10-15。**目标实现规格，尚未实现。** 本文细化 SD-3；新增数值边界、接口路径和事务算法是工程选择，不冒充用户逐字段确认。字段规范见本页，持久提交见[ID-2](persistence.md)，执行内部见[ID-3](runtime-graph.md)。

## 1. 实现职责与入口

```text
HTTP Route → IdentityResolver → ApplicationService → Catalog/RunControl
                                           ├→ Repository（目录事务）
                                           └→ CommitCoordinator（运行事务）
```

Route 只解析 DTO、身份、幂等键和 HTTP 条件头；不能直接更新 Run、拼接工作目录、调用模型或在请求连接内启动 Agent。API 断开不取消已经创建的 Run。Worker 领取持久启动意图执行；接入层与 Worker 可以在同进程，但不能共享一个只能存于内存的任务队列作为事实来源。

所有写接口的身份为服务端注入的 `RequestIdentity(request_id, actor_id, space_id, trace_id)`。路径中的 Space 必须和身份授权匹配，Agent 和资源的 space_id 必须在同一查询条件内检查。无查看权限返回 404，已能查看但无操作权限返回 403。

本版权限矩阵如下；资源库的共享授权可以收紧此矩阵，不能扩大 Space 权限。

| 操作 | viewer | editor | admin |
| --- | --- | --- | --- |
| 查看已发布 Agent、自己可访问的 Session、使用 Agent | 是 | 是 | 是 |
| 创建 Agent、编辑草稿、导入资源 | 否 | 是 | 是 |
| 发布、停止自己发起的 Run | 否／可以停止自己的 Run | 是 | 是 |
| 查看别人私有 Session、停止别人 Run | 否 | 否 | 是 |
| 删除 Agent、管理成员／凭据、撤销资源 | 否 | 否 | 是 |

Agent 对 Space 可见不意味着成员可以读取其他成员的聊天历史。`Session.owner_actor_id` 决定历史可见性；后台管理查看仍记录访问审计。不把 API token 视为隐含管理员：它固定 actor、space 和权限范围，并同时受当前成员权限限制。

## 2. DTO 的序列化规则

- JSON 使用 snake_case；所有对象拒绝未知字段，除非字段明确为 `config` 或任意业务 JSON。
- 内部 ID 为服务端 UUIDv4；客户端不得提交 agent_id、file_root_id、run_id、lease_epoch 作为新建身份。
- `version` 为不可变正整数；普通实体 `revision` 为从 1 开始的 CAS 修订（Context 空 manifest 的 effective_revision 为 0）；列表顺序只在有语义时参与哈希，例如 Hook 顺序、模型工具调用顺序。
- 哈希采用 UTF-8 canonical JSON：对象键排序、紧凑分隔符、禁止 NaN/Infinity；语义无序列表先按其 key 排序。字段 default 先展开再哈希，不将凭据值、时间戳和传输 request_id 放入内容哈希。
- 空与缺省不同：缺省字段使用定义默认值；允许 null 的字段显式清除；不接受空字符串代替 null。
- HTTP 请求体上限为 2 MiB；文件导入走独立上传。列表 `limit=50`、范围 1..200，使用不透明游标，不能用页码遍历运行记录。
- 本文中 `ref<T>` 必须含 `{id,version,content_hash}`，不是一个会自动解引用为最新版本的资源名。

### 2.1 创建和编辑

```json
{
  "name": "资料研究助手",
  "description": "检索并整理团队资料",
  "mode": "plan_executor"
}
```

`name` 去首尾空白后 1..100 字符；description 0..2000 字符；mode 仅 react、plan_executor。名称不作为身份，也不要求 Space 内唯一。创建事务保存 Agent、revision=1 的空草稿和 file_root_id；物理目录延迟创建，不能因为存储不可用留下半个 Agent。

草稿通过 `PUT /agents/{id}/draft` **整体替换可编辑配置**，不实现任意 JSON Patch。请求带 `If-Match: "draft-7"`；body 不含 mode/file_root_id。服务端从 Agent 注入这两个不变字段。更新草稿允许“尚未完整、不能发布”的状态，但字段类型、范围及不存在的 role_key 引用必须立即拒绝；发布必需值可为空并在 validate 中报告。

```text
AgentDraft
  model: ModelBindingDraft | null
  roles: RoleDraft[]
  execution: ExecutionPolicy
  context: ContextPolicy
  environment: {persistent_sandbox: bool, execution_profile_ref: ref | null}
  hooks: HookBinding[]
```

### 2.2 角色及 Prompt

| 字段 | 类型和规则 |
| --- | --- |
| role_key | `[a-z][a-z0-9_]{0,63}`；同一配置唯一，作为图绑定键 |
| kind | react/planner/executor；ReAct 恰有一个 react；Plan 恰有一个 planner 和零到多个 executor |
| name、description | 展示名称与模型可见委派说明；Executor description 发布时必填 |
| prompt | `{source:inline,text}` 或 `{source:version,ref}`；text 不能为空；Executor 不拼接 Planner Prompt |
| variables | VariableSpec[]；名称唯一；reserved 前缀 `_runtime_` 不允许用户定义 |
| input_schema、output_schema | 可空；JSON Schema draft 2020-12，自包含 `$defs`，禁止远程 `$ref` |
| capabilities | CapabilityBinding[]；模型可见名称同角色内唯一 |

`VariableSpec={name,type,required,default?,source}`。type 为 string/number/integer/boolean/object/array；source 为 input 或 fixed；fixed 必须有 default。Prompt 使用 <code v-pre>{{name}}</code> 纯文本替换，不执行 Jinja/Python、属性访问或表达式。非字符串按 canonical JSON 转成文本；值再次包含 <code v-pre>{{…}}</code> 不递归展开。发布检查所有占位符有定义；新 Run 校验所有 required 输入及类型，错误返回 422，尚未创建 Run。

`CapabilityBinding={binding_key,kind,resource_ref,member_key?,visible_name,config,authorization}`。kind 为 builtin_tool/tool/mcp/skill；mcp member_key 指 discovery 版本内固定工具名。Skill 不把全部内容塞入模型上下文，而进入技能目录。Executor 委派描述由 RoleDraft 生成，不允许用户用同名普通工具覆盖。第一版保留 `delegate_` 前缀给委派，工具名匹配 `[A-Za-z_][A-Za-z0-9_-]{0,63}`。

### 2.3 模型绑定和凭据

```text
ModelBindingDraft
  connection_ref: ref<ModelConnection> | null
  credential_slot_id: UUID | null
  model_id: string | null
  think: {enabled: bool, intensity: string | null}
  context_window_tokens: positive int | null
  max_output_tokens: positive int | null
  metadata_source: provider | catalog | user | unknown
  metadata_ref: ref<ModelMetadata> | null
```

只接受该连接 profile 支持的 think 值；不把不支持的开关原样传给供应商。Provider profile 决定 API 地址、协议、认证头和 think 映射，连接版本固定它；模型 Key 的值只在 CredentialsService 中保存。更新 Key 不改发布内容，下一次物理 Attempt 解析当前未撤销的凭据；已发请求不会换 Key。不能把新 Key 静默映射为另一个模型或供应商。

窗口与输出上限未知时 validate 给出 `MODEL_METADATA_REQUIRED`；前端用 K 单位输入，API 始终整数 token，`1K=1024`，只接受转换后整数。`256K` 为 262144；展示同时显示精确值，来源 user/provider/catalog。不能用默认 32768 冒充已识别的规格。max_output 必须小于 context_window；完整预算还需满足 ID-5 的固定材料检查。

### 2.4 默认策略与覆盖规则

所有字段保存在单个 Agent 草稿及 Release，不从一个可变全局配置中动态继承。角色没有单独覆盖 execution/context/model 的接口；角色可独立配置 Prompt、变量、输入输出、能力；Hook 以 matcher 精确选择角色。

| 字段 | 默认值 | 合法范围／语义 |
| --- | --- | --- |
| execution.loop_max | 100 | 整数 1..100000，共享根 Run 额度 |
| execution.run_timeout_seconds | 3600 | 整数 1..86400 |
| execution.parallel_enabled | false | 工具和委派同时遵守 |
| execution.executor_max_parallel | 2 | 整数 1..128；关闭并行时仍保存值但实际逐项执行 |
| execution.model_first_output_timeout_seconds | 60 | 整数 1..3600；首有效输出后不再应用此定时器 |
| execution.model_stream_idle_timeout_seconds | null | null不设流中空闲时限；显式1..3600，开始输出后检测空闲，异常仍按retry策略处理 |
| execution.tool_timeout_seconds | 10 | 整数 1..3600；含 Tool/MCP/脚本 |
| execution.retry.enabled | false | Runtime 自动重试，与 LLM 另发调用分开 |
| execution.retry.max_attempts | 2 | 整数 1..20，包含首次 |
| execution.retry.delay_seconds | 2 | 0..300，可小数 |
| context.recent_runs | 2 | 整数 0..100；0 时历史全部按用户输入与最终结果投影 |
| context.trigger_ratio | 0.8 | 大于 0、小于 1 |
| context.output_reserve_tokens | null | null 按ID-5取 min(已知模型最大输出或4096, floor(W/4))；显式值为正整数；实际请求max_output不得超过本预留 |
| context.safety_margin_tokens | null | null用max(256,ceil(W×1%))；显式非负整数，与窗口一起参与硬预算检查 |
| context.summary_target_tokens | null | null 用 ID-5 的计算值；显式值是更小的上限，不能扩大模型窗口 |
| context.compaction_prompt_addendum | 空字符串 | 0..20000 字符；补充默认压缩指令，不替换其保护规则 |
| environment.persistent_sandbox | false | false 仅保存本 Run 工作文件；true 按 Owner 保留 |

HookBinding 的 timeout 默认 5000ms、范围 1..60000ms，并受有效 Run/Invocation 剩余时间收紧。上述工程上限用于输入校验，不是承诺可以承载这么高的吞吐。修改上限需变更 schema 版本和测试，不能悄悄截断用户值。

## 3. 发布操作：按步骤实现

### 3.1 验证报告

`POST /agents/{id}/validate {draft_revision}` 返回：

```json
{
  "valid": false,
  "draft_revision": 7,
  "errors": [{"code":"MODEL_METADATA_REQUIRED","path":"/model/context_window_tokens","message":"请填写模型上下文窗口"}],
  "warnings": [],
  "resolved_resource_refs": [],
  "compiled_descriptor_hash": null
}
```

报告错误按 path、code 排序，禁止只返回第一个错误。顺序为 DTO→角色/资源→Schema/Prompt→图编译→环境可用性。validate 不创建模型调用、不执行 Skill 脚本、不启动沙箱、不改草稿；远程资源发现必须是独立明确的管理操作。

### 3.2 发布算法

发布使用 `POST /agents/{id}/releases`，body `{draft_revision}`、If-Match、Idempotency-Key。标准 Agent 图由固定模板版本生成；没有用户在这个 API 上传的自由图。返回 202 ReleaseReceipt。

1. 读取 revision 对应草稿、资源固定版本和当前授权，完成纯验证与编译。序列化Definition、bindings及完整CompiledGraphDescriptor写入不可变内容存储，得到各自PayloadRef和内容hash；Release固定compiled_descriptor_ref与descriptor_hash，后者为剔除自哈希字段计算的语义hash，不拿它冒充blob地址；数据库尚未切换当前发布。
2. **目录事务 CAT-PUBLISH**：锁 Agent；检查 lifecycle=active、barrier=none、revision 未变、调用方仍有权限。对引用资源按 UUID 排序取共享锁，检查未撤销。再次确认编译输入 hashes 相同。
3. 以 `(agent_id,draft_revision,definition_hash)` 查已有冻结 Definition，存在则复用；不存在创建下一个版本。图模板编译产物相同可复用内容，Definition 身份不靠内容哈希替代。
4. 写 AgentRelease、ReleaseJob(state=stopping)、Agent.barrier=publishing、start_seq 水位和幂等回执。**不改 published_release_id。** 提交事务。
5. 发布 Worker 查询所有使用该 Agent 的活跃 Invocation（含未来 Workflow 内引用），将根 Run 的停止意图逐一提交；不持有 Agent 锁等待运行终结。由于屏障已提交，新的直接 Run 和新的 AgentNode 调用均返回 `RELEASE_TRANSITION`，不能穿越屏障。
6. 等全部目标 Invocation 收束、无旧授权 writer 后，目录事务重新锁 Agent 和 ReleaseJob，验证 barrier/job_id、发布内容与授权仍有效，切换 published_release_id，ReleaseJob=succeeded，清除 barrier。直接 Session 保留，新提问使用新版本。
7. 若中途资源撤销、环境不能收束或编译产物缺失：Job=failed 并记录 error，旧指针不变。仅在已经确认执行权收束后清除屏障；否则保持 barrier，管理员看到 `cleanup_required`，不能假装发布成功。

每一步可重入：job_id、release_id 不变；已停止 Run 不再启动；写完候选后崩溃只继续 Job，不再创建第二份发布。屏障期间编辑草稿返回 409；成功后可以继续编辑下一 revision。没有“发布等待连接一直不返回”的接口。

WorkflowRelease 固定引用旧 AgentRelease；发布 Agent 不自动更新 Workflow 绑定。屏障结束后它仍可调用固定旧版本，除非版本被撤销。是否停旧运行和是否改变定义是两件事。

### 3.3 资源版本操作

`POST /resources/imports` 接收 upload_id 或 `{git_url,revision,subdirectory}`；Git 必须先解析为 commit SHA，实际归档文件 hash 列入 manifest。资源导入 Worker 以 job_id 幂等工作，目录版本只在完整校验成功后提交。失败暂存包不出现在可选资源列表。

- Skill：必须有合法 SKILL.md；目录文件禁止越界软链接；允许 scripts/references/assets；脚本不在导入时执行。
- Tool：保存输入输出 schema、实现定位与执行 profile；实现定位不是允许 HTTP 用户指定宿主 Python import。
- MCP：显式 discover 操作保存工具名/schema 的固定 discovery 版本；运行仍校验服务端是否符合该 schema，漂移给出错误，不偷偷增加工具权限。
- Hook：声明触发点、受限字段 schema 和执行器类型；可修改范围由平台挂点决定，不能由 Hook 自己声明授予。
- ModelConnection：提供方/调用方案/profile/address 固定版本；查询可用模型结果与元数据来源一同保存；连接失败不是可用模型列表为空的成功响应。

删除资源时先检查有效 Release 依赖，存在返回 409 `DEPENDENCY_CONFLICT` 及依赖 ID；强制安全撤销将版本 revoked，并阻止后续 Attempt，正在网络中的调用走收束。删除 Agent 不删除共享资源或其他 Agent 的凭据。

## 4. 会话输入的可实现协议

### 4.1 记录和 DTO

```text
Session: session_id, agent_id, owner_actor_id, input_seq, active_run_id?, revision
UserInput: input_id, session_id, input_seq, client_message_id, origin_run_id?,
  kind: question|supplement, body_ref, received_at
UserInputDelivery: delivery_id, input_id, target_run_id?, operation_id?,
  previous_delivery_id?, state: pending|claimed|delivered|carried,
  context_seq?, first_output_receipt_ref?, delivered_at?
UserInputCommand: client_message_id, text, variables={}, structured_input=null, artifacts=[]
InputReceipt: input_id, session_seq, run_id?, disposition, delivery_state, queued_input_ids[]
```

`UserInput` 是不可变原始输入；`UserInputDelivery` 是投递账本，不把状态写回原始消息。claimed 表示已绑定冻结模型请求但尚无首输出证据。API 的 session_seq 映射 input_seq；delivery_state=pending_next_run 是“旧 delivery=carried，新 delivery=target_run_id:null/state:pending”的展示投影，不增加第二套状态。多种 payload 至少有一种非空；artifacts 必须是授权 ArtifactRef，不接受宿主路径。

`origin_run_id` 固定接收来源；转交先把旧 delivery 置 carried，再建立有 previous_delivery_id 的新 delivery，最后由新 Run 绑定。已 delivered 的记录不允许转交。一个 input 至多一个未 carried 的当前 delivery；历史按 input_id 去重，原接收轮保留转交标记，最终交付轮显示原文，不重复注入。

### 4.2 APP-INPUT 事务

输入路径为 `POST /sessions/{id}/inputs`。同一会话由 Session 行锁线性化：

```text
resolve request identity and validate body
BEGIN
  lock Agent gate FOR SHARE; reject deleting/publishing/revoked
  lock Session FOR UPDATE; check ownership
  resolve idempotency(client_message_id, actor, session); mismatch →409
  if active_run_id: lock that Run FOR UPDATE
  allocate Session.input_seq += 1 and insert immutable UserInput
  if no active Run:
      validate current Release and input variables/schema
      select current delivery(target_run_id=null,state=pending) in input_seq order
      insert Run(queued, release fixed, deadline=accepted_at+timeout)
      bind carried pending deliveries to Run; insert current delivery(state=pending,target_run_id=Run.id)
      Session.active_run_id=Run.id
      insert durable start intent
      disposition=new_run
  elif Run accepts model work:
      insert delivery(target_run_id=active Run,state=pending); disposition=supplement
  else:
      insert delivery(target_run_id=null,state=pending); disposition=pending_next_run
  persist InputReceipt and idempotency receipt
COMMIT
```

在提交前校验失败会回滚新输入和 seq 分配，客户端收到 422 而非一个永远不消费的输入。运行中补充不重新展开已冻结的 Prompt 变量；变量变化作为普通用户补充进入下一 Loop，不改变本次 Release 或执行配置。

Plan 只由 Planner 的 prepare 阶段领取 pending 输入。Executor 不读取 Session 收件箱。ReAct 由唯一入口角色领取。领取和 `commit_model_ready` 冻结请求关联；首有效输出或完整终帧将 claimed 改 delivered。首输出前失败且不再重试，不伪造delivered；终态时按下面规则转交。

### 4.3 APP-FINALIZE 与下一轮转交

终态提交必须先锁 Session 再锁 Run，和 APP-INPUT 使用相同顺序。检查无活跃 writer/后继，再固定 Run 终态；把 pending/无首输出的 claimed 补充旧delivery改carried，再建target_run_id=null/state=pending的新delivery；清除 Session.active_run_id；写一条 RunTerminal 和 InputForwarded 事实。

初始问题已定义本 Run，即使未出首输出也留在本轮未完成记录；只转交后续补充。当前 Run 最终失败但已经 delivered 的补充仍属于本 Run，历史不能丢。

Run 结束后再次输入创建新 Run，将 pending_next_run 的旧补充和这次新输入一起按 session_seq 放入新用户输入区；不会因为转交自动启动模型。两个并发新输入串行化后，第一个 new_run、第二个 supplement。用户显式停止以后到终态以前的新输入先 pending_next_run，不再开启旧模型。

`GET /sessions/{id}/inputs` 必须显示“已接收／下轮生效／已交给模型”，不能只展示一个成功发送气泡。客户端流断开后按 input_id 和 run_id 重新查询，不能重新生成 client_message_id 导致第二次提问。

## 5. HTTP 接口清单与响应

以下为目标 `/api/v2` 协议，用于重写时避免与原型接口混淆；不要求保留原型兼容路由。列表返回 `{items,next_cursor}`；单实体返回字段对象，不额外嵌套任意 data 层。

| 方法与路径 | 请求／条件 | 成功 | 领域调用 |
| --- | --- | --- | --- |
| POST /spaces/{s}/agents | CreateAgent、Idempotency-Key | 201 AgentView | AgentCatalog.create |
| GET /agents/{a} | 查看权限 | 200，ETag draft revision | AgentCatalog.get |
| PUT /agents/{a}/draft | 完整Draft、If-Match、幂等键 | 200 Draft | update_draft |
| POST /agents/{a}/validate | draft_revision | 200 ValidationReport | validate |
| POST /agents/{a}/releases | draft_revision、If-Match、幂等键 | 202 ReleaseReceipt | publish_agent |
| GET /release-jobs/{j} | 权限 | 200 job | ReleaseService.get_job |
| POST /agents/{a}/sessions | 幂等键 | 201 SessionView | SessionService.create |
| POST /sessions/{s}/inputs | UserInputCommand、幂等键 | 202 InputReceipt | submit_input |
| GET /sessions/{s}/history | before_run_seq、limit | 200 HistoryPage | history |
| GET /sessions/{s}/inputs | after_session_seq | 200 InputPage | input_delivery |
| GET /runs/{r} | 权限 | 200 RunView | RunService.get |
| POST /runs/{r}/stop | 幂等键，无任意状态字段 | 202 StopReceipt；已终态200 | RunService.stop |
| GET /runs/{r}/events | Last-Event-ID 或 after_seq | 200 text/event-stream | events |
| GET /runs/{r}/tasks | cursor、limit | 200 TaskPage | executions |
| GET /operations/{o} | run授权、result视图 | 200 OperationView | operation |
| DELETE /agents/{a} | If-Match、幂等键 | 202 DeletionReceipt | delete |
| GET /deletion-jobs/{j} | admin | 200 job | deletion_status |
| POST /resources/imports | source、幂等键 | 202 ResourceImportReceipt | import_resource |
| GET /resource-imports/{j} | 权限 | 200 job | import_status |

缺 If-Match 返回 428，旧 revision 返回 409 `REVISION_CONFLICT` 并带 current_revision；缺幂等键返回 400。409 不能由 SDK 自动换 revision 重试覆盖用户编辑。幂等回执与实体变更同事务，保存直到所属实体删除；原始输入也有 `(session_id,client_message_id)` 唯一索引。无活跃Run时同一个已消费幂等键仍返回旧回执，不创建新轮次。

```json
{
  "error": {
    "code": "REVISION_CONFLICT",
    "message": "草稿已发生变化，请重新加载后合并",
    "request_id": "transport-uuid",
    "details": {"current_revision": 8},
    "violations": [{"path":"/draft_revision","code":"STALE"}]
  }
}
```

422 配置/输入错误，409 版本/依赖/发布屏障，401 无身份，403 无操作权限，404 不可见，503 基础设施暂不可用。Run 执行后的模型/工具错误不改写成创建输入接口的 HTTP 500：它们是 Run/Operation 的已持久结果。错误信息脱敏，不返回 `.env`、Key、数据库错误 SQL 或内部宿主路径。

## 6. 输出、查询及页面状态

RunView 必含 `run_id,status,release_id,started_at,deadline_at,loops{limit,reserved,consumed},terminal_reason?,result_ref?,partial_output_refs[],cleanup_state,last_event_seq,pending_next_input_ids[]`。中途角色输出有 scope/task 标签，不能伪装为最终回答。

SSE 使用持久 outbox 的公开投影；`id` 为根 Run public_seq，不以毫秒时间作为游标。

```text
id: 18
event: operation.completed
data: {"run_id":"R1","seq":18,"task_id":"T2","status":"succeeded","result_summary":"已读取 3 个文件"}

id: 19
event: run.terminal
data: {"run_id":"R1","seq":19,"status":"succeeded","final_output_ref":{"kind":"blob","blob_id":"B1"}}
```

1. API 先读持久化 `(after_seq,last_seq]` 再等待通知；通知仅唤醒查询，不替代回放。断线 Last-Event-ID 从最后收到事件后继续。
2. 连上已结束 Run 时先回放未读事件，再发完 terminal 关闭；重复收到同 seq 客户端丢弃。
3. 15s SSE 注释心跳不入 outbox、不推进 seq；缓慢客户端输出缓冲达到 256 条时断开，允许游标恢复，不无限占用 Worker 内存。
4. 原始模型 token 不逐字走事件总线。适配器收齐一轮结果后形成事件；只有根 Run 终态标记 final。模型已输出但失败的尝试作为 incomplete 详情，不拼进下一次成功回答。
5. 展示 Hook 后的有效结果；原始详情仅授权审计接口提供。发布配置、Prompt、模型 Key 和全部私有上下文不得默认投影到公开进度。

客户端运行状态以服务端 RunView/seq 为准；按钮“结束”收到202后显示“正在结束”，terminal 到达后显示终态；cleanup失败另显示清理异常。不能在点击瞬间清空过程、把运行标成已取消或重复启动。

## 7. 删除：必须执行的顺序

删除不等于数据库 CASCADE 后异步猜测目录。`CAT-DELETE` 锁 Agent，设 lifecycle=deleting/barrier=deleting，登记 DeletionJob 及完整 OwnerRef。新的 Run/引用/发布都拒绝；目录资源先隐藏但保持管理 Job 可查。

Worker 按持久步骤执行：

1. 找所有该 owner 产生且仍运行的 Invocation，以及消费了其内容的活跃根 Run，发送停止并等待 writer 收束／撤权。
2. 找 source_owner 依赖闭包：撤销 Artifact 授权和受影响外层投影；不可拆分的摘要、模型回答或脚本产物整体删除，不靠 LLM 判断是否仍包含来源。保留无内容 tombstone 和 error_code。
3. 撤销沙箱句柄/挂载/会话凭据；删除 owner 的持久及临时 work、系统原始记录、摘要和 blobs。每个物理删除记录成功，失败重试同一个 job，不能先把目录身份删掉。
4. 清理该 Agent 的 Session、Run 私有数据、Definition、Release、绑定；工作流中固定引用改为 deleted tombstone、不能再执行。不会级联删除共享资源实体，也不会删除整个外层 Workflow。
5. 全部完成才 Job=succeeded 并保留最小审计 `{owner_id,deleted_at,actor,job_id}`，无原始内容。返回的 202 不是承诺已经删完。

同一 owner 的重复删除返回原 Job。正常保留不设 TTL；清理孤立暂存文件、失败导入或未引用 blob 是内部垃圾回收，不表示业务会话自动过期。

## 8. 从请求到恢复的贯穿示例

1. 用户创建 Plan Agent A，草稿 revision=1；配置两个角色 main/research，保存 revision=2，发布得到 L1。
2. `inputs(client_message_id=c1)` 创建 S1/R1，固定 L1，初始输入 U1；重复 c1 返回同 R1。
3. Worker 创建 I1/G1/T1，模型一次返回两个 research 委派。G1 保存 T2/T3，不新建用户 Session 或 Run。
4. 用户补充 U2，API 回 disposition=supplement。T2/T3 不收到 U2；T1 的下次模型准备阶段才领取。
5. T2 结束后机器宕机。恢复仍为 R1/I1/G1/T1/T2/T3；T2 的结果不重跑，T3 按 Operation 现场接续。
6. T1 的最终回复完成恰与 U3 同时到达：若 U3 先锁 Session，先保存 supplement 再被终态事务转 pending_next_run；若终态事务先锁，U3 直接创建下一 Run。不存在已返回“接收成功”却无记录的中间状态。
7. 下一次输入 U4 创建 R2，绑定当时当前 Release；先注入转交 U3 再 U4，历史包括 R1 用户输入 U1/U2 及有效结果。

## 9. 验收用例

| 编号 | 操作与故障注入 | 必须断言 |
| --- | --- | --- |
| CAT-01 | 相同Idempotency-Key发20次create | 一个Agent/file_root/初始草稿；回执ID一致 |
| CAT-02 | 两请求基于revision7同时PUT | 恰一个成功，另一个409，无丢失更新 |
| CAT-03 | ReAct提交Executor/model覆盖/未知字段 | 422与精确路径；没有悄悄忽略字段 |
| PUB-01 | 候选发布事务后杀Worker | 重启同Job恢复；旧指针未提前切换 |
| PUB-02 | 发布屏障与输入并发 | 输入要么已固定旧版本并被收束，要么409；不能漏停 |
| PUB-03 | 发布中撤销资源 | 候选失败、不切换；屏障按收束证据清理 |
| IN-01 | 两输入同时遇到空闲Session | 一个新Run一个补充；session_seq严格有序 |
| IN-02 | 接受补充后、模型首输出前Run结束 | 原始输入存在；转交一次；下一Run注入一次 |
| IN-03 | 首帧后模型失败 | 输入delivered，不再转交；保留未完成状态 |
| OUT-01 | SSE断线并从seq18重连 | 返回seq>18；终态只逻辑展示一次 |
| DEL-01 | 删除到物理目录一步失败 | Job未成功、目录身份仍可重试、Agent不可新运行 |
| DEL-02 | 外层摘要含被删Agent结果 | 摘要内容撤销，其他Agent私有原始记录不被误删 |

测试通过的标准是检查数据库、文件和API三者结果，不只看状态码。应用API实现的路径、字段与错误码应从本文生成 OpenAPI，CI 比较生成结果，禁止前端自造另一套状态名。
