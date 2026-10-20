# Platform 交互原型

这是一份基于示例数据的交互预览，不是平台前端实现，不连接 API、模型或凭据。

- [在线体验](https://doc.agentloom.bmilk.cn/prototypes/platform-interaction/)
- [交互设计说明](../../../docs/platform/interaction-design.md)
- [可编辑片段](platform-interaction.fragment.html)
- [独立网页导出](../../../docs/public/prototypes/platform-interaction/index.html)

网页顶部的「原型预览设置」可切换使用者角色、连接状态、异常场景和沙箱服务接入状态。配置、校验、发布、补充和停止均在浏览器内模拟；资源与成员页只展示信息布局。刷新会重置应用与运行，只保留预览设置。

整体布局参考第一版：左侧空间导航与用户信息、顶部路径、应用卡片与搜索；配置页采用左侧表单／右侧发布操作，会话页采用左侧对话与输入／右侧运行过程。窄屏导航折叠，双栏自动堆叠。

新建表单同时填写名称、选择 ReAct／Plan、填写可选描述。执行模式没有默认选项，未选择不能创建；创建后只读展示，Plan 才显示专业执行器配置。点击创建或名称输入框按 Enter 使用相同校验。

配置页按「基础配置／能力配置／运行环境」组织，包含 Tool、Skill、MCP 成员选择、Plan 各角色独立授权、执行沙箱开关、工作区读写权限及文件保留策略。保存、放弃修改和发布快照涵盖这些配置；校验会检查 MCP 空成员、exec 缺少沙箱以及只读工作区与写入工具冲突。

沙箱服务默认未接入；「已接入（模拟）」仅用于体验未来环境服务可用后的交互，不代表当前 SDK 已完成隔离执行环境。工作文件是否保留与执行沙箱是否开启是两个独立选项。

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

导出快照包含独立运行所需的显示与状态兼容代码，部署时不需要安装该技能。浏览器中的模拟操作不代表 SDK 或平台集成测试通过。
