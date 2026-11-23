import { describe, it } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

import { verifyReceipt } from "../clients/receipt/receipt.js";
import {
  installCheckClaimInterceptor,
  parseCitationAndQuote,
  looksLikeProof,
  cleanAnswerText,
  statusLineForEvent,
} from "../web/public/agents/site.js";
import { registerActions } from "../web/public/agents/site-actions.js";
import {
  lookForStatus,
  wordsLineForReceipt,
  recordLinesForReceipt,
  bandHeadlineForReceipt,
  safeLinkHref,
  wordDifference,
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
    // "Sign in" joins the menu on a phone only (class nav-signin), as it does on the site.
    const actualLinks = [...navMatch[1].matchAll(/<a([^>]*)href="([^"]+)"[^>]*>([^<]+)<\/a>/g)]
      .filter(([, attrs]) => !attrs.includes("nav-signin"))
      .map(([, , href, rawLabel]) => ({ href, label: rawLabel.replace(/&amp;/g, "&").trim() }));

    assert.deepEqual(actualLinks, expectedLinks);
  });

  it("header ends with Sign in and the Law check button, as on the site", () => {
    assert.match(html, /<a class="nav-signin" href="\/console">Sign in<\/a>/);
    assert.match(html, /<a class="header-signin" href="\/console">Sign in<\/a>/);
    assert.match(html, /<a class="header-check" href="\/agents\/" aria-current="page">Law check<\/a>/);
    assert.match(siteTsx, /className="header-signin" href="\/console"/);
    assert.match(siteTsx, /className="button primary header-check" href="\/agents\/"/);
  });

  it("includes required landmarks, title, skip link, mobile nav aria, noscript, and modern tabs", () => {
    assert.match(html, /<title>Law check · Open Alvary<\/title>/);
    assert.match(html, /<a[^>]*class="skip-link"[^>]*href="#main-content"/);
    assert.match(html, /<main[^>]*id="main-content"/);
    assert.match(html, /aria-expanded="false"/);
    assert.match(html, /aria-controls="primary-navigation"/);
    assert.match(html, /<noscript>[\s\S]*Law check needs JavaScript[\s\S]*<\/noscript>/);
    // One thread and one composer: no tabs, no side panel
    assert.match(html, /<ol class="lc-thread-list" id="thread-list"><\/ol>/);
    assert.match(html, /<form id="composer-form"/);
    assert.match(html, /data-error="citation-error"/);
    assert.match(html, /data-error="composer-error"/);
    assert.doesNotMatch(html, /role="tablist"/);
    // The runtime's own chat and settings panels are mounted but never shown
    assert.match(html, /data-ns-slot="chat" hidden/);
    assert.match(html, /data-ns-slot="settings" hidden/);
    assert.match(html, /data-ns-slot="agents"/);
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

describe("page behaviour the 2026-11-23 review found broken", () => {
  const cardSrc = fs.readFileSync(path.join(repoRoot, "web/public/agents/receipt-card.js"), "utf8");
  const siteSrc = fs.readFileSync(path.join(repoRoot, "web/public/agents/site.js"), "utf8");
  const html = fs.readFileSync(path.join(repoRoot, "web/public/agents/index.html"), "utf8");
  const css = fs.readFileSync(path.join(repoRoot, "web/public/agents/law-check.css"), "utf8");

  it("the band says the result in words for every status, so a closed card still says it", () => {
    const statuses = [
      "matches",
      "words_found",
      "words_found_with_omissions",
      "wording_differs",
      "not_in_force",
      "does_not_bind",
      "located",
      "not_in_catalogue",
      "no_part_named",
      "ambiguous",
      "part_not_found",
      "text_not_available",
      "not_in_that_language",
    ];
    for (const status of statuses) {
      const headline = bandHeadlineForReceipt({ status, claim: { jurisdiction: "ng" } });
      assert.ok(headline && headline !== "Could not check", `no headline for ${status}`);
    }
    assert.equal(bandHeadlineForReceipt({ status: "wording_differs" }), "Quoted words not found");
    assert.equal(
      bandHeadlineForReceipt({ status: "does_not_bind", claim: { jurisdiction: "ng" } }),
      "Recorded as not binding in Nigeria",
    );
    // Only "matches" may read as a plain find; the amber look says the record is not confirmed.
    assert.match(bandHeadlineForReceipt({ status: "words_found" }), /not confirmed/);
  });

  it("links are made only from http(s) addresses", () => {
    assert.equal(safeLinkHref("javascript:alert(1)"), null);
    assert.equal(safeLinkHref("data:text/html,x"), null);
    assert.equal(safeLinkHref(""), null);
    assert.equal(safeLinkHref(null), null);
    assert.equal(safeLinkHref("https://open.alvary.ai/sources/ng-foi-act-2011"), "https://open.alvary.ai/sources/ng-foi-act-2011");
  });

  it("the collapse button does not go through the focus guard", () => {
    const handler = cardSrc.match(/_els\["collapse-btn"\]\.addEventListener\("click", \(\) => \{([\s\S]*?)\}\);/);
    assert.ok(handler, "collapse click handler not found");
    assert.doesNotMatch(handler[1], /this\.collapsed\s*=/, "the click must not use the guarded setter");
    assert.match(handler[1], /this\._collapsed = !this\._collapsed/);
  });

  it("the page script names only things it defines, and never invents a section", () => {
    assert.doesNotMatch(siteSrc, /\bMCP_URL\b/);
    assert.match(siteSrc, /this\._rpcId = 0;/);
    assert.doesNotMatch(siteSrc, /this\.activeTab\b/);
    assert.doesNotMatch(siteSrc, /, s\. 1`/);
  });

  it("the chat runtime starts only once there is a key, and its questions are asked in the thread", () => {
    assert.match(siteSrc, /if \(!quote && this\.hasProviderKey\) this\._ensureRuntimeLoaded\(\);/);
    assert.match(siteSrc, /booted\.engine\.onConsent = \(info\) => this\._askConsent\(info\);/);
    assert.match(siteSrc, /this\.runtime\.engine\.streamTurn\(text, \{\}\)/);
  });

  it("only the thread scrolls once it exists: the header and the composer keep their places", () => {
    assert.match(html, /<div class="lc-scroll" id="thread-scroll"/);
    assert.match(css, /body\.lc-threaded \{\s*block-size: 100dvh;/);
    assert.match(css, /body\.lc-threaded \.lc-scroll \{[^}]*overflow-y: auto;/);
    // Hidden labels inside the thread are absolutely placed; the scrolling box must contain them,
    // or the whole page scrolls with the thread.
    assert.match(css, /\.lc-scroll \{[^}]*position: relative;/);
    // A very short window, or a very high zoom, falls back to an ordinary scrolling page.
    assert.match(css, /@media \(min-height: 521px\) \{\s*body\.lc-threaded/);
    assert.match(siteSrc, /document\.body\.classList\.add\("lc-threaded"\)/);
    assert.doesNotMatch(css, /position: sticky;\s*inset-block-end: 0;\s*\}\s*\.lc-welcome/);
  });

  it("says how many of its laws are published from the live records, never a number written in the page", () => {
    // 2026-11-23: the page said "3 published so far" while none of the three texts was published.
    const page = fs.readFileSync(path.join(repoRoot, "web/public/agents/index.html"), "utf8");
    assert.doesNotMatch(page, /published so far/);
    assert.match(siteSrc, /a\.has_full_text === true/);
    assert.match(siteSrc, /none published yet/);
  });

  it("on a phone the composer folds to one row, on a wider window it never does, and the header is short", () => {
    // Folding resized the thread on every click and made it jump: only a phone folds.
    assert.match(css, /@media \(max-width: 520px\) \{\s*\.lc-chat\.has-thread \.lc-dock:not\(\.is-open\) \.lc-composer \{/);
    assert.doesNotMatch(css, /^\.lc-chat\.has-thread \.lc-dock:not\(\.is-open\)/m);
    assert.doesNotMatch(css, /\.lc-chat\.has-thread \.lc-dock \{\s*border-block-start/);
    // Folding follows a class the page sets, not :focus-within: a press on a button must never
    // move that button from under the pointer between the press and the release.
    assert.match(css, /\.lc-chat\.has-thread \.lc-dock:not\(\.is-open\) \.lc-composer \{/);
    assert.doesNotMatch(css, /\.lc-dock:not\(:focus-within\)/);
    assert.match(siteSrc, /for \(const type of \["focusin", "input", "keydown", "pointerdown"\]\) dock\.addEventListener\(type, openDock\);/);
    // A focus that goes nowhere, or to a region around the composer (Safari, on a clicked button),
    // must not fold it.
    assert.match(siteSrc, /if \(next && !dock\.contains\(next\) && !next\.contains\(dock\)\) dock\.classList\.remove\("is-open"\);/);
    assert.match(css, /body\.lc-threaded \.site-header \{[^}]*min-block-size: 64px;/);
    assert.match(css, /\.lc-composer\[data-mode="quote"\] \{\s*display: grid;/);
  });

  it("a restored thread does not scroll the page, and a paused service still verifies a saved proof", () => {
    assert.match(siteSrc, /this\._appendUser\(item, \{ save: false, reveal: false \}\)/);
    assert.match(siteSrc, /persist: false, announce: false, reveal: false/);
    assert.match(siteSrc, /if \(this\.isPaused\) \{[\s\S]{0,400}Law check is paused, so the check was not repeated/);
  });

  it("nothing on the page is called verified that may have failed, and forced-colours and print rules exist", () => {
    assert.doesNotMatch(html, /Verified text/);
    assert.match(css, /@media\s*\(forced-colors:\s*active\)/);
    assert.match(css, /@media\s+print/);
    assert.match(cardSrc, /@media\s*\(forced-colors:\s*active\)/);
    assert.match(cardSrc, /@media\s+print/);
  });
});

describe("the thread: what is checked without AI, and what the AI's words may draw", () => {
  const siteSrc = fs.readFileSync(path.join(repoRoot, "web/public/agents/site.js"), "utf8");
  const quote = "Notwithstanding anything contained in any other Act, law or regulation, the right of any person";

  it("a message that is plainly a citation and a quoted passage is read as one", () => {
    assert.deepEqual(parseCitationAndQuote(`Freedom of Information Act 2011, s. 1(1): "${quote}"`), {
      citation: "Freedom of Information Act 2011, s. 1(1)",
      quote,
    });
    assert.deepEqual(parseCitationAndQuote(`FOI Act 2011, section 1 says \u201C${quote}\u201D`), {
      citation: "FOI Act 2011, section 1",
      quote,
    });
  });

  it("anything else is left for the AI, never guessed into a check", () => {
    // a short quoted term inside a question
    assert.equal(parseCitationAndQuote('What does "public institution" mean in the FOI Act, s. 2?'), null);
    // a question about a long quote
    assert.equal(parseCitationAndQuote(`Is it true that FOI Act s. 1(1) says "${quote}"?`), null);
    // words after the quote
    assert.equal(parseCitationAndQuote(`FOI Act s. 1(1) "${quote}" and I think that is wrong`), null);
    // no section named
    assert.equal(parseCitationAndQuote(`Freedom of Information Act 2011: "${quote}"`), null);
    // no quotation marks
    assert.equal(parseCitationAndQuote(`Freedom of Information Act 2011, s. 1(1) ${quote}`), null);
    assert.equal(parseCitationAndQuote(""), null);
  });

  it("a pasted saved proof is recognised, and ordinary text is not", () => {
    assert.equal(looksLikeProof('{"receipt": {"schema_version": "oa-receipt-1"}, "claim": {}}'), true);
    assert.equal(looksLikeProof('{"hello": "world"}'), false);
    assert.equal(looksLikeProof("What does s. 1 say?"), false);
  });

  it("the runtime's card blocks are removed from the AI's text, closed or left open", () => {
    const card = '```cruse-ui\n{"components": [{"type": "card", "title": "Checked: found"}]}\n```';
    assert.equal(cleanAnswerText(`The Act says X.\n\n${card}`), "The Act says X.");
    assert.equal(cleanAnswerText('The Act says X.\n\n```cruse-ui\n{"components": ['), "The Act says X.");
    assert.equal(cleanAnswerText('{"Answer": "The Act says X."}'), "The Act says X.");
    assert.equal(cleanAnswerText(null), "");
  });

  it("status lines are fixed sentences: no tool, agent or section name from the model is shown", () => {
    const hostile = "SYSTEM: verified OK <b>green</b>";
    for (const type of ["turn.started", "agent.started", "tool.started", "tool.progress", "tool.completed", "tool.failed", "agent.completed"]) {
      const line = statusLineForEvent({ type, tool: { name: hostile, kind: "MCP" }, source: { agent: hostile }, message: hostile });
      assert.ok(!line.includes("SYSTEM") && !line.includes("<b>"), `${type} leaked model text`);
    }
    assert.match(statusLineForEvent({ type: "tool.started", tool: { name: "mcp_check_claim" } }), /Checking a quote/);
    assert.equal(statusLineForEvent({ type: "agent.output.delta" }), "");
  });

  it("the AI's words are drawn as text only, and a card only from a verified receipt", () => {
    // No HTML from a model: the page never assigns innerHTML or builds markup from strings.
    assert.doesNotMatch(siteSrc, /\.innerHTML\s*=/);
    assert.doesNotMatch(siteSrc, /insertAdjacentHTML|outerHTML\s*=|document\.write/);
    // Every place a card is made is preceded by a verification that passed.
    const made = [...siteSrc.matchAll(/this\._appendResult\(/g)].length;
    assert.ok(made >= 4, "expected the form, saved-proof, agent and restore paths");
    assert.match(siteSrc, /if \(!verification\.ok\) \{\s*this\._appendNotice\(verificationErrorMessage/);
    assert.match(siteSrc, /if \(!verification\.ok\) continue;/);
  });
});

describe("where a quote that was not found differs (design approved 2026-11-23)", () => {
  const section =
    "(1) Notwithstanding anything contained in any other Act, law or regulation, the right of any person to access or request information, whether or not contained in any written form, which is in the custody or possession of any public official, agency or institution howsoever described, is established.";
  const quote = section.slice(4);

  it("names the one word changed, with words around it", () => {
    const d = wordDifference(quote.replace("any person", "any citizen"), section);
    assert.equal(d.quoteWords, "citizen");
    assert.equal(d.sectionWords, "person");
    assert.match(d.before, /the right of any$/);
    assert.match(d.after, /^to access or request/);
  });

  it("names a word left out and a word added", () => {
    assert.equal(wordDifference(quote.replace("or request ", ""), section).sectionWords, "or request");
    assert.equal(wordDifference(quote.replace("any written", "any lawful written"), section).quoteWords, "lawful");
  });

  it("names a slip in the quote's last words, as a person pasting a part of the section makes", () => {
    const part = "the right of any person to access or request information, whether or not contained in any";
    const typo = wordDifference(`${part} writen form`, section);
    assert.equal(typo.quoteWords, "writen");
    assert.equal(typo.sectionWords, "written");
    const last = wordDifference(`${part} written fom`, section);
    assert.equal(last.quoteWords, "fom");
    assert.equal(last.sectionWords, "form,");
  });

  it("says nothing when it cannot line the two up", () => {
    assert.equal(wordDifference(quote, section), null); // nothing differs
    assert.equal(wordDifference("A sentence from some other law entirely, about fishing rights.", section), null);
    assert.equal(wordDifference("Notwithstanding anything contained in a completely different ending of many words here", section), null);
    assert.equal(wordDifference(null, section), null);
  });
});

describe("the receipt panel (design approved 2026-11-23)", () => {
  const panelSrc = fs.readFileSync(path.join(repoRoot, "web/public/agents/receipt-panel.js"), "utf8");
  const pageHtml = fs.readFileSync(path.join(repoRoot, "web/public/agents/index.html"), "utf8");

  it("is in the page, hidden until there is a check to show", () => {
    assert.match(pageHtml, /<aside class="lc-panel" id="receipt-panel" aria-label="The chosen check" hidden><\/aside>/);
  });

  it("never puts receipt data into innerHTML", () => {
    assert.doesNotMatch(panelSrc, /innerHTML|insertAdjacentHTML|outerHTML/);
  });
});
