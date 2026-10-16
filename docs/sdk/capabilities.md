# SDK 模型、Skill、Tool、MCP 与执行环境

状态：详细设计，尚未实现。SDK 接收已解析的模型适配器、能力绑定、只读 Skill 包及工作空间授权。Key 管理、Git／文件夹导入、共享目录和应用文件根分配见[平台配置](../platform/configuration.md)；SDK 不提供管理端 CRUD。

公共类型见[定义](definitions.md)；Operation／Attempt／Hook 只有[事件总线](event-bus.md)一套实现；上下文算法见[上下文](context.md)。

## 1. 模型适配、首输出与完整响应

### 1.1 冻结绑定及单次调用接口

```python
class ModelAdapter:
    async def discover(self, connection_config, credential_handle) -> list[ModelDescriptor]: ...
    async def stream_once(self, request: ModelRequest, ctx: ModelAttemptContext) -> AsyncIterator[ProviderEvent]: ...

class ModelGateway:
    async def generate_once(self, request_ref: FrozenModelRequestRef,
                            ctx: ModelAttemptContext) -> ModelAttemptResult: ...

class ModelManager:
    async def execute_once(self, operation_ref, attempt_ref, execution_permit) -> RawResultRef: ...
```

`ModelManager` 在执行前核对 execution_permit 与冻结 request_hash；业务模型没有同事务预占的 LoopTicket 不出网。压缩调用无需业务票据，但仍验证 ExecutionRecord／Invocation 控制、lease、授权和有效截止。Gateway／Adapter 的 `once` 禁用 SDK 内部重试；多次 Attempt 仅由调用管道驱动。

| DTO | 字段与约束 |
| --- | --- |
| `ModelDescriptor` | `provider_model_id, context_window_tokens?, max_output_tokens?, supported_features[], metadata_source:provider\|verified_preset\|user, fetched_at?, profile_version`；未知保持 null |
| `ModelRequest` | `operation_id, request_hash, execution_binding_ref, model_id, messages[], tools[], output_format_ref?, think_config, max_output_tokens, effective_parameters_ref, source_revision_refs[], protected_input_ids[], purpose:business\|compaction` |
| `ModelAttemptContext` | `operation_id, attempt_id, attempt_no, lease_epoch, deadline_at, first_output_timeout_ms, idle_timeout_ms?, credential_handle, cancel_signal, receipt_sink`；凭据不序列化 |
| `ProviderEvent` | `kind:heartbeat\|role\|text_delta\|reasoning_delta\|tool_name_delta\|tool_arguments_delta\|usage\|completed\|error, provider_index?, provider_call_id?, data, received_monotonic_ns` |
| `FirstOutputReceipt` | `receipt_id, operation_id, attempt_id, kind:text\|reasoning\|tool_name\|tool_arguments\|complete_frame, observed_at, has_display_content, provider_request_id?, evidence_hash` |
| `ModelResponse` | `text_blocks[], reasoning_blocks[], tool_calls[], usage?, finish_reason?, completion_evidence_ref, provider_request_id?, raw_transport_ref, complete:true` |
| `ModelResponseCall` | `ordinal, provider_call_id?, model_name, arguments_text, parsed_arguments?, parse_error?`；增加call_id:UUID；SDK 在raw响应保存事务内一次绑定稳定call_id/ordinal，接受决策仅复用 |
| `ModelAttemptResult` | `status:succeeded\|failed\|cancelled\|unknown, response_ref?, partial_ref?, first_output_receipt_ref?, error?, started_at, ended_at` |
| `PartialModelMaterial` | `attempt_id, text_fragments[], reasoning_fragments[], incomplete_calls[], complete:false, last_received_at, error_ref`；仅内部审计 |

首版只有 `OpenAIChatAdapter` 和 `OpenAIResponsesAdapter` 两种协议实现；供应商 preset 显式固定协议、API／Coding Plan 地址、feature 映射、错误和完成帧映射版本。本文不猜测 GLM、Aliyun、DeepSeek、MiniMax 的 Think 字段或窗口。适配 profile 未实现某项配置时构造返回 `MODEL_FEATURE_UNSUPPORTED`，不得忽略参数后宣称已启用。

`ModelAdapter.discover` 只返回实际查询／核验信息，不能探测时明确失败或保留未知；构造要求的窗口由用户显式提供。普通请求不会在运行中偷偷重新发现或切换 endpoint/model。

### 1.2 Attempt 内聚合状态机

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

### 1.3 标准输出与修正接口

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

NodeDriver 在同一提交中保存反馈及下一 prepare_context 阶段；该决策的票据已在接受模型响应时 consumed，此处不得再次消费。下一次修正获得新的业务模型 Operation 和 LoopTicket，使用同一根额度／deadline。没有独立修正次数、重试开关或额外额度；已耗尽直接按图运行协议结束。合法完整终帧只是传输完成证据；无动作且正文为空／纯空白，或候选不符合输出 Schema，进入同一输出修正流程，不因此发起连接重试。

## 2. 冻结能力目录、调用结果与按需读取

### 2.1 目录与 Handler

| DTO | 完整字段 |
| --- | --- |
| `CapabilitySpec` | `resource_key, capability_key, kind:builtin\|hosted\|mcp\|script, model_name, description, input_schema, output_schema?, handler_ref, credential_binding_ref?` |
| `ResolvedCatalog` | `program_key, binding_key, capabilities[], model_name_to_key, skill_entries[], access_scope, environment_policy`；调用方已解析的不可变目录 |
| `CapabilityRequest` | `operation_id, resource_key, capability_key, arguments` |
| `ToolInvocationContext` | `content_scope_ref, run_id, graph_execution_id, node_task_id?, invocation_id?, session_id?, role_key?, execution_binding_ref, binding_config, workspace_scope_ref?, operation_id, attempt_id, attempt_no, lease_epoch, deadline_at, authorization_ref, cancellation` |
| `ToolResult` | `status:succeeded\|failed\|unknown, content:ContentBlock[], error?, artifacts:ArtifactRef[], metadata` |
| `ContentBlock` | `type:text\|json\|artifact_ref`，分别含 `text`、`json_ref`、`artifact_ref`；不能内嵌任意系统路径 |

`CapabilityRegistry.for_role(program_key,role_key)`、`for_node_binding(program_key,binding_key)` 返回已注入目录；`resolve(catalog,model_name)` 只查本目录。构造时拒绝 model_name 冲突，运行时缺名返回 CAPABILITY_NOT_FOUND；不搜索平台目录，也不比较平台发布版本指纹。

注册描述、角色选择、绑定配置和不可变视图的装配过程见[能力注册](assembly.md#4-能力如何注册和绑定)。binding_config 是通过注册 Schema 校验的固定配置，由可信 ExecutionBindingRef 解析，不从 LLM 参数中接收；同一适配器服务多个角色时不得把它缓存成全局可变状态。

`CapabilityAdapter.invoke_once(request,context)->ToolResult` 只执行一次；Manager 校验调用可信范围、最终 schema、凭据撤销及执行许可，再调用原 Adapter 并规范化为管道 RawResult。它不同于总线的 EventHandler：后者由 Manager 实现，前者只实现一次具体能力调用。Hook allow 不扩大目录。普通工具不创建新 NodeTask；Planner 委派 action 由图运行负责，不能交给普通 handler_ref。

SDK 生成的对象 input_schema 默认 additionalProperties=false；MCP schema 保留其声明扩展语义。导入收拢许可本地 `$ref`，拒绝外部网络 `$ref`；运行校验不得顺带出网取 schema。

`OperationError` 复用[调用协议的唯一 DTO 定义](event-bus.md#21-范围信封和响应)，不另定义精简枚举。Adapter 通常填 connect／send／receive／execute，Pipeline 还可产生 prepare／after 阶段错误。成功没有 error；unknown 必须有 error 且 external_outcome=unknown。

### 2.2 结果预览与 result.read

原始结果全量按 content scope 写不可变 blob；后置 Hook 得到完整受限引用，普通模型投影只含有效预览。默认 SDK 预览上限固定为 64KiB UTF-8，遇字符边界向前截断，不增加用户配置项。

`ResultPreview` 字段为 `result_ref, media_type, total_bytes, preview_start, preview_end, text?, structured_ref?, truncated, source_operation_id, effective_result_hash`。区间为 UTF-8 字节半开范围；非文本返回 artifact 元信息，不把二进制解码成聊天文本。预览只表示已实际返回范围，不暗示 Agent 读过全文。

`ResultRead{result_ref,offset:int>=0,limit:int=16384}` 允许 `1..65536`；返回 `ResultSlice{result_ref,offset,end_offset,next_offset?,total_bytes,text,etag}`。要求 offset 是 UTF-8 边界，否则 `INVALID_OFFSET`；limit 截断到边界。超末尾返回空且 next_offset=null。

result.read 根据 content scope、调用 NodeTask／角色或显式结果授权验证，并检查 source_scope 尚可用；没有 AgentSession 的外层能力合法使用同一接口。不同 Executor 的 result_ref 不能因同 Invocation 共卷而直接读取。读取本身产生新 Operation／结果记录，响应不暴露 blob 宿主路径。

## 3. Skill 包、发现与脚本

### 3.1 接收已准备的资源包

平台负责上传、Git 获取、版本管理与导入校验；高代码用户也可以直接传本地已准备目录。SDK 的 SkillLoader 接收 `SkillPackage{package_key, root_handle, entries, files_manifest}`，不能接到平台 resource_id 后回查应用数据库。

SDK 在使用边界验证包内路径、manifest 完整性、SKILL.md 元数据、受支持字段和读取范围。发现不支持的执行语义时报 SKILL_PROFILE_UNSUPPORTED；不静默声称与 Claude Code 全兼容。资源包只读挂载，脚本产物写授权工作区。

### 3.2 支持与读取

标准 name／description／license／compatibility／metadata 保存。`arguments,argument-hint,$ARGUMENTS,$N` 仅做文本模板替换：`$ARGUMENTS` 是原参数串，`$1..$N` 来自显式 `arguments[]`；未提供的位置渲染为空，不执行 shell、不二次解释模板。

`user-invocable` 决定显式用户使用入口，`disable-model-invocation` 从模型发现清单移除并禁止模型自主激活；显式用户授权的使用记录仍校验角色权限。allowed-tools 与角色目录取交集，disallowed-tools 再排除；Skill 不授予新工具。

`model,effort,context:fork,agent` 拒绝装配；动态 shell 注入和未支持的专属变量／设置拒绝。hooks 只产出待显式绑定资源，不能因读 SKILL.md 就激活 Hook。

`SkillLoader.list_entries(scope)->SkillEntrySummary[]` 只返回名称、说明、固定入口和 resource_key；`read_entry(SkillRead{resource_key,entry_name,relative_path,arguments[]?},scope)->SkillReadResult` 返回 `text,result_ref,resource_key,path,rendered_arguments_hash?`。相对引用只能在该 Skill 根内解析。`read_resource(resource_key,path,scope)->SkillReadResult` 用同一校验读取 scripts/references/assets，非文本资源仅返回内容引用和元信息，text 为 null；list_entries 不执行脚本。

资源挂载为 `/skills/<name>` 只读，references／assets 按需读取。执行脚本必须单独调用 exec，例如 `argv=["python3","/skills/report/scripts/build.py","--output","/work/report.md"]`；skill.read 不执行脚本、不建业务 NodeTask。

解释器和依赖来自冻结 image_digest；不存在时返回 `EXEC_DEPENDENCY_MISSING`，不得在 SDK 宿主进程 pip install。普通脚本文件访问不逐次发 Hook，外层 exec 负责唯一 Operation 和输出审计。

## 4. MCP 连接、调用与错误归一

### 4.1 连接与方法

固定 MCP profile 为 `2025-11-25`；只支持 stdio 与 Streamable HTTP 的 JSON／SSE，不自动降级独立旧 HTTP+SSE。本文使用现有设计指定版本，不补充未经核验的供应商扩展。

| DTO | 字段 |
| --- | --- |
| `McpConnectionBinding` | `binding_key,transport:stdio\|streamable_http,endpoint_ref?\|command_ref?,credential_binding_ref,protocol_profile,tool_catalog_ref,authorization_policy_ref` |
| `McpConnectionKey` | `namespace, binding_key, credential_generation, workspace_scope_id, execution_view_key`；由注入依赖生成，不接受模型指定 |
| `McpSessionHandle` | `handle_id,key,run_id,lease_epoch,remote_session_id?,negotiated_version,capabilities_ref,expires_at,state:connecting\|ready\|closing\|closed\|failed` |
| `McpCall` | `operation_id,attempt_id,request_id,capability_key,remote_tool_name,arguments,deadline_at` |
| `McpRawResult` | `rpc_result_ref?,protocol_error?,is_error:bool,content_ref?,structured_content_ref?,progress_refs[],remote_request_id` |

`McpClientPool.connect(binding,scope,execution_view,lease)->McpSessionHandle` 完成 initialize／版本校验／initialized 后才 ready；`ping(handle)` 只检查该连接；`call_once(handle,McpCall)->McpRawResult` 是一次实际发送，重连／重试不隐藏于此接口；`close(handle,reason)->CloseReceipt` 幂等关闭。

`list_tools(handle,cursor?)` 分页到 cursor=null，返回规范化发现结果，由调用方决定保存资源版本；list_changed 只产生能力目录变化通知，不改当前已解析目录。tools/call 前验证实际服务工具名/schema fingerprint 与冻结目录匹配，不匹配返回 `MCP_CATALOG_MISMATCH`，不临时选择同名新工具。

resources/list、templates/list、resources/read 仅在授权后包装普通读取能力，不自动读全部资源。prompts/list/get 导入可选模板，不能覆盖已有 system／user Prompt。logging／progress 只做脱敏观察；cancelled 不证明外部副作用撤销。

客户端不宣告 sampling、elicitation、tasks；服务端请求这些方法返回 JSON-RPC method-not-found（-32601），不启动隐藏模型或用户暂停。roots/list 只给专用 stdio 受限可见根，真实隔离仍靠挂载。

### 4.2 凭据、分区与失败

stdio 进程由 SandboxGateway 在该(scope,execution_view_key,epoch)专用执行域启动，与普通 exec 进程分离；凭据仅注入该 MCP 服务声明变量。HTTP pool 按 McpConnectionKey 分区，活动句柄再绑定 run_id／epoch；远端 MCP-Session-Id 不作为 SDK scope 授权。connect与每次call都核验当前角色的execution_view_key/资源挂载digest，不能让同/work的另一角色复用该stdio进程；HTTP会话也按此键隔离roots/授权视图。恢复重建句柄，不能把旧 epoch 连接移交接管后的运行现场。

OAuth 在配置阶段完成授权码＋PKCE、metadata、audience 和刷新策略；执行期不能交互补权。API Key／Header 使用独立凭据绑定，不传平台登录 token。过期／撤销无法刷新返回 `MCP_AUTH_REQUIRED`，不换账号。

访问工作文件的 MCP 必须使用同 scope 的受限挂载，或短期 `WorkspaceAccessToken{content_scope_ref,workspace_scope_id,execution_view_key,run_id,lease_epoch,path_prefixes[],operations[],expires_at,audience}`。每次网关请求验证签名和数据库有效执行域；不是只信 token 未过期。第三方不能获得完整宿主挂载根或通用脚本密钥。

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

annotations 仅展示；只读／幂等声明不参与 SDK 重试准入。远端支持稳定业务幂等键时传 operation_id，但不宣称外部副作用恰好一次。所有 RPC/stdio stderr 日志脱敏并保存到对应 content scope。

## 5. WorkspaceScope、挂载与文件生命周期

### 5.1 构造绑定、持久身份和派生路径

`WorkspaceBinding` 为构造时的配置值：`root_handle,content_scope_ref,environment_policy,resource_mounts[]`。root_handle 由调用方／EnvironmentProvider 解析为受限文件根；environment_policy 使用下文 EnvironmentPolicy；每个 resource_mount 包含 `resource_key,resource_handle,logical_path,read_only`，并须满足策略中的 ResourceMountRule。资源句柄可以是已准备 Skill 包，不能是供 SDK 回查的平台资源 ID。绑定不含 run_id、invocation_id、workspace_scope_id、partition_key 或 lease。

`WorkspaceScope` 是 SDK 创建的持久运行身份，完整字段为 `workspace_scope_id,content_scope_ref,root_handle,partition_kind:session|invocation|run,partition_key,run_id,invocation_id?,persistent`。当前 lease_epoch 不存入此 DTO；旧 scope 的存在不代表旧执行权仍有效。

`WorkspaceRegistration` 为 `scope_ref,creation_key,program_key,environment_policy_ref,mount_intent_id,state:registered|ready|cleanup_pending|cleaned,revision`。creation_key=`(content_scope_ref,partition_kind,partition_key,run_id)`，同键参数不一致返回 `WORKSPACE_BINDING_CONFLICT`。

EnvironmentProvider 接收绑定中的稳定 root_handle 和目录解析策略；SDK 在初始化执行身份后生成并登记 WorkspaceScope，再按持久登记准备目录及授权句柄，不硬编码平台 application_id 或 /shared/agentloom。

| 调用位置 | 相对注入根的分区 | persistent=true | persistent=false |
| --- | --- | --- | --- |
| 直接 Agent | sessions/<session_key> | work/ | runs/<run_id>/work/ |
| 有 Session 的 Workflow 外层能力 | sessions/<session_key> | work/ | runs/<run_id>/work/ |
| 无 Session 的 Workflow 外层能力 | runs/<run_id> | work/，ExecutionRecord 后保留 | work/，ExecutionRecord 收束后删除 |
| 完整子 Agent 调用 | invocations/<invocation_id> | work/ | runs/<run_id>/work/ |

表中后两列拼在分区路径之后；run 分区不再重复嵌套 runs/<run_id>。直接 Agent 未传 session_id 时创建一次性 Session；无 Session／AgentInvocation 的纯能力 Workflow 使用 run 分区；Workflow 中完整 Agent 调用使用 invocation 分区。session_key 是 SDK 派生的稳定路径键。调用方可将 Agent 的 root_handle 映射到创建时固定的应用目录；SDK 不负责创建应用。模型／Hook 不提交宿主路径或 partition_key。Planner／Executor 复用所属 Invocation 的工作分区，跨完整 Agent 调用只能显式授权文件。

`EnvironmentManager.prepare(EnvironmentRequest,ExecutionScope)` 的 request 字段为 `workspace_scope_ref,environment_policy_ref,image_digest,resource_manifest_ref,expected_registration_revision`，返回 `EnvironmentHandle{workspace_handle,sandbox_handle,resource_mounts[],expires_at}`。先事务登记 scope 和 mount_intent，事务后幂等创建目录并验证根 manifest，再提交 ready；同 ID 重入不生成新目录。

`EnvironmentPolicy` 固定 `persistent,image_digest,exec_timeout_ms,egress_allowlist[],resource_mount_rules[]`；`EgressRule` 为 `kind:domain|cidr,value,ports[]`，空列表表示禁外网。`ResourceMountRule` 为 `resource_key,logical_path,read_only=true`，资源写权限不能由调用参数覆盖。执行服务部署配置额外固定 runner_limits_version、tmp_size_bytes 和 max_processes，写入policy_digest；它们属于需验收的基础设施参数，不增加Agent用户配置。

`WorkspaceHandle` 为 `handle_id,workspace_scope_id,run_id,lease_epoch,execution_domain_id,authorized_mounts[],issued_at,expires_at`；只存在服务端／受限 SDK 中，不入 GraphState。`authorized_mounts` 每项含 `logical_root:/work|/skills/<name>|/wiki|/resources/<key>,resource_ref?,read_only`。

Planner／Executor 使用同 Invocation `/work`，各自只读资源目录按角色授权装配。ExecutionDomain以(scope,execution_view_key,epoch)区分受限执行视图；不同角色的只读挂载不同，禁止用资源并集扩大授权。多个视图可以位于同一Agent沙箱设施内，共用同一/work路径，不为每个Executor复制文件或另造应用沙箱身份；不支持隔离挂载视图的driver必须报ENVIRONMENT_VIEW_UNSUPPORTED。外层节点无 invocation_id 合法，使用 Workflow scope；不同 invocation 即使同一个 Agent 也不能共享可写根。恢复沿用 registration／路径及各execution_view_key，撤权枚举scope下全部角色视图，再重建当前 epoch 的句柄。

同一持久会话目录在新 ExecutionRecord 重新登记 scope、重新授权句柄；上一 ExecutionRecord 的句柄一律失效。单活动 ExecutionRecord 的会话占用协议继续适用。持久保存的是文件，不是容器进程、内存或后来安装的软件。

### 5.2 Artifact 授权和来源

`ArtifactRef` 为 `artifact_id,content_scope_ref,workspace_scope_id,relative_path,etag,media_type,size,source_operation_id,grant_id?,source_dependencies[],availability:available|expired|source_deleted`。

`grant_artifact(ArtifactGrant{artifact_ref,recipient_scope_ref,operations:[read],expected_etag},scope)` 先验证源仍可读及接收方授权，再登记 grant 和来源依赖；返回接收方只读引用。不同 owner／partition 的完整路径不是授权凭证。

需要物化到接收工作区时，先登记该工作区对源的依赖，再复制到暂存文件、校验 hash、原子发布授权导入文件；文件写在事务外，可读引用发布在事务内。副本继续携带传递 source_scope；脚本产物无法安全拆分时按整个受影响工作区撤销，不能通过复制清除来源。

源是临时工作文件时，ArtifactRef 不延长生命周期，不自动转存最终产物。源清理后访问返回 `ARTIFACT_EXPIRED`；内容范围删除后返回 `SOURCE_SCOPE_DELETED`，不能从系统 blob 缓存恢复已撤销正文。

### 5.3 清理协议

`CleanupRequest` 为 `run_id,workspace_scope_ids[],reason:run_terminal|scope_deleted,expected_cleanup_revision,idempotency_key`，由 SystemCleanupScope 调用。`CleanupReceipt` 为 `run_id,scope_results[{workspace_scope_id,status:retained|removed|retry_pending,error?}],revocation_receipt_refs[],completed_at?`。

顺序固定：关闭新业务准入 → 收束全部写任务 → 取得每个 scope 的撤销证据 → 删除登记的全部非持久目录 → 提交 cleanup receipt。某个子 Invocation 完成不得提前清理。run.ending 是待细化的生命周期扩展，不直接复用 Operation HookContext，也不承担必要清理；未来接入仍不得延长根截止或阻止收束，见[挂点覆盖](event-bus.md#12-挂点覆盖与尚未细化的生命周期扩展)。

物理删除仅操作登记的精确 work 目录，拒绝 owner 根、系统 blob、共享资源；重复删除不存在目录视为已完成。中途失败登记 cleanup_pending 继续重试，ExecutionRecord 的终态与 cleanup_state 分开。持久 scope 标 retained，无 TTL。页面关闭、SSE 断开、机器故障不触发临时目录清理。

应用删除时机由平台或高代码调用方决定，通过 SDK 删除指定记录与 scope；不向模型开放同等权限。先撤销源和派生可读投影，再清理正文；共享资源包不是 ExecutionRecord 清理对象。

## 6. 基础文件工具的完整行为

### 6.1 路径、编码与版本

首版文件适配器目标为 Linux/NFSv4 POSIX，开发可用同契约的本地 POSIX。路径输入仅允许 UTF-8 `/work/...` 或相对 `/work` 的路径；读取另可接受明确获准只读挂载。拒绝 NUL、越根 `..`、设备节点、魔术链接和未授权 mount；规范化不是授权判断。

Linux 文件 driver 以授权根 dirfd 使用 openat2 的 beneath/no-magic-links 约束，禁止越界符号链接；递归 list/glob/grep 不跟随目录符号链接。写操作拒绝符号链接目标及父路径中的符号链接，防止校验后换链。开发 driver 用逐段 dirfd+O_NOFOLLOW 保持同规则；不能只检查字符串前缀。

`FileVersion` 为 `path,kind:file|directory,etag,size,sha256?,modified_at`。普通文件 etag=`sha256(canonical(kind,size,content_sha256))`；目录 etag 为排序直属条目的 `(name,kind,etag)` 的 digest；递归删除检查事务前取得的整棵树 manifest digest。

`FileSlice` 为 `path,etag,offset,end_offset,next_offset?,total_bytes,text`，offset／limit 是 UTF-8 字节；offset 非边界报错，limit 向前收敛到边界。默认 limit=16384，最大65536；检测二进制或无效 UTF-8 返回 `BINARY_FILE`，以 artifact 方式读，不能忽略解码错误。

### 6.2 方法契约

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

### 6.3 并发、原子替换与幂等

WorkspaceService 在 scope 的服务私有 `.locks` 目录使用稳定路径键锁；锁文件不在模型挂载视图内。单文件读写均取得该规范路径的 advisory lock；多路径操作先按规范 UTF-8 路径排序取锁，避免 move 对向死锁。

每个操作先取得工作区树锁：普通文件读写取共享，目录创建／删除／移动取排他；随后取得相关文件锁。文件锁只约束经服务的调用，脚本普通 POSIX 写不会自动遵守；脚本与编辑并行时仍可能产生冲突或未知结果，不能宣称全局可串行化。

原子写步骤固定：取得句柄执行域授权 → 锁内重新读现有 etag → 校验 if_match → 同目录创建 O_EXCL 临时文件 → 写完整正文 → fsync文件 → 再核对执行域许可 → rename发布 → fsync父目录 → 保存服务端 mutation receipt → 返回 FileVersion。

ETag 检查和 rename 位于同一个受撤销控制的执行域中。不能以“写前查过数据库 lease”代替 fencing；调用期间 lease 失效由 SandboxGateway 撤销域、禁止旧写入者继续访问。NFS advisory lock 在共享后端必须经验收，锁服务失效时报错，不能降为无锁覆盖。

`FileMutationCommand` 为 `mutation_id=operation_id,request_hash,workspace_scope_id,run_id,lease_epoch,kind,paths[],expected_etags[],new_content_hash?,staging_ref?`。服务内部持久 journal 含 `state:prepared|published|confirmed|unknown,receipt_ref?,observed_etags[],error?`，不是第二套 SDK Operation 权威。

重复同 mutation_id／hash 有 confirmed receipt 时直接返回原结果；不同 hash 返回 `IDEMPOTENCY_CONFLICT`。prepared／published 状态恢复先锁原路径、检查暂存及目标证据；目标已是预期内容可补交原 receipt，不覆盖它。证据不能确定，保留 unknown 并返回 `FILE_MUTATION_OUTCOME_UNKNOWN`，不得凭无 receipt 宣称未写。

未决 journal 阻止同服务后续相同路径变更，直到恢复检查结束；脚本不受此 journal 自动防重。fs.move/delete 的恢复只有已持久 receipt 或能证明源/目标身份与预先记录一致时确认，不能把“路径现在不存在”单独当作本操作成功。

文件写和 journal fsync 全在数据库事务外。SDK 提交的 RuntimeCommit 仅引用原结果／文件 receipt；Operation raw_saved 后不重复写文件。文件已改而 SDK 结果未提交仍属于可能已发生副作用，不承诺数据库与 POSIX 跨系统原子事务。

## 7. SandboxGateway 的运行与撤销驱动

### 7.1 方法与 DTO

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
| `ExecutionStatus` | `execution_id,state:accepted\|running\|exited\|terminating\|terminated\|unknown,exit_code?,signal?,stdout_ref?,stderr_ref?,started_at?,ended_at?,output_truncated:bool,error?` |
| `TerminationReceipt` | `execution_id,state:terminated\|already_exited\|unconfirmed,process_group_empty:bool,checked_at,evidence_ref` |
| `RevocationReceipt` | `receipt_id,run_id,lease_epoch,domain_results[],state:revoked\|unconfirmed,checked_at,evidence_ref` |
| `DomainRevocationResult` | `execution_domain_id,workspace_scope_id,all_processes_exited,write_handles_closed,mount_access_revoked,token_epoch_revoked,error?` |

Exec 的请求级 deadline=`min(开始时间+timeout_ms,根截止,Invocation截止)`；默认10秒可由冻结用户配置调整。没有返回码但因超时被杀不是 exit_code=0；返回 `EXEC_TIMEOUT`。stdout/stderr全量存blob，模型用各自预览和截断标记。空 env_patch 默认不继承 SDK 宿主进程环境，仅服务固定 PATH/LANG；受保护变量和凭据名不能被覆盖。

### 7.2 可实现的驱动命令

生产执行服务实现独立 `agentloom-execd`，通过 mTLS 的控制 API 接收结构化命令；SDK 宿主不在自己的进程执行用户脚本，也不给容器数据库连接。

| 驱动命令 | 输入 | 输出和失效规则 |
| --- | --- | --- |
| `PUT /v1/domains/{scope_id}/{execution_view_key}/{epoch}` | scope_ref、ExecutionViewSpec、ExecutionRecord授权证明、mount_manifest、expiry | 同(scope,execution_view_key,epoch)幂等返回domain；不同配置hash拒绝；旧epoch不能ensure复活 |
| `POST /v1/domains/{domain_id}/executions` | owner、command、deadline、command_hash | 唯一键`(domain_id,owner)`；相同命令返回原execution；必须先持久accepted意图再启动 |
| `GET /v1/executions/{id}` | 授权的run／scope查询 | 返回持久ExecutionStatus；不重启进程、不把未知当退出 |
| `POST /v1/executions/{id}/terminate` | reason、当前控制授权 | 先阻止后续输入，SIGTERM进程组，最长2秒后SIGKILL，确认整个cgroup空；不能只杀父PID |
| `POST /v1/runs/{run}/epochs/{epoch}/revoke` | 接管或停止授权、期望domain清单hash | 固定撤销所有登记域／文件网关token，核对无漏项后返回RevocationReceipt；不依赖旧Worker配合 |

driver 将 ExecCommand 编译为固定 OCI process spec：argv 直接数组传入；shell_script 作为受控临时文件并执行 `[/bin/sh,<script_path>]`，不把模型参数拼进服务自身 shell 命令。执行镜像必须包含所选解释器；OCI spec 用固定非root UID、独立 mount/PID/network namespace、只读根文件系统、只读资源、唯一/work可写挂载及有界/tmp，不挂宿主socket、服务密钥或整个NFS根。

`cwd` 必须解析为授权挂载内目录；exec 授权只允许此受限域。默认 network namespace 无外网，冻结 egress allowlist 由执行服务防火墙／出口代理强制；域名规则通过代理在请求和重定向时重新校验解析目标，不只在启动时解析一次。

服务持久保存 domain／execution 映射和租约到期时间，心跳续期由可信 SDK 执行现场提交；本地到期自动停止接受调用并撤销域。数据库 lease 失效后的旧网关token、文件句柄和MCP连接全部失效，不能仅停止调度线程而保留可写子进程。

生产写进程必须处于可由执行服务独立控制的 cgroup／挂载域；SDK Worker 失联时执行服务仍能终止该域。若沙箱执行服务所在宿主也失联、无法证明NFS挂载已失去写能力，返回 unconfirmed，阻止同可写卷新epoch的ensure；不能用网络超时或epoch数字宣称撤销成功。

新执行权领取与旧域撤销按 checkpoints.md 接管事务执行；只有 `state=revoked` 且每个domain四项撤销事实为true，才允许新epoch获得写句柄。revoke完成后旧请求即使网络恢复也返回 `ENVIRONMENT_REVOKED`，不会恢复被撤销mount。

### 7.3 环境失败与收束

- accepted 意图已保存但进程未启动：inspect返回accepted；原执行服务按同 execution_id 启动一次，不能由 SDK 生成新 ID 重开。
- 进程已结束但结果未交付：inspect取回原输出；SDK 保存 raw 后继续后置链，不再次exec。
- 进程存在但不可查询：记unknown，先收束原域；调用管道根据已识别错误和重试策略处理，不能把unknown当作已停止。
- revoke unconfirmed：ExecutionRecord接管保留阻塞诊断，不发新写句柄；停止／清理可继续重试撤销，但不能先删除仍可被写入的目录。
- 只读查看也需要授权；同路径不意味着不同epoch可以复用旧handle。成功 revoke 不承诺撤销已经完成的远端业务副作用。

## 8. 跨模块提交目录

所有 blob／文件／网络操作在事务外；以下“写”均指在 RuntimeCommit 内发布已验证引用和状态。

| 命令 | 事务读取与版本条件 | 一致写集与幂等键 |
| --- | --- | --- |
| `prepare_accept_model` 生成写集 | completed effective响应、原context revision、未接受operation | assistant有效消息、实际调用来源；`model:<operation>:accept`；与ticket.consume、batch或候选阶段同提交 |
| `prepare_batch_delivery` 生成写集 | batch全部结算、ordinal不变、父context／task revision | 原反馈槽、完整tool组、batch consumed、phase；`batch:<batch>:deliver` |
| 输出修正 | 候选关联原model operation、validation hash | 格式反馈和下一阶段；`format:<operation>:feedback`；不发新的隐式Attempt |
| 模型raw/effective | Attempt／Operation revision、原request hash、当前执行许可 | raw／effective引用、Hook状态、最终响应；模型assistant追加留给接受决策提交 |
| workspace登记 | 可信owner固定根、partition／ExecutionRecord关系、调用方固定的环境配置 | registration、mount_intent；`workspace:<owner>:<partition>:<run>` |
| 文件结果 | 原Operation／Attempt及gateway receipt关联、版本 | raw FileVersion／错误引用；`raw:<attempt>`；不在事务里做rename |
| artifact发布 | 源etag、grant及source_scope仍有效、目标scope归属 | ArtifactRef、grant、来源依赖、可读引用；`artifact:<operation>:<ordinal>` |
| 环境撤销完成 | 根控制／接管身份、预期domain清单 | revocation receipt引用、接管屏障或清理许可；`revoke:<run>:<epoch>` |
| scope清理 | 已有撤销证据、准确登记路径、cleanup revision | scope cleanup_state、重试任务或receipt；`cleanup:<run>:<scope>` |

事务提交回执前不能唤醒成功等待者、执行工具或发下一个模型请求。必要写失败返回 `PERSISTENCE_UNAVAILABLE`；lease失效返回 `LEASE_LOST`，旧worker只能停止，不能自行把本地成功结果推成新head。

## 9. 文件样例与能力验收断言

### 9.1 文件冲突和租约撤销

Planner／Executor E1、E2共享/work。E1和E2都读取report.md的etag=e0；E1 fs.edit提交e1，E2带e0写入得到FILE_VERSION_CONFLICT，文件保持e1，模型收到冲突后重新读取。两个调用各自有Operation，不按参数相同合并。

Worker A epoch7写进程失联，Worker B准备epoch8。执行服务若不能确认epoch7挂载撤销，revoke返回unconfirmed，B没有写句柄；确认全部进程空、挂载失效、token撤销后才准许epoch8。最后整个ExecutionRecord收束，非持久scope的全部产物删除，系统历史／blob仍保留。

### 9.2 必测断言

上下文和压缩断言见[上下文验收表](context.md#7-上下文验收断言)；编号保留以便交叉追踪。

| 编号／注入点 | 必须断言 |
| --- | --- |
| C07 首输出边界 | 心跳／空delta／usage不计；工具参数非空计；空完整终帧计送达但has_display_content=false |
| C09 streaming retry | Attempt1 partial与Attempt2正文不拼接；部分tool arguments从不执行 |
| C10 接受模型重复通知 | effective保存不提前append；accept+ticket.consume+batch只提交一次 |
| C11 批次两结果逆序完成 | 父消息按ordinal；未全部结算无下一请求；重复delivery不重复反馈 |
| C15 raw后故障 | 摘要／模型／工具均只做后置处理；不再次执行能力 |
| C18 最后一次格式修正 | 正常完成可成功；需要下一票据才耗尽；没有独立修正额度 |
| C19 大结果 | raw全量hash一致，预览范围准确；result.read新Operation；未读正文不自动注入 |
| C20 Skill包使用边界 | 拒绝包内越界路径、符号链接逃逸或manifest不符；SDK不执行包的导入／安装脚本；Git获取与导入校验由平台验收 |
| C21 Skill权限 | allowed-tools不能扩大角色目录；model/context:fork拒绝；read不启动脚本 |
| C22 MCP差错 | isError不是transport；unknown文字不重试；sampling返回不支持；目录变更不热切换 |
| C23 外层文件工具 | 无AgentSession仍解析Workflow scope；输入伪造owner／宿主路径被拒绝 |
| C24 工作区隔离 | read/glob/grep、符号链接、脚本、文件MCP均不能跨Session／Invocation |
| C25 并发edit | 两请求同etag至多一个写成功，另一conflict；结果不由完成速度选择 |
| C26 文件崩溃窗口 | fsync后rename前／rename后receipt前均恢复原mutation；证据不足unknown，不假成功 |
| C27 NFS失联 | 不能降为无锁写；无撤销证据不发新epoch写句柄；旧句柄恢复联网仍被拒绝 |
| C28 子任务完成 | 临时产物仍可被授权父读取；根ExecutionRecord收束前不清理 |
| C29 临时卷清理 |全部登记非持久scope删除，持久scope保留，系统上下文不受影响 |
| C31 同scope不同角色Skill | /work读写共享；execution_view_key不同，各自只能读获授权资源；revoke枚举所有视图 |

这些测试使用假模型流、假MCP服务、可故障注入BlobStore及本地／NFS文件driver；验收必须检查持久身份、hash、事务回执和执行次数，不能只检查最终文本是否相似。
