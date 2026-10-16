# SDK 实施顺序与验收

状态：待实施测试计划。文档构建、UML 校验不等于运行测试通过。SDK 测试不得依赖平台用户、应用、发布表或 HTTP 服务。

## 1. 实施顺序

| 阶段 | 交付 | 门槛 |
| --- | --- | --- |
| S1 对象与公共端口 | Agent、Node、Definition、作用域、事件、RuntimeStore 协议与内存适配 | 不导入 Platform；接口实现、对象归属和唯一写入者明确 |
| S2 调用基础 | EventBus、Pipeline、Manager、Hook、Attempt | 请求恰好路由一个 Handler；拒绝、失败放行、结果等待通过 |
| S3 Agent 执行 | ReactNode／PlannerNode／ExecutorNode、批次、LoopTicket | 三种确定性场景、额度与串并行边界通过 |
| S4 上下文和文件 | 历史、补充、压缩、SDK 工具、受限执行环境 | 不丢用户输入，Hook 修改持续可见，主子共享且跨 Session 隔离 |
| S5 持久恢复 | 原子存储、执行权、快照增量、父子交接 | 独立进程故障注入，旧 writer 撤权，终态与后继一致 |
| S6 Workflow | 源码迁入、编译／状态／reducer、AgentNode、Saver adapter | 原生语义与 SDK 原子提交适配测试通过 |
| S7 独立交付 | 安装包、三个示例、可用适配器清单 | 安装 SDK 即可执行；持久恢复须用明确支持的后端验证 |

## 2. 对象与执行关系测试

| 编号 | 场景 | 必须断言 |
| --- | --- | --- |
| SDK01 | ReAct 直接回答／多轮工具 | 一 Graph、一 React Task、同 Context；工具只有 Operation |
| SDK02 | Planner 同类型两次委派 | 同图三个 Task、三个私有 Context；不是三个图 |
| SDK03 | 无 Executor | Planner 可调用授权工具；不出现虚构默认执行器 |
| SDK04 | parallel=false，混合工具与委派 | 按原 ordinal 逐项完成；慢后置 Hook 也不能越过串行顺序 |
| SDK05 | parallel=true，max=2，三个 Executor | 最多两个活跃子任务；已结束子任务释放槽位，未完成结果 Hook 仍须等待后再结算 |
| SDK06 | 同批单项失败 | 其他继续；全批结算一次反馈，不按完成先后逐项 ReAct |
| SDK07 | 重入 begin、模型重新提出相同参数 | 前者复用 Operation；后者新 Operation；不同语义不合并 |
| SDK08 | 第 100 张票据仍 reserved | 等待结算，不误报耗尽；第 100 次可成功，第 101 次不能出网 |
| SDK09 | SDK 自动重试、格式纠正、压缩 | 尝试不重复计票；模型纠正正常计票；压缩不计业务票 |
| SDK10 | 外层 AgentNode 调 Plan | 一个根 ExecutionRecord、外图与内图分离；Executor 仍在内图；私有 Context 不进入 GraphState |
| SDK11 | 纯能力 Workflow | 可以没有 AgentInvocation／RoleContext；不造假上下文 |
| SDK12 | 同一绑定在不同 ExecutionRecord 并行 | Node 对象不保存可变消息；ExecutionRunner、总线、Task scope 不串用 |
| SDK13 | 完整空候选／格式不合规，再修正成功 | 接受每个完整决策只 consume 一次；反馈键固定 format；新修正新票据；不偷换为网络重试 |
| SDK14 | 第 100 次返回格式错误 | 原票据只扣一次；修正需第 101 次时预算异常；不追加总结调用 |
| SDK15 | 纯工具 Workflow 未传 session_id | 无 AgentInvocation 仍有合法 run 文件分区；不伪造会话或重复嵌套 runs 目录 |
| SDK16 | Workflow 两次调用同一 Agent，随后其中一次故障重入 | 两个独立 Invocation、input_only 上下文；恢复复用原实例，不读取既有聊天 Session |
| SDK17 | 同一 ExecutionRecord 两个 Agent 使用同名 Hook binding_key | 按各自 program_key／ExecutionBindingRef 选链；外层和内层绑定不混用 |
| SDK18 | 构造时禁止所有外部 I/O，再修改原配置 dict | 无模型／MCP／Hook／沙箱调用；已构造程序的深冻结配置不变 |
| SDK19 | 两个调用者重投同 run_id／初始输入 | 默认附着同 ExecutionRecord，仅一个 owner；不同初始内容明确冲突，不追加补充 |
| SDK20 | 新对象 start(run_id)，不传 input | 接回原检查点；不存在则 RUN_NOT_FOUND，不凭空创建问题 |
| SDK21 | 取消 ainvoke／关闭 events，再另行 wait | 只解除观察，执行继续；RuntimeEngine supervisor 保持任务引用 |
| SDK22 | stop 先于 start；已有 stopping ExecutionRecord 再次 start | 前者阻止初始化，后者允许观察／恢复收束，均不能启动新业务 |
| SDK23 | RuntimeEngine shutdown，再由新对象恢复 | 不伪造用户停止、不删除现场；注入的共享客户端不被错误关闭 |
| SDK24 | MCP 注册多工具但省略 member_key；Skill 含脚本但未绑定 exec | 构造明确拒绝歧义，脚本能力不因读 Skill 自动授权 |
| SDK25 | HookReadPort 读其他 Context 或最新版本、Hook 结束后继续读 | 拒绝越权／版本漂移／过期端口；原始元数据中没有存储客户端 |
| SDK26 | 终态事件后清理缓慢；从 terminal_seq 重连 | 答案流在固定终态序号结束；清理状态单独查询，不重发最终结果 |
| SDK27 | 相同 command_id 不同 stop 参数；接收回执未知重投 | 冲突明确，重投不双重接收，不把未知提交解释为 ExecutionRecord 失败 |
| SDK28 | ReactAgent／PlanAgent 赋给 Agent 类型，再使用统一入口 | 类型检查与调用合同一致；构造不执行，接口不暴露 Node.activate 或平台 CRUD |
| SDK29 | StateGraph 编译纯工具图 | 返回 Workflow；不要求模型、AgentDefinition 或 RoleContext，不因复用 RunHandle 继承 Agent |
| SDK30 | AgentNode 绑定内置 Agent／只实现公共方法的第三方 Agent | 前者走内部调用桥、同根 ExecutionRecord；后者编译时报 ConfigurationError，不偷偷调用 start 新建根 ExecutionRecord |
| SDK31 | 同一 RuntimeEngine 并发执行两个不同 run_id，其中一个关闭观察或终结 | 两个 ExecutionRecord、两个有效 ExecutionRunner；总线、Manager 活动索引、预算和上下文隔离，共享客户端不被其中一次收尾关闭 |
| SDK32 | 旧 Runner 失权后重建并接管原 run_id | ExecutionRecord 与已提交 Task／Operation 身份不变，新建 ExecutionRunner／总线／激活端口；旧句柄或代理不能继续以旧 epoch 写入，终态记录只读附着不创建 Runner |

## 3. Hook 与 EventBus 测试

请求只有一个 Manager，观察者不得响应请求；空 Hook 链也保存 Operation 事实。前置 deny 不创建 Attempt；后置 deny 保留执行事实。Hook 异常 fail-open 与必要存储失败严格分开。

覆盖合法 patch、越权 patch、保护输入删除、稳定 record ID、双 patch 冲突、CAS 重定位、matcher 恢复、已经 applied 不再调用、结果已保存只续提交流程。后续 Loop、历史、压缩和恢复读取同一有效修订。

快速完成先于 waiter 登记仍能读结果；重复通知不重执行；取消一个 waiter 不取消 Operation；outbox 丢通知可从游标补读，不能通过事件重放执行工具。

详细逐项断言见[调用设计](event-bus.md)和[上下文](context.md)。

## 4. 故障切点矩阵

| 宕机位置 | 恢复必须做什么 | 不得做什么 |
| --- | --- | --- |
| Operation 已登记，尚未外发 | 继续前置处理、冻结和许可 | 新增第二逻辑调用 |
| 外发已开始，无可靠结果 | 查询可用外部证据或记录 unknown，按错误策略处理 | 把超时当未执行证明 |
| raw 已保存，Hook 未完成 | 续后置链及有效提交 | 重发工具／模型请求 |
| Hook result 已保存未 applied | 校验并提交原结果 | 再跑同一 Hook |
| Task 已完成，通知未发送 | 扫描同事务后继意图 | 重跑已完成 Task |
| Executor 部分完成 | 保留完成结果，恢复未完成成员 | 把全部历史交 Planner 重新规划取代恢复 |
| parent Operation 完成未消费 | 续消费和 pending writes 屏障 | 再创建 child Invocation |
| Workflow task writes 未完整提交 | 阻止 reducer／后继推进 | 仅等后台 Future 就宣称可靠 |
| 新 owner 接管 | 递增 epoch、撤销旧 writer、再激活 | 仅靠本机锁或租约超时推断文件已安全 |
| stop 时两个子任务仍写文件 | 关闭新准入，全部收束再清理 | 第一个完成时删除共享工作目录 |
| 终态时补充未消费 | 转 carried，下一次主动提问接回 | 默默丢弃、后台自行创建新 ExecutionRecord |

## 5. 交付示例及实际验证

三个示例均须只安装 SDK：ReAct 调工具、Plan 两个隔离 Executor、Workflow 组合 Agent 与能力。先用确定模型适配器断言状态与调用次数，再以真实模型检查协议链路。故障恢复采用共享持久后端和两个独立进程；内存演示不能作为跨机恢复证据。

报告必须写明 commit、运行环境、适配器、输入、关键 ID、故障位置、断言和真实结果。未运行项标记未执行。平台权限、发布及 HTTP 路由测试在[平台验收](../platform/verification.md)，不塞入 SDK 单元测试。

## 6. 文档合同门槛

进入实现前仍须补齐 [整理复核记录](../reorganization-review.md#3-仍未完成的详细设计)列出的合同：SDK Schema、公开查询／删除及扩展端口机器合同、Workflow compile／Saver／初始及追加输入、生命周期 Hook。直接 Agent 的构造／调用和注册 DTO 已在[装配](assembly.md)与[公共接口](public-api.md)细化。上述测试是待执行验收，不表示这些接口已有可运行代码。实现文档检查至少覆盖 Node 激活签名、OperationError 枚举、PreparedModelRequest 字段、序号初值、修正计票和公开重入语义的一致性。
