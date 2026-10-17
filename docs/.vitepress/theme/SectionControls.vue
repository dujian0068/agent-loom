<script setup lang="ts">
import { onMounted, onBeforeUnmount, ref } from "vue";
import { getScrollOffset, onContentUpdated } from "vitepress";

const sectionCount = ref(0);
let scrollFrame = 0;
const article = () => document.querySelector<HTMLElement>(".VPDoc .vp-doc");

function setAll(open: boolean) {
  article()?.querySelectorAll<HTMLDetailsElement>("details").forEach((section) => {
    section.open = open;
  });
}

function reveal(target: Element) {
  let changed = false;
  let section = target.closest("details");
  while (section && article()?.contains(section)) {
    if (!section.open) {
      section.open = true;
      changed = true;
    }
    section = section.parentElement?.closest("details") || null;
  }
  return changed;
}

function revealHash(hash = window.location.hash) {
  if (!hash) return;
  let target: HTMLElement | null;
  try {
    target = document.getElementById(decodeURIComponent(hash.slice(1)));
  } catch {
    return;
  }
  if (!target || !article()?.contains(target) || !reveal(target)) return;
  cancelAnimationFrame(scrollFrame);
  // Run after VitePress's anchor navigation, once the hidden content has layout.
  scrollFrame = requestAnimationFrame(() => {
    if (!target?.isConnected) return;
    const padding = parseFloat(getComputedStyle(target).paddingTop) || 0;
    window.scrollTo(0, window.scrollY + target.getBoundingClientRect().top - getScrollOffset() + padding);
  });
}

function updateContent() {
  sectionCount.value = article()?.querySelectorAll("details.loom-section").length || 0;
  revealHash();
}
function onHashChange() { revealHash(); }
function onClick(event: MouseEvent) {
  if (event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
  const link = (event.target as Element)?.closest?.("a[href]");
  if (!link || link.hasAttribute("target") || link.hasAttribute("download")) return;
  const destination = new URL(link.getAttribute("href")!, window.location.href);
  if (destination.origin === location.origin && destination.pathname === location.pathname) revealHash(destination.hash);
}
function onBeforeMatch(event: Event) {
  if (event.target instanceof Element) reveal(event.target);
}

onContentUpdated(updateContent);
onMounted(() => {
  updateContent();
  window.addEventListener("hashchange", onHashChange);
  window.addEventListener("click", onClick, true);
  document.addEventListener("beforematch", onBeforeMatch);
});
onBeforeUnmount(() => {
  cancelAnimationFrame(scrollFrame);
  window.removeEventListener("hashchange", onHashChange);
  window.removeEventListener("click", onClick, true);
  document.removeEventListener("beforematch", onBeforeMatch);
});
</script>

<template>
  <div v-if="sectionCount" class="loom-section-tools" aria-label="章节折叠控制">
    <span>点击章节标题可折叠</span>
    <div><button type="button" @click="setAll(true)">全部展开</button><button type="button" @click="setAll(false)">全部收起</button></div>
  </div>
</template>
