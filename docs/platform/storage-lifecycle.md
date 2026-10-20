# 平台存储、发布与删除生命周期

更新：2026-10-20。平台库保存管理、输入、调度与运行控制事实；LangGraph checkpointer 保存图内部状态。首版使用同一 PostgreSQL 实例中的隔离 schema 和独立 repository，不直接对 checkpointer 私有表做 ORM cascade，也不假定两个提交共用事务。

## 1. 平台逻辑表

| 表／集合 | 关键字段 | 唯一性与职责 |
| --- | --- | --- |
| users / memberships | actor_id、space_id、role | 成员关系唯一，角色修改可审计 |
| applications / application_drafts | app_id、mode、file_root_id、gate_revision、revision | 模式固定；草稿 CAS；接入 gate |
| application_releases | release_id、app_id、version、snapshot_ref、implementation_manifest | 同应用版本唯一，固定内容与图版本 |
| resources / resource_versions | resource_id、space_id、kind、version、content_ref、digest、status | 固定就绪资源，不随应用级联删除 |
| credentials | credential_id、密文／外部引用、授权策略 | 不进入检查点或前端 |
| session_bindings | session_id、app_id、owner_actor_id、submission_seq、active_run_id | 私有会话；串行路由；仅一个活动／预留 Run |
| runs | run_id、session_id、release_id、graph_thread_id、snapshot_ref、deadline_at | graph_thread_id 唯一；执行身份及公开结果 |
| execution_leases | run_id、owner、generation、lease_until、revocation_state | 单写者；失效后先撤销旧写者再接管 |
| input_submissions / input_deliveries | input_id、session_id、client_message_id、seq、body_ref、target_run_id、delivery_state | 同会话 client_message_id 唯一；原输入交接不复制 |
| dispatch_intents / stop_commands | task_id／command_id、run_id、status、attempt、claim_generation | 执行与控制分开扫描；至少一次投递 |
| operation_ledger | operation_id、run_id、task_id、args_hash、effect_policy、receipt_ref | 对账外部副作用；未知结果不能盲目重放 |
| run_events | run_id、seq、source_key、public_payload | Run 内 seq、来源键分别唯一，供 SSE 续读 |
| request_receipts | actor_id、space_id、idempotency_key、request_hash、result_ref | HTTP 重试返回原回执 |
| release_jobs / deletion_jobs / cleanup_jobs | job_id、phase、pending_refs、error | 分阶段、可恢复；清理不覆盖业务终态 |
| runtime_bindings / workspace_scopes | binding_id、存储引用、执行域、file_root_id、run_id、generation | 后端管理的环境和文件身份 |
| admission_tombstones | run_id／app_id、closed_at、reason | 删除后仍阻止旧任务重新创建现场 |

图状态、checkpoint_ns、pending writes 等由所选 PostgreSQL checkpointer 管理，迁移及清理通过它的公开合同验证。平台 RunRecord 是队列与生命周期事实，图检查点是恢复现场；业务终态必须依据持久图结果或经核实的停止事实写入。

InputDelivery 的 accepted／delivered 依据图内持久标记对账。Session 的正式用户输入和公开答案是跨轮历史来源，不复制私有图消息作为另一套可写现场。身份和输入协议见[运行接入](runtime-integration.md)。

## 2. 发布顺序

1. 校验草稿及资源授权，生成候选快照、实现清单和资源依赖。
2. 平台事务切 gate 为 publishing，保存 ReleaseJob；暂停新输入与新 Run，保留停止和历史查询。
3. 覆盖所有旧版本 Run，包括尚未启动的预留 Run；保存准入关闭事实，取消未启动任务，对活动 Run 发停止命令。Worker 在领取和外部动作边界重新检查控制状态。
4. 同机 Supervisor 确认停止及旧执行域撤销，等待必要文件清理；没有业务 Worker 时由控制／维护入口处理。未知远端副作用保留诊断，不伪造收束完成。
5. 同一发布任务与 gate_revision 核对成功后，在短事务中激活候选版本并开放接入。
6. 同一 Session 下次新提问使用新发布及新 graph_thread_id；旧任务接续始终用原快照。

不持数据库行锁等待模型、MCP、进程或沙箱。失败保留任务与候选快照，不提前显示发布成功。后续引入跨应用依赖时，须扩展发布扫描至完整依赖闭包后才能开放 Workflow。

## 3. 删除顺序

以下是首版完整删除目标，尚未实现；原型保持入口禁用。必须验证选定 checkpointer 的 thread／子图清理、平台待交接输入及环境清理后再开放。

1. 标记 deleting，关闭输入和任务准入，保存专属数据清单及墓碑。
2. 停止所有活动或预留 Run，确认原写者、子进程及沙箱已撤销。
3. 关闭展示订阅，通过 RuntimeGateway 调用受支持的清理接口删除所属图 thread、子图命名空间及辅助状态；不能清空整个 Saver。
4. 删除正式输入、待下轮交接、公开历史和运行关联；按范围清理独占工作区、产物及资源缓存。
5. 最后删除应用发布与配置，保留防迟到启动的最小墓碑、删除回执和规定的审计；共享资源仅解除引用。
6. 各阶段幂等、失败可重试，未完成不得报告完全删除。

不能先删除恢复快照、密钥引用或文件挂载，再要求活动图正常结束。图清理与平台清理的进度单独保存；checkpointer 没有提供的清理范围须先补平台适配合同，不能伪造方法名。

## 4. 文件生命周期分工

WorkspaceService 分配应用固定根及隔离的会话／Run 目录，SandboxGateway 管理执行域。图运行按角色接收受限文件视图；同次 Plan 工作文件可按授权共享，Skill 包按角色只读挂载。模型 state、检查点、其他会话和凭据不挂给脚本。

retention=run 时，在整次 Run 终态且确认无写者后清理；retention=session 时，在同一会话内保留文件，应用删除时清理。原始 Skill 包和共享资源不随 Run 删除。容器进程可以提前释放；保留文件不保证保留进程或已安装软件。故障和网页断线都不是清理触发点。

## 5. 部署职责

平台负责 Worker 放置、队列、执行权、数据库连接、凭据供应、目录挂载及环境撤权。LangGraph 负责按提供的 checkpointer 保存和读取图状态，不自动提供 Worker 租约或跨机器沙箱接管。

首版同机恢复；只共享 PostgreSQL 不足以承诺跨机恢复。备份采用停写窗口，停止接入和运行推进后备份平台 schema、检查点 schema、资源包、实现清单及工作文件；恢复核对它们属于同一静止部署状态。

当前没有新产品部署脚本。文档站发布只更新说明，不部署业务后端，见[文档站维护](../documentation-site.md)。
