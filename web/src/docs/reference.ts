// The API reference, written as Markdown pages from the API's own OpenAPI document and MCP tool list
// (data/, copied from the code by scripts/docs_reference.py) and the example answers in
// data/examples.json (recorded from a running copy). Nothing here is typed by hand twice.

import { CODES } from "./errors";

export const API_ORIGIN = "https://api.open.alvary.ai";
export const BASE = `${API_ORIGIN}/api/v1`;
export const PUBLIC_BASE = "https://open.alvary.ai/api";

type Schema = {
  $ref?: string;
  type?: string | string[];
  enum?: unknown[];
  items?: Schema;
  properties?: Record<string, Schema>;
  additionalProperties?: Schema | boolean;
  anyOf?: Schema[];
  oneOf?: Schema[];
  allOf?: Schema[];
  description?: string;
  default?: unknown;
  minimum?: number;
  maximum?: number;
  format?: string;
  required?: string[];
};
type Parameter = { name: string; in: string; required?: boolean; description: string; schema: Schema };
type Operation = {
  operationId: string;
  summary: string;
  description: string;
  tags: string[];
  parameters?: Parameter[];
  security?: unknown[];
  requestBody?: { description: string; required?: boolean; content: Record<string, { schema: Schema }> };
  responses: Record<string, { description: string; content?: Record<string, { schema: Schema }> }>;
};
type Methods = { get?: Operation; post?: Operation };
export type Spec = {
  paths: Record<string, Methods>;
  components: { schemas: Record<string, Schema> };
  tags: { name: string; description: string }[];
};
export type Tool = {
  name: string;
  title: string;
  description: string;
  inputSchema: {
    properties: Record<string, Schema & { minLength?: number; maxLength?: number; maxItems?: number }>;
    required?: string[];
  };
};
// operationId → an example request (path values and query) and the answer it gave.
export type Examples = Record<
  string,
  { path?: Record<string, string>; query?: [string, string][]; body?: unknown; answer?: unknown; note?: string }
>;

// Each route has one method: GET for reading, POST where the request carries a list (Check cited parts).
const methodOf = (methods: Methods): ["GET" | "POST", Operation] =>
  methods.get ? ["GET", methods.get] : ["POST", methods.post!];

export type ReferencePage = { slug: string; title: string; description: string; markdown: string; group: string };

export const slugFor = (operationId: string) => "api/" + operationId.replace(/_/g, "-");

const refName = (ref: string) => ref.split("/").pop()!;

function typeOf(schema: Schema, spec: Spec): string {
  if (schema.$ref) return refName(schema.$ref);
  const alternatives = schema.anyOf ?? schema.oneOf;
  if (alternatives) {
    const kinds = alternatives.filter((s) => s.type !== "null").map((s) => typeOf(s, spec));
    const nullable = alternatives.some((s) => s.type === "null");
    return kinds.join(" or ") + (nullable ? " or null" : "");
  }
  if (schema.allOf?.length === 1) return typeOf(schema.allOf[0], spec);
  if (schema.enum) return schema.enum.map((v) => JSON.stringify(v)).join(", ");
  const types = Array.isArray(schema.type) ? schema.type : schema.type ? [schema.type] : [];
  if (types.includes("array")) return `array of ${schema.items ? typeOf(schema.items, spec) : "values"}`;
  if (types.includes("object") && schema.additionalProperties && typeof schema.additionalProperties === "object")
    return `object of ${typeOf(schema.additionalProperties, spec)}`;
  const named = types.map((t) => (t === "string" && schema.format === "date-time" ? "date-time" : t));
  return named.join(" or ") || "value";
}

// A table cell: pipes escaped, line breaks flattened.
const cell = (text: string) => text.replace(/\|/g, "\\|").replace(/\n/g, " ");
// Error codes in descriptions link to the errors page.
const ERROR_CODES = new Set(CODES.map(([, code]) => code));
const linkCodes = (text: string) =>
  text.replace(/`([a-z_]+)`/g, (m, code) => (ERROR_CODES.has(code) ? `[\`${code}\`](/docs/errors#${code})` : m));

function referenced(schema: Schema): string[] {
  const out: string[] = [];
  const walk = (s?: Schema) => {
    if (!s) return;
    if (s.$ref) out.push(refName(s.$ref));
    walk(s.items);
    if (typeof s.additionalProperties === "object") walk(s.additionalProperties);
    [...(s.anyOf ?? []), ...(s.oneOf ?? []), ...(s.allOf ?? [])].forEach(walk);
  };
  walk(schema);
  return out;
}

// The fields of a schema and of every schema it contains, each as a table under its own heading.
function fields(root: string, spec: Spec): string[] {
  const out: string[] = [];
  const seen = new Set<string>();
  const queue = [root];
  while (queue.length) {
    const name = queue.shift()!;
    if (seen.has(name)) continue;
    seen.add(name);
    const schema = spec.components.schemas[name];
    if (!schema?.properties) {
      if (schema?.enum) out.push(`### ${name}`, `One of ${schema.enum.map((v) => `\`${v}\``).join(", ")}.`);
      continue;
    }
    out.push(`### ${name}`);
    if (schema.description) out.push(linkCodes(schema.description));
    const rows: string[][] = [];
    for (const [field, value] of Object.entries(schema.properties)) {
      rows.push([`\`${field}\``, cell(typeOf(value, spec)), cell(linkCodes(value.description ?? ""))]);
      queue.push(...referenced(value).filter((n) => spec.components.schemas[n]));
    }
    out.push(table(["Field", "Type", "Description"], rows));
  }
  return out;
}

const quote = (v: string) => JSON.stringify(v);
// One block each: blocks are joined with a blank line, so a table's rows and a fence's lines join here.
const table = (head: string[], rows: string[][]) =>
  [
    `| ${head.join(" | ")} |`,
    `|${head.map(() => "---").join("|")}|`,
    ...rows.map((row) => `| ${row.join(" | ")} |`),
  ].join("\n");
const fence = (lang: string, text: string) => "```" + lang + "\n" + text + "\n```";

function postExamples(url: string, body: unknown): string[] {
  const json = JSON.stringify(body ?? {}, null, 2);
  const curl = [
    `curl -X POST ${url}`,
    `-H "Authorization: Bearer $OPEN_ALVARY_KEY"`,
    `-H "Content-Type: application/json"`,
    `-d '${JSON.stringify(body ?? {})}'`,
  ];
  const python = [
    "import os",
    "",
    "import requests",
    "",
    "response = requests.post(",
    `    ${quote(url)},`,
    `    json=${json.replace(/\n/g, "\n    ").replace(/true/g, "True").replace(/false/g, "False").replace(/null/g, "None")},`,
    `    headers={"Authorization": f"Bearer {os.environ['OPEN_ALVARY_KEY']}"},`,
    "    timeout=30,",
    ")",
    "response.raise_for_status()",
    "answer = response.json()",
  ];
  const javascript = [
    `const response = await fetch(${quote(url)}, {`,
    `  method: "POST",`,
    "  headers: { Authorization: `Bearer ${process.env.OPEN_ALVARY_KEY}`, \"Content-Type\": \"application/json\" },",
    `  body: JSON.stringify(${json.replace(/\n/g, "\n  ")}),`,
    "});",
    "if (!response.ok) throw new Error((await response.json()).detail);",
    "const answer = await response.json();",
  ];
  return [fence("curl", curl.join(" \\\n  ")), fence("python", python.join("\n")), fence("javascript", javascript.join("\n"))];
}

function requestExamples(
  path: string,
  operation: Operation,
  media: string | undefined,
  request: Examples[string] = {},
  method: "GET" | "POST" = "GET",
): string[] {
  if (method === "POST")
    return [
      ...postExamples(BASE + path, request.body ?? { items: [{ anchor_id: "nod_…", span_hash: "…" }] }),
      ...(request.body ? [] : ["Use the `anchor_id` and `span_hash` of each evidence unit you cite."]),
    ];
  const filled = path.replace(/\{(\w+)\}/g, (_, name) =>
    request.path?.[name] ? encodeURIComponent(request.path[name]) : `{${name}}`,
  );
  const url = BASE + filled;
  const query = request.query ?? [];
  const keyed = operation.security === undefined || operation.security.length > 0;
  const json = !media || media === "application/json";
  const curl = [query.length ? `curl -G ${url}` : `curl ${url}`];
  for (const [name, value] of query)
    curl.push(/^[\w.-]+$/.test(value) ? `-d ${name}=${value}` : `--data-urlencode ${quote(`${name}=${value}`)}`);
  if (keyed) curl.push(`-H "Authorization: Bearer $OPEN_ALVARY_KEY"`);
  const python = [
    ...(keyed ? ["import os", ""] : []),
    "import requests",
    "",
    "response = requests.get(",
    `    ${quote(url)},`,
    query.length
      ? `    params={${query.map(([n, v]) => `${quote(n)}: ${/^\d+$/.test(v) ? v : quote(v)}`).join(", ")}},`
      : null,
    keyed ? `    headers={"Authorization": f"Bearer {os.environ['OPEN_ALVARY_KEY']}"},` : null,
    "    timeout=30,",
    ")",
    "response.raise_for_status()",
    json ? "answer = response.json()" : "feed = response.text",
  ].filter((line) => line !== null);
  const javascript = [
    `const url = new URL(${quote(url)});`,
    ...query.map(([n, v]) => `url.searchParams.append(${quote(n)}, ${quote(v)});`),
    keyed
      ? "const response = await fetch(url, {\n  headers: { Authorization: `Bearer ${process.env.OPEN_ALVARY_KEY}` },\n});"
      : "const response = await fetch(url);",
    json
      ? "if (!response.ok) throw new Error((await response.json()).detail);\nconst answer = await response.json();"
      : "if (!response.ok) throw new Error(`HTTP ${response.status}`);\nconst feed = await response.text();",
  ];
  const placeholder = /\{\w+\}/.test(filled)
    ? [`Replace \`${filled.match(/\{\w+\}/)![0]}\` with an \`event_id\` from [List changes](/docs/api/list-changes).`]
    : [];
  return [
    fence("curl", curl.join(" \\\n  ")),
    fence("python", python.join("\n")),
    fence("javascript", javascript.join("\n")),
    ...placeholder,
  ];
}

function operationPage(path: string, methods: Methods, spec: Spec, examples: Examples): ReferencePage {
  const [method, operation] = methodOf(methods);
  const example = examples[operation.operationId];
  const keyed = operation.security === undefined || operation.security.length > 0;
  const lines: string[] = [];
  lines.push(fence("http", `${method} ${BASE}${path}`));
  if (!keyed) lines.push("> Tip: No key is needed for this one.");
  if (path.startsWith("/changes") || path === "/change-coverage")
    lines.push(
      `> Tip: The same route answers without a key at \`${PUBLIC_BASE}${path}\`, for anyone following changes.`,
    );
  const parameters = operation.parameters ?? [];
  if (parameters.length) {
    const rows = parameters.map((p) => {
      const extras = [
        p.schema.default !== undefined && !/default/i.test(p.description)
          ? `Default \`${JSON.stringify(p.schema.default)}\`.`
          : "",
        p.schema.type === "array" ? "Repeat it for several values." : "",
      ].filter(Boolean);
      return [
        `\`${p.name}\``,
        p.in,
        cell(typeOf(p.schema, spec)),
        p.required ? "Yes" : "No",
        cell([p.description, ...extras].join(" ")),
      ];
    });
    lines.push("## Parameters", table(["Name", "In", "Type", "Required", "Description"], rows));
  }
  const requestBody = operation.requestBody?.content["application/json"]?.schema;
  if (operation.requestBody && requestBody?.$ref) {
    lines.push("## Request body", linkCodes(operation.requestBody.description), ...fields(refName(requestBody.$ref), spec));
  }
  const ok = operation.responses["200"];
  const [media, content] = Object.entries(ok?.content ?? {})[0] ?? [];
  const body = content?.schema;
  lines.push("## Request", ...requestExamples(path, operation, media, example, method));
  lines.push("## Response");
  if (media && media !== "application/json") lines.push(`\`200 OK\` with \`${media}\`.`);
  else if (body) {
    const root = body.$ref ? refName(body.$ref) : body.items?.$ref ? refName(body.items.$ref) : null;
    lines.push(`\`200 OK\` with ${body.type === "array" ? "an array of " : ""}${root ? `\`${root}\`` : "JSON"}.`);
    if (example?.answer !== undefined) {
      lines.push(fence("json", JSON.stringify(example.answer, null, 2)));
      if (example.note) lines.push(example.note);
    }
    if (root) lines.push(...fields(root, spec));
  }
  const errors = Object.entries(operation.responses).filter(([status]) => status !== "200");
  if (errors.length) {
    lines.push(
      "## Errors",
      "Each is a problem document; see [Errors](/docs/errors).",
      table(
        ["Status", "When"],
        errors.map(([status, response]) => [status, cell(linkCodes(response.description))]),
      ),
    );
  }
  return {
    slug: slugFor(operation.operationId),
    title: operation.summary,
    description: operation.description,
    markdown: lines.join("\n\n"),
    group: operation.tags[0],
  };
}

function toolsPage(tools: Tool[], examples: Examples): ReferencePage {
  const lines = [
    `The MCP server at \`${API_ORIGIN}/mcp\` gives AI tools these read-only tools. They answer exactly what the REST API answers for the same request, under the same limits. To connect a tool, see [Connect AI tools](/docs/ai-tools).`,
    "AI tools call them for you when you ask a question. To call one yourself, send a JSON-RPC `tools/call` with a key; no session is needed:",
    fence(
      "curl",
      [
        `curl ${API_ORIGIN}/mcp`,
        `-H "Authorization: Bearer $OPEN_ALVARY_KEY"`,
        `-H "Content-Type: application/json"`,
        `-H "Accept: application/json, text/event-stream"`,
        `-d '{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "search_catalogue", "arguments": {"query": "constitution", "limit": 3}}}'`,
      ].join(" \\\n  "),
    ),
    "The answer's `result.content[0].text` holds the same JSON as the REST answer. A refusal (no key, no access, over a limit) is a JSON-RPC error whose `data.code` is one of the [error codes](/docs/errors).",
  ];
  for (const tool of tools) {
    lines.push(`## \`${tool.name}\` {#${tool.name}}`, `**${tool.title}.** ${tool.description}`);
    const props = Object.entries(tool.inputSchema.properties);
    if (props.length) {
      const rows = props.map(([name, schema]) => {
        const bounds = [
          schema.minimum !== undefined && schema.maximum !== undefined ? `${schema.minimum} to ${schema.maximum}.` : "",
          schema.maxItems ? `Up to ${schema.maxItems}.` : "",
        ].filter(Boolean);
        const required = tool.inputSchema.required?.includes(name) ? "Yes" : "No";
        return [
          `\`${name}\``,
          cell(typeOf(schema, {} as Spec)),
          required,
          cell([schema.description ?? "", ...bounds].join(" ")),
        ];
      });
      lines.push(table(["Argument", "Type", "Required", "Description"], rows));
    } else lines.push("No arguments.");
    const example = examples[`mcp:${tool.name}`];
    if (example?.answer !== undefined)
      lines.push(
        "Example answer (the text of `result.content[0]`):",
        fence("json", JSON.stringify(example.answer, null, 2)),
      );
  }
  return {
    slug: "api/mcp",
    title: "MCP tools",
    description: "The tools AI tools use, and how to call them yourself.",
    markdown: lines.join("\n\n"),
    group: "MCP",
  };
}

// Within each group, the order a reader needs them in (by operation ID); any other route comes last.
const READING_ORDER = [
  "retrieve-evidence",
  "check-currentness",
  "search",
  "list-sources",
  "get-source",
  "get-versions",
  "get-rights",
  "resolve-citation",
  "list-jurisdictions",
  "get-coverage",
  "list-changes",
  "get-change",
  "list-recent-changes",
  "get-atom-feed",
  "get-rss-feed",
  "get-change-coverage",
  "list-plans",
  "get-quota-policy",
  "list-releases",
];

export function referencePages(spec: Spec, tools: Tool[], examples: Examples): ReferencePage[] {
  const pages = Object.entries(spec.paths).map(([path, methods]) => operationPage(path, methods, spec, examples));
  const groups = spec.tags.map((t) => t.name);
  const rank = (page: ReferencePage) => {
    const at = READING_ORDER.indexOf(page.slug.replace(/^api\//, ""));
    return groups.indexOf(page.group) * 100 + (at < 0 ? 99 : at);
  };
  pages.sort((a, b) => rank(a) - rank(b));
  return [...pages, toolsPage(tools, examples)];
}

// The reference's overview: base address, keys, formats and every route.
export function referenceOverview(spec: Spec): string {
  const lines = [
    "The REST API reads the published corpus: records, their texts and rights, citations, coverage and changes. It is read-only; every answer is JSON unless the route says otherwise.",
    table(
      ["", ""],
      [
        ["Base address", `\`${BASE}\``],
        [
          "Sign-in",
          "`Authorization: Bearer <key>`: an API key from the console, or an access token from a connected app. See [Authentication](/docs/authentication).",
        ],
        ["Format", "JSON in UTF-8; dates and times in ISO 8601, UTC"],
        ["Errors", "Problem documents with a `code`. See [Errors](/docs/errors)."],
        ["Limits", "Per organisation, shared by its keys and apps. See [Plans and rate limits](/docs/plans)."],
        [
          "OpenAPI",
          `[\`${BASE}/openapi.json\`](${BASE}/openapi.json): import it into Postman, Insomnia or a client generator.`,
        ],
      ],
    ),
  ];
  for (const tag of spec.tags) {
    const rows = Object.entries(spec.paths)
      .filter(([, methods]) => methodOf(methods)[1].tags[0] === tag.name)
      .map(([path, methods]) => {
        const [method, operation] = methodOf(methods);
        return [`[\`${method} ${path}\`](/docs/${slugFor(operation.operationId)})`, cell(operation.summary)];
      });
    lines.push(`## ${tag.name}`, tag.description, table(["Route", "What it does"], rows));
  }
  lines.push("## MCP", `AI tools use the MCP server at \`${API_ORIGIN}/mcp\`. See [MCP tools](/docs/api/mcp).`);
  return lines.join("\n\n");
}
