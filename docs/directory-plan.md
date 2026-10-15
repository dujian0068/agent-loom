# AgentLoom 重建目录

更新：2026-10-15。旧业务代码已移除，两份交付物先在同一仓库维护；是否拆成两个 Git 仓库尚未决定。边界见 [SDK / Platform](design/sdk-platform-boundary.md)。

```text
agent-loom/
├── agentloom-sdk/
│   ├── README.md       SDK 职责
│   ├── src/            待实现的 Python SDK
│   ├── tests/          待实现的 SDK 契约、流转和恢复测试
│   └── examples/       ReAct、Plan–Executor、Workflow 验收要求
├── agentloom-platform/
│   ├── README.md       平台职责
│   ├── api/            待实现的管理 API 与 SDK 装配
│   ├── web/            待实现的 Vue 工作台
│   ├── tests/          待实现的平台 API 集成测试
│   └── deploy/         新实现的部署入口预留
├── docs/               需求、设计草案及文档站
├── .github/workflows/  GitHub Pages 文档构建与发布
├── package.json        仅文档站脚本与依赖
└── package-lock.json   文档依赖锁定
```

`src/`、`api/`、`web/`、`tests/`、`deploy/` 目前仅有目录占位。没有可安装的 SDK 包、可启动的平台或已实现的示例，不提供旧启动／部署命令。

依赖方向是 Platform → SDK；SDK 独立运行所需的协议和适配器放在 SDK 内，不能要求加载平台应用、用户或发布记录。

根目录仍可能存在被忽略的本地配置、工具环境，以及未提交的 `examples/agents/` 用户素材；它们不属于新产品实现。旧 Demo hosting 元数据保存在被忽略的 `work/rebuild-preserved/`，不会随源码分发。

无需在当前目录维护 legacy 源码、旧数据库迁移或旧部署兼容层。
