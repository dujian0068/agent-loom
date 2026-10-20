# 阅读指南

先读[SDK／Platform 边界](sdk-platform-boundary.md)，确定要了解的是独立 SDK 还是采用 LangChain + LangGraph 的平台。

早期设计的整理历史见[复核记录](reorganization-review.md)；其中旧 Platform → SDK 方案已由 2026-10-20 的平台设计取代。

## 阅读 SDK

实际接入先读 [SDK 接入指南](sdk/integration.md)，通过 PropertiesConfig 和 AgentLoom 运行现有定义；再读[实现文档](sdk/implementation.md)确认当前接口、模块职责和验证边界。以下是完整目标设计的阅读顺序，Workflow 等尚未实现部分以实现文档为准。

1. [行为约定](sdk/requirements.md)：执行模式和已经确认的规则。
2. [定义与实例](sdk/definitions.md)：AgentDefinition、GraphDefinition、NodeTask、Operation 分别是什么。
3. [对象协作 UML](sdk/object-collaboration.md)：Agent、Node、EventBus、Handler、Hook、Skill 的类关系和时序。
4. [构造与资源绑定](sdk/assembly.md)、[公共接口](sdk/public-api.md)：独立开发者传什么、SDK 创建什么，以及如何执行、停止和恢复。
5. [Agent 执行](sdk/runtime.md)、[调用总线](sdk/event-bus.md)、[上下文](sdk/context.md)、[能力环境](sdk/capabilities.md)：进入模块算法和数据字段；运行时引擎 RuntimeEngine 的服务范围见[装配设计](sdk/assembly.md#11-runtimeengine-管理的范围)。
6. [检查点](sdk/checkpoints.md)、[Workflow](sdk/workflow.md)：持久化、两套引擎与故障交接。
7. [验收](sdk/verification.md)：实现顺序和需要证明的不变量。

## 阅读 Platform

1. [总览](platform/index.md)、[领域模型](platform/domain-model.md)：应用、发布、资源及会话归属。
2. [配置与发布](platform/configuration.md)：用户可以设置哪些内容、怎样验证和固定配置。
3. [能力类型](platform/capabilities.md)：Tool、Skill、MCP 等如何注册、加载和授权。
4. [LangChain / LangGraph 接入](platform/runtime-integration.md)：ReAct／Plan 图、停止、输入与恢复。
5. [HTTP API](platform/api.md)、[存储生命周期](platform/storage-lifecycle.md)：接入、管理事务及清理。
6. [验收](platform/verification.md)：使用真实 API、数据库和进程验证运行集成。

页面侧栏按两份交付物组织，页面内目录定位具体字段和算法。图可以展开查看；继承、归属和调用图分开解释。SDK 核心链路已有实现，Platform 仍为设计；文档构建通过不能代替运行验证或完整目标验收。
