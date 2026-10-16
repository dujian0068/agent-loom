# 阅读指南

先读[SDK／Platform 边界](sdk-platform-boundary.md)，确定要了解的是执行底座还是管理平台。

需要确认这次整理是否完整，先看[复核结论](reorganization-review.md)：它区分已修正的错误、仍需补齐的合同和尚未执行的实现测试。

## 阅读 SDK

1. [行为约定](sdk/requirements.md)：执行模式和已经确认的规则。
2. [定义与实例](sdk/definitions.md)：AgentDefinition、GraphDefinition、NodeTask、Operation 分别是什么。
3. [对象协作 UML](sdk/object-collaboration.md)：Agent、Node、EventBus、Handler、Hook、Skill 的类关系和时序。
4. [构造与资源绑定](sdk/assembly.md)、[公共接口](sdk/public-api.md)：独立开发者传什么、SDK 创建什么，以及如何执行、停止和恢复。
5. [Runtime](sdk/runtime.md)、[调用总线](sdk/event-bus.md)、[上下文](sdk/context.md)、[能力环境](sdk/capabilities.md)：进入模块算法和数据字段。
6. [检查点](sdk/checkpoints.md)、[Workflow](sdk/workflow.md)：持久化、两套引擎与故障交接。
7. [验收](sdk/verification.md)：实现顺序和需要证明的不变量。

## 阅读 Platform

1. [总览](platform/index.md)、[领域模型](platform/domain-model.md)：应用、发布、资源及会话归属。
2. [配置与发布](platform/configuration.md)：用户可以设置哪些内容、怎样验证和固定配置。
3. [SDK 装配](platform/sdk-integration.md)：发布定义如何变成可执行对象，如何重新构造以恢复。
4. [HTTP API](platform/api.md)、[存储生命周期](platform/storage-lifecycle.md)：接入、管理事务及清理。
5. [验收](platform/verification.md)：使用 API 验证平台与 SDK 衔接。

页面侧栏按两份交付物组织，页面内目录定位具体字段和算法。图可以展开查看；继承、归属和调用图分开解释。所有设计尚待实现，文档验证不能替代运行验证。
