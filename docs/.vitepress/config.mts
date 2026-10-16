import { defineConfig } from "vitepress";
import { existsSync, statSync, readFileSync, writeFileSync, mkdirSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const docsRoot = path.resolve(fileURLToPath(new URL("..", import.meta.url)));
const repoRoot = path.dirname(docsRoot);
const repository = "https://github.com/dujian0068/agent-loom";
const base = process.env.AGENTLOOM_DOCS_BASE || "/";
if (!base.startsWith("/") || !base.endsWith("/")) {
  throw new Error("AGENTLOOM_DOCS_BASE 必须以 / 开始和结束，例如 /agent-loom/");
}

export default defineConfig({
  lang: "zh-CN",
  title: "AgentLoom",
  description: "AgentLoom SDK 与低代码平台设计文档",
  base,
  lastUpdated: true,
  cleanUrls: false,
  ignoreDeadLinks: false,
  vite: {
    server: { strictPort: true },
    preview: { strictPort: true },

  },
  themeConfig: {
    siteTitle: "AgentLoom 文档",

    nav: [
      { text: "职责边界", link: "/sdk-platform-boundary" },
      { text: "SDK", link: "/sdk/" },
      { text: "Platform", link: "/platform/" },
      { text: "对象 UML", link: "/sdk/object-collaboration" },
    ],
    socialLinks: [{ icon: "github", link: repository }],
    sidebar: [
      {
        text: "开始阅读",
        items: [
          { text: "文档首页", link: "/" },
          { text: "职责边界", link: "/sdk-platform-boundary" },
          { text: "Agent 设计约定", link: "/agent-design-decisions" },
          { text: "阅读指南", link: "/reading-guide" },
        ],
      },
      {
        text: "SDK · 执行底座",
        items: [
          { text: "SDK 总览", link: "/sdk/" },
          { text: "行为约定与默认值", link: "/sdk/requirements" },
          { text: "定义、节点与运行实体", link: "/sdk/definitions" },
          { text: "对象协作 UML 与时序", link: "/sdk/object-collaboration" },
          { text: "构造、资源绑定与生命周期", link: "/sdk/assembly" },
          { text: "公共 API 与依赖注入", link: "/sdk/public-api" },
          { text: "Agent Loop 与轻量图调度", link: "/sdk/runtime" },
          { text: "EventBus、Handler 与 Hook", link: "/sdk/event-bus" },
          { text: "上下文、历史与压缩", link: "/sdk/context" },
          { text: "模型、能力与环境", link: "/sdk/capabilities" },
          { text: "检查点与故障接管", link: "/sdk/checkpoints" },
          { text: "Workflow 与双引擎适配", link: "/sdk/workflow" },
          { text: "实施与验收", link: "/sdk/verification" },
        ],
      },
      {
        text: "Platform · 配置平台",
        items: [
          { text: "Platform 总览", link: "/platform/" },
          { text: "管理领域与实体", link: "/platform/domain-model" },
          { text: "配置、资源与发布", link: "/platform/configuration" },
          { text: "SDK 装配与恢复接入", link: "/platform/sdk-integration" },
          { text: "HTTP API 与输入协议", link: "/platform/api" },
          { text: "存储与生命周期", link: "/platform/storage-lifecycle" },
          { text: "API 集成验收", link: "/platform/verification" },
        ],
      },
      {
        text: "文档维护",
        collapsed: true,
        items: [
          { text: "目录规划", link: "/directory-plan" },
          { text: "完整索引", link: "/README" },
          { text: "整理复核记录", link: "/reorganization-review" },
          { text: "维护文档站", link: "/documentation-site" },
        ],
      },
    ],
    outline: { level: [2, 3], label: "本页目录" },
    docFooter: { prev: "上一篇", next: "下一篇" },
    returnToTopLabel: "返回顶部",
    sidebarMenuLabel: "文档目录",
    darkModeSwitchLabel: "主题",
    lightModeSwitchTitle: "切换为浅色",
    darkModeSwitchTitle: "切换为深色",
    skipToContentLabel: "跳到正文",
    editLink: {
      pattern: `${repository}/edit/master/docs/:path`,
      text: "在 GitHub 编辑此页",
    },
    lastUpdated: { text: "最近更新", formatOptions: { dateStyle: "medium" } },
    search: {
      provider: "local",
      options: {
        _render(src, env, md) {
          const html = md.render(src, env);
          return env.frontmatter?.search === false ? "" : html;
        },
        miniSearch: {
          options: {
            // The same self-contained function runs during indexing and in the browser.
            tokenize: (text: string) => {
              const normalized = text.toLocaleLowerCase("zh-CN");
              const words = Array.from(
                new Intl.Segmenter("zh-CN", { granularity: "word" }).segment(
                  normalized,
                ),
              )
                .filter((part) => part.isWordLike)
                .map((part) => part.segment);
              const han = normalized.match(/[\p{Script=Han}]+/gu) || [];
              for (const run of han) {
                for (let i = 0; i < run.length - 1; i++)
                  words.push(run.slice(i, i + 2));
              }
              return Array.from(new Set(words));
            },
          },
          searchOptions: {
            prefix: true,
            fuzzy: 0.1,
            boost: { title: 5, titles: 2, text: 1 },
            boostDocument: (id: string) => {
              if (/\/(sdk|platform)\//.test(id)) return 3;
              if (/agent-design-decisions/.test(id)) return 3;
              return /README/.test(id) ? 0.5 : 1;
            },
          },
        },
        translations: {
          button: { buttonText: "搜索文档", buttonAriaLabel: "搜索文档" },
          modal: {
            displayDetails: "显示详细内容",
            resetButtonTitle: "清空搜索",
            backButtonTitle: "关闭搜索",
            noResultsText: "没有找到相关内容",
            footer: {
              selectText: "选择",
              selectKeyAriaLabel: "回车",
              navigateText: "切换",
              navigateUpKeyAriaLabel: "上",
              navigateDownKeyAriaLabel: "下",
              closeText: "关闭",
              closeKeyAriaLabel: "Esc",
            },
          },
        },
      },
    },
  },
  async buildEnd(site) {
    const redirects: Record<string, string> = JSON.parse(
      readFileSync(path.join(docsRoot, ".vitepress/redirects.json"), "utf8"),
    );
    for (const [from, to] of Object.entries(redirects)) {
      if (![from, to].every((value) =>
        /^[a-z0-9/-]+\.html$/.test(value) &&
        !value.startsWith("/") && !value.split("/").includes("..")
      )) throw new Error("Invalid documentation redirect");
      const output = path.join(site.outDir, from);
      const destination = base + to;
      if (!existsSync(path.join(site.outDir, to))) {
        throw new Error("Missing redirect target: " + to);
      }
      mkdirSync(path.dirname(output), { recursive: true });
      writeFileSync(output,
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">' +
        '<meta name="robots" content="noindex"><title>文档已迁移</title>' +
        '<meta http-equiv="refresh" content="0;url=' + destination + '">' +
        '<link rel="canonical" href="' + destination + '">' +
        '</head><body><p>文档已整理到 <a href="' + destination + '">新章节</a>。</p>' +
        '<script>location.replace(' + JSON.stringify(destination) + ')</script></body></html>'
      );
    }
  },
  markdown: {
    anchor: {
      // Keep GitHub-style heading links, including existing numeric Chinese anchors.
      slugify: (text: string) =>
        text
          .toLowerCase()
          .replace(/[^\p{L}\p{N}\p{M}_\-\s]/gu, "")
          .trim()
          .replace(/\s/g, "-"),
    },
    config(md) {
      // Retain repository-relative Markdown for GitHub while giving the site valid source links.
      md.core.ruler.after("inline", "repository-links", (state) => {
        const current = state.env.path || path.join(docsRoot, "index.md");
        for (const block of state.tokens) {
          for (const token of block.children || []) {
            if (token.type !== "link_open") continue;
            const href = token.attrGet("href");
            if (!href) continue;
            if (/^https?:\/\/localhost(?::|\/|$)/i.test(href)) {
              // These documented local application URLs are external to the documentation site.
              token.attrSet("target", "_blank");
              token.attrSet("rel", "noopener noreferrer");
              continue;
            }
            if (/^(?:[a-z]+:|\/|#)/i.test(href)) continue;
            const [pathname, suffix = ""] = href.split(/(?=[?#])/s, 2);
            const target = path.resolve(
              path.dirname(current),
              decodeURIComponent(pathname),
            );
            if (!existsSync(target)) continue;
            const relative = path
              .relative(repoRoot, target)
              .split(path.sep)
              .join("/");
            const insideDocs = target.startsWith(docsRoot + path.sep);
            if (insideDocs && target.endsWith(".svg")) {
              token.attrSet("target", "_blank");
              token.attrSet("rel", "noopener noreferrer");
              continue;
            }
            if (insideDocs && target.endsWith(".md")) continue;
            if (relative.startsWith("../")) continue;
            const kind = statSync(target).isDirectory() ? "tree" : "blob";
            token.attrSet(
              "href",
              `${repository}/${kind}/master/${relative.split("/").map(encodeURIComponent).join("/")}${suffix}`,
            );
            token.attrSet("target", "_blank");
            token.attrSet("rel", "noopener noreferrer");
          }
        }
      });
      const fence = md.renderer.rules.fence!;
      md.renderer.rules.fence = (tokens, index, options, env, self) => {
        if (tokens[index].info.trim() === "mermaid") {
          const source = md.utils.escapeHtml(
            JSON.stringify(tokens[index].content),
          );
          return `<MermaidDiagram :source="${source}" />`;
        }
        return fence(tokens, index, options, env, self);
      };
    },
  },
});
