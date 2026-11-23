/**
 * Unified consent controller for Example Vendor (§12).
 *
 * Supplies the runtime's engine.onConsent hook AND governs the page's own
 * direct check_claim calls to open.alvary.ai with one card:
 *   "This page will ask open.alvary.ai to find and check legal wording.
 *    It sends the claims, the citation and the quoted words, nothing else."
 * Buttons: "Allow" and "Not now".
 */

export const CONSENT_MESSAGE =
  "This page will ask open.alvary.ai to find and check legal wording. " +
  "It sends the claims, the citation and the quoted words, nothing else.";

export class UnifiedConsentController {
  /**
   * @param {(prompt: { message: string, info?: any }) => Promise<"allow" | "decline">} promptFn
   */
  constructor(promptFn) {
    this.promptFn = promptFn;
    this.decision = null; // null | "allow" | "decline"
    this.promptCount = 0;
  }

  /**
   * Ask once per session (unless reset), returning "allow" or "decline".
   * Compatible with runtime's engine.onConsent(info).
   *
   * @param {any} [info]
   * @returns {Promise<"allow" | "decline">}
   */
  async requestConsent(info = null) {
    if (this.decision === "allow") return "allow";
    this.promptCount += 1;
    const answer = await this.promptFn({ message: CONSENT_MESSAGE, info });
    const normalized = answer === "allow" || answer === "always" ? "allow" : "decline";
    if (normalized === "allow") {
      this.decision = "allow";
    }
    return normalized;
  }

  reset() {
    this.decision = null;
  }
}

/**
 * Browser UI helper: opens a modal <dialog> consent card with Allow / Not now.
 *
 * @param {HTMLDialogElement} dialogEl
 * @returns {(prompt: { message: string }) => Promise<"allow" | "decline">}
 */
export function createDialogConsentPrompt(dialogEl) {
  return ({ message }) =>
    new Promise((resolve) => {
      const msgEl = dialogEl.querySelector("[data-consent-message]");
      const allowBtn = dialogEl.querySelector("[data-consent-allow]");
      const declineBtn = dialogEl.querySelector("[data-consent-decline]");
      if (msgEl) msgEl.textContent = message;

      const cleanup = (result) => {
        allowBtn?.removeEventListener("click", onAllow);
        declineBtn?.removeEventListener("click", onDecline);
        dialogEl.removeEventListener("cancel", onCancel);
        if (dialogEl.open) dialogEl.close();
        resolve(result);
      };
      const onAllow = () => cleanup("allow");
      const onDecline = () => cleanup("decline");
      const onCancel = (e) => {
        e.preventDefault();
        cleanup("decline");
      };

      allowBtn?.addEventListener("click", onAllow);
      declineBtn?.addEventListener("click", onDecline);
      dialogEl.addEventListener("cancel", onCancel);

      if (typeof dialogEl.showModal === "function") {
        dialogEl.showModal();
      } else {
        dialogEl.setAttribute("open", "");
      }
    });
}
