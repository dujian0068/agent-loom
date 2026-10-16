---
layout: home
hero:
  name: AgentLoom
  text: SDK 与 Platform
  tagline: 高代码执行底座，低代码应用平台。职责、对象和交互分开阅读。
  actions:
    - theme: brand
      text: SDK 设计
      link: /sdk/
    - theme: alt
      text: Platform 设计
      link: /platform/
    - theme: alt
      text: SDK 对象 UML
      link: /sdk/object-collaboration
features:
  - title: SDK
    details: ReactAgent、PlanAgent、Workflow，包含执行、能力、上下文、Hook 和检查点恢复。
    link: /sdk/
  - title: Platform
    details: 用户与空间、资源与 Key、应用配置、发布、网页及 HTTP API，通过公共接口装配 SDK。
    link: /platform/
  - title: 清楚的边界
    details: SDK 无平台数据库依赖；平台负责版本和装配，SDK 负责执行与恢复。
    link: /sdk-platform-boundary
---

## 当前状态

新版代码仍为目录骨架，文档是待实施的详细设计。[Agent 设计约定](agent-design-decisions.md)记录需求，[阅读指南](reading-guide.md)说明阅读顺序。旧的混合设计已整理为两组文档，不再保留重复正文。

先看 [SDK 与 Platform 的职责边界](sdk-platform-boundary.md)，再进入对应模块。
