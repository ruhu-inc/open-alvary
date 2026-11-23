import { describe, it } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

import { verifyReceipt } from "../clients/receipt/receipt.js";
import { installCheckClaimInterceptor } from "../web/public/agents/site.js";
import { registerActions } from "../web/public/agents/site-actions.js";
import {
  lookForStatus,
  wordsLineForReceipt,
  recordLinesForReceipt,
} from "../web/public/agents/receipt-card.js";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(__dirname, "..");

const VEND_MCP = path.join(repoRoot, "web/public/agents/runtime/mcp.js");
const CHALLENGE_MCP =
  "/private/tmp/claude-501/-Users-ijidailassa--claude-spares-spare-sbGHsw/a5cf5f73-8c9e-44ca-befc-468bb7b369dd/scratchpad/challenge/agentic-web/skills/agentify-website/runtime/mcp.js";
const VEND_ACTIONS = path.join(repoRoot, "web/public/agents/runtime/actions.js");
const CHALLENGE_ACTIONS =
  "/private/tmp/claude-501/-Users-ijidailassa--claude-spares-spare-sbGHsw/a5cf5f73-8c9e-44ca-befc-468bb7b369dd/scratchpad/challenge/agentic-web/skills/agentify-website/runtime/actions.js";

const mcpPath = fs.existsSync(VEND_MCP) ? VEND_MCP : CHALLENGE_MCP;
const actionsPath = fs.existsSync(VEND_ACTIONS) ? VEND_ACTIONS : CHALLENGE_ACTIONS;

const { McpClient } = await import(pathToFileURL(mcpPath).href);
const { ActionRegistry } = await import(pathToFileURL(actionsPath).href);

const vectors = JSON.parse(
  fs.readFileSync(path.join(repoRoot, "clients/receipt/vectors.json"), "utf8"),
);

describe("LC-16 fetch wrapper with McpClient", () => {
  it("intercepts McpClient.callTool('check_claim', ...) cleanly without disturbing the client response", async () => {
    const validVec = vectors.cases.find((c) => c.name === "valid_matches");
    const envelope = { receipt: validVec.receipt, claim: validVec.claim };
    const envelopeText = JSON.stringify(envelope);

    const originalFetch = globalThis.fetch;
    globalThis.fetch = async (_url, init) => {
      const req = JSON.parse(init.body);
      if (req.method === "initialize") {
        return new Response(
          JSON.stringify({
            jsonrpc: "2.0",
            id: req.id,
            result: { protocolVersion: "2025-06-18", capabilities: {} },
          }),
          { status: 200, headers: { "Content-Type": "application/json", "Mcp-Session-Id": "sess-1" } },
        );
      }
      if (req.method === "notifications/initialized") {
        return new Response("", { status: 202 });
      }
      if (req.method === "tools/call" && req.params.name === "check_claim") {
        return new Response(
          JSON.stringify({
            jsonrpc: "2.0",
            id: req.id,
            result: {
              content: [{ type: "text", text: envelopeText }],
              structuredContent: envelope,
            },
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }
      throw new Error(`Unexpected RPC method: ${req.method}`);
    };

    const intercepted = [];
    const interceptor = installCheckClaimInterceptor({
      onCheckClaim: (payload) => intercepted.push(payload),
    });

    try {
      const client = new McpClient({ url: "https://open.alvary.ai/agents/mcp" });
      const clientText = await client.callTool("check_claim", validVec.claim);

      // McpClient still receives the full text response untouched
      assert.equal(clientText, envelopeText);

      // Wrapper observed the exact signed receipt and claim
      assert.equal(intercepted.length, 1);
      assert.deepEqual(intercepted[0].envelope.receipt, validVec.receipt);
      assert.deepEqual(intercepted[0].envelope.claim, validVec.claim);

      // Intercepted receipt verifies with receipt.js
      const ver = await verifyReceipt(intercepted[0].envelope.receipt, vectors.jwks, {
        claim: intercepted[0].envelope.claim,
        now: vectors.now,
      });
      assert.equal(ver.ok, true);
      assert.equal(ver.status, "matches");
    } finally {
      interceptor.uninstall();
      globalThis.fetch = originalFetch;
    }
  });

  it("captures tool errors (rate_limited / paused / unavailable) via onToolError", async () => {
    const originalFetch = globalThis.fetch;
    globalThis.fetch = async (_url, init) => {
      const req = JSON.parse(init.body);
      if (req.method === "initialize") {
        return new Response(
          JSON.stringify({ jsonrpc: "2.0", id: req.id, result: { protocolVersion: "2025-06-18" } }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }
      if (req.method === "notifications/initialized") {
        return new Response("", { status: 202 });
      }
      return new Response(
        JSON.stringify({
          jsonrpc: "2.0",
          id: req.id,
          result: {
            isError: true,
            content: [{ type: "text", text: "rate_limited: try again in 20 seconds" }],
            structuredContent: { code: "rate_limited", retry_after_seconds: 20 },
          },
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      );
    };

    const errors = [];
    const interceptor = installCheckClaimInterceptor({
      onToolError: (err) => errors.push(err),
    });

    try {
      const client = new McpClient({ url: "https://open.alvary.ai/agents/mcp" });
      const resText = await client.callTool("check_claim", { citation: "Act, s. 1" });
      assert.match(resText, /MCP tool error: rate_limited/);
      assert.equal(errors.length, 1);
      assert.equal(errors[0].code, "rate_limited");
      assert.equal(errors[0].seconds, 20);
    } finally {
      interceptor.uninstall();
      globalThis.fetch = originalFetch;
    }
  });
});

describe("web/public/agents/index.html matches web/src/site.tsx and §10 requirements", () => {
  const html = fs.readFileSync(path.join(repoRoot, "web/public/agents/index.html"), "utf8");
  const siteTsx = fs.readFileSync(path.join(repoRoot, "web/src/site.tsx"), "utf8");

  it("header navigation links match web/src/site.tsx links exactly", () => {
    const linksBlockMatch = siteTsx.match(/export const links = \[([\s\S]*?)\];/);
    assert.ok(linksBlockMatch, "Could not find links array in web/src/site.tsx");
    const expectedLinks = [...linksBlockMatch[1].matchAll(/\["([^"]+)",\s*"([^"]+)"\]/g)].map(
      ([, href, label]) => ({ href, label }),
    );
    assert.ok(expectedLinks.length >= 6);

    const navMatch = html.match(/<nav[^>]*id="primary-navigation"[^>]*>([\s\S]*?)<\/nav>/);
    assert.ok(navMatch, "Missing #primary-navigation in index.html");
    const actualLinks = [...navMatch[1].matchAll(/<a[^>]*href="([^"]+)"[^>]*>([^<]+)<\/a>/g)].map(
      ([, href, rawLabel]) => ({ href, label: rawLabel.replace(/&amp;/g, "&").trim() }),
    );

    assert.deepEqual(actualLinks, expectedLinks);
  });

  it("includes required landmarks, title, skip link, mobile nav aria, noscript, and modern tabs", () => {
    assert.match(html, /<title>Law check · Open Alvary<\/title>/);
    assert.match(html, /<a[^>]*class="skip-link"[^>]*href="#main-content"/);
    assert.match(html, /<main[^>]*id="main-content"/);
    assert.match(html, /aria-expanded="false"/);
    assert.match(html, /aria-controls="primary-navigation"/);
    assert.match(html, /<noscript>[\s\S]*Law check needs JavaScript[\s\S]*<\/noscript>/);
    assert.match(html, /focusgroup="tablist nomemory"/);
    assert.match(html, /hidden="until-found"/);
    assert.match(html, /data-error="quote-error"/);
    // Strict CSP: zero inline scripts
    const scriptTags = [...html.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script>/gi)];
    assert.equal(scriptTags.length, 1);
    assert.match(scriptTags[0][1], /src="\.\/site\.js"/);
    assert.equal(scriptTags[0][2].trim(), "");
  });
});

describe("web/public/agents/law-check.css meets §§10.2, 10.5, 10.10, 10.13", () => {
  const css = fs.readFileSync(path.join(repoRoot, "web/public/agents/law-check.css"), "utf8");

  it("contains required runtime overrides and modern-web-guidance CSS rules", () => {
    assert.match(css, /\.ns-presentation\s*\{\s*display:\s*none;?\s*\}/);
    assert.match(css, /field-sizing:\s*content/);
    assert.match(css, /:user-invalid/);
    assert.match(css, /@media\s*\(pointer:\s*coarse\)/);
    assert.match(css, /text-wrap:\s*balance/);
    assert.match(css, /text-wrap:\s*pretty/);
    assert.match(css, /\.ns-llmbadge/);
    assert.match(css, /\.ns-msg\.bot\.ns-rich::before/);
    assert.match(css, /prefers-reduced-motion:\s*reduce/);
    assert.match(css, /border-block-end:/);
    // Must not use overflow-x: hidden anywhere in CSS rules
    const cssNoComments = css.replace(/\/\*[\s\S]*?\*\//g, "");
    assert.doesNotMatch(cssNoComments, /overflow-x:\s*hidden/);
  });
});

describe("web/public/agents/site-actions.js", () => {
  it("registers open_section and show_receipt as effect: 'read' passing ActionRegistry validation", () => {
    const registry = new ActionRegistry();
    const scope = { base: "https://open.alvary.ai/agents", network: "law_desk" };
    registerActions({
      register: (def) => registry.register(scope, def),
    });
    const catalog = registry.catalog(scope);
    const names = catalog.map((a) => a.name).sort();
    assert.deepEqual(names, ["open_section", "show_receipt"]);
    for (const action of catalog) {
      assert.equal(action.effect, "read");
    }
  });
});

describe("web/public/agents/receipt-card.js architecture & state mapping", () => {
  const cardSrc = fs.readFileSync(path.join(repoRoot, "web/public/agents/receipt-card.js"), "utf8");

  it("uses open Shadow DOM, adoptedStyleSheets, @container, @layer, ElementInternals, and no :host-context", () => {
    assert.match(cardSrc, /attachShadow\(\{\s*mode:\s*"open"\s*\}\)/);
    assert.match(cardSrc, /adoptedStyleSheets/);
    assert.match(cardSrc, /container-type:\s*inline-size/);
    assert.match(cardSrc, /@container\s*\(/);
    assert.match(cardSrc, /@layer\s+/);
    assert.match(cardSrc, /attachInternals/);
    assert.match(cardSrc, /beforeprint/);
    assert.match(cardSrc, /afterprint/);
    assert.doesNotMatch(cardSrc, /:host-context/);
  });

  it("maps all 5 looks and words/record lines accurately", () => {
    assert.equal(lookForStatus("matches"), "green");
    assert.equal(lookForStatus("words_found"), "amber");
    assert.equal(lookForStatus("words_found_with_omissions"), "amber");
    assert.equal(lookForStatus("wording_differs"), "red");
    assert.equal(lookForStatus("not_in_force"), "red");
    assert.equal(lookForStatus("does_not_bind"), "red");
    assert.equal(lookForStatus("not_in_catalogue"), "grey");
    assert.equal(lookForStatus("located"), "neutral");

    assert.equal(
      wordsLineForReceipt({ findings: { quote: "exact" }, part: { pinpoint: "s. 1(1)" } }).text,
      "Quoted words found",
    );
    assert.equal(
      wordsLineForReceipt({ findings: { quote: "absent" }, part: { pinpoint: "s. 1(1)" } }).text,
      "These words are not in s. 1(1) of the published text",
    );

    const recordLines = recordLinesForReceipt({
      claim: { assessed_at: "2027-01-05", date_sent: false, jurisdiction: "ng" },
      findings: {
        currentness: { state: "in_force", basis: "provision", assessed_at: "2027-01-05" },
        applicability: { binds: "not_bound", relation: "national" },
      },
    });
    assert.equal(recordLines[0].text, "Recorded as in force on 5 January 2027, from reviewed legal events");
    assert.equal(recordLines[1].text, "Recorded as not binding in Nigeria");
  });
});
