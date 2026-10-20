# agentloom-platform

更新：2026-10-20。Platform 是基于 **LangChain + LangGraph** 的低代码产品，独立于 agentloom-sdk。首版面向团队私有部署，支持多人、空间权限和应用发布，前后端独立开发、构建与部署。已提供首个可运行开发版本，包括正式 Vue 页面、FastAPI、独立 Worker 和部署配置；实现范围与未完成项目见[当前实现](implementation.md)，启动方式见[工程说明](../../agentloom-platform/README.md)。

## 1. 平台的工作

```text
空间管理员维护模型、Tool、Skill、MCP 和环境
  → 编辑者创建应用，填写名称、选择 ReAct / Plan、填写描述
  → 绑定各角色资源，配置工作空间，校验并发布
  → API 保存用户输入与运行任务
  → Worker 按固定发布装配 LangChain Agent / LangGraph 图
  → 图保存检查点，平台保存运行回执和公开事件
  → 成员在自己的会话中查看结果
```

| 层 | 责任 |
| --- | --- |
| Web | 独立 Vue + TypeScript SPA；资源管理、配置、发布和会话页面 |
| Platform API | 身份、空间、应用、资源、发布、幂等输入、运行控制、HTTP 与 SSE |
| ExecutionHost / Worker | 任务派发、单写者、输入收件箱、停止、恢复与清理 |
| LangChain | 模型集成、工具、ReAct Agent、中间件扩展 |
| LangGraph | Plan 执行图、子图状态、检查点和按原图接续 |
| PostgreSQL / 环境服务 | 管理与运行记录、图检查点，以及独立的资源包、工作文件和沙箱 |

平台管理运行的队列和控制事实，图检查点保存内部执行现场。LangGraph 的 checkpointer 不替代任务队列、空间权限、外部操作幂等或沙箱服务。详细责任见[运行接入](runtime-integration.md)。

## 2. 阅读顺序

1. [当前实现](implementation.md)与[团队私有部署实施方案](implementation-plan.md)：区分已经接通的功能、目标设计及阶段验收。
2. [首版交互设计](interaction-design.md)：页面、资源添加与绑定、配置、会话及异常反馈。
3. [能力类型与资源接入](capabilities.md)：Tool、Skill、MCP、Toolkit、子 Agent、沙箱和中间件。
4. [领域对象](domain-model.md)、[配置与发布](configuration.md)：所有权、版本、授权及固定快照。
5. [LangChain / LangGraph 运行接入](runtime-integration.md)：ReAct、Plan、输入、停止、检查点与恢复。
6. [HTTP API](api.md)、[存储与生命周期](storage-lifecycle.md)：公开协议、持久事实、发布与删除。
7. [平台验收](verification.md)：真实接口、进程故障和权限验证。

## 3. 默认产品约束

应用属于空间，会话属于创建者并固定绑定一个应用。创建表单同时填写名称、必选 ReAct／Plan 和可选描述；模式不预选，创建后不能改变。草稿不能运行。ReAct 没有 Executor 配置；Plan 按 Planner 和专业 Executor 分别配置 Prompt 与能力，通用默认执行器关闭。

Tool、Skill、MCP 各有独立左侧管理页，先添加资源再绑定。文件工作空间、执行沙箱和文件保留策略独立配置；Skill 含脚本不自动获得执行权。

发布停止使用旧配置的相关活动 Run；同一会话下一次新提问使用新发布，旧 Run 的恢复始终使用原图、原资源和原快照。用户没有暂停／恢复按钮，故障接续由平台调度和 LangGraph 检查点共同完成。

执行策略由平台解析并随发布固定，不再引用 SDK 默认值。SDK 的独立代码和设计仍保留，职责关系见[交付物边界](../sdk-platform-boundary.md)。
