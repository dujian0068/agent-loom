# SDK／Platform 文档整理复核

复核日期：2026-10-16。范围：重组版本 `9b98c23`，对照重组前 `16a4bd9` 的有效约定及两层现有正文、UML、接口、恢复协议。本次修改仅涉及文档，不是 SDK 或平台功能实现。

## 1. 结论

**拆分方向正确，重组后的内容还存在实质不一致，不能把目录整理完成等同于详细设计全部完成。** 本轮已修正能够直接确定的错误，补回遗漏的 Workflow 边界；尚未细化的合同集中列在第 3 节，不再用“只差实现验证”笼统带过。

以下核心分工保持成立：

| 事项 | 当前设计 |
| --- | --- |
| 依赖与交付 | Platform 单向调用独立 SDK；SDK 不查询用户、应用、发布或平台业务库 |
| 执行与恢复 | SDK 接收已解析配置和依赖；同 run_id 接续原执行位置，不要求平台恢复 Executor |
| 双引擎 | 轻量 AgentGraphEngine 和通用 WorkflowEngine 都在 SDK；集中持久化，分别保存引擎现场 |
| 执行实例 | Planner 和 Executor 是同一内层图的任务；外层 AgentNode 调用完整 Agent 才增加内层图与 Invocation |
| 能力与 Hook | Tool／MCP／Skill 操作使用统一调用管道；Hook 决定与有效上下文一致提交，观察通知不承担执行结果 |
| 平台职责 | 管理资源、凭据、权限、应用、发布、装配和 Worker；通过 SDK 公共接口查询及控制运行 |

## 2. 已修正的问题

| 问题 | 修正与权威位置 |
| --- | --- |
| 平台装配表要求传入运行期 WorkspaceScope，但此时尚无 ExecutionRecord／Invocation | 新增构造值 WorkspaceBinding；SDK 初始化后登记运行 scope。见[构造接口](sdk/public-api.md)、[环境](sdk/capabilities.md)、[平台装配](platform/sdk-integration.md) |
| UML 的 NodeDriver.advance／NodeServices 与实际 activate／NodeRuntime 协议不符；外层和内层接口继承混淆 | 统一内层签名及 SkillLoader／CapabilityAdapter 的方法；外层明确实现 WorkflowNodeAdapter，节点定义可共用，激活接口不混用。见[定义](sdk/definitions.md)、[对象 UML](sdk/object-collaboration.md) |
| 时序图让批次准备器准入、Manager 直接启动子任务、数据库负责环境撤权 | 准备、提交、调度、撤权分别回到对应责任对象；图按持久意图再执行。见[能力交互和恢复时序](sdk/object-collaboration.md) |
| 实体引用方向和数量不准确 | RunBinding 指向所用 Release；queued ExecutionRecord 可尚无图；纯工具 Workflow 可不创建 AgentGraphEngine 服务。见[平台实体](platform/domain-model.md)、[SDK 实例](sdk/definitions.md) |
| OperationError、PreparedModelRequest、revision 初值及 Session 名称跨篇不一致 | 使用唯一 DTO、统一序号规则，清除未定义的 Conversation 别名。见[调用合同](sdk/event-bus.md)、[上下文](sdk/context.md)、[检查点](sdk/checkpoints.md) |
| 格式纠正曾要求第二次消费同一 LoopTicket；反馈幂等键不同；空完成帧含义冲突 | 接受完整决策消费一次，修正另领票据；统一 format 键与 ValidationIssue。合法空候选交输出校验，不假装传输失败。见[Loop](sdk/runtime.md)、[模型适配](sdk/capabilities.md) |
| 无 Session、无 AgentInvocation 的纯工具 Workflow 没有合法工作目录规则 | 补充 run 分区；直接 Agent 仍自动建立一次性 Session；完整子 Agent 用 invocation 分区。见[文件范围](sdk/capabilities.md) |
| Workflow 产品与数据边界在整理中被压缩过度 | 补回独立草稿、状态／reducer／节点映射、局部策略和 input_only 上下文；不得隐式继承聊天 Session。见[平台配置](platform/configuration.md)、[Workflow](sdk/workflow.md) |
| 共用总线容易被误解为共用所有 Agent 的 Hook | 按可信 ExecutionBindingRef／program_key 选择固定链，再按时机匹配。见[HookManager](sdk/object-collaboration.md)、[EventBus](sdk/event-bus.md) |
| 凭据更新的幂等摘要排除了凭据，导致不同 Key 可能被当成同请求 | 用服务端 HMAC 纳入秘密字段的差异判断，明文不进入回执和日志。见[平台 API](platform/api.md) |
| 工程范围和已确认需求混写；run.ending 被写得像已有完整协议 | 区分产品默认与工程取值；生命周期 Hook 的未完成合同显式标出，必要清理不依赖它。见[行为约定](sdk/requirements.md)、[挂点覆盖](sdk/event-bus.md) |

## 3. 仍未完成的详细设计

这些是可定位的设计工作，既不是已实现功能，也不是要求重新确认已决定的产品行为。

| 项目 | 缺什么 | 完成标准 |
| --- | --- | --- |
| SDK Schema 与样例 | 旧含平台身份的 JSON Schema 已撤下，新 DTO 还没有完整机器合同 | 按 SDK／Platform 分开提供 Schema、有效／无效样例和自动验证；字段、可空性、枚举与正文一致 |
| 公共接口及装配 DTO | 直接 Agent 构造／运行入口、RunHandle、注册与 HookSet 已细化；公共查询分页／删除 DTO、扩展端口机器合同、Workflow compile 仍缺精确定义 | 完整 Python Protocol／DTO、异常、并发与生命周期语义；三个独立示例可据此实现，不能让开发者自行补接口 |
| Workflow 内核及 Saver | 已定义一致性语义；实际源码导入清单、必要依赖、逐方法映射、原生写入键及 DDL 未定稿 | 固定导入基线／许可证；明确 put、put_writes、读取、任务映射和确认屏障的参数与持久结构 |
| Workflow 输入与表达式 | 草稿及 input_only 边界已补回；初始输入回执映射、活动输入接收点／终态交接、条件与映射表达式仍缺细节 | 区分 Workflow 状态输入与直接 Agent 补充；明确定向投递、拒绝、消费及恢复，不能自动广播；冻结表达式语法和校验规则 |
| 生命周期 Hook | 调用型前后置链已详细设计；ExecutionRecord／节点生命周期 Hook 的专属输入、身份、重入与挂点目录不全 | 明确可观察／可修改／可拒绝边界，定义不带 Operation 的挂点协议；不得阻止必要清理或扩大权限 |

工程方案完成后仍需实现和故障测试。目前没有可运行的新 SDK／Platform，文档构建和图渲染都不能替代它们。

## 4. 本轮验证范围

校验项为：Markdown／站内路径、锚点、文档构建、UML 语法和渲染，以及修改过的契约名称和图文责任检查。运行验收仅更新测试矩阵，**本轮未执行 SDK、模型、数据库或测试环境的功能测试**。

本轮本地文档构建通过；站点检查覆盖 53 个 HTML 页面、1,347 个内部链接、289 个锚点和 218 个资源引用；8 个含图页面的 19 张 Mermaid 图完成渲染检查。构建仍有 Mermaid 相关大 bundle 提示，不影响本次构建成功；没有把它算成 SDK 运行验收。

详细实现验收见[SDK 测试计划](sdk/verification.md)与[平台测试计划](platform/verification.md)。部署到文档站只更新说明，不更新旧测试服务。

## 5. SDK 接口设计追加

2026-10-16，在整理复核之后继续细化[构造与绑定](sdk/assembly.md)和[公共接口](sdk/public-api.md)：

- 区分 Definition、可复用程序、Runtime 宿主、ExecutionRunner 和 RunHandle 的身份与生命周期；构造不出网，start 才准入执行。
- 固定能力注册、角色授权、HookSet／HookBinding／matcher，以及不携带存储客户端的 Hook 读取视图。
- 消除顶层与主角色的双份输入输出 Schema；直接 Agent 取主角色格式。
- 明确同 ExecutionRecord 重入默认附着、无输入恢复、稳定 ID 冲突、后台任务与观察取消，以及 Runtime shutdown 的所有权。
- 定义公开结果／异常分类、输入和停止回执、事件终态序号；同提交保存 terminal_seq，观察终结不等待无限文件清理。

本项仍是设计，没有新增 SDK 业务实现。第 3 节只缩小已经补齐的缺口，其余工作保留。本轮文档构建通过，站点检查覆盖 54 页、1,454 个内部链接、315 个锚点和 225 个资源引用；新增／关联的 3 页共 12 张 Mermaid 图渲染通过，8 段 Python 接口示例通过语法解析。未执行 SDK 业务测试。

## 6. Agent 接口与 Node 接口的职责校正

2026-10-16，按照公共 Agent 接口与图内 Node 接口分工更新设计：

- ReactAgent、PlanAgent 实现 Agent；UML 使用接口实现箭头。具体程序持有 Definition、节点绑定和运行依赖，接口不再被画成实体容器。
- 创建由具体类构造函数承担，平台 AssemblyService 选择类型；Agent 统一 start／ainvoke／astream 的使用合同。
- Workflow 保持独立可执行类型，StateGraph.compile 返回 Workflow；函数／工具工作流不需要 Agent 的模型或聊天上下文。
- ReactNode 实现内层 Node 协议，PlannerNode／ExecutorNode 继承其 Loop；完整 Agent 通过外层 AgentNode 组合接入。外层迁入内核继续由 WorkflowNodeAdapter 适配，不混用两种激活签名。
- AgentNode 嵌入须具备 SDK 内部执行绑定；仅实现公共 Agent 方法不足以保证同根执行和恢复，编译时须明确校验。

新增 SDK28—SDK30 验收项。本次文档构建及站点检查通过；5 页共 16 张 Mermaid 图渲染通过，8 段 Python 接口示例通过语法解析。以上仍为设计与文档验证，未新增或执行 SDK 业务代码。

## 7. 运行时引擎命名与职责

2026-10-16，执行宿主从 Runtime 更名为 **RuntimeEngine（运行时引擎）**，构造参数及示例变量统一使用 `runtime_engine`。第 5 节保留当时的名称作为复核历史；当前类型、UML 与调用方式以本次更新后的[装配设计](sdk/assembly.md#11-runtimeengine-管理的范围)和[公共接口](sdk/public-api.md)为准。

RuntimeEngine 组织共享基础设施，托管 Agent／Workflow 执行，装配单次现场并协调资源释放。Agent 自己的模型、Prompt、能力及 Hook 配置独立，各 ExecutionRecord 的上下文、预算、事件总线和执行进度隔离。图内调度、模型调用、文件与沙箱操作继续由对应模块负责；运行时引擎不增加第三套图调度器。

NodeRuntime、RuntimeStore、RuntimeCommit 等是不同职责的类型，Node.activate 的参数及恢复协议未随宿主更名。执行记录与本机驱动器的最终名称、作用域补充见下一节。文档没有实现新的 SDK 功能。

## 8. 执行记录、执行驱动器与对象作用域

2026-10-16，按确认采用 `ExecutionRecord` 替换原 `Run` 实体名，采用 `ExecutionRunner` 替换原 `RunRuntime` 类型名。前者是可持久保存的执行记录，后者是在本机取得执行权后创建、驱动该次执行的对象。正文、接口说明与 UML 使用同一名称，不保留两套类型别名。

协议中的 `run_id` 仍标识同一次执行，RunHandle／RunResult／RunControl、存储表和 API 路径未另行改名；它们各自是句柄、结果、控制或访问入口，不表示另一个执行实体。NodeRuntime 仍是任务激活期间的受限服务视图。

[对象关系与交互 UML](sdk/object-collaboration.md#11-作用域与生命周期的读法)增加作用域总表，并在对象图下逐项标明所属范围、生命周期及共享规则。明确同一 RuntimeEngine 可托管多次执行；每次 ExecutionRunner 独立装配总线、Manager 与引擎服务；同一执行内共享服务，但角色上下文、任务进度、调用参数与 Hook 决定各自隔离。接管重建本机对象，沿用持久身份；共享目录或适配器不扩大授权。
