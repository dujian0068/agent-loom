---
outline: false
---

# AgentLoom 文档中心

**两份独立交付物：高代码 `agentloom-sdk` 与基于它的低代码 `agentloom-platform`。** SDK 提供 ReAct、Plan–Executor、Workflow 和执行恢复；平台管理用户、应用、资源及发布。

旧业务代码已清除，新目录目前只有骨架，没有可运行产品。先看 **[SDK / Platform 边界与重建基线](design/sdk-platform-boundary.md)**。其他设计草案涉及平台身份和版本的部分，需要按新边界继续调整。

## 按问题阅读

| 你想了解什么 | 从哪里开始 |
| --- | --- |
| SDK 与平台各管什么，为什么恢复属于 SDK | [交付与恢复边界 →](design/sdk-platform-boundary.md) |
| 已确认的产品行为 | [Agent 设计约定 →](agent-design-decisions.md) |
| 清理后项目有哪些目录 | [重建目录 →](directory-plan.md) |
| Definition、发布绑定与执行实例 | [领域模型草案 →](design/domain-model.md) |
| 内层 Agent 与外层 Workflow 怎样衔接 | [双引擎与检查点适配 →](design/dual-engine-integration.md) |
| 检查点怎样保存并继续原任务 | [检查点协议草案 →](design/checkpoint-protocol.md) |
| EventBus 怎样协调调用、Hook 和恢复 | [事件总线设计 →](design/runtime-messaging-checkpoints.md) |

阅读顺序见[阅读指南](reading-guide.md)，完整索引见[文档目录](README.md)。

左侧目录切换文档，右侧目录定位章节；顶部搜索可查找术语。图表在正文中显示。文档构建与链接检查只验证文档制品，不证明 SDK 或平台功能已实现。
