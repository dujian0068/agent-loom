# Platform HTTP API 与接入协议

状态：目标接口，尚未实现。平台管理、认证、调度和投影由本层负责；执行、上下文、停止收束和恢复调用 [SDK 公共接口](../sdk/public-api.md)。

## 1. 通用协议

使用 /api/v1 前缀，不保留旧原型路由。JSON 为 snake_case；请求结构拒绝未知字段。列表返回 {items,next_cursor}，limit 默认 50、范围 1..200；请求正文最大 2 MiB，文件另走上传接口。

创建及变更请求要求 Idempotency-Key；草稿修改／发布另要求 If-Match。相同键且正文不同返回 409 IDEMPOTENCY_CONFLICT。幂等摘要排除传输时间，但必须覆盖全部业务字段；凭据字段用服务端专用密钥 HMAC 后参与摘要，不保存或输出明文，也不把可离线猜测的裸密钥 hash 放入日志。相同幂等键提交不同 Key 必须冲突；摘要密钥轮换期间保留原回执所需的 key_id。平台 HTTP 幂等回执与 SDK input／run 幂等是两层机制，不能互相取代。

认证、Space 成员关系和权限在 API 层完成；不可见对象返回 404。公开 ID 不赋予 SDK 操作权，也不允许 HTTP 客户端提交 lease、continuation 或强制 Run 状态。

## 2. 管理接口

| 方法与路径 | 输入／条件 | 结果与责任 |
| --- | --- | --- |
| POST /spaces/{s}/agents | name、description、mode | 201 Agent；分配文件根及空草稿 |
| GET /agents/{a} | 查看权限 | AgentView 与 draft ETag |
| PUT /agents/{a}/draft | 全量可编辑字段、If-Match | 保存新 revision，mode 不可改 |
| POST /agents/{a}/validate | draft_revision | ValidationReport，不启动执行 |
| POST /agents/{a}/releases | revision、If-Match | 202 ReleaseReceipt，异步收束并切换 |
| GET /release-jobs/{j} | 发布权限 | 当前阶段、失败与重试信息 |
| POST /spaces/{s}/workflows | name、description | Workflow 草稿 |
| PUT /workflows/{w}/draft | 节点、边、Schema、绑定、If-Match | 保存图编辑配置 |
| POST /workflows/{w}/validate | revision | 编译及依赖诊断 |
| POST /workflows/{w}/releases | revision、If-Match | 固定图与完整 Agent 依赖 |
| POST /resources/imports | upload_id 或 Git source | 202 ImportReceipt |
| GET /resource-imports/{j} | 资源权限 | 进度、校验、产物版本 |
| GET /resources | kind、cursor | 元信息与可选版本 |
| POST /model-connections | provider profile、endpoint override? | 连接元信息 |
| PUT /model-connections/{m}/credential | secret | 仅返回保存状态，不回传明文 |
| POST /model-connections/{m}/discover | 连接及授权 | 已发现模型／元信息，未知项明确标记 |
| DELETE /agents/{a} | If-Match、管理权限 | 202 DeletionReceipt |
| DELETE /workflows/{w} | If-Match、管理权限 | 202 DeletionReceipt |
| GET /deletion-jobs/{j} | 管理权限 | 收束、SDK记录清理、文件清理阶段 |

缺少 If-Match 返回 428，旧 revision 返回 409 REVISION_CONFLICT；类型或配置错误返回 422；未认证 401、无操作权 403、基础设施暂不可用 503。错误体固定 {error:{code,message,request_id,details?,violations[]}}，不泄露 Key、SQL 或宿主路径。

## 3. 执行接口

| 方法与路径 | 平台处理 | SDK 调用 |
| --- | --- | --- |
| POST /agents/{a}/sessions | 建 SessionBinding，固定 Agent 和用户 | 延迟创建 SDK Session |
| POST /workflows/{w}/sessions | 固定 Workflow 及状态输入类型 | 延迟创建 SDK Session |
| POST /sessions/{s}/inputs | 保存提交意图；Agent 判定新提问或补充；Workflow 当前仅定义新 Run 输入 | ainvoke；直接 Agent 的活动补充调用 inputs.append |
| GET /sessions/{s}/history | 验证会话可见性、分页投影 | 公共历史查询 |
| GET /sessions/{s}/inputs | 返回正式输入及投递状态 | 公共输入查询 |
| GET /runs/{r} | 合并 RunBinding 的应用和 Release 标识 | runs.get |
| POST /runs/{r}/stop | 验证操作者，生成稳定 command_id | runs.stop |
| GET /runs/{r}/events | 授权公开事件投影，支持 Last-Event-ID | events.read／订阅 |
| GET /runs/{r}/tasks | 按授权展示进度 | 公共 task 查询 |
| GET /operations/{o} | 验证所属 Run／资源访问范围 | 公共 operation 查询 |

SDK 查询门面是唯一运行事实来源。平台不得为了这些 HTTP 接口查询 SDK 私有表或自己修改执行状态。

UserInputCommand 为 client_message_id、content、variables?。同 client_message_id 重投返回原归属；首次用户问题需要验证输入 Schema／变量。运行中补充不修改原 Definition 或 Prompt 变量快照。

以下补充、carried 和结束竞态针对直接 Agent 会话。Workflow 活动输入的接收点和回执未定稿，发布前必须明确不支持此入口并返回可识别诊断，不能保存为 accepted 后丢失，也不能转发到任意内层 Planner。Workflow 初始状态输入和 Agent 会话输入的具体序列化仍需在接口 Schema 中分别定义。

InputReceipt 至少包含 submission_id、input_id、session_id、run_id?、disposition、status。平台可靠保存但 SDK 尚未确认时，status=submitting；收到 SDK InputReceipt 后才标 accepted。disposition 为 new_run/supplement/pending_next_run。不能在 SDK 尚未接收时显示“模型已收到”。

## 4. 跨存储提交和输入竞态

1. 平台事务验证当前发布与权限，分配稳定 run_id、input_id，保存 RunBinding／DispatchIntent／HTTP receipt；状态 submitting。
2. Worker 装配 SDK，携稳定 ID 调用。若返回不明，先查询该 ID 或重投同一请求，不创建第二个 Run。
3. SDK 自己原子登记 Session、初始输入、Run 或补充；平台保存确认回执，运行投影从 SDK 拉取。
4. 两次并发新提问先由平台按 Session 串行决定，SDK 的活动 Run 约束再次保护跨 Worker 竞态；失败重试不会分配新身份。
5. 已知 SDK 未接收且返回 RUN_ENDED，平台把同一次用户提交路由到下一 Run；若接收结果未知，必须先确认原 input_id，不能猜测未接收而跨 Run 重投。
6. SDK 已接受但未消费的补充在终态转 carried。平台展示“下轮生效”，下一次主动提问才启动；不自己操作投递表。

显式停止后进入 stopping，新的文字登记为下一提问待投递意图；不让旧 Run 开始新的模型请求。无 Worker 时 stop 仍通过 SDK 控制门面保存有效意图。流程细则见[SDK 装配](sdk-integration.md)。

## 5. 结果、SSE 与页面

RunView 包含 platform application/release 标识，以及 SDK status、loops、deadline、result、error、cleanup_state、last_event_seq。平台投影必须注明 SDK 已确认的 revision；不能把投递成功等同于执行成功。

SSE id 使用 SDK 根 Run 的公开单调 seq；断线续读已有事件，传输心跳没有业务 seq。SDK 模型流在内部聚合，页面接收明确进度和完整调用结果；最终答案只能由已提交终态返回。

关闭网页或 SSE 不停止执行。网络断开后按原 run_id 查询；用户点击结束才调用 stop。错误终态可能没有任何模型文本；已有材料标记未完成，不能伪装成功结果。

## 6. 不属于平台 API 的接口

不暴露“设置 Loop 次数已用值”“修改 Task 状态”“更新检查点”“插入工具成功结果”“重启已结束 Run”等运行写接口。用户重新提问属于新 Run；故障接管由 SDK 判断原 Run 状态，调用方不强制某个 Executor 从头执行。
