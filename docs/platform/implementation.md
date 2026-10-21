# Platform 当前实现

更新：2026-10-21。平台已从交互原型进入首个可运行开发版本，正式代码位于 [agentloom-platform](../../agentloom-platform/README.md)。本页描述当前代码；其他设计页保留完整目标，未列为实现的部分不能视为已经交付。

## 已接通的流程

| 范围 | 当前行为 |
| --- | --- |
| 独立工程 | Vue 3 + TypeScript + Vite；FastAPI Python 包；Web 不进入后端镜像，API 不托管页面 |
| 账号与空间 | 管理员初始化、密码登录、Cookie／CSRF、成员创建与角色调整、viewer／editor／admin、管理操作审计 |
| 应用 | 创建时模式必选且不可变、可选描述、草稿 revision／If-Match、校验、固定发布快照及发布记录 |
| Tool | 独立目录；10 个工作区文件工具与 `write_todos` 可用；其他集成模板保留但禁用；列表不显示提供方或绑定按钮 |
| Skill | 独立添加与版本；`SKILL.md` 基础元数据校验、文本目录导入、固定文件快照；按角色渐进读取，不执行脚本 |
| MCP | HTTP 连接、凭据、真实发现、显式成员选择；固定成员、描述与 Schema；Schema 漂移时失败，不自动扩权 |
| 模型 | OpenAI 兼容服务地址／模型 ID；凭据独立加密保存、不返回明文；测试模型必须由服务端显式开启 |
| 环境 | 工作区读写、run／session 文件保留；路径边界、符号链接防护；无沙箱时禁止发布带沙箱配置的应用 |
| ReAct | 实际 `create_agent`；只装配当前角色所选 Tool、Skill、MCP |
| Plan | 实际 `StateGraph`；结构化 Planner → 按顺序运行独立 Executor → Planner 汇总，校验执行器与任务数 |
| 会话 | 创建者私有、稳定消息 ID 去重、持久输入和任务、公开历史、SSE 序号续读、停止 |
| 执行 | 独立 Worker 为每个 Run 创建子进程；控制维护可独立运行；持久队列、同机文件锁、generation、共享预算 |
| 恢复 | 原发布、原内部 thread 和持久检查点；图已完成而平台回执未完成时仅对账；未知工具结果阻止重放 |
| 部署 | 分离 Web／API 镜像，Compose 的 PostgreSQL／API／Worker／控制维护／Web，独立 graph schema |

浏览器已通过名称／模式／描述创建、独立执行器配置、角色工具选择、环境设置、保存、校验、发布及 Plan 会话运行；测试结果在页面上明确标为“测试模型”。原在线 HTML 原型仍是设计预览，不会因此变成托管业务服务。

2026-10-21 已将正式应用部署至 a2，前端、API、Worker、维护进程与独立 PostgreSQL 均已运行。正式入口为 [agentloom.bmilk.cn](https://agentloom.bmilk.cn)，新域名源站 HTTPS、登录退出与 CSRF 检查通过，公网浏览器登录和资源页面已验证；部署验证与回滚方式见 [a2 部署记录](deployment-a2.md)。

## 实现入口

| 文件 | 责任 |
| --- | --- |
| [http.py](../../agentloom-platform/api/src/agentloom_platform/http.py) | 认证、空间权限、幂等、草稿、发布、资源、会话和 SSE |
| [applications.py](../../agentloom-platform/api/src/agentloom_platform/applications.py) | 资源引用、校验和发布快照 |
| [resources.py](../../agentloom-platform/api/src/agentloom_platform/resources.py) | 目录、Skill 校验、MCP 发现、凭据读取 |
| [runtime.py](../../agentloom-platform/api/src/agentloom_platform/runtime.py) | 模型与角色装配、Plan 图、预算中间件、工具回执、检查点 |
| [worker.py](../../agentloom-platform/api/src/agentloom_platform/worker.py) | 子进程、单写者、停止、恢复、清理及发布屏障 |
| [tools.py](../../agentloom-platform/api/src/agentloom_platform/tools.py) | 授权工作区和只读 Skill 阅读 |
| [persistence.py](../../agentloom-platform/api/src/agentloom_platform/persistence.py) | 管理、输入、运行、操作回执和公开事件存储 |
| [web/src](../../agentloom-platform/web/src/) | 应用、配置、资源、会话及空间管理页面 |

实际 HTTP 路径使用 `/spaces/{id}/resources/{kind}` 注册资源、`/resources/{id}/discover` 发现 MCP、`/resources/{id}/enable-members` 固定成员。API 运行时的 `/api/v1/openapi.json` 是当前接口合同；[API 设计](api.md)中的异步导入、删除等目标接口尚未全部实现。

依赖以 `api/requirements.lock` 和 `web/package-lock.json` 锁定。发布记录图版本、状态版本及后端依赖、运行图和工具实现的内容摘要；版本不兼容时拒绝恢复。当前检查点显式限制可反序列化的自定义类型，不启用 pickle fallback。

## 当前边界

- 运行中补充输入、Plan 重新规划／并行执行、完整任务层级 UI 尚未实现；当前会话在任务结束后接收下一次提问。
- 沙箱／`exec`、文件补丁、搜索／浏览器／数据库／邮件／日历／云盘等外部工具适配器尚未接入。子 Agent 委派目前通过 Plan 专业执行器实现，没有开放独立 `task` 工具。
- Skill 暂时保存文本文件快照，不支持 Git／压缩包异步导入和可执行脚本。
- 尚无应用完整删除、检查点生命周期回收、空间创建 UI、资源停用 UI、凭据轮换记录、未知操作结果处置 UI、SSO 与密码找回。
- 列表目前返回全部项目，`next_cursor=null`；大规模数据分页和观测界面未实现。
- PostgreSQL 写操作使用短事务 advisory lock 保证初版业务约束，事务中不做网络请求；高吞吐的细粒度并发领取待后续实现。
- 只支持固定执行主机及共享卷，不能把文件锁当成跨主机 fencing。操作结果不明时保守阻塞，不宣称外部副作用恰好执行一次。
- 尚未进行真实供应商密钥联调、沙箱验收、所有子图提交边界的故障注入、升级和备份恢复验证；当前开发版本不能宣称完整 P0—P3 已验收。

## 验证方式

[平台集成测试](../../agentloom-platform/tests/)不导入 SDK，使用实际 FastAPI、SQLAlchemy、LangChain、LangGraph 和 SQLite／PostgreSQL checkpointer；模型由显式测试实现或本机 OpenAI 兼容 HTTP 服务提供可控响应。本机 MCP 服务通过真实 HTTP 与 `langchain-mcp-adapters` 通信。

已覆盖模式必填、草稿 CAS、跨空间引用、私有会话、幂等请求及权限撤销后的重试、首条消息并发、发布固定与停止屏障、检查点终态对账、进程文件锁、真实 Worker 子进程、模型执行中停止、工具回执和预算、文件路径与只读限制、Skill profile、MCP 成员选择与 Schema 漂移、凭据隔离。新增中途 SIGKILL 接续、重复工具编号、装配前撤权、实时 SSE 撤权、清理失败重试及前端异步保存／路由保护。具体证据和限制见 [2026-10-21 回归记录](testing-2026-10-21.md)。

[Platform CI](../../.github/workflows/platform.yml)分别运行前端组件／请求测试与构建、SQLite 和 PostgreSQL 集成测试、独立镜像构建及 Compose 冒烟检查。每次提交是否通过以该提交的 CI 结果为准。[完整验收计划](verification.md)仍包含未完成项目。
