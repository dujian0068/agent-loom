import DefaultTheme from "vitepress/theme";
import MermaidDiagram from "./MermaidDiagram.vue";
import DocsHome from "./DocsHome.vue";
import "./style.css";

export default {
  extends: DefaultTheme,
  enhanceApp({ app }) {
    app.component("MermaidDiagram", MermaidDiagram);
    app.component("DocsHome", DocsHome);
  },
};
