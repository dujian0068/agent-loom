# agentloom-platform

基于 agentloom-sdk 的低代码平台。当前只有目录骨架，没有可启动的前后端或部署脚本。

## 边界

- 管理用户、空间、权限、应用、草稿与发布版本。
- 管理模型凭据、Skill、Tool、MCP、Hook 等可复用资源，提供配置页面。
- 将已选配置解析为 SDK 构造参数及依赖；发布前完成权限、资源和应用配置检查。
- 准备运行机器、会话标识、稳定 Run 标识及存储／工作空间依赖，通过 SDK 公共接口发起执行。
- 故障后使用该 Run 原配置重新构造 Agent，交回原 Run 标识；不在平台重写 Planner／Executor 的恢复算法。
- 提供网页、HTTP API、运行状态展示与运维接入。

## 目录

| 目录 | 用途 |
| --- | --- |
| `api/` | 平台 API、管理领域和 SDK 装配 |
| `web/` | Vue 配置与运行工作台 |
| `tests/` | 平台 API 与 SDK 集成验收 |
| `deploy/` | 新实现具备可运行闭环后再提供部署入口 |

平台只依赖 SDK 公开接口。删除 Platform 目录后，SDK 自身及三个示例仍须能独立运行。详细约定见 [SDK / Platform 边界](../docs/sdk-platform-boundary.md)。
