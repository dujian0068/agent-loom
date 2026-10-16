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
| 平台装配表要求传入运行期 WorkspaceScope，但此时尚无 Run／Invocation | 新增构造值 WorkspaceBinding；SDK 初始化后登记运行 scope。见[构造接口](sdk/public-api.md)、[环境](sdk/capabilities.md)、[平台装配](platform/sdk-integration.md) |
| UML 的 NodeDriver.advance／NodeServices 与实际 activate／NodeRuntime 协议不符；外层和内层接口继承混淆 | 统一内层签名及 SkillLoader／CapabilityAdapter 的方法；外层明确实现 WorkflowNodeAdapter，节点定义可共用，激活接口不混用。见[定义](sdk/definitions.md)、[对象 UML](sdk/object-collaboration.md) |
| 时序图让批次准备器准入、Manager 直接启动子任务、数据库负责环境撤权 | 准备、提交、调度、撤权分别回到对应责任对象；图按持久意图再执行。见[能力交互和恢复时序](sdk/object-collaboration.md) |
| 实体引用方向和数量不准确 | RunBinding 指向所用 Release；queued Run 可尚无图；纯工具 Workflow 可不创建 AgentGraphEngine 服务。见[平台实体](platform/domain-model.md)、[SDK 实例](sdk/definitions.md) |
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
| 公共接口及装配 DTO | 部分公开方法仍以参数概览表示；HookBinding、注册描述及 Workflow 编译返回类型还需精确字段合同 | 完整 Python Protocol／DTO、异常、并发与生命周期语义；三个独立示例可据此实现，不能让开发者自行补接口 |
| Workflow 内核及 Saver | 已定义一致性语义；实际源码导入清单、必要依赖、逐方法映射、原生写入键及 DDL 未定稿 | 固定导入基线／许可证；明确 put、put_writes、读取、任务映射和确认屏障的参数与持久结构 |
| Workflow 输入与表达式 | 草稿及 input_only 边界已补回；初始输入回执映射、活动输入接收点／终态交接、条件与映射表达式仍缺细节 | 区分 Workflow 状态输入与直接 Agent 补充；明确定向投递、拒绝、消费及恢复，不能自动广播；冻结表达式语法和校验规则 |
| 生命周期 Hook | 调用型前后置链已详细设计；Run／节点生命周期 Hook 的专属输入、身份、重入与挂点目录不全 | 明确可观察／可修改／可拒绝边界，定义不带 Operation 的挂点协议；不得阻止必要清理或扩大权限 |

工程方案完成后仍需实现和故障测试。目前没有可运行的新 SDK／Platform，文档构建和图渲染都不能替代它们。

## 4. 本轮验证范围

校验项为：Markdown／站内路径、锚点、文档构建、UML 语法和渲染，以及修改过的契约名称和图文责任检查。运行验收仅更新测试矩阵，**本轮未执行 SDK、模型、数据库或测试环境的功能测试**。

本轮本地文档构建通过；站点检查覆盖 53 个 HTML 页面、1,347 个内部链接、289 个锚点和 218 个资源引用；8 个含图页面的 19 张 Mermaid 图完成渲染检查。构建仍有 Mermaid 相关大 bundle 提示，不影响本次构建成功；没有把它算成 SDK 运行验收。

详细实现验收见[SDK 测试计划](sdk/verification.md)与[平台测试计划](platform/verification.md)。部署到文档站只更新说明，不更新旧测试服务。
