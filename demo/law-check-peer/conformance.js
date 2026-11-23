/**
 * Stage One Conformance test runners (§4, §12).
 *
 * Four refusal cases, each executed directly against the vendored challenge
 * runtime with a scripted router (no AI key required):
 *   1. invalid_network — malformed network rejected by validateNetwork
 *   2. cycle           — peer calls back into the caller's call chain (COMPOSITION_CYCLE)
 *   3. over_budget     — caller exceeds per-partner / per-message remote call budget
 *   4. denied_origin   — caller attempts to contact an origin outside allowed_peers
 */

import { AgentNetwork, validateNetwork } from "../../web/public/agents/runtime/kernel.js";
import { runAgent } from "../../web/public/agents/runtime/runner.js";
import { manifestAllowsPeer } from "../../web/public/agents/runtime/engine.js";
import { TraceBus } from "../../web/public/agents/runtime/trace.js";

class ScriptedRouter {
  constructor(steps) {
    this.steps = steps;
    this.index = 0;
  }

  async chat() {
    const step = this.steps[this.index++];
    if (!step) {
      return { content: "Done.", toolCalls: [] };
    }
    if (step.tool) {
      return {
        content: "",
        toolCalls: [
          {
            id: `scripted-${this.index}`,
            type: "function",
            function: {
              name: step.tool,
              arguments: JSON.stringify(step.arguments || {}),
            },
          },
        ],
      };
    }
    return { content: step.text || "", toolCalls: [] };
  }
}

function makeComposition(overrides = {}) {
  return {
    deadline: Date.now() + 90_000,
    maxRemoteCalls: 4,
    maxCallsPerPeer: 1,
    maxAgentCalls: 24,
    remoteCalls: 0,
    peerCalls: new Map(),
    agentCalls: 0,
    ...overrides,
  };
}

/**
 * Case 1: Invalid network — front agent references a missing tool.
 */
export async function runInvalidNetworkCase() {
  const brokenConfig = {
    llm_config: { model_name: "scripted" },
    tools: [
      {
        name: "broken_front",
        instructions: "Call a tool that does not exist.",
        tools: ["nonexistent_specialist"],
      },
    ],
  };
  const net = new AgentNetwork(brokenConfig, "broken_net");
  const validation = validateNetwork(net);
  return {
    caseId: "invalid_network",
    title: "Invalid network",
    stopped: validation.errors.length > 0,
    report: validation.errors.join("\n"),
    trace: [{ kind: "validation_error", errors: validation.errors }],
  };
}

/**
 * Case 2: Cycle — network A calls peer B, which is already in the call chain (COMPOSITION_CYCLE).
 */
export async function runCycleCase() {
  const peerUrl = "https://open.alvary.ai/agents/check/law_check";
  const net = new AgentNetwork(
    {
      llm_config: { model_name: "scripted" },
      link_labels: { [peerUrl]: "Open Alvary law check" },
      tools: [
        {
          name: "vendor_desk",
          instructions: "Call the partner network.",
          tools: [peerUrl],
        },
      ],
    },
    "vendor_desk",
  );

  const trace = new TraceBus();
  const llm = new ScriptedRouter([
    { tool: "agents_check_law_check", arguments: { request: "Check s. 1(1)" } },
    { text: "Stopped after cycle detection." },
  ]);

  let toolResultSeen = "";
  const originalChat = llm.chat.bind(llm);
  llm.chat = async (req) => {
    for (const m of req.messages || []) {
      if (m.role === "tool") toolResultSeen = String(m.content || "");
    }
    return originalChat(req);
  };

  const ctx = {
    network: net,
    frontMan: "vendor_desk",
    displayOrigin: "https://demo.example",
    baseOrigin: "https://demo.example",
    llm,
    trace,
    slyData: {},
    composition: makeComposition(),
    requestConsent: async () => "allow",
    resolveRemote: async (url) => {
      const err = new Error(`Cycle detected calling ${url}`);
      err.code = "COMPOSITION_CYCLE";
      throw err;
    },
  };

  await runAgent(ctx, "vendor_desk", "Check a claim", null, []);

  return {
    caseId: "cycle",
    title: "Cycle",
    stopped: toolResultSeen.startsWith("Skipped") && toolResultSeen.includes("already in this request's call chain"),
    report: toolResultSeen,
    trace: trace.events,
  };
}

/**
 * Case 3: Over-budget — calling the same partner twice in one message exceeds per-partner budget (1).
 */
export async function runOverBudgetCase() {
  const peerUrl = "https://open.alvary.ai/agents/check/law_check";
  const net = new AgentNetwork(
    {
      llm_config: { model_name: "scripted" },
      link_labels: { [peerUrl]: "Open Alvary law check" },
      tools: [
        {
          name: "vendor_desk",
          instructions: "Call the partner twice.",
          tools: [peerUrl],
        },
      ],
    },
    "vendor_desk",
  );

  const trace = new TraceBus();
  const llm = new ScriptedRouter([
    { tool: "agents_check_law_check", arguments: { request: "First claim" } },
    { tool: "agents_check_law_check", arguments: { request: "Second claim" } },
    { text: "Stopped after over-budget refusal." },
  ]);

  const toolResults = [];
  const originalChat = llm.chat.bind(llm);
  llm.chat = async (req) => {
    toolResults.length = 0;
    for (const m of req.messages || []) {
      if (m.role === "tool") toolResults.push(String(m.content || ""));
    }
    return originalChat(req);
  };

  const ctx = {
    network: net,
    frontMan: "vendor_desk",
    displayOrigin: "https://demo.example",
    baseOrigin: "https://demo.example",
    llm,
    trace,
    slyData: {},
    composition: makeComposition(),
    requestConsent: async () => "allow",
    resolveRemote: async () => ({
      invoke: async () => JSON.stringify([{ claim: 1, none: true }]),
    }),
  };

  await runAgent(ctx, "vendor_desk", "Check two claims in two calls", null, []);

  const secondResult = toolResults[1] || "";
  const limitEvent = trace.events.find((e) => e.kind === "composition_limit") || null;

  return {
    caseId: "over_budget",
    title: "Over-budget request",
    stopped: secondResult.startsWith("Skipped") && limitEvent !== null,
    report: secondResult,
    trace: trace.events,
  };
}

/**
 * Case 4: Denied origin — attempting to contact an origin not in allowed_peers (or in deny_origins).
 */
export async function runDeniedOriginCase() {
  const manifest = {
    allowed_peers: ["https://open.alvary.ai"],
    peer_policy: { mode: "closed", deny_origins: ["https://untrusted.example"] },
  };
  const targetOrigin = "https://untrusted.example";
  const allowed = manifestAllowsPeer(targetOrigin, manifest);
  const errorMessage = allowed
    ? ""
    : `Policy: untrusted.example is not in demo.example's allowed_peers; refusing to contact it.`;
  return {
    caseId: "denied_origin",
    title: "Denied origin",
    stopped: !allowed,
    report: errorMessage,
    trace: [{ kind: "policy_denied", targetOrigin, mode: manifest.peer_policy.mode, allowed }],
  };
}

export const CONFORMANCE_CASES = {
  invalid_network: runInvalidNetworkCase,
  cycle: runCycleCase,
  over_budget: runOverBudgetCase,
  denied_origin: runDeniedOriginCase,
};
