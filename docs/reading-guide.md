# 阅读指南

项目已进入 SDK / Platform 重建阶段，旧业务实现和旧使用说明已删除。新代码尚未实现；已有详细设计保留用于修订，不再直接按旧平台与 Runtime 耦合的结构开发。

## 推荐顺序

1. [SDK / Platform 边界与重建基线](design/sdk-platform-boundary.md)：先明确交付边界、构造注入、Run 身份和 SDK 恢复责任。
2. [Agent 设计约定](agent-design-decisions.md)：已确认的执行、上下文、文件与 Hook 行为。
3. [领域模型](design/domain-model.md) → [图与节点](design/graph-node-engine.md)：结合新边界重新审视定义、任务和服务的归属。
4. [双引擎与检查点适配](design/dual-engine-integration.md) → [检查点协议](design/checkpoint-protocol.md)：保存各自现场、关联原调用、避免重做已保存结果。
5. [Agent 执行](design/agent-execution.md) → [上下文](design/context-lifecycle.md) → [能力与 Hook](design/capabilities-hooks.md) → [事件总线](design/runtime-messaging-checkpoints.md)。
6. [领域接口](design/domain-interfaces.md)、[数据契约](design/data-contracts.md)与[实施规格](design/implementation/index.md)：这些具体字段和方法还需按 SDK / Platform 分离后再落地。

## 如何判断一项设计是否可以直接实现

- 已确认产品行为以设计约定为准，交付及恢复职责以重建基线为准。
- 旧草案中的平台用户／空间、发布引用和业务外键不能成为 SDK 的必填配置。
- SDK 允许注入执行存储，但不查询平台业务表来补齐构造参数。
- Schema 样例和文档图只能辅助讨论；SDK、真实存储、模型与 API 都需分别实施和验收。

当前目录见[重建目录](directory-plan.md)。旧部署记录和使用方法不适用于当前重建结构。

[完整文档目录](README.md) · [维护文档站](documentation-site.md)
