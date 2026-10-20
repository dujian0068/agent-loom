# agentloom-platform

基于 agentloom-sdk 的低代码平台。当前只有目录骨架，没有可启动的前后端或部署脚本。

首版面向团队私有部署，支持多人、空间权限和应用发布。工程范围、API／Worker 分工、数据约束和实施顺序见[团队私有部署实施方案](../docs/platform/implementation-plan.md)。

前后端必须分离：`web/` 独立构建和部署静态前端，`api/` 独立提供 HTTP API 与 SSE，Worker 使用后端包单独运行。前端产物不打入 Python 镜像，FastAPI 不托管 SPA；同域入口代理只负责请求分流。

页面、操作流程与状态反馈见[首版交互设计](../docs/platform/interaction-design.md)。

[在线交互原型](https://doc.agentloom.bmilk.cn/prototypes/platform-interaction/)使用示例数据；源码与维护说明位于 [web/prototypes](web/prototypes/README.md)。

## 边界

- 管理用户、空间、权限、应用、草稿与发布版本。
- 管理模型凭据、Skill、Tool、MCP、Hook 等可复用资源，提供配置页面。
- 将已选配置解析为 SDK 构造参数及依赖；发布前完成权限、资源和应用配置检查。
- 准备运行机器、会话标识、稳定 ExecutionRecord 标识及存储／工作空间依赖，通过 SDK 公共接口发起执行。
- 故障后使用该 ExecutionRecord 原配置重新构造 Agent，交回原 ExecutionRecord 标识；不在平台重写 Planner／Executor 的恢复算法。
- 提供网页、HTTP API、运行状态展示与运维接入。

## 目录

| 目录 | 用途 |
| --- | --- |
| `api/` | 平台 API、管理领域和 SDK 装配 |
| `web/` | 独立构建、部署的 Vue 配置与运行工作台 |
| `tests/` | 平台 API 与 SDK 集成验收 |
| `deploy/` | 新实现具备闭环后，分别提供 Web、API、Worker 与入口代理部署 |

平台只依赖 SDK 公开接口。删除 Platform 目录后，SDK 自身及三个示例仍须能独立运行。详细约定见 [SDK / Platform 边界](../docs/sdk-platform-boundary.md)。
