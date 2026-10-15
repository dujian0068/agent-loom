# 维护与部署文档站

文档使用 VitePress 1.6.4 生成网页。Node.js 需要 22.12+。`docs/*.md` 仍是正文来源，可继续在 GitHub 阅读；侧栏、搜索和主题由 `docs/.vitepress` 配置。不维护第二套 HTML 正文。

## 本地阅读

在项目根目录安装文档依赖，然后启动：

```sh
npm ci
npm run docs:dev
```

打开 http://127.0.0.1:8767 。修改 Markdown 后页面会自动更新。文档站独立于业务后端，不需要数据库、供应商 Key 或 `.env`。

## 构建与预览

```sh
npm run docs:build
npm run docs:preview
```

输出位于 `docs/.vitepress/dist`，可以部署到静态 Web 服务器。预览默认也使用端口 8767，先关闭同端口开发服务再启动预览。重新构建后须重启预览服务，再刷新浏览器，避免旧页面引用已替换的资源。

构建产物、依赖和缓存被 Git 忽略；提交 Markdown、主题、配置和根目录的 package-lock.json 即可。

## 使用子路径部署

若站点地址使用 `/agent-loom/` 等前缀，构建时设置：

```sh
AGENTLOOM_DOCS_BASE=/agent-loom/ npm run docs:build
AGENTLOOM_DOCS_BASE=/agent-loom/ npm run docs:preview
```

上传 dist 内容到服务器的对应路径。GitHub Pages 使用下面的自动部署流程；自行部署服务器时仍可使用这些命令。

站内使用 `.html` 地址，静态服务器无需配置单页应用回退。保留 `404.html` 作为未找到页面。首页为 `index.html`。

## GitHub Pages 自动部署

项目文档入口：[AgentLoom 文档站](https://doc.agentloom.bmilk.cn/)。这是 `agent-loom` 仓库的项目站，使用独立自定义域名，与个人博客分别部署。

首次启用：

1. 打开本仓库的 [Settings → Pages](https://github.com/dujian0068/agent-loom/settings/pages)。
2. 在 **Build and deployment → Source** 选择 **GitHub Actions**。
3. 在 **Custom domain** 中设置 `doc.agentloom.bmilk.cn`。DNS 中的 `doc.agentloom` CNAME 指向 `dujian0068.github.io`，不包含仓库名。
4. DNS 检查通过、证书就绪后勾选 **Enforce HTTPS**。
5. 向 `master` 推送提交，或到 [Actions](https://github.com/dujian0068/agent-loom/actions/workflows/docs-pages.yml) 选择 **Deploy documentation to GitHub Pages → Run workflow → master**。
6. 等待 Build 和 Deploy 都成功，再通过部署结果中的链接打开网站。初次部署前入口可能返回 404。

工作流位于 [docs-pages.yml](../.github/workflows/docs-pages.yml)。`master` 上的文档、文档依赖或此工作流更新时，自动执行 `npm ci`、构建、校验站内链接并发布；也支持手动重跑。构建前通过 `actions/configure-pages` 读取实际 Pages 地址：自定义域名使用根路径 `/`，未绑定自定义域名时自动使用对应项目子路径，确保页面、导航和资源地址一致。仅上传 `docs/.vitepress/dist`。

自定义域名以仓库 **Settings → Pages** 的绑定为准。使用 GitHub Actions 发布时，GitHub 不会通过仓库里的 `CNAME` 文件设置域名；保留的 CNAME 文件仅与当前绑定保持一致，修改文件后仍须同步修改 Pages 设置。

构建任务只有仓库和 Pages 设置的读取权限；部署任务使用 GitHub 内置 `GITHUB_TOKEN` 的 Pages 写入权限和 OIDC，不需要添加个人 Token 或服务器密码。部署使用 `github-pages` 环境，并串行执行，避免覆盖正在发布的版本。无需创建 `gh-pages` 分支，也无需改动个人博客仓库。

发布失败时先查看 Actions 中失败的步骤：构建或链接校验失败须修正文档；Pages 未启用时先完成第 2 步再重跑；若仓库有环境审批规则，需要按已有规则批准部署。要发布旧版内容，应将文档变更恢复到 `master` 后重新部署。

## 依赖与校验

根目录 package.json/lock 仅管理文档依赖。旧业务前端及其依赖清单已删除；新 SDK / Platform 尚未建立产品构建流程。构建工具固定为 Vite 7.3.7、Vue 插件 6.0.8，Mermaid 的 KaTeX 固定为 0.19.0；升级时须同时复验构建和导航。

`npm run docs:check` 在构建后检查产物里的站内页面、锚点、图片和脚本链接，需要 Python 3。子路径构建使用 `npm run docs:check -- --base /agent-loom/`。

## 维护规则

- 新增文档后在 `docs/.vitepress/config.mts` 中添加侧栏，并更新文档索引。
- 正文继续写相对 Markdown 链接，例如 `design/overview.md`；构建时自动转换成站内地址。
- 指向仓库中实际存在文件的相对链接由文档插件转到 GitHub `master` 的 blob/tree 地址，不会把源码复制进站点；不要继续链接已删除的旧业务代码。
- 原有显式 `<a id="...">` 锚点保留；更名标题前检查入站章节链接。
- 搜索在浏览器本地运行，支持中文分词；不把搜索词发送到外部搜索服务。旧使用说明已删除；保留的在审设计需标明 SDK / Platform 边界调整状态，不能当成已实现接口。
- Mermaid 代码块在浏览器渲染为 SVG；当前图表直接维护在设计正文，不保留已过时的独立配图或生成脚本。
- VitePress 的生产构建会检查页面链接，不要用忽略所有死链的选项掩盖错误。

框架参考：[VitePress](https://vitepress.dev/guide/getting-started)、[本地搜索](https://vitepress.dev/reference/default-theme-search)、[Mermaid](https://mermaid.js.org/config/usage.html)、[GitHub Pages 工作流](https://docs.github.com/en/pages/getting-started-with-github-pages/using-custom-workflows-with-github-pages)。
