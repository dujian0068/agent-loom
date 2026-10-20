# AgentLoom 目录与交付物

更新：2026-10-20。SDK 与 Platform 暂在同一个 Git 仓库维护，但运行依赖已经分离：Platform 直接使用 LangChain + LangGraph，不再导入 agentloom-sdk。边界见[交付说明](sdk-platform-boundary.md)。

```text
agent-loom/
├── agentloom-sdk/
│   ├── README.md       独立 SDK 接入说明
│   ├── pyproject.toml  可安装 Python 包
│   ├── src/            现有 SDK 实现
│   └── tests/          SDK 测试
├── agentloom-platform/
│   ├── README.md       平台选型与边界
│   ├── api/            待实现 FastAPI、Worker 及 LangChain / LangGraph 装配
│   ├── web/            独立前端工程位置；当前有 prototypes/ 交互原型
│   ├── tests/          待实现平台合同、故障及端到端测试
│   └── deploy/         待实现 Web、API、Worker 及控制维护的独立部署
├── docs/
│   ├── sdk/            SDK 接入、实现状态与目标设计
│   ├── platform/       平台配置、能力类型、运行接入、API 和生命周期
│   └── .vitepress/     文档站、导航和旧地址跳转
├── .github/workflows/  文档构建与 GitHub Pages 发布
├── package.json        仅文档站脚本与依赖
└── package-lock.json   文档依赖锁定
```

SDK 已有可安装实现；其实际能力与验证边界见[实现记录](sdk/implementation.md)。Platform 的业务前后端与运行服务尚未实现；交互原型使用浏览器模拟数据，不是生产 Vue 工程或后端。

平台后端的 runtime、execution、environment、resources 等模块计划见[实施方案](platform/implementation-plan.md#后端模块和前端页面)。前端与后端分别拥有依赖清单、构建任务和部署产物，根目录 Node 依赖仅用于文档站。

本地被忽略的环境、配置和未提交素材不属于交付物。旧业务实现与旧 SDK 接入平台方案可从 Git 历史查看，不在当前产品目录恢复 legacy 服务。
