# AgentLoom

AgentLoom 重新划分为两个独立交付物，暂时保留在同一个 Git 仓库：

| 交付物 | 职责 | 目录 |
| --- | --- | --- |
| `agentloom-sdk` | Python 开发者通过代码构建 ReAct、Plan–Executor；负责执行、上下文、能力调用与检查点恢复。Workflow 属于后续设计 | [SDK](agentloom-sdk/README.md) |
| `agentloom-platform` | 基于 LangChain + LangGraph 提供用户、空间、资源、应用配置、发布、网页和 HTTP API | [Platform](agentloom-platform/README.md) |

2026-10-20 起，**Platform → LangChain + LangGraph**；平台不再依赖 agentloom-sdk。SDK 作为独立 Python 交付物保留，仍不依赖平台应用、用户、发布记录或业务数据库。

## 当前状态

2026-10-15 已移除旧前后端、Runtime、图模块、工具 SDK、业务测试、部署脚本及对应旧使用说明。

SDK 已提供可安装的 `agentloom-sdk` 包，实现独立 ReAct / Plan–Executor、能力调用、Hook、上下文、工作空间与单机文件恢复。通过 `PropertiesConfig → AgentLoom → build(agent_def)` 接入，具体步骤和可运行脚本见[接入指南](docs/sdk/integration.md)，接口状态、模块职责及验证见[实现文档](docs/sdk/implementation.md)。SDK 的 Workflow 暂不接入。Platform 已提供独立 Vue 前端、FastAPI、Worker、资源配置与会话运行的首个开发版本；启动方式见[平台工程说明](agentloom-platform/README.md)，交付范围与剩余验收见[当前实现](docs/platform/implementation.md)。

在仓库根目录执行 `./scripts/test_all.py`，默认最多使用 4 个独立进程并发运行全部 SDK／QA 测试并保存日志与摘要；可用 `--workers 8` 指定并发数，`--workers 1` 串行运行。需安装 SDK 的 `test` 依赖。最近一次结果入口为 `.agentloom/test-results/latest.json`；使用 `./scripts/test_all.py --check` 核对已有结果是否仍匹配当前源码和环境，无需重新执行测试。

## 从这里继续

1. [SDK / Platform 职责边界](docs/sdk-platform-boundary.md)：两份独立交付物，以及平台与 LangChain／LangGraph 的职责。
2. [Agent 设计约定](docs/agent-design-decisions.md)：已确认的产品行为。
3. [目录规划](docs/directory-plan.md)：本次实际建立的目录。
4. [文档目录](docs/README.md)：领域、双引擎、事件总线、上下文和检查点设计。

设计文档已按两份交付物整理：[SDK 设计](docs/sdk/index.md)、[Platform 设计](docs/platform/index.md)。SDK 的 [对象协作 UML](docs/sdk/object-collaboration.md) 细化了 Agent、Node、EventBus、EventHandler、HookHandler、SkillHandler 与工具、资源的关系和交互。SDK 的 LangGraph 源码适配计划尚未实施；新版平台直接使用上游包，接入方案见[运行设计](docs/platform/runtime-integration.md)，能力分类见[Tool／Skill／MCP](docs/platform/capabilities.md)。

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
