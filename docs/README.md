# AgentLoom 文档索引

| 入口 | 用途 |
| --- | --- |
| [设计约定](agent-design-decisions.md) | 已确认需求与权威文档位置 |
| [SDK／Platform 边界](sdk-platform-boundary.md) | 领域归属、单向依赖和恢复责任 |
| [SDK 总览](sdk/index.md) | 高代码底座设计入口 |
| [SDK 接入指南](sdk/integration.md) | 当前版本安装、PropertiesConfig、build、工具／Hook、执行和恢复 |
| [SDK 实现文档](sdk/implementation.md) | 当前接口状态、模块分工、已验证快照和目标缺口 |
| [SDK 对象 UML](sdk/object-collaboration.md) | Agent、Node、EventBus、Hook、Skill 协作 |
| [SDK 构造与绑定](sdk/assembly.md) | 构造参数、资源／Hook 注册、对象生命周期 |
| [SDK 公共 API](sdk/public-api.md) | start、ainvoke、RunHandle、输入、停止和恢复 |
| [SDK 默认工具与 SPI](sdk/builtin-tools-spi.md) | 默认工具、文件存储与沙箱扩展、事件调用和验收 |
| [Platform 总览](platform/index.md) | 配置化平台设计入口 |
| [团队私有部署实施方案](platform/implementation-plan.md) | 首版范围、模块、事务、SDK 接入及阶段验收 |
| [Platform 首版交互设计](platform/interaction-design.md) | 页面结构、权限入口、配置发布、会话及异常反馈 |
| [阅读指南](reading-guide.md) | 按角色选择阅读顺序 |
| [整理复核记录](reorganization-review.md) | 已修正的不一致及尚未闭合的详细设计 |
| [目录规划](directory-plan.md) | 实际目录与后续代码组织 |
| [文档站维护](documentation-site.md) | 本地预览与 GitHub Pages 发布 |

本目录同时包含当前接入说明、实现记录和目标设计，阅读时以各页标明的范围为准。旧 design/ 下混合平台与 SDK 的正文已撤下，历史版本在 Git 中保留。旧网页路径重定向到对应新章节，不参与全文搜索。
