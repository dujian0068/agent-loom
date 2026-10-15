# AgentLoom

AgentLoom 重新划分为两个独立交付物，暂时保留在同一个 Git 仓库：

| 交付物 | 职责 | 目录 |
| --- | --- | --- |
| `agentloom-sdk` | Python 开发者通过代码构建 ReAct、Plan–Executor 和 Workflow；负责执行、上下文、能力调用与检查点恢复 | [SDK](agentloom-sdk/README.md) |
| `agentloom-platform` | 基于 SDK 提供用户、空间、资源、应用配置、发布、网页和 HTTP API | [Platform](agentloom-platform/README.md) |

依赖方向是 **Platform → SDK**。SDK 不查询平台应用、用户或发布记录，不要求平台数据库；调用者在构造时传入必要配置与依赖。

## 当前状态

2026-10-15 已移除旧前后端、Runtime、图模块、工具 SDK、业务测试、部署脚本及对应旧使用说明。

**当前两个交付目录只有结构与职责说明，还没有可运行的 SDK 或平台。** 不提供旧服务启动命令，也不把空目录或接口草案当成已实现功能。测试服务器和数据库未在本次清理中修改。

## 从这里继续

1. [SDK / Platform 边界与重建基线](docs/design/sdk-platform-boundary.md)：构造注入、领域归属、Run 恢复及三种独立示例的验收要求。
2. [Agent 设计约定](docs/agent-design-decisions.md)：已确认的产品行为。
3. [目录规划](docs/directory-plan.md)：本次实际建立的目录。
4. [文档目录](docs/README.md)：领域、双引擎、事件总线、上下文和检查点设计。

保留的 SD-3 / ID-1 设计需要按新边界继续调整。尤其不能把旧草案的用户、空间、发布引用或平台表结构直接作为 SDK 的必填契约。LangGraph 源码适配尚未实施，兼容范围也需要逐项验证。

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

根目录 Node.js 依赖只服务于文档站。发布流程见[文档站维护](docs/documentation-site.md)，线上地址为 [agentloom.bmilk.cn](https://agentloom.bmilk.cn/)；线上内容随 GitHub Pages 发布更新。

`.env`、本地数据和未提交的示例素材不属于这次旧源码删除范围，不会被加入 Git。独立运行的 ReAct、Plan–Executor、Workflow 示例将在 SDK 实现后交付。
