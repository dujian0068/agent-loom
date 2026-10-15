# AgentLoom 文档目录

当前进入 SDK / Platform 重建阶段。旧业务代码及旧使用说明已清除，先阅读 [SDK / Platform 边界与重建基线](design/sdk-platform-boundary.md)，再阅读 [Agent 设计约定](agent-design-decisions.md)。

两份交付物目前只有目录骨架。以下 SD-3 / ID-1 设计保留用于继续细化，涉及平台身份、发布、配置指纹和持久表的部分需重新划分归属，不直接作为 SDK 的公共契约。

## 设计入口

| 内容 | 入口 |
| --- | --- |
| 两份交付物、构造注入与 SDK 恢复 | [重建基线](design/sdk-platform-boundary.md) |
| 已确认产品规则 | [Agent 设计约定](agent-design-decisions.md) |
| 实际目录与状态 | [重建目录](directory-plan.md) |
| 总体架构与模块 | [系统设计总览](design/overview.md) |
| 对象、归属与实例数量 | [领域模型与 UML](design/domain-model.md) |
| 定义、节点与双引擎 | [图与节点](design/graph-node-engine.md)、[双引擎与检查点适配](design/dual-engine-integration.md) |
| 字段与一致性 | [数据结构与契约](design/data-contracts.md) |
| 保存现场与接管 | [检查点结构与父子恢复](design/checkpoint-protocol.md) |
| 领域端口和交互 | [领域接口](design/domain-interfaces.md) |
| Loop 与委派 | [Agent 执行](design/agent-execution.md) |
| 历史、补充与压缩 | [上下文生命周期](design/context-lifecycle.md) |
| 扩展与环境 | [能力与 Hook](design/capabilities-hooks.md) |
| 消息与调用交付 | [事件总线](design/runtime-messaging-checkpoints.md) |
| 存储与验收草案 | [存储与 API](design/storage-recovery-api.md)、[验收计划](design/verification-roadmap.md) |
| 设计取舍 | [架构决策记录](design/architecture-discussion.md) |

## 待按边界调整的实施规格

[实施入口](design/implementation/index.md) · [平台配置/API](design/implementation/catalog-api.md) · [持久化/事务](design/implementation/persistence.md) · [Runtime/图](design/implementation/runtime-graph.md) · [EventBus/Hook](design/implementation/invocation.md) · [上下文/能力/环境](design/implementation/context-capabilities.md)。

这些文档不是当前已实现接口的说明。调整后仍须通过 SDK 独立运行、持久恢复与平台 API 集成验收。

## 文档维护

[阅读指南](reading-guide.md) · [构建与发布文档站](documentation-site.md)。

图表使用正文内嵌 Mermaid。当前站点维护重建阶段的产品与设计说明。
