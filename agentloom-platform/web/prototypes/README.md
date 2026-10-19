# Platform 交互原型

这是一份基于示例数据的交互预览，不是平台前端实现，不连接 API、模型或凭据。

- [在线体验](https://doc.agentloom.bmilk.cn/prototypes/platform-interaction/)
- [交互设计说明](../../../docs/platform/interaction-design.md)
- [可编辑片段](platform-interaction.fragment.html)
- [独立网页导出](../../../docs/public/prototypes/platform-interaction/index.html)

网页顶部的「原型预览设置」可切换使用者角色、连接状态和异常场景。配置、校验、发布、补充和停止均在浏览器内模拟；资源与成员页只展示信息布局。刷新会重置应用与运行，只保留预览设置。

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
