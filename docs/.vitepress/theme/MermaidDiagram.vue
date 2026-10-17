<script setup lang="ts">
import { computed, ref, nextTick, onMounted, onBeforeUnmount, watch } from "vue";
import { useData } from "vitepress";

const props = defineProps<{ source: string }>();
const { isDark } = useData();
const markup = ref("");
const error = ref(false);
const content = ref<HTMLElement | null>(null);
const scrollContainer = ref<HTMLElement | null>(null);
const inlineHeight = ref(0);
const dialog = ref<HTMLDialogElement | null>(null);
const expanded = ref(false);
const diagramWidth = ref("");
const zoom = ref(1.15);
const zoomedWidth = computed(() => diagramWidth.value ? `${parseFloat(diagramWidth.value) * zoom.value}px` : undefined);
let revision = 0;
let alive = true;
let scrollPosition = 0;

function expandDiagram() {
  if (!dialog.value || !markup.value || error.value) return;
  scrollPosition = window.scrollY;
  inlineHeight.value = scrollContainer.value?.getBoundingClientRect().height || 0;
  zoom.value = 1.15;
  expanded.value = true;
  dialog.value.showModal();
  window.scrollTo(0, scrollPosition);
}

async function restoreDiagram() {
  expanded.value = false;
  await nextTick();
  window.scrollTo(0, scrollPosition);
}

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
    const rendered = (expanded.value ? dialog.value : content.value)?.querySelector("svg");
    const width = rendered?.viewBox.baseVal.width;
    if (width) diagramWidth.value = `${width}px`;
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
    <div class="loom-diagram-toolbar">
      <span>手绘架构图</span>
      <button v-if="markup && !error" type="button" @click="expandDiagram" aria-haspopup="dialog">放大查看 ↗</button>
    </div>
    <div ref="scrollContainer" class="loom-diagram-scroll" :style="{ height: expanded ? `${inlineHeight}px` : undefined }">
      <template v-if="!expanded">
        <div v-if="markup && !error" ref="content" class="loom-diagram-content" :style="{ '--loom-diagram-width': diagramWidth || undefined }" v-html="markup" />
        <pre v-else class="loom-diagram-source">{{ source }}</pre>
      </template>
    </div>
    <figcaption v-if="error">图表暂时未能显示，以上为原始定义。</figcaption>
    <dialog ref="dialog" class="loom-diagram-dialog" aria-label="放大架构图" @close="restoreDiagram">
      <div class="loom-diagram-toolbar">
        <span>架构关系图</span>
        <div class="loom-diagram-controls">
          <button type="button" aria-label="缩小图表" :disabled="zoom <= 0.5" @click="zoom = Math.max(0.5, zoom - 0.15)">−</button>
          <button type="button" aria-label="重置图表缩放" @click="zoom = 1">{{ Math.round(zoom * 100) }}%</button>
          <button type="button" aria-label="放大图表" :disabled="zoom >= 2" @click="zoom = Math.min(2, zoom + 0.15)">＋</button>
          <button type="button" @click="dialog?.close()">关闭 · Esc</button>
        </div>
      </div>
      <div v-if="expanded" class="loom-diagram-scroll">
        <div class="loom-diagram-content" :style="{ '--loom-diagram-width': zoomedWidth }" v-html="markup" />
      </div>
    </dialog>
  </figure>
</template>
