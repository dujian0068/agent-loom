import { defineConfig } from "vitepress";
import { existsSync, statSync } from "node:fs";
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
      { text: "阅读指南", link: "/reading-guide" },
      { text: "系统设计", link: "/design/overview" },
      { text: "SDK / Platform", link: "/design/sdk-platform-boundary" },
    ],
    socialLinks: [{ icon: "github", link: repository }],
    sidebar: [
      {
        text: "开始阅读",
        items: [
          { text: "文档首页", link: "/" },
          { text: "SDK / Platform 重建边界", link: "/design/sdk-platform-boundary" },
          { text: "重建目录", link: "/directory-plan" },
          { text: "阅读指南", link: "/reading-guide" },
          { text: "Agent 设计约定", link: "/agent-design-decisions" },
        ],
      },
      {
        text: "系统设计草案 · 待按交付边界调整",
        items: [
          { text: "系统设计总览", link: "/design/overview" },
          { text: "领域实体与 UML", link: "/design/domain-model" },
          { text: "Definition、Node 与图引擎", link: "/design/graph-node-engine" },
          { text: "双引擎与检查点适配", link: "/design/dual-engine-integration" },
          { text: "数据结构与契约", link: "/design/data-contracts" },
          { text: "检查点结构与父子恢复", link: "/design/checkpoint-protocol" },
          { text: "领域接口与交互", link: "/design/domain-interfaces" },
          { text: "Agent Loop 与委派", link: "/design/agent-execution" },
          { text: "上下文与生命周期", link: "/design/context-lifecycle" },
          { text: "能力、Hook 与环境", link: "/design/capabilities-hooks" },
          { text: "事件总线 EventBus", link: "/design/runtime-messaging-checkpoints" },
          { text: "存储、恢复与 API", link: "/design/storage-recovery-api" },
          { text: "实现与验收", link: "/design/verification-roadmap" },
          { text: "架构决策与后续事项", link: "/design/architecture-discussion" },
        ],
      },
      {
        text: "实施草案 · 待按交付边界调整",
        items: [
          { text: "实施入口与开发顺序", link: "/design/implementation/" },
          { text: "配置、发布与应用 API", link: "/design/implementation/catalog-api" },
          { text: "持久结构、事务与恢复", link: "/design/implementation/persistence" },
          { text: "Runtime、Loop 与图执行", link: "/design/implementation/runtime-graph" },
          { text: "事件总线、调用与 Hook", link: "/design/implementation/invocation" },
          { text: "上下文、能力与执行环境", link: "/design/implementation/context-capabilities" },
        ],
      },
      {
        text: "文档维护",
        collapsed: true,
        items: [
          { text: "完整文档索引", link: "/README" },
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
              if (/\/design\//.test(id)) return 3;
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
