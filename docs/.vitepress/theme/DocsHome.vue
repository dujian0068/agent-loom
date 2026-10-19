<script setup lang="ts">
import { withBase } from "vitepress";

const modules = [
  {
    number: "01",
    name: "SDK 设计",
    label: "THE EXECUTION FOUNDATION",
    description: "深入执行底座，理解 Agent 如何协作、管理上下文，并从检查点恢复。",
    tags: ["Agent 与 Workflow", "运行时与事件", "上下文与恢复"],
    link: "/sdk/",
    className: "sdk",
  },
  {
    number: "02",
    name: "Platform 设计",
    label: "THE APPLICATION LAYER",
    description: "从资源与配置到应用发布，理解平台如何通过公共接口装配 SDK。",
    tags: ["领域与资源", "配置与发布", "API 与生命周期"],
    link: "/platform/",
    className: "platform",
  },
];
const shared = [
  { name: "阅读指南", detail: "找到适合你的阅读路径", link: "/reading-guide.html" },
  { name: "Agent 设计约定", detail: "了解目标、规则与默认行为", link: "/agent-design-decisions.html" },
  { name: "SDK 与 Platform 的职责边界", detail: "先厘清各层负责什么", link: "/sdk-platform-boundary.html" },
];
</script>

<template>
  <main class="loom-home">
    <section class="loom-home-hero" aria-labelledby="loom-home-title">
      <div class="loom-home-intro">
        <p class="loom-eyebrow"><span /> AGENTLOOM / DESIGN DOCUMENTATION</p>
        <h1 id="loom-home-title">从执行底座，<br />到<span>应用平台。</span></h1>
        <p class="loom-home-description">沿着职责、对象与交互，理解 AgentLoom。<br class="loom-desktop-break" />两组独立的设计文档，一套清晰的架构边界。</p>
        <a class="loom-reading-link" :href="withBase('/reading-guide.html')">第一次阅读？从阅读指南开始 <span aria-hidden="true">↗</span></a>
      </div>
      <div class="loom-architecture" aria-label="Platform 通过公共接口装配 SDK，SDK 负责执行与恢复">
        <div class="loom-map-topline"><span>架构一览</span><span class="loom-map-dot" /></div>
        <div class="loom-map-layer loom-map-platform">
          <span class="loom-map-index">应用层 / 01</span>
          <strong>Platform</strong>
          <span>配置 · 资源 · 发布 · API</span>
        </div>
        <div class="loom-map-connector"><span />通过公共接口装配<span aria-hidden="true">↓</span></div>
        <div class="loom-map-layer loom-map-sdk">
          <span class="loom-map-index">执行层 / 02</span>
          <strong>SDK</strong>
          <span>Agent · Workflow · Runtime</span>
        </div>
        <div class="loom-map-footnote"><span />执行与恢复由 SDK 负责</div>
      </div>
    </section>

    <a class="loom-prototype-banner" :href="withBase('/prototypes/platform-interaction/index.html')" target="_blank" rel="noopener noreferrer" aria-label="体验 Platform 交互原型（新标签页）">
      <span class="loom-prototype-icon" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none"><rect x="3" y="4" width="18" height="16" rx="3" /><path d="M3 9h18M7 6.5h.01M10 6.5h.01M10 12l5 3-5 3z" /></svg></span>
      <div><span class="loom-prototype-kicker">交互原型 · 示例数据</span><h2>Platform 交互体验</h2><p>从应用配置到发布与会话，直接体验平台的页面和操作流程。</p></div>
      <span class="loom-prototype-action">进入体验 <span aria-hidden="true">↗</span></span>
    </a>

    <section class="loom-module-section" aria-labelledby="loom-modules-title">
      <div class="loom-section-heading"><h2 id="loom-modules-title">选择你的阅读入口</h2><span>独立阅读，按需深入</span></div>
      <div class="loom-module-grid">
        <a v-for="module in modules" :key="module.link" :href="withBase(module.link)" class="loom-module-card" :class="module.className">
          <div class="loom-module-top"><span class="loom-module-number">{{ module.number }}</span><span class="loom-card-arrow" aria-hidden="true">↗</span></div>
          <p class="loom-module-label">{{ module.label }}</p>
          <h3>{{ module.name }}</h3>
          <p class="loom-module-description">{{ module.description }}</p>
          <div class="loom-module-tags"><span v-for="tag in module.tags" :key="tag">{{ tag }}</span></div>
          <div class="loom-module-bottom">进入设计文档 <span aria-hidden="true">→</span></div>
        </a>
      </div>
    </section>

    <section class="loom-shared-section" aria-labelledby="loom-shared-title">
      <div class="loom-section-heading"><h2 id="loom-shared-title">开始之前，建立共同理解</h2><span>公共文档</span></div>
      <div class="loom-shared-grid">
        <a v-for="item in shared" :key="item.link" :href="withBase(item.link)" class="loom-shared-link"><div><h3>{{ item.name }}</h3><p>{{ item.detail }}</p></div><span aria-hidden="true">↗</span></a>
      </div>
    </section>

    <div class="loom-home-note"><span class="loom-note-icon" aria-hidden="true">i</span><p>设计描述目标架构，当前能力与验证范围以<span>实施记录</span>为准。</p><a :href="withBase('/sdk/implementation.html')">查看当前实现 <span aria-hidden="true">→</span></a></div>
    <footer class="loom-home-footer"><span>AgentLoom</span><span>职责清晰，设计可循。</span></footer>
  </main>
</template>
