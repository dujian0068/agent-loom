# agentloom-platform

Platform 是基于 SDK 的低代码产品。首版面向团队私有部署，支持多人、空间权限和应用发布。当前只有目录骨架，HTTP 路径及数据结构尚未实现；[团队私有部署实施方案](implementation-plan.md)基于 2026-10-19 的 SDK 公开接口收敛首版工程范围，其余章节继续描述完整目标设计。

## 1. 平台的工作

```text
用户配置
  → 管理领域保存草稿与资源引用
  → 校验并发布
  → 为请求解析完整配置和执行依赖
  → 构造 SDK Agent / Workflow
  → 调用 SDK
  → 通过 SDK 公共查询与事件展示结果
```

| 平台模块 | 负责 | 委托给 SDK |
| --- | --- | --- |
| Identity / Space | 用户、成员、访问与操作权限 | 只传已授权的执行范围 |
| Application / Release | Agent／Workflow 身份、草稿、发布和版本选择 | 构造与执行校验 |
| ResourceCatalog | 模型凭据、Skill、Tool、MCP、Hook、Prompt 资源 | 资源的实际运行 |
| AssemblyService | 解析并准备构造参数、存储和环境 | 不再回查目录的执行 |
| ExecutionHost | 请求幂等、工作分派、机器故障后重投 | ExecutionRecord 执行权、现场恢复和具体 Loop |
| RunView / SSE | 展示 SDK 状态与事件，平台关联信息 | 唯一执行终态和调用结果 |
| DeletionService | 决定应用与专属数据删除范围 | 停止、收束与执行数据清理接口 |

平台不得直接写 SDK 的任务状态、循环序号、调用结果、租约、快照或有效上下文。使用同一数据库部署不改变这条边界。

## 2. 阅读顺序

准备实施时先读[团队私有部署实施方案](implementation-plan.md)，了解部署结构、模块、事务、SDK 缺口及阶段验收。

页面和操作流程见[首版交互设计](interaction-design.md)，覆盖应用配置、发布确认、私有会话、权限及异常状态。

1. [领域对象](domain-model.md)：应用与资源属于谁、版本及删除规则。
2. [配置与发布](configuration.md)：用户配置如何变成固定运行输入。
3. [SDK 装配与接入](sdk-integration.md)：请求、补充、停止、故障调度和父子调用。
4. [HTTP API](api.md)：管理接口与执行门面。
5. [存储与生命周期](storage-lifecycle.md)：平台库、SDK 执行存储、发布／删除屏障。
6. [平台验收](verification.md)：页面之外的 API 集成和故障测试。

## 3. 默认产品约束

Agent 属于空间；同一 Session 绑定一个 Agent 应用；模式创建后不变；草稿不能运行。ReAct 没有 Executor 配置；Plan 可配置专业 Executor，通用默认执行器当前关闭。

发布会停止使用旧配置的相关活动 ExecutionRecord；下次新 ExecutionRecord 使用新发布配置。运行中和故障接管不悄悄切换版本。用户无暂停／恢复按钮，机器故障由平台调度、SDK 恢复。

能力执行和默认预算统一引用 [SDK 行为约定](../sdk/requirements.md)，不在平台复制另一套默认值。
