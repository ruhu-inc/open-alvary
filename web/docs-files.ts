// At build time, every documentation page also as Markdown (/docs/<page>.md), the list of pages for
// AI tools (/llms.txt, as llmstxt.org describes), all of them in one file (/llms-full.txt), and the
// pages' addresses for the sitemap (/docs/index.json).

import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import type { Plugin } from "vite";
import { buildPages, docPath, type DocPage, type Plan, readingOrder } from "./src/docs/pages";
import type { Examples, Spec, Tool } from "./src/docs/reference";

const SITE = "https://open.alvary.ai";
const DOCS = join(__dirname, "src", "docs");

function load() {
  const files = Object.fromEntries(
    readdirSync(join(DOCS, "content"))
      .filter((name) => name.endsWith(".md"))
      .map((name) => [name.slice(0, -3), readFileSync(join(DOCS, "content", name), "utf8")]),
  );
  const json = (name: string) => JSON.parse(readFileSync(join(DOCS, "data", name), "utf8"));
  return buildPages(
    files,
    json("openapi.json") as Spec,
    json("mcp-tools.json") as Tool[],
    json("examples.json") as Examples,
    json("plans.json") as Plan[],
  );
}

const markdownUrl = (slug: string) => `${SITE}/docs/${slug || "index"}.md`;

// Plain Markdown for readers outside the site: absolute links, numbered steps as (1), cards as a list.
export function portable(page: DocPage): string {
  const body = page.markdown
    .replace(/^(#{1,4} .*?)\s+\{#[\w-]+\}$/gm, "$1")
    .replace(/^(\s*[-*]|\s*\d+\.)\s+\{(\d+)\}\s*/gm, "$1 ($2) ")
    .replace(/^:::cards\n([\s\S]*?)\n:::$/gm, (_, list: string) =>
      list.replace(/^-\s+\[([^\]]+)\]\(([^)]+)\)\s*(.*)$/gm, "- [$1]($2): $3"),
    )
    .replace(/\]\((\/[^)]*)\)/g, (_, path: string) => `](${SITE}${path})`);
  return `# ${page.title}\n\n> ${page.description}\n\n${body}\n`;
}

export function docsFiles(): Plugin {
  return {
    name: "open-alvary-docs-files",
    apply: "build",
    generateBundle() {
      const { pages, nav } = load();
      const emit = (fileName: string, source: string) => this.emitFile({ type: "asset", fileName, source });
      for (const page of pages.values()) emit(`docs/${page.slug || "index"}.md`, portable(page));
      const ordered = readingOrder(nav).map((slug) => pages.get(slug)!);
      const list = nav.flatMap((section) => [
        `## ${section.title}`,
        "",
        ...section.items
          .flatMap((item) => (typeof item === "string" ? [item] : item.items))
          .map((slug) => pages.get(slug)!)
          .map((page) => `- [${page.title}](${markdownUrl(page.slug)}): ${page.description}`),
        "",
      ]);
      emit(
        "llms.txt",
        [
          "# Open Alvary",
          "",
          "> A public catalogue of African law, with each record's official source and rights status. A read-only REST API (https://api.open.alvary.ai/api/v1) and MCP server (https://api.open.alvary.ai/mcp) for organisations with an account (the Starter plan is free), and a public change feed (https://open.alvary.ai/api/changes).",
          "",
          "Each page below is Markdown. The OpenAPI document is at https://api.open.alvary.ai/api/v1/openapi.json.",
          "",
          ...list,
          "## Optional",
          "",
          `- [All the documentation in one file](${SITE}/llms-full.txt)`,
          "",
        ].join("\n"),
      );
      emit("llms-full.txt", ordered.map(portable).join("\n---\n\n"));
      emit("docs/index.json", JSON.stringify(ordered.map((page) => docPath(page.slug))));
    },
  };
}
