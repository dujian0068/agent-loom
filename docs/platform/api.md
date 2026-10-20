# Platform HTTP API 与接入协议

更新：2026-10-20。以下保留目标协议。首个开发版本已实现管理、认证、输入、控制和公开投影，实际路径及字段以运行中的 `/api/v1/openapi.json` 为准；异步导入、活动补充、分页和删除等目标接口尚未全部实现，见[当前实现](implementation.md)。Worker 通过[LangChain / LangGraph 运行适配](runtime-integration.md)执行，不向前端提供 SDK 或图内部接口。

## 1. 通用协议

使用 `/api/v1` 前缀，JSON 为 snake_case，拒绝未知字段。列表返回 `{items,next_cursor}`，limit 默认 50、范围 1..200；正文最大 2 MiB，未来文件上传使用独立接口。

前后端独立构建部署；API 不托管 SPA，未知 API 返回 JSON 404，前端深层路由由 Web 回退。OpenAPI 和 SSE Schema 是协作边界，见[前后端分离约束](implementation-plan.md#前后端分离约束)。

创建和变更请求要求 Idempotency-Key，草稿修改／发布另要求 If-Match。同键不同正文返回 409 IDEMPOTENCY_CONFLICT；摘要覆盖方法、目标和业务字段。凭据用专用密钥 HMAC 参与摘要，保留 key_id，不保存明文或可离线猜测的裸密钥 hash。HTTP 幂等与运行输入去重分别验证。

身份和空间权限在服务端检查，不可见对象返回 404。客户端不得提交 graph_thread_id、checkpoint_id、owner、generation 或强制运行状态。公开 run_id 本身不赋予访问权。

## 2. 管理接口

| 方法与路径 | 输入／条件 | 结果与责任 |
| --- | --- | --- |
| POST /spaces/{s}/agents | name、description?、必选 mode | 201；模式无默认值，分配文件根与初始草稿 |
| GET /agents/{a} | 查看权限 | AgentView、draft ETag |
| PUT /agents/{a}/draft | 全量可编辑字段、If-Match | 保存新 revision，mode 不可改 |
| POST /agents/{a}/validate | draft_revision | ValidationReport，不启动模型／工具 |
| POST /agents/{a}/releases | revision、If-Match | 202 ReleaseReceipt，固定候选版本 |
| GET /release-jobs/{j} | 发布权限 | 阶段、失败与重试信息 |
| GET /resources | kind、cursor | 元信息和固定版本 |
| POST /spaces/{s}/tools | 注册标识、版本、Schema、配置 | 受信 Tool 目录项 |
| POST /resources/imports | Skill 包来源或 Git source | 202 ImportReceipt，完成校验后才就绪 |
| GET /resource-imports/{j} | 资源权限 | 进度、诊断、产物版本 |
| POST /spaces/{s}/mcp-connections | transport、服务／模板、凭据引用 | 待发现连接版本 |
| POST /mcp-connections/{m}/discover | 管理权限、目标版本 | 显式发现结果，不自动授予 Agent |
| POST /mcp-connections/{m}/versions | 发现结果引用、选定成员及 Schema | 固定就绪资源版本 |
| POST /resources/{r}/versions | Tool／Skill 的配置、来源或实现引用 | 新版本；MCP 使用专用发现及版本接口，不覆盖旧发布引用 |
| GET /resources/{r}/references | 资源权限、版本 | 按角色区分草稿与发布引用 |
| POST /model-connections | provider、endpoint、能力元信息 | 连接资源 |
| PUT /model-connections/{m}/credential | secret、管理权限 | 只返回保存状态 |
| POST /model-connections/{m}/discover | 连接与授权 | 明确的模型／元信息发现结果 |
| DELETE /agents/{a} | If-Match、管理权限 | 202 DeletionReceipt；能力未就绪时明确拒绝 |
| GET /deletion-jobs/{j} | 管理权限 | 停止、图清理、输入与文件清理阶段 |

“绑定到应用”通过保存指定角色的 draft 完成，后续仍需校验和发布；不提供绕过草稿的即时授权接口。MCP discovery 不等于启用所有工具。Workflow 管理和执行接口为后续范围，首版不注册对应路由。

缺少 If-Match 返回 428，旧 revision 返回 409 REVISION_CONFLICT；配置错误 422、未认证 401、无操作权 403、基础设施暂不可用 503。错误体为 `{error:{code,message,request_id,details?,violations[]}}`，不泄露 Key、SQL 或宿主路径。

## 3. 执行接口

| 方法与路径 | 平台合同 |
| --- | --- |
| POST /agents/{a}/sessions | 建私有 SessionBinding，固定应用和用户；此时不创建图 thread |
| POST /sessions/{s}/inputs | 保存正式提交并返回 202；串行决定新提问、活动补充或待下轮归属 |
| GET /sessions/{s}/history | 正式用户输入和公开答案的分页投影，不返回内部图 messages |
| GET /sessions/{s}/inputs | 输入接入、采用和交接状态 |
| GET /runs/{r} | 平台 RunView、版本、恢复和清理诊断 |
| POST /runs/{r}/stop | 保存稳定 command_id 和准入关闭事实；返回持久回执 |
| GET /runs/{r}/events | 公开 RunEvent，支持 Last-Event-ID |
| GET /runs/{r}/tasks | 授权的 Plan 任务进度，隐藏私有上下文 |
| GET /operations/{o} | 经授权和过滤的操作状态，未知结果明确显示 |

UserInputCommand 为 client_message_id、content、variables?。同 client_message_id 重投返回原归属；初始问题验证 Schema 和变量，活动补充只支持非空文本，拒绝变量覆盖及未支持附件。Plan 补充只进入 Planner。

InputReceipt 至少包含 submission_id、input_id、session_id、run_id?、disposition、status、delivery_state。disposition 为 new_run／supplement／pending_next_run；status 为 submitting／accepted／rejected。submitting 表示平台已保存但运行侧尚未确认；accepted 依据图中持久接入标记，不能解释为模型已执行。delivery_state 使用 pending／delivered／pending_next_run，原消息的后续采用结果可查询。

转入 pending_next_run 时，旧图可能尚未接入该输入，status 可以仍为 submitting；页面优先显示“待下轮生效”，不能一直显示“正在发送”。下一 Run 持久接入后沿原 input_id 更新回执，保留此前交接记录。

StopReceipt 包含 command_id、run_id、status=requested／confirmed、observed_at；requested 不等于已停止。RunView 的 stopping 直到执行域及未决调用得到核实后才转终态。

## 4. 跨存储提交和输入竞态

1. 平台短事务保存输入、Run、DispatchIntent 与幂等回执；新 Run 固定当前发布及内部 thread。
2. 同一 Session 仅有一个活动或预留 Run，同一 Run 只允许一个图写者；Worker 不以队列重复投递创建新身份。
3. 初始输入和补充以稳定消息 ID 进入图检查点；检查点先写、平台回执后写，通过对账处理崩溃窗口。
4. 活动补充由唯一写者在模型决策边界消费，不并发调用同一 thread。结束事务确定尚未采用的内容归属，标为待下轮，不自动启动任务。
5. 图已完成但平台终态未写时，从原结果完成对账；已发生但结果不明的外部副作用不能盲目重试。
6. stopping 期间暂不接受新的提问，返回明确冲突并保留客户端文本；终态后由用户主动发送。此前已保存的补充继续按交接合同处理。

补充、单写者和停止是平台合同，不是 LangChain 的 inputs.append／runs.stop 方法。完整实现约束见[运行接入](runtime-integration.md)。

## 5. 结果、SSE 与页面

RunView 至少包含 run_id、application_id、release_id、session_id、status、dispatch_status、recovery_state、result?、error?、usage?、deadline_at、cleanup_state、revision、last_event_seq、observed_at。缺失的供应商用量保持 null，不伪造精确值。图未接入时显示 queued／提交中，记录异常缺失时给出诊断，不退回新任务状态。

SSE id 使用平台持久 RunEvent 的单调 seq，Last-Event-ID 为排他游标；心跳不占业务序号。重复来源事件去重，终态后读取完整 RunView。上游流式回调不是可断线续读的事件存储，必须先持久化再推送。首版展示公开阶段与完整答案，不承诺 token 流式输出。

关闭网页或 SSE 不停止执行，断网后查询原 run_id；只有明确停止请求写入控制命令。清理失败不覆盖业务结果；失败的中间材料不能显示为完整成功答案。每次读取批次重新核验权限，凭据与模型内部推理不进入公开 DTO。

## 6. 不属于平台 API 的接口

不开放修改检查点、设置内部任务成功、插入工具结果、重置已用预算、抢占 owner 或重启终态 Run 的接口。用户重新提问产生新 Run，基础设施恢复继续原 Run；这些状态只能由经过验证的执行／控制服务推进。
