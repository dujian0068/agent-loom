# AgentLoom

AgentLoom 重新划分为两个独立交付物，暂时保留在同一个 Git 仓库：

| 交付物 | 职责 | 目录 |
| --- | --- | --- |
| `agentloom-sdk` | Python 开发者通过代码构建 ReAct、Plan–Executor；负责执行、上下文、能力调用与检查点恢复。Workflow 属于后续设计 | [SDK](agentloom-sdk/README.md) |
| `agentloom-platform` | 基于 SDK 提供用户、空间、资源、应用配置、发布、网页和 HTTP API | [Platform](agentloom-platform/README.md) |

依赖方向是 **Platform → SDK**。SDK 不查询平台应用、用户或发布记录，不要求平台数据库；调用者在构造时传入必要配置与依赖。

## 当前状态

2026-10-15 已移除旧前后端、Runtime、图模块、工具 SDK、业务测试、部署脚本及对应旧使用说明。

SDK 已提供可安装的 `agentloom-sdk` 包，实现独立 ReAct / Plan–Executor、能力调用、Hook、上下文、工作空间与单机文件恢复。通过 `PropertiesConfig → AgentLoom → build(agent_def)` 接入，具体步骤和可运行脚本见[接入指南](docs/sdk/integration.md)，接口状态、模块职责及验证见[实现文档](docs/sdk/implementation.md)。Workflow 暂不接入，Platform 仍为设计与目录结构。

## 从这里继续

1. [SDK / Platform 职责边界](docs/sdk-platform-boundary.md)：构造注入、领域归属、ExecutionRecord 恢复及三种独立示例的验收要求。
2. [Agent 设计约定](docs/agent-design-decisions.md)：已确认的产品行为。
3. [目录规划](docs/directory-plan.md)：本次实际建立的目录。
4. [文档目录](docs/README.md)：领域、双引擎、事件总线、上下文和检查点设计。

设计文档已按两份交付物整理：[SDK 设计](docs/sdk/index.md)、[Platform 设计](docs/platform/index.md)。SDK 的 [对象协作 UML](docs/sdk/object-collaboration.md) 细化了 Agent、Node、EventBus、EventHandler、HookHandler、SkillHandler 与工具、资源的关系和交互。LangGraph 源码适配尚未实施。

## 文档站

```sh
npm ci
npm run docs:dev
```

打开 http://127.0.0.1:8767 。验证文档：

```sh
npm run docs:build
npm run docs:check
```

根目录 Node.js 依赖只服务于文档站。发布流程见[文档站维护](docs/documentation-site.md)，线上地址为 [doc.agentloom.bmilk.cn](https://doc.agentloom.bmilk.cn/)；线上内容随 GitHub Pages 发布更新。

`.env`、本地数据和未提交的示例素材不属于 SDK 包。当前使用说明放在 SDK README 和接入指南中，没有恢复此前删除的示例目录。
