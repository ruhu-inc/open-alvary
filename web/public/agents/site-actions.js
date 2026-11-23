/**
 * Website actions for Law check (/agents/).
 *
 * Registered with the agentic-web runtime via agent-manifest.json ("actions.module").
 * Both actions are read-only (effect: "read") and never navigate away or move focus.
 */

import { LawCheckPage } from "./site.js";

export function registerActions(actions) {
  actions.register({
    name: "open_section",
    description:
      "Add an 'Open in the reader' link (/sources/<source_id>) to the receipt card for a section and highlight that card without leaving the page.",
    effect: "read",
    availability: "local-only",
    inputSchema: {
      type: "object",
      additionalProperties: false,
      required: ["source_id"],
      properties: {
        source_id: {
          type: "string",
          minLength: 1,
          maxLength: 160,
          description: "The Open Alvary source_id of the cited record.",
        },
        pinpoint: {
          type: "string",
          minLength: 1,
          maxLength: 120,
          description: "Optional section pinpoint (for example, 's. 1(1)').",
        },
      },
    },
    execute: (args) => LawCheckPage.openSection(args),
  });

  actions.register({
    name: "show_receipt",
    description:
      "Expand a verified receipt card by its receipt_id and, on wide screens, scroll it into view without moving keyboard focus.",
    effect: "read",
    availability: "local-only",
    inputSchema: {
      type: "object",
      additionalProperties: false,
      required: ["receipt_id"],
      properties: {
        receipt_id: {
          type: "string",
          minLength: 1,
          maxLength: 120,
          description: "The receipt_id (for example, 'rcp_...') to reveal.",
        },
      },
    },
    execute: (args) => LawCheckPage.showReceipt(args),
  });
}
