# agentloom-sdk

面向 Python 开发者的 Agent 与 Workflow 执行 SDK。当前只有目录骨架，没有可安装的实现包或已实现的公共 API。

## 边界

- ReactAgent、PlanAgent 实现公共 Agent 接口；Workflow 是由 StateGraph 编译得到的独立入口。内部使用轻量 Agent 引擎与通用 Workflow 引擎。
- Node 定义图内调度合同；ReactNode、PlannerNode、ExecutorNode 承担角色 Loop。完整 Agent 通过 AgentNode 组合进入 Workflow，不与节点类建立继承关系。
- 提供模型、Tool、MCP、Skill、Hook、上下文、工作空间和检查点扩展接口。
- 调用者传入完整执行配置与必要依赖。SDK 不管理用户、应用、发布版本，不要求平台服务或平台业务库。
- SDK 根据稳定 Run 标识和检查点恢复执行现场；重新构造对象不等于新建 Run。
- 已完成的 Executor 结果保留，未完成的 Executor 从可靠保存点继续；不把全部历史交给 Planner 重新规划。

详细约定见 [SDK / Platform 边界](../docs/sdk-platform-boundary.md)。

## 目录

| 目录 | 用途 |
| --- | --- |
| `src/` | 待实现的 SDK 源码 |
| `tests/` | SDK 契约、执行、恢复与存储适配测试 |
| [examples/](examples/README.md) | 三种独立运行示例的验收要求；当前尚无执行代码 |

先明确公共接口与可恢复执行契约，再实现最小可运行闭环；不移回旧 Runtime，也不建立依赖平台内部模块的兼容层。
