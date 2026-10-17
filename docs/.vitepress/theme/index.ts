import DefaultTheme from "vitepress/theme";
import MermaidDiagram from "./MermaidDiagram.vue";
import DocsHome from "./DocsHome.vue";
import SectionControls from "./SectionControls.vue";
import { h } from "vue";
import "./style.css";

export default {
  extends: DefaultTheme,
  Layout: () => h(DefaultTheme.Layout, null, {
    "doc-before": () => h(SectionControls),
  }),
  enhanceApp({ app }) {
    app.component("MermaidDiagram", MermaidDiagram);
    app.component("DocsHome", DocsHome);
  },
};
