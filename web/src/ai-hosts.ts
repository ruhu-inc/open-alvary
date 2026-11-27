// Shared catalogue of AI tools that connect to Open Alvary over MCP (or REST with an API key).
// Imported by /connect, /developers, /docs/ai-tools (at build time in Node and in the browser),
// and /console/* so host lists, setup commands and consent notes never drift across surfaces.

export type AiHostInstallStep = {
  label: string;
  command?: string;
  copyable?: boolean;
};

export type AiHost = {
  id: string;
  name: string;
  summary: string;
  audience: "consumer" | "agent";
  status: "verified" | "developer_fallback" | "deferred";
  authMethod: "oauth_cimd" | "oauth_dcr" | "api_key";
  pluginRepoUrl?: string;
  consentNote: string;
  installSteps: AiHostInstallStep[];
};

export const PLUGIN_REPO_URL = "https://github.com/ruhu-inc/open-alvary-skills";
export const DEFAULT_MCP_URL = "https://api.open.alvary.ai/mcp";

// Node-safe for Vite build scripts (web/docs-files.ts), while keeping staging and localhost
// origin derivation in the browser matching web/src/console/pages.tsx.
export function mcpUrl(origin?: string): string {
  const resolved = origin ?? (typeof location !== "undefined" ? location.origin : "");
  if (!resolved) return DEFAULT_MCP_URL;
  try {
    const url = new URL(resolved);
    if (url.hostname === "open.alvary.ai") return DEFAULT_MCP_URL;
    return `${url.origin}/mcp`;
  } catch {
    return DEFAULT_MCP_URL;
  }
}

export function formatCommand(command: string, origin?: string): string {
  return command.replaceAll("{{mcpUrl}}", mcpUrl(origin));
}

export const AI_HOSTS: readonly AiHost[] = [
  {
    id: "claude",
    name: "Claude",
    summary: "Settings → Connectors → Add custom connector, paste the MCP address, then sign in.",
    audience: "consumer",
    status: "verified",
    authMethod: "oauth_cimd",
    consentNote: "Shows Verified publisher on the Open Alvary approval page.",
    installSteps: [
      { label: "In Claude, open Settings → Connectors and choose Add custom connector." },
      { label: "Paste the Open Alvary MCP address and add it:", command: "{{mcpUrl}}", copyable: true },
      { label: "Choose Connect. Sign in to Open Alvary, pick your organisation, and approve." },
    ],
  },
  {
    id: "code",
    name: "Claude Code",
    summary: "Run claude mcp add with the MCP address, then authenticate via /mcp.",
    audience: "agent",
    status: "verified",
    authMethod: "oauth_cimd",
    pluginRepoUrl: PLUGIN_REPO_URL,
    consentNote: "Shows Verified publisher with a local browser sign-in notice on the Open Alvary approval page.",
    installSteps: [
      {
        label: "Add the Open Alvary MCP server:",
        command: "claude mcp add --transport http open-alvary {{mcpUrl}}",
        copyable: true,
      },
      {
        label: "Run /mcp in Claude Code, choose open-alvary, and select Authenticate to approve in your browser.",
      },
    ],
  },
  {
    id: "codex",
    name: "Codex",
    summary: "Run codex mcp add with the MCP address, then sign in with codex mcp login.",
    audience: "agent",
    status: "verified",
    authMethod: "oauth_cimd",
    pluginRepoUrl: PLUGIN_REPO_URL,
    consentNote: "Shows Verified publisher with a local browser sign-in notice on the Open Alvary approval page.",
    installSteps: [
      {
        label: "Add the Open Alvary MCP server:",
        command: "codex mcp add open-alvary --url {{mcpUrl}}",
        copyable: true,
      },
      {
        label: "Sign in and approve in the browser window that opens:",
        command: "codex mcp login open-alvary",
        copyable: true,
      },
    ],
  },
  {
    id: "antigravity",
    name: "Antigravity",
    summary: "Install the Open Alvary plugin with agy plugin install, then approve in your browser.",
    audience: "agent",
    status: "deferred",
    authMethod: "oauth_cimd",
    pluginRepoUrl: PLUGIN_REPO_URL,
    consentNote: "Deferred pending live OAuth client verification and publisher pinning.",
    installSteps: [
      {
        label: "Install the Open Alvary plugin (MCP server and legal research skills):",
        command: "agy plugin install https://github.com/ruhu-inc/open-alvary-skills",
        copyable: true,
      },
      { label: "When Antigravity connects to Open Alvary, sign in, pick your organisation, and approve." },
    ],
  },
  {
    id: "chatgpt",
    name: "ChatGPT",
    summary: "Add the Open Alvary MCP address in ChatGPT connector settings once enabled.",
    audience: "consumer",
    status: "deferred",
    authMethod: "oauth_cimd",
    consentNote: "Deferred pending OAuth client verification.",
    installSteps: [
      { label: "Add a custom MCP connector using the Open Alvary address:", command: "{{mcpUrl}}", copyable: true },
      { label: "Sign in to Open Alvary, pick your organisation, and approve." },
    ],
  },
  {
    id: "cowork",
    name: "Claude Cowork",
    summary: "Connect Open Alvary in Claude Cowork once verified.",
    audience: "consumer",
    status: "deferred",
    authMethod: "oauth_cimd",
    pluginRepoUrl: PLUGIN_REPO_URL,
    consentNote: "Deferred pending verification in Claude Cowork.",
    installSteps: [
      { label: "Add the Open Alvary MCP address in Claude Cowork:", command: "{{mcpUrl}}", copyable: true },
      { label: "Sign in to Open Alvary, pick your organisation, and approve." },
    ],
  },
  {
    id: "gemini-cli",
    name: "Gemini CLI",
    summary: "Install the Open Alvary extension in Gemini CLI once verified.",
    audience: "agent",
    status: "deferred",
    authMethod: "oauth_cimd",
    pluginRepoUrl: PLUGIN_REPO_URL,
    consentNote: "Deferred pending Gemini CLI verification.",
    installSteps: [
      { label: "Add the Open Alvary MCP address in Gemini CLI:", command: "{{mcpUrl}}", copyable: true },
      { label: "Approve the connection in the browser window that opens." },
    ],
  },
  {
    id: "cursor",
    name: "Cursor",
    summary: "Add the Open Alvary MCP server in Cursor once verified.",
    audience: "agent",
    status: "deferred",
    authMethod: "oauth_dcr",
    pluginRepoUrl: PLUGIN_REPO_URL,
    consentNote: "Shows Unverified app when connecting through dynamic client registration.",
    installSteps: [
      { label: "Add the Open Alvary MCP address in Cursor MCP settings:", command: "{{mcpUrl}}", copyable: true },
      { label: "Approve the connection in the browser window that opens." },
    ],
  },
  {
    id: "rest",
    name: "Your own code",
    summary: "Create an API key under /console/keys and send it in the Authorization header.",
    audience: "agent",
    status: "developer_fallback",
    authMethod: "api_key",
    consentNote: "Uses an API key created in /console/keys rather than browser approval.",
    installSteps: [
      { label: "Create an API key in the console under API keys (/console/keys)." },
      {
        label: "Send Authorization: Bearer <key> to the REST API or this MCP address:",
        command: "{{mcpUrl}}",
        copyable: true,
      },
    ],
  },
];

export function verifiedHosts(): AiHost[] {
  return AI_HOSTS.filter((host) => host.status === "verified");
}

export function hostListText(
  hosts: readonly AiHost[] = verifiedHosts(),
  conjunction: "and" | "or" = "and",
): string {
  const names = hosts.map((host) => host.name);
  if (names.length <= 1) return names[0] ?? "";
  if (names.length === 2) return `${names[0]} ${conjunction} ${names[1]}`;
  return `${names.slice(0, -1).join(", ")} ${conjunction} ${names[names.length - 1]}`;
}
