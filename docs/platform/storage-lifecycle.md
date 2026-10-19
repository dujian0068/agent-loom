# 平台存储、发布与删除生命周期

平台库保存管理事实，SDK 执行存储保存运行事实。可以部署在同一个 PostgreSQL 实例的不同 schema，也可以分开部署；不建立要求 SDK 查询平台表才能恢复的外键。

## 1. 平台逻辑表

| 表／集合 | 关键字段 | 唯一性与职责 |
| --- | --- | --- |
| users / memberships | actor_id、space_id、role | 平台身份与权限 |
| applications | app_id、space_id、kind、mode?、file_root_id、lifecycle、gate_revision | Agent mode 创建后不变 |
| application_drafts | app_id、revision、config | 编辑 CAS |
| application_releases | release_id、app_id、version、snapshot_ref、resource_refs | 不可变；同应用版本唯一 |
| resources / resource_versions | resource_id、space_id、kind、version、content_ref | 共享资源，不被 Agent 级联删除 |
| credentials | credential_id、密文／外部密钥引用、访问策略 | 不进入 SDK 检查点或前端返回 |
| session_bindings | session_binding_id、app_id、owner_actor_id、sdk_session_id、runtime_namespace_ref | 同会话应用固定；仅管理关系 |
| run_bindings | sdk_run_id、session_binding_id、release_id、execution_snapshot_ref、runtime_namespace_ref、dispatch_id | 一个 SDK ExecutionRecord 对应一次逻辑请求 |
| execution_dependencies | sdk_run_id、app_id、release_id、resource_version_ref | Workflow 引用与发布／删除查找 |
| dispatch_intents | dispatch_id、sdk_run_id、input_id、kind、payload_ref、status、attempt | 至少一次投递；相同逻辑请求不换身份 |
| request_receipts | actor_id、space_id、idempotency_key、request_hash、result_ref | HTTP 重试幂等 |
| release_jobs / deletion_jobs | job_id、app_id、phase、pending_refs、error | 跨存储收束和补偿 |

平台可以缓存 SDK 状态，但缓存不承担权威恢复职责。不能把 sdk_run_id 当作跨 SDK 存储实现的数据库外键，也不能对 SDK 表执行 ORM cascade。

## 2. 发布顺序

1. 校验草稿、权限和全部资源引用；生成候选不可变快照及依赖清单。
2. 平台事务切 app gate 为 publishing，持久化 ReleaseJob。此后拒绝该应用的新 ExecutionRecord；使用该应用的 Workflow 新装配也必须遵守相同接入屏障。
3. 查询已登记 RunBinding／execution_dependencies，对所有相关活动 ExecutionRecord 发出 SDK stop。未送达的旧 DispatchIntent 标记取消；对于所有已分配 sdk_run_id 都调用 SDK stop（包括尚无 ExecutionRecord 的 ID），由 SDK admission 关闭标记挡住迟到初始化；已经送达则按正常停止收束。
4. 等待 SDK 完成收束。没有 Worker 时重建 SDK 控制入口处理停止，不能在平台写 cancelled。
5. 平台事务更新 published_release，恢复 gate。失败保留任务和明确状态；不提前将候选版本显示为可用。
6. 同一 Session 的下一次新 ExecutionRecord 使用新发布；现有恢复仍使用它原先的配置。

不持平台行锁等待网络、SDK 执行或沙箱退出。用户编辑草稿不改变正在运行的配置。

## 3. 删除顺序

以下是完整删除目标。当前 SDK 的 records.delete 只清理符合条件的单个 Run，仍缺少完整会话及待交接输入清理端口；首版正式交付前需要补齐[删除前提](implementation-plan.md#发布屏障和删除前提)，平台不能通过直接删 SDK 文件补足该接口。

1. 标记 deleting、关闭新输入和分派，记录专属资源清单。
2. 找到直接运行及 Workflow 依赖中的活动 ExecutionRecord，调用 SDK stop 并等待收束／撤权确认。
3. 关闭相关展示订阅；按 SDK 的删除端口清理已收束的运行、会话和恢复记录。
4. 清理专属文件根及持久沙箱；共享资源只解除引用。
5. 最后删除平台配置、发布和关联记录，保留完成任务需要的最小删除回执，防止迟到分派重新启动。
6. 分阶段幂等重试；某步失败时不能显示全部删除成功。

平台不能先删除恢复配置或挂载依赖，再要求 SDK 把活动 ExecutionRecord 正常收束。跨应用文件／内容引用由 execution_dependencies 保守追踪，删除前处理依赖，不能依赖 LLM 判断材料来源。

## 4. 文件生命周期分工

平台创建 Agent 应用时分配固定文件根，装配时传入 WorkspaceBinding 和 EnvironmentProvider。SDK 在 ExecutionRecord／Invocation 初始化后派生并登记 WorkspaceScope，接管沿用原 scope。平台不代建含运行身份或执行权的对象。在同一个 AgentInvocation 内 Planner 和 Executor 共享授权工作区，私有消息与检查点存储不挂给脚本修改。

持久沙箱文件暂不过期，删除应用时清理；关闭持久沙箱时，仅 ExecutionRecord 工作文件在整次执行收束后删除。系统会话／检查点、预配置 Skill 与应用资源另行管理。故障或浏览器断线不是清理触发点。

## 5. 部署职责

平台负责 Worker 放置、存储地址、凭据供应、目录挂载与公共访问。SDK 负责基于提供的存储和环境执行协议、租约与撤权检查。只共享数据库不等于具备可靠跨机恢复，文件和脚本执行域也须满足 SDK 的撤权契约。

当前仓库尚无新产品部署脚本；旧测试服务没有因文档重组被升级或清库。文档站使用独立 GitHub Pages 流程，见[文档站维护](../documentation-site.md)。
