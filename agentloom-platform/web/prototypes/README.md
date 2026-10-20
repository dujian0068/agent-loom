# Platform 交互原型

这是一份基于示例数据的交互预览，不是平台前端实现，不连接 API、模型或凭据。

- [在线体验](https://doc.agentloom.bmilk.cn/prototypes/platform-interaction/)
- [交互设计说明](../../../docs/platform/interaction-design.md)
- [可编辑片段](platform-interaction.fragment.html)
- [独立网页导出](../../../docs/public/prototypes/platform-interaction/index.html)

网页顶部的「原型预览设置」可切换使用者角色、连接状态、异常场景和沙箱服务接入状态。默认以管理员体验资源添加；编辑者可查看资源与绑定应用，使用者仅能使用已发布应用。注册、导入、发现、绑定、配置、校验、发布、补充和停止均在浏览器内模拟；模型与环境概览、成员页只展示信息布局。刷新会重置资源、应用与运行，只保留预览设置。

整体布局参考第一版：左侧空间导航与用户信息、顶部路径、应用卡片与搜索；配置页采用左侧表单／右侧发布操作，会话页采用左侧对话与输入／右侧运行过程。窄屏导航折叠，双栏自动堆叠。

左侧分别提供 **Tool · 工具、Skill · 技能、MCP · 服务** 页面，先维护空间资源，再绑定到 Agent：

- Tool：统一目录包含 25 项示例能力，按文件与工作区、命令与任务、外部集成、自定义工具、子 Agent 编排分类；可搜索名称、来源与服务商。11 项文件操作标明 Deep Agents 能力及平台扩展，另含 exec、write_todos 和 Plan 委派说明；可绑定工具均能从目录加入应用角色草稿。
- 外部工具模板包含联网搜索、网页正文提取、浏览器、数据库查询、远端代码解释器、邮件搜索／发送、日历读取／创建、云盘检索。初始为「待配置连接」；管理员选择服务商、连接名称和示例凭据，模拟生成就绪新版本后才能绑定。连接升级不改变既有授权和发布快照。
- 自定义 Tool 仍可填写名称、标识、描述、版本、受信工具注册标识与输入 Schema，校验后加入目录。预置条目显示的是平台资源版本与适配参数，不冒充上游包版本；真实执行尚未实现。
- 子 Agent 委派在工具目录中可发现，并通向 Plan 专业执行器配置，由主图调度子图；不会把 task 自动加入每个 Agent。Skill 加载与 MCP 接入保留各自页面，不重复注册成普通 Tool。
- Skill：从 Git 仓库及提交／标签或压缩包地址模拟导入，展示 SKILL.md、资源和脚本信息，按固定版本只读挂载。
- MCP：添加协议、服务地址和凭据引用后进入「待发现」；校验模拟 tools/list 结果，显式选择成员并固定版本，才可绑定。
- 三类资源均可搜索、查看版本和应用引用、添加新版本、选择目标应用与角色加入草稿。MCP 绑定还需逐项授权成员，可同时绑定多个服务。
- Agent 能力页的「管理 Tool／Skill／MCP」通向对应页面，离开前仍执行未保存检查。资源升级不替换原绑定；显式切换版本并重新发布才生效，旧发布快照保留。

新建表单同时填写名称、选择 ReAct／Plan、填写可选描述。执行模式没有默认选项，未选择不能创建；创建后只读展示，Plan 才显示专业执行器配置。点击创建或名称输入框按 Enter 使用相同校验。

配置页按「基础配置／能力配置／运行环境」组织，包含预置／集成 Tool、Skill、MCP 成员选择、Plan 各角色独立授权、执行沙箱开关、工作区读写权限及文件保留策略。新工具默认不绑定；未配置连接的集成可见但不可选。保存、放弃修改和发布快照涵盖这些配置；校验会检查 MCP 空成员、集成连接缺失、exec 缺少沙箱以及只读工作区与写入工具冲突。远端代码解释器使用供应商执行环境，与应用本地沙箱开关分开。

沙箱服务默认未接入；「已接入（模拟）」仅用于体验未来环境服务可用后的交互，不代表平台已接入真实隔离执行环境。工作文件是否保留与执行沙箱是否开启是两个独立选项。

`docs/public/prototypes/platform-interaction/index.html` 是随文档站发布的导出快照。GitHub 可以查看源码；在线体验需打开上面的文档站链接。页面不依赖本地可视化目录或运行中的预览服务。

修改片段后，使用 visualize 技能的导出工具更新快照（将 `<visualize-skill-root>` 替换为该技能所在目录），再构建和检查文档：

```sh
python3 <visualize-skill-root>/scripts/render.py \
  agentloom-platform/web/prototypes/platform-interaction.fragment.html \
  docs/public/prototypes/platform-interaction/index.html \
  --title "AgentLoom Platform 交互原型" --force
npm run docs:build
npm run docs:check
```

导出快照包含独立运行所需的显示与状态兼容代码，部署时不需要安装该技能。平台计划使用 LangChain + LangGraph；浏览器中的模拟操作不代表真实运行或平台集成测试通过，参见[运行接入](../../../docs/platform/runtime-integration.md)。
