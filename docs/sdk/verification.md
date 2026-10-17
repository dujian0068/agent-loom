# SDK 实施顺序与验收

状态：待实施测试计划。文档构建、UML 校验不等于运行测试通过。SDK 测试不得依赖平台用户、应用、发布表或 HTTP 服务。

## 1. 实施顺序

| 阶段 | 交付 | 门槛 |
| --- | --- | --- |
| S1 对象与公共端口 | Agent、Node、Definition、作用域、事件、RuntimeStore 协议与内存适配 | 不导入 Platform；接口实现、对象归属和唯一写入者明确 |
| S2 调用基础 | EventBus、HandlerRegistry、EventHandler、HookHandler、Attempt | 请求恰好路由一个 Handler；拒绝、失败放行、结果等待通过 |
| S3 Agent 执行 | ReactAgent、AgentNode 及角色绑定、AgentContext、批次、LoopTicket | 三种确定性场景、额度与串并行边界通过 |
| S4 上下文和文件 | 历史、补充、压缩、SDK 工具、受限执行环境 | 不丢用户输入，Hook 修改持续可见，主子共享且跨 Session 隔离 |
| S5 持久恢复 | 原子存储、执行权、快照增量、父子交接 | 独立进程故障注入，旧 writer 撤权，终态与后继一致 |
| S6 Workflow | 源码迁入、编译／状态／reducer、AgentNode、Saver adapter | 原生语义与 SDK 原子提交适配测试通过 |
| S7 独立交付 | 安装包、三个示例、可用适配器清单 | 安装 SDK 即可执行；持久恢复须用明确支持的后端验证 |

默认工具与后端可替换性的新增验收见[工具 SPI01–SPI10](builtin-tools-spi.md#10-后续实现验收)，归入 S2／S4／S5。该组用例尚待实施；本地文件测试不替代共享文件系统、对象存储及真实脚本挂载的合同验证。

## 2. 对象与执行关系测试

| 编号 | 场景 | 必须断言 |
| --- | --- | --- |
| SDK01 | ReAct 直接回答／多轮工具 | 一 Graph、一 React Task、同 Context；工具只有 Operation |
| SDK02 | Planner 同类型两次委派 | 同图三个 Task、三个 ReactAgent、三个私有 AgentContext；同类 Executor 只复用配置，不复用活动对象 |
| SDK03 | 无 Executor | Planner 可调用授权工具；不出现虚构默认执行器 |
| SDK04 | parallel=false，混合工具与委派 | 按原 ordinal 逐项完成；慢后置 Hook 也不能越过串行顺序 |
| SDK05 | parallel=true，max=2，三个 Executor | 最多两个活跃子任务；已结束子任务释放槽位，未完成结果 Hook 仍须等待后再结算 |
| SDK06 | 同批单项失败 | 其他继续；全批结算一次反馈，不按完成先后逐项 ReAct |
| SDK07 | 重入 begin、模型重新提出相同参数 | 前者复用 Operation；后者新 Operation；不同语义不合并 |
| SDK08 | 第 100 张票据仍 reserved | 等待结算，不误报耗尽；第 100 次可成功，第 101 次不能出网 |
| SDK09 | SDK 自动重试、格式纠正、压缩 | 尝试不重复计票；模型纠正正常计票；压缩不计业务票 |
| SDK10 | 外层 AgentNode 调 Plan | 一个根 ExecutionRecord、外图与内图分离；Executor 仍在内图；私有 Context 不进入 GraphState |
| SDK11 | 纯能力 Workflow | 可以没有 AgentInvocation／AgentContext；不造假上下文 |
| SDK12 | 同一绑定在不同 ExecutionRecord 并行 | 节点定义／工厂可复用，活动 AgentNode／ReactAgent 按 Task 隔离；ExecutionRunner、总线、Task scope 不串用 |
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
| SDK29 | StateGraph 编译纯工具图 | 返回 Workflow；不要求模型、AgentDefinition 或 AgentContext，不因复用 RunHandle 继承 Agent |
| SDK30 | AgentNode 绑定内置 Agent／只实现公共方法的第三方 Agent | 前者走内部调用桥、同根 ExecutionRecord；后者编译时报 ConfigurationError，不偷偷调用 start 新建根 ExecutionRecord |
| SDK31 | 同一 RuntimeEngine 并发执行两个不同 run_id，其中一个关闭观察或终结 | 两个 ExecutionRecord、两个有效 ExecutionRunner；总线、Handler 活动索引、预算和上下文隔离，共享客户端不被其中一次收尾关闭 |
| SDK32 | 旧 Runner 失权后重建并接管原 run_id | ExecutionRecord 与已提交 Task／Operation 身份不变，新建 ExecutionRunner／总线／激活端口；旧句柄或代理不能继续以旧 epoch 写入，终态记录只读附着不创建 Runner |
| SDK33 | AgentNode 作为 Agent 调用，再作为 Node 在图内激活 | 公共入口只转发一次；图内入口继续原任务，不新增根记录、不重复模型／工具 Hook |
| SDK34 | 同一 Agent 实例绑定另一 run_id，另案同 ID 重投 | 前者返回 AGENT_INSTANCE_BOUND；后者附着原执行。省略 ID 不得意外重用旧结果或开启新执行 |
| SDK35 | 任务等待后重激活，或迁移到另一机器 | 重建 NodeContext 和当前有效的服务代理，沿用原 task/context/continuation；不恢复旧 lease、锁、客户端或协程 |
| SDK36 | Hook 修改已提交，Agent 持有旧消息缓存 | 下一次模型物化读到有效新修订；节点／Agent 缓存不能覆盖已提交修改；检查点不含 NodeContext |
| SDK37 | Workflow 同一 PreparedAgentRef 并发调用两次 | 共用冻结配置与服务，创建两个独立活动 Agent 及上下文；节点绑定不能捕获编译时活动状态 |
| SDK38 | 同一份 AgentDefinition 独立构造 ReactAgent，再绑定为 Plan 的 Executor | 无须转换另一种角色配置；Prompt、格式和能力选择一致，策略来自各自完整 Agent 装配；两个执行的实例及上下文独立 |
| SDK39 | Plan 的同一个 Executor key 被委派两次 | 两个 ReactAgent 读取同一份冻结 AgentDefinition，各有 Context；共用原模型、根 Loop 额度、总截止与上下文策略，不新增根执行或图 |
| SDK40 | PlanAgent.definition 与 Executor 定义配置不同 Prompt、格式和能力 | Planner 使用 definition，Executor 使用 executors[key]；公开输入输出使用 Planner 格式，能力不合并，不拼接 Planner Prompt |
| SDK41 | Plan 传入活动 Agent／嵌套 Plan，或无效、保留 Executor key | 构造阶段拒绝，未创建执行身份；AgentDefinition 不接受 mode、角色列表、模型或策略覆盖字段 |
| SDK42 | 同一定义用于两个不同 Executor key，Hook 仅匹配其中一个 | NodeBinding 分别生成可信 role_key 与授权视图；只命中对应位置，显示名与定义复用不混淆 Hook、调用和恢复身份 |
| SDK43 | 同批调用 Skill 正文读取、Skill 脚本和 MCP | Skill 读取只路由 SkillHandler；脚本及 MCP 路由 ToolHandler；每个模型 call_id 仅对应一个业务 Operation，不因子调用重复登记 |
| SDK44 | 配置 Skill 包，尝试读取未授权包或在未配置 exec 时执行其脚本 | 目录和入口仅含当前角色授权资源；越界读取和 exec 拒绝；读取 SKILL.md 不启动脚本、Hook 或新 NodeTask |
| SDK45 | skill.read 的 before 被拒绝，或 after 修改已提交 | 拒绝前 Loader 读取次数为零；有效文本修改进入原 Agent 后续上下文与恢复视图，原包和 raw 不变 |
| SDK46 | Skill 原始结果保存后崩溃，再恢复；或保存前资源被替换 | 已保存结果不重读、不重复注入；尚未保存只能读原 manifest，资源缺失／不匹配明确失败，不回查平台最新包 |
| SDK47 | 两个 Executor 共用 SkillPackage 和工作文件，能力选择不同 | SkillLoader 可复用但每次校验独立 scope；只读 Skill 不可写，脚本产物进入获准 /work，结果和消息不串入其他 Executor |
| SDK48 | Executor／完整Agent前置放行后，在纯准备、原子启动前后分别故障 | 提交前target/permit均无，可重做纯准备；提交后target/permit/父关联/ready同时存在，只激活原目标；不得先T_FREEZE |
| SDK49 | grant前停止、StageResult或上下文版本冲突 | StartExecutor／StartChild拒绝提交，无子任务和许可；恢复不靠缺target补建 |
| SDK50 | 终态提交后清理前宕机，另案清理租约过期时双worker争抢 | CleanupService接管原Job，不建Runner／不调用模型；只当前清理epoch可提交，不改变terminal_seq和业务结果 |
| SDK51 | 目录已删除但清理回执未知；另有持久scope和新Run目录 | 重查同job/scope精确目录并完成；持久scope retained、新Run目录不变；无假成功或扩大删除 |
| SDK52 | request_many重读已提交模型批次；另案无模型来源节点／原始空列表 | 前者复用原Batch/Operation/decision票据，顺序不变；后者明确拒绝，不造模型票据或空批次；Workflow用原生分派 |
| SDK53 | Skill声明allowed-tools或disallowed-tools；另案读取两个普通Skill | 前者构造报SKILL_PROFILE_UNSUPPORTED且平台不删字段假兼容；后者不更改角色工具目录 |
| SDK54 | Skill原串含连续空格/引号，与等价参数数组分别读取；重入和Hook改参 | 原串精确保留、数组规范渲染、位置参数确定；两输入hash不同；双字段非null拒绝；已存raw恢复不重新渲染 |
| SDK55 | start时故意阻塞claim／恢复屏障；另案已有有效owner | start在接收和推进意图已保存后先返回句柄；已有owner只附着，不报busy、不等待恢复 |
| SDK56 | 按UML关联纯工具Workflow及Workflow调用Plan | 外图invocation_id为空；内图恰一Invocation；不能为满足错误基数创建虚构Invocation |

## 3. Hook 与 EventBus 测试

接口和组合注册关系必须先通过以下验收；这些是待实施用例，文档构建不能替代运行验证。

| 场景 | 必须断言 |
| --- | --- |
| 接口实现 | EventBusImpl 可作为 EventBus、HandlerRegistry；HookHandler 可作为三接口；末端子 Hook 只要求 EventHandler |
| 本地注册隔离 | 两个组合处理器注册同名子项，resolve 只返回各自表；不自动回退根表；根业务键重复拒绝 |
| 顺序与有效修改 | 第一个子 Hook 修改已提交后，第二个读到新版本；最终 StageResult 保留累计效果，合法 deny 后续计数为零 |
| 转发与递归 | HookHandler.handle 只委托本地 request；Loop发出的前后 CallStageEvent 不新增 Operation／Attempt／LoopTicket，也不再触发自身 Hook |
| 恢复与重入 | 子 Hook applied 后宕机，以同 Operation 与游标接续；重复阶段 request 不重复执行已完成项，raw_saved 后不重调业务工具 |
| 类型与授权 | 根发送代理无注册入口；伪造阶段事件拒绝；错误输入／响应合同和循环注册在 freeze 前报错，失败不半冻结 |

增加事件归属断言：

| 场景 | 必须断言 |
| --- | --- |
| 发出者 | model.generate.before／after 的 source 是原 Loop；ModelHandler／ToolHandler 不调用 Hook、不发本次外围阶段事件；总线不能自行补发 |
| 阶段屏障 | 阻塞 before 处理器，Adapter 调用次数保持 0；允许后才执行，拒绝后仍为 0 |
| 无用户 Hook | 空链返回已提交原视图，正常继续；Loop 代码没有 has_hooks 或 HookHandler 分支 |
| 有效修改 | before 修改进入实际请求；after 修改进入本次交付、后续上下文及恢复读取；raw 保持原样 |
| 流式与重试 | 多个 delta 不增加阶段事件数；同 Operation 的自动重试不重复已完成 before；最终 raw 选定后才发一次逻辑 after |
| 恢复 | raw_saved 后崩溃，恢复由原发起方续 after；StageResult 已提交但回执丢失时查回原结果，Adapter 不再调用 |
| 并行成员 | 每个成员独立 before／after；其中一个完成不触发下一模型 Loop，全批 settled 才交付 |

请求只有一个 Handler，观察者不得响应请求；空 Hook 链也保存 Operation 事实。前置 deny 不创建 Attempt；后置 deny 保留执行事实。Hook 异常 fail-open 与必要存储失败严格分开。

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
| graph调用纯准备后、StartExecutor／StartChild前后 | 提交前重做纯准备，提交后续原target；permit与目标不可分离 | 为取得启动计划先提交无目标permit |
| 业务终态后清理未完成 | 用CleanupLease继续原Job，按清理白名单提交 | 重新领取业务lease、激活Agent、改写原答案或重复终态事件 |

## 5. 交付示例及实际验证

三个示例均须只安装 SDK：ReAct 调工具、Plan 两个隔离 Executor、Workflow 组合 Agent 与能力。先用确定模型适配器断言状态与调用次数，再以真实模型检查协议链路。故障恢复采用共享持久后端和两个独立进程；内存演示不能作为跨机恢复证据。

报告必须写明 commit、运行环境、适配器、输入、关键 ID、故障位置、断言和真实结果。未运行项标记未执行。平台权限、发布及 HTTP 路由测试在[平台验收](../platform/verification.md)，不塞入 SDK 单元测试。

## 6. 文档合同门槛

进入实现前仍须补齐 [整理复核记录](../reorganization-review.md#3-仍未完成的详细设计)列出的合同：SDK Schema、公开查询／删除及扩展端口机器合同、Workflow compile／Saver／初始及追加输入、生命周期 Hook。直接 Agent 的构造／调用和注册 DTO 已在[装配](assembly.md)与[公共接口](public-api.md)细化。上述测试是待执行验收，不表示这些接口已有可运行代码。实现文档检查至少覆盖 Node 激活签名、OperationError 枚举、PreparedModelRequest 字段、序号初值、修正计票和公开重入语义的一致性。
