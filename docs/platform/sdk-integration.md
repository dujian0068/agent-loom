# 平台如何装配和调用 SDK

这是两份交付物的唯一执行衔接方式。平台不把 SDK 当成管理服务，也不在数据库里补写执行现场。

## 1. AssemblyService

输入：已固定的 execution_snapshot_ref、运行身份、所需资源访问授权。

输出：可执行 Agent／Workflow、SDK Runtime 及受限依赖。输出只存在于本机，不序列化模型客户端、连接或协程。

| 平台配置 | 传入 SDK |
| --- | --- |
| 应用模式、主角色和 Executor 配置 | AgentDefinition、已解析 Prompt／角色能力 |
| 模型连接与凭据引用 | ModelAdapter／CredentialResolver、模型参数 |
| 工具／MCP／Skill 资源版本 | 已解析 Registry、MCP 连接配置、只读 Skill 目录／Loader |
| Hook 配置 | 有序 HookBinding 和受限处理器 |
| 上下文、执行、并行策略 | SDK Policy 值对象 |
| 应用文件根、会话和持久策略 | WorkspaceScope 与 EnvironmentProvider |
| 部署的执行存储 | RuntimeStore、ContentStore、StorageNamespace |
| Workflow 中的 Agent 引用 | 预先装配的 ExecutableAgent 或 PreparedAgentRef |

装配所有 Workflow Agent 依赖时固定所引用配置并保存依赖清单。SDK 运行中不调用平台发布查询接口，AgentNode 通过已装配程序注册表启动内层执行。

## 2. 新请求接入

1. HTTP 层认证，校验空间与应用访问权、发布状态、输入格式和幂等键。
2. 平台事务锁应用接入 gate 和 SessionBinding；生成稳定 sdk_run_id、input_id，保存原发布配置引用、RunBinding、DispatchIntent。先提交，再通知 Worker。
3. Worker 重新检查该分派未被平台屏障取消，按保存配置调用 AssemblyService。
4. 调用 SDK 执行入口。SDK 在自己的存储中幂等初始化 Run、输入和执行现场，取得执行权，再运行。
5. SDK 回执返回后平台更新分派投影；如果回执丢失，重投同 run_id/input_id，SDK 返回原运行／原结果。
6. 平台通过 SDK 事件／查询展示结果，HTTP 连接断开不撤销 Run。

两个存储不依赖跨库事务。平台 DispatchIntent 保证不会丢请求，SDK 的执行入口幂等保证不会重复初始化；dispatch_status 只表示传输，不成为运行事实。

## 3. 故障恢复

```mermaid
sequenceDiagram
    participant H as 平台ExecutionHost
    participant A as AssemblyService
    participant S as SDK执行入口
    participant C as SDK RuntimeStore
    participant E as SDK图与节点
    H->>A: 原配置快照与同一个run_id
    A-->>H: 新构造的Agent及依赖
    H->>S: invoke(input, session_id, run_id, input_id)
    S->>C: 查原Run并申请有效执行权
    C-->>S: 检查点、增量、已保存结果
    S->>E: 恢复原Planner/Executor及等待关系
    E->>C: 继续一致提交
    S-->>H: 已提交进度和终态
```

平台只处理“让哪台机器再调用 SDK”，不处理“恢复哪个 Executor”。失效判断、epoch、旧环境写权限撤销、已有结果复用和下一节点激活均由 SDK／其注入适配器完成。

有效 owner 尚在执行时，平台重复投递不能强行夺权；SDK 拒绝或附着观察。已终态的 Run 不重新开始，原输入不重复追加，额度和截止时间不重置。

## 4. 执行中补充

平台接收文本时，先根据 SessionBinding 查询 SDK 当前状态；该查询仅用于路由，不能代替 SDK 的原子检查。

- 仍在执行：以稳定 input_id 调用 SDK inputs.append。SDK 将其存为正式用户输入，并决定 pending／delivered／carried。
- 初次 append 遇到 RUN_ENDED，且 SDK 明确未接收该 input_id：平台在 SessionBinding 事务内将这次请求改为下一提问，创建新的 RunBinding；同一 HTTP 幂等键不得同时形成两次新 Run。
- 已接收补充后 Run 结束：该输入由 SDK 标为 carried，平台展示“待下次提问”；不再另发一个同内容输入，不自动开始下一 Run。
- 回执未知：先以同 input_id 重试／查询，不能先创建新 Run。SDK 的去重和终态转交决定最终归属。

Plan 的补充只交给 Planner；平台不能为了“及时生效”广播到 Executor。下一 Run 的 SDK 初始化会接入 carried 输入，平台不重新拼接历史。

## 5. 停止、发布和删除

用户停止通过 SDK runs.stop 发出幂等控制命令；没有 Worker 时也能保存控制意图，由 SDK 后续接管入口先处理控制，不能先启动业务动作。

发布／删除先在平台关闭新分派 gate，再对所有已分配 sdk_run_id 调用 stop；SDK 对尚未初始化的 ID 也持久关闭准入，防止通过旧检查的 Worker 迟到启动。RunBinding 必须在新分派通知前保存，Workflow 的依赖清单必须包含所调用应用，避免发布扫描漏掉已经派发的旧执行。

平台不持应用锁等待 SDK 收束。SDK 确认终态及环境清理后，平台完成发布切换或删除；跨存储失败通过持久任务重试，不直接修改 SDK 的状态。

## 6. 状态和事件展示

SDK 是 Run／任务／调用状态的权威源；平台保存展示缓存和访问关联。SSE 带稳定 seq，可用 Last-Event-ID 续读；重复事件去重，终态优先于迟到进度。完整模型响应可产生进度，不能当成 Run 最终答案。

平台的会话页只访问获授权的 SessionBinding；模型参数、Hook、错误等敏感诊断通过 SDK 权限投影和平台字段过滤输出，不透出凭据或原始宿主路径。

## 7. 适配器位置

通用 PostgreSQL、内存、文件与模型协议实现可以随 SDK 的可选扩展交付；只依赖平台凭据库、资源库或特定运维系统的适配放在 Platform。适配器实现 SDK Protocol，不让 SDK import Platform。

持久执行存储必须完整实现一致提交协议。给 SDK 传一个只有 save_checkpoint 的回调，无法保证上下文、操作结果、批次和检查点的一致恢复。

## 8. 验收

- 拦截平台目录查询：Agent 构造完成后，SDK 执行不再访问应用／发布表。
- 在 SDK 已接收、平台尚未收到回执处终止 Worker；重投不新增 Run 和输入。
- A 机器 Executor 执行中故障，B 使用同配置和 Run 恢复；平台没有任何针对角色阶段的分支。
- 发布与新分派并发，旧发布关联执行全部被纳入停止范围。
- SDK 存储与平台库分开运行仍能通过上述场景。
