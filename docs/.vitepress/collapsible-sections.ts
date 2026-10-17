import type { MarkdownRenderer } from "vitepress";

/** Preserve real headings and anchors while making each section a native disclosure. */
export function collapsibleSections(md: MarkdownRenderer) {
  md.core.ruler.push("loom-collapsible-sections", (state) => {
    if (state.env.frontmatter?.layout === "home") return;
    const output = [];
    const levels: number[] = [];
    let rawDetailsDepth = 0;

    const html = (content: string) => {
      const token = new state.Token("html_block", "", 0);
      token.content = content + "\n";
      return token;
    };
    const closeSection = () => {
      output.push(html("</div></details>"));
      levels.pop();
    };

    for (let i = 0; i < state.tokens.length; i++) {
      const token = state.tokens[i];
      // Author-written disclosures keep their own structure and open/closed state.
      if (token.type === "html_block") {
        rawDetailsDepth += (token.content.match(/<details(?:\s|>)/gi) || []).length;
        rawDetailsDepth -= (token.content.match(/<\/details\s*>/gi) || []).length;
      }
      const level = Number(token.tag.slice(1));
      if (token.type === "heading_open" && token.level === 0 && rawDetailsDepth === 0 && level === 1) {
        while (levels.length) closeSection();
      }
      if (token.type !== "heading_open" || token.level !== 0 || rawDetailsDepth > 0 || level < 2 || level > 6) {
        output.push(token);
        continue;
      }

      while (levels.length && levels[levels.length - 1] >= level) closeSection();
      const id = token.attrGet("id");
      const labelledBy = id ? ` aria-labelledby="${md.utils.escapeHtml(id)}"` : "";
      output.push(html(`<details class="loom-section" data-level="${level}" open${labelledBy}><summary class="loom-section-summary">`));
      // Keep the original heading tokens, including VitePress's permalink.
      output.push(token);
      while (++i < state.tokens.length) {
        output.push(state.tokens[i]);
        if (state.tokens[i].type === "heading_close") break;
      }
      output.push(html('</summary><div class="loom-section-body">'));
      levels.push(level);
    }
    while (levels.length) closeSection();
    state.tokens = output;
  });
}
