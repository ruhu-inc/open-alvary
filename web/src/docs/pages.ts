// Every documentation page: the Markdown in content/, plus the pages written from the API's own
// description (reference.ts) and the error codes (errors.ts). The sidebar order is NAV. Pure
// TypeScript, used by the site and by the build (web/docs-files.ts), which publishes each page as
// Markdown and the whole set as llms.txt.

import { formatCommand, mcpUrl, verifiedHosts } from "../ai-hosts";
import { errorsMarkdown } from "./errors";
import { frontMatter } from "./markdown";
import { type Examples, referenceOverview, referencePages, type Spec, type Tool } from "./reference";

export type DocPage = { slug: string; title: string; description: string; markdown: string };
export type NavItem = string | { group: string; items: string[] };
export type NavSection = { title: string; items: NavItem[] };
export type Plan = {
  id: string;
  name: string;
  description: string;
  allowance: {
    requests_per_minute: number;
    burst?: number;
    concurrency: number;
    bytes_per_day?: number;
    jurisdictions?: number;
    watches: number;
    digest: string;
    shared_watches?: boolean;
    webhook_endpoints: number;
  };
  listing: { order: number; price: string; for?: string; limits?: string };
};

export const docPath = (slug: string) => (slug ? `/docs/${slug}` : "/docs");

const n = (v: number) => v.toLocaleString("en");
// Data a day in decimal units, as the plans publish it: 200 MB, 2 GB.
const data = (v?: number) => (!v ? "" : v >= 1e9 ? `${n(v / 1e9)} GB` : `${n(v / 1e6)} MB`);

function plansTable(plans: Plan[]): string {
  const rows = [...plans].sort((a, b) => a.listing.order - b.listing.order);
  return [
    "| Plan | Price | Requests a minute | Burst | At once | Data a day | Jurisdictions | Monitors | Webhooks |",
    "|---|---|---|---|---|---|---|---|---|",
    ...rows.map((p) => {
      const a = p.allowance;
      const custom = p.listing.limits;
      return [
        `**${p.name}**`,
        p.listing.price,
        custom ?? n(a.requests_per_minute),
        custom ? "" : n(a.burst ?? a.requests_per_minute),
        custom ? "" : n(a.concurrency),
        custom ? "" : data(a.bytes_per_day),
        a.jurisdictions ? `${a.jurisdictions} of your choice` : "All",
        `${n(a.watches)}, ${a.digest === "daily" ? "daily or weekly" : a.digest} email${a.shared_watches ? ", shared" : ""}`,
        a.webhook_endpoints ? n(a.webhook_endpoints) : "None",
      ]
        .join(" | ")
        .replace(/^/, "| ")
        .concat(" |");
    }),
  ].join("\n");
}

function aiHostsMarkdown(): string {
  const endpoint = mcpUrl();
  const hosts = verifiedHosts();
  const authLabel = (method: string) =>
    method === "oauth_cimd"
      ? "OAuth in browser (Verified publisher)"
      : method === "oauth_dcr"
        ? "OAuth in browser"
        : "API key";
  const blocks: string[] = [
    [
      "| Tool | Sign-in | Summary |",
      "|---|---|---|",
      ...hosts.map((host) => `| **${host.name}** | ${authLabel(host.authMethod)} | ${host.summary} |`),
    ].join("\n"),
  ];
  for (const host of hosts) {
    const cliCommands: string[] = [];
    const steps = host.installSteps.map((step, i) => {
      const cmd = step.command ? formatCommand(step.command) : undefined;
      if (cmd && cmd !== endpoint) cliCommands.push(cmd);
      const inlineCommand = cmd ? ` \`${cmd}\`` : "";
      return `${i + 1}. ${step.label}${inlineCommand}`;
    });
    blocks.push(`### ${host.name}`);
    if (cliCommands.length) blocks.push("```bash\n" + cliCommands.join("\n") + "\n```");
    blocks.push(steps.join("\n"));
    const note = host.pluginRepoUrl
      ? `${host.consentNote} Plugin and agent skills: [${host.pluginRepoUrl.replace(/^https?:\/\//, "")}](${host.pluginRepoUrl}).`
      : host.consentNote;
    blocks.push(note);
  }
  return blocks.join("\n\n");
}

export function buildPages(
  files: Record<string, string>,
  spec: Spec,
  tools: Tool[],
  examples: Examples,
  plans: Plan[],
): { pages: Map<string, DocPage>; nav: NavSection[] } {
  const pages = new Map<string, DocPage>();
  const includes: Record<string, string> = {
    plans: plansTable(plans),
    "api-overview": referenceOverview(spec),
    "ai-hosts": aiHostsMarkdown(),
  };
  for (const [name, source] of Object.entries(files)) {
    const { meta, body } = frontMatter(source);
    const slug = name === "index" ? "" : name;
    const markdown = body
      .replace(/^\{\{example:(\w+)\}\}$/gm, (_, id) =>
        examples[id]?.answer === undefined
          ? ""
          : "```json\n" +
            JSON.stringify(examples[id].answer, null, 2) +
            "\n```" +
            (examples[id].note ? `\n\n${examples[id].note}` : ""),
      )
      .replace(/^\{\{([\w-]+)\}\}$/gm, (m, key) => includes[key] ?? m)
      .trim();
    pages.set(slug, { slug, title: meta.title ?? name, description: meta.description ?? "", markdown });
  }
  pages.set("errors", {
    slug: "errors",
    title: "Errors",
    description: "Every error code, what it means and what to do.",
    markdown: errorsMarkdown(),
  });
  const reference = referencePages(spec, tools, examples);
  for (const page of reference) pages.set(page.slug, page);
  const groups = [...new Set(reference.map((p) => p.group))];
  const nav: NavSection[] = [
    { title: "Start here", items: ["", "quickstart", "authentication", "plans", "console"] },
    {
      title: "Guides",
      items: [
        "search",
        "records",
        "retrieval",
        "jurisdictions",
        "rights",
        "pagination",
        "changes",
        "mirror",
        "webhooks",
        "ai-tools",
      ],
    },
    {
      title: "API reference",
      items: [
        "api",
        "errors",
        ...groups.map((group) => ({ group, items: reference.filter((p) => p.group === group).map((p) => p.slug) })),
      ],
    },
    { title: "Help", items: ["help", "changelog", "compatibility"] },
  ];
  for (const section of nav)
    for (const item of section.items)
      for (const slug of typeof item === "string" ? [item] : item.items)
        if (!pages.has(slug)) throw new Error(`The docs sidebar names a page that doesn't exist: ${slug}`);
  return { pages, nav };
}

// The sidebar's pages in reading order, for previous and next.
export function readingOrder(nav: NavSection[]): string[] {
  return nav.flatMap((section) => section.items.flatMap((item) => (typeof item === "string" ? [item] : item.items)));
}
