# agentloom-platform

基于 LangChain + LangGraph 的团队 Agent 平台，不再依赖 agentloom-sdk。当前只有设计文档、目录骨架和使用模拟数据的交互原型，没有可启动的业务前后端或部署脚本。

首版支持多人、空间权限、私有会话、应用配置与发布。前后端必须分离：`web/` 独立构建和部署 Vue + TypeScript 前端，`api/` 使用 FastAPI 提供 HTTP API 与 SSE，Worker 使用后端包单独运行；前端产物不打入 Python 镜像，FastAPI 不托管 SPA。

- [团队私有部署实施方案](../docs/platform/implementation-plan.md)
- [LangChain / LangGraph 运行接入](../docs/platform/runtime-integration.md)
- [Tool、Skill、MCP 等能力类型](../docs/platform/capabilities.md)
- [首版交互设计](../docs/platform/interaction-design.md)
- [在线交互原型](https://doc.agentloom.bmilk.cn/prototypes/platform-interaction/)及[源码维护说明](web/prototypes/README.md)

## 边界

- 平台管理用户、空间、权限、应用、草稿、不可变发布、资源版本与凭据。
- ReAct 使用 LangChain Agent，Plan 使用 LangGraph 编排 Planner 与专业 Executor。
- Tool、Skill、MCP 保留独立管理页，按应用角色绑定；沙箱、工作空间与文件保留分开配置。
- 平台负责队列、输入收件箱、停止命令、单写者和清理；LangGraph 保存图检查点，生产采用 PostgreSQL checkpointer。
- 故障按同一 run_id、graph_thread_id 和原发布恢复；对外部副作用、图版本兼容和旧进程撤权单独验收。

## 目录

| 目录 | 计划用途 |
| --- | --- |
| `api/` | 管理 API、ExecutionHost、LangChain／LangGraph 装配、资源和环境适配 |
| `web/` | 独立构建与部署的工作台；当前仅有 `prototypes/` 交互预览 |
| `tests/` | 平台 API、运行适配、权限与故障恢复验收 |
| `deploy/` | 分别提供 Web、API、Worker、控制维护进程及入口代理部署 |

SDK 与 Platform 是独立交付物，平台不导入 `agentloom`，不复用它的 RuntimeEngine 或文件状态。详细约定见[交付物边界](../docs/sdk-platform-boundary.md)。
