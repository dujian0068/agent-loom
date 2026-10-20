# LangChain 能力类型与平台资源接入

更新与资料核对：2026-10-20。本页区分上游支持与本平台计划支持；平台尚无真实后端。采用 LangChain + LangGraph，不意味着下面所有集成都已实现。

## 1. Tool、Skill、MCP 分别是什么

三者处在不同层次：**Tool 是一个可调用操作，Skill 是指导 Agent 完成任务的技能包，MCP 是连接外部服务的协议。** 平台仍分别提供 Tool、Skill、MCP 管理页，先添加空间资源，再按固定版本绑定到应用角色。

| 类型 | 上游支持方式 | 示例 | 平台接入方式与范围 |
| --- | --- | --- | --- |
| 自定义 Tool | Python 函数经 `@tool` 或工具抽象提供名称、说明及参数 Schema | 工单查询、业务 API、计算 | 首版支持部署方受信注册项；UI 选择 registry_key 和版本，不上传任意 Python |
| 现成 Tool / Toolkit | 集成包提供工具；Toolkit 是一组相关工具 | 搜索、网页获取、数据库、文件、外部应用 | 按需安装并登记为 Tool 资源，逐成员授权；安装包不等于全空间自动可用 |
| MCP tools | 官方适配层把服务端工具转换为 LangChain tools | 企业系统、远端搜索服务 | 首版显式发现、固定成员及 Schema，再由 Agent 按角色选择 |
| Skill | Deep Agents 提供 SkillsMiddleware；直接用 LangChain 时须接入技能加载层 | SQL 审查规范、报告流程、文档生成方法 | 首版保留独立技能目录，以受限读取和按需加载实现；不把技能包当作一个 Python 函数 |
| 子 Agent / 子图 | 可以把子 Agent 封装成 Tool，或用 LangGraph 编排 | 研究、分析、写作 Executor | 首版用于 Plan 内部角色，独立上下文和授权；跨应用 Agent 工具后续设计 |
| 模型供应商内置工具 | 支持的模型可在供应商侧执行工具 | 联网搜索、代码解释器 | 后续按 provider／model 能力白名单开放；不能当作本地 Tool 或自建沙箱 |
| 客户端执行工具 | headless tools 通过中断／恢复交给客户端执行 | 浏览器剪贴板、用户设备文件选择 | 需独立客户端执行与回执协议，首版不开放 |

普通工具、供应商工具和客户端工具的区别见 [LangChain Tools](https://docs.langchain.com/oss/python/langchain/tools)；现成能力按[工具集成目录](https://docs.langchain.com/oss/python/integrations/tools/index)选择；子 Agent 的封装方式见[上游说明](https://docs.langchain.com/oss/python/langchain/multi-agent/subagents)。

HTTP／OpenAPI、数据库查询、向量检索、浏览器操作、文件读写和 Shell 是工具的用途或实现来源，不必各自成为一种顶层资源。检索流程也可以封装为 Tool；文档加载器、Embedding、向量库和 Memory 本身不是已授权的模型工具，上传文件不等于自动建立 RAG。

## 2. Tool 资源

平台为工具定义固定注册标识、实现版本、输入／输出约束、执行位置、超时、凭据引用和副作用属性。模型可见的参数与服务端注入的用户、空间、工作区、密钥分开。函数工具支持按实现提供同步或异步调用；I/O 工具的异步、超时和取消行为必须实测。

```text
ToolVersion
  resource_id, version, name, description
  source_kind             registered_function | integration | workspace
  registry_key, implementation_digest
  input_schema, output_schema?
  execution_location      worker | sandbox | remote_service
  credential_ref?, timeout_seconds
  effect_policy           read_only | idempotent | non_idempotent
```

这份 DTO 是平台设计，不是上游 `BaseTool` 的原始序列化格式。首版原型中的 11 项文件操作是平台准备提供的工具清单，不是 LangChain 默认自动安装的工具。文件 Tool 通过 WorkspaceService 实现；`exec` 单独登记，必须具备沙箱授权。

发布时固定实际工具名称和 Schema，拒绝同角色名称冲突及系统委派名称覆盖。运行时再校验角色授权，不能只靠“模型没有看到这个工具”阻止越权。带外部副作用的重试遵循[运行接入](runtime-integration.md#3-新请求与故障恢复)。

## 3. MCP 接入

当前官方文档使用 `langchain.mcp.MCPAdapter`；`langchain[mcp]>=1.4.0` 的该命名空间仍标为 beta。旧示例常见 `langchain-mcp-adapters`，实施时按锁定版本选择适配入口，不能混用两套示例。[LangChain MCP](https://docs.langchain.com/oss/python/langchain/mcp)

平台通过自己的 McpGateway 隔离该适配差异。连接流程为：管理员登记服务与凭据 → 显式发现 → 固定就绪版本 → 对空间开放成员 → 编辑者绑定 Agent 角色。运行装配只使用发布快照的成员清单；如需重新握手或读取 Schema，必须比对快照，发现漂移则报错，不能顺便授予新工具。

| MCP 内容 | 含义 | 首版处理 |
| --- | --- | --- |
| Tools | 服务提供的可执行操作 | 适配成角色可调用工具 |
| Resources | 服务提供的可读取内容 | 协议具备此概念，但首版不承诺独立管理和读取入口 |
| Prompts | 服务提供的提示模板 | 不自动覆盖应用 Prompt；后续需显式导入／选择合同 |

MCP 的工具、资源、提示模板是不同原语；接通 tools 不等于完整支持所有 MCP 功能。[MCP 服务端概念](https://modelcontextprotocol.io/docs/learn/server-concepts)

上游提供远端 Streamable HTTP、受控本地 stdio 等连接方式。首版优先远端 HTTP；stdio 仅允许管理员登记的受信启动模板，运行在受控执行环境，不从普通应用表单直接启动宿主命令。旧 SSE、OAuth、交互式 elicitation 等按选定客户端和服务端组合逐项验收，不把整个协议矩阵标成已兼容。

工具返回内容需要做大小限制、结果存储和公开投影；服务报告的只读／幂等提示不能代替平台执行策略。多个 MCP 连接分别授权，不能把所有发现成员直接交给每个 Agent。

## 4. Skill 接入

Deep Agents 的 SkillsMiddleware 支持逐步提供技能元信息、`SKILL.md` 正文及包内资源；它属于额外的 Agent 能力层。只安装 LangChain、创建普通 Agent 并放置一个 `skills/` 目录，不会自动得到这一行为。[Deep Agents Skills](https://docs.langchain.com/oss/python/deepagents/skills)

本平台首版确定支持以下最小技能合同：

1. Git 固定 commit 或导入固定包；校验 `SKILL.md` 的 name、description、正文、资源清单及内容摘要。
2. 按 Agent 角色生成技能目录，只向该角色提供已绑定版本的元信息。
3. 通过受限 `read_skill`／资源读取工具按需加载正文和 references／assets，保留稳定资源引用及版本。
4. 脚本只是包内容；执行须额外绑定 `exec`、开启可用沙箱，并遵守该角色文件与网络授权。

实现收在平台 SkillLoader 和 LangChain middleware 边界；可评估复用 Deep Agents SkillsMiddleware，但须验证只读挂载、角色隔离、持久状态与依赖版本后采用，不默认切换为整套 `create_deep_agent` 行为。

平台只声明上述基础 profile。出现 `allowed-tools`、`disallowed-tools`、动态工具激活等会改变执行或授权语义的扩展时，必须有明确映射，否则返回 `SKILL_PROFILE_UNSUPPORTED`；不能忽略这些字段后宣称兼容。即便以后支持 Deep Agents 的 `include_tools`，也只能从该角色已授权集合中选择，不能凭 Skill 内容扩大权限。

## 5. 沙箱、工作空间及中间件

| 配置 | 本质 | 与工具的关系 |
| --- | --- | --- |
| 执行沙箱 | 隔离命令／脚本的运行环境 | `exec` 的依赖；并非注册一个 Tool 就自动具备隔离 |
| 文件工作空间 | Agent 能访问的目录和文件权限 | 文件工具与脚本使用同一授权视图；角色 Skill 挂载单独只读 |
| 文件保留策略 | Run 结束清理或同会话保留 | 独立于是否启用沙箱，不表示保留容器进程 |
| Middleware / Hook | 模型、工具调用周围的控制或观测逻辑 | 适合预算、输入接入、授权、审计、上下文处理；不是模型主动选择的普通工具 |
| Checkpointer / Store | 图状态持久化／跨 thread 数据存储 | 不是沙箱或工作文件存储，也不自动提供空间权限 |

LangChain 提供 middleware 扩展点，Deep Agents 也有沙箱接入；平台仍需实际环境服务、授权和清理实现。[自定义 middleware](https://docs.langchain.com/oss/python/langchain/middleware/custom)、[沙箱接入](https://docs.langchain.com/oss/python/deepagents/sandboxes)

运行授权取空间授权、固定发布绑定、角色授权与当前撤销限制的交集。关闭沙箱后，已授权的远端 MCP、普通 Tool 和 Skill 阅读仍可使用；脚本执行不可用。内部控制、预算和授权中间件失败时阻止新动作；非关键观察日志可以降级并记录诊断，不能照搬旧 SDK 的 Hook 失败策略。

## 6. 产品入口与实施范围

左侧保留 Tool／Skill／MCP 三个独立页面。模型、环境和存储另设管理入口；Agent 编辑器选择资源与运行环境。创建时同一表单填写名称、必选 ReAct／Plan、可选描述；模式没有默认值，创建后固定。

首版交付受信 Tool、文件 Tool、基础 Skill、MCP tools 和 Plan Executor；沙箱脚本在隔离服务验收后开放。供应商内置工具、客户端工具、跨应用 Agent 调用、MCP Resources／Prompts 的独立管理、完整知识库作为后续范围。这是平台交付顺序，并非上游能力限制。
