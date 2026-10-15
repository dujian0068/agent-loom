<script setup lang="ts">
import { ref, nextTick, onMounted, onBeforeUnmount, watch } from "vue";
import { useData } from "vitepress";

const props = defineProps<{ source: string }>();
const { isDark } = useData();
const markup = ref("");
const error = ref(false);
const content = ref<HTMLElement | null>(null);
let revision = 0;
let alive = true;

async function renderDiagram() {
  const current = ++revision;
  try {
    const { default: mermaid } = await import("mermaid");
    if (!alive || current !== revision) return;
    mermaid.initialize({
      startOnLoad: false,
      securityLevel: "strict",
      theme: "base",
      look: "handDrawn",
      handDrawnSeed: 42,
      fontFamily: "ui-sans-serif, system-ui, sans-serif",
      fontSize: 13,
      themeVariables: isDark.value
        ? {
            darkMode: true,
            fontSize: "13px",
            background: "#1b2027",
            primaryColor: "#213a37",
            primaryTextColor: "#e8f3f0",
            primaryBorderColor: "#568d83",
            secondaryColor: "#293743",
            tertiaryColor: "#292b39",
            lineColor: "#8ca7a2",
          }
        : {
            fontSize: "13px",
            background: "#ffffff",
            primaryColor: "#eaf5f2",
            primaryTextColor: "#18352f",
            primaryBorderColor: "#77aea3",
            secondaryColor: "#edf3f9",
            tertiaryColor: "#f5f2fa",
            lineColor: "#708d87",
          },
      flowchart: {
        wrappingWidth: 190,
        minNodeWidth: 72,
        nodeSpacing: 36,
        rankSpacing: 42,
        diagramPadding: 12,
      },
    });
    const id = `loom-diagram-${crypto.randomUUID()}`;
    const { svg } = await mermaid.render(id, props.source);
    if (!alive || current !== revision) return;
    markup.value = svg;
    error.value = false;
    await nextTick();
    // Mermaid emits width="100%"; preserve its measured layout so wide diagrams
    // scroll instead of shrinking their text and boxes inside the article column.
    const rendered = content.value?.querySelector("svg");
    const width = rendered?.viewBox.baseVal.width;
    if (rendered && width) {
      rendered.style.width = `${width}px`;
      rendered.style.maxWidth = "none";
    }
  } catch {
    if (alive && current === revision) error.value = true;
  }
}
onMounted(renderDiagram);
watch([() => props.source, isDark], renderDiagram);
onBeforeUnmount(() => {
  alive = false;
  revision++;
});
</script>

<template>
  <figure class="loom-diagram" aria-label="架构关系图">
    <div v-if="markup && !error" ref="content" class="loom-diagram-content" v-html="markup" />
    <pre v-else class="loom-diagram-source">{{ source }}</pre>
    <figcaption v-if="error">图表暂时未能显示，以上为原始定义。</figcaption>
  </figure>
</template>
