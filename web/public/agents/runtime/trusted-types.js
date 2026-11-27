// trusted-types.js: the runtime's one Trusted Types policy, "agentify-website".
// Added by agentic-web. See README.md in this folder.
//
// The policy returns each string unchanged. That is safe because every value the runtime
// puts in its own HTML goes through escapeHtml, a model's HTML goes only through
// renderSafeHtml (cruse-render.js), pages parsed with DOMParser stay out of the live
// page, and the only script URL is the runtime's own sandbox worker. Keep it that way
// when changing a template.

let policy;

/**
 * The policy, created on first use.
 *
 * :return: The policy, or null when the browser has no Trusted Types or the page's
 *   Content-Security-Policy does not allow this name. The strings then stay plain, so a
 *   default policy, if the page has one, still receives them.
 */
function trustedPolicy() {
  if (policy !== undefined) return policy;
  policy = null;
  try {
    policy = globalThis.trustedTypes?.createPolicy("agentify-website", { createHTML: (html) => html, createScriptURL: (url) => url }) ?? null;
  } catch {
    // The page does not allow this name. The browser reports that itself.
  }
  return policy;
}

/**
 * HTML the runtime built, in the form the browser accepts for innerHTML and similar.
 *
 * :param html: HTML whose values are escaped, or a model's HTML on its way to renderSafeHtml.
 * :return: TrustedHTML where the browser has Trusted Types, else the string.
 */
export function trustedHtml(html) {
  const p = trustedPolicy();
  return p ? p.createHTML(String(html)) : html;
}

/**
 * A script URL of the runtime's own, in the form the browser accepts for new Worker().
 *
 * :param url: The URL, as a string or URL.
 * :return: TrustedScriptURL where the browser has Trusted Types, else the URL.
 */
export function trustedScriptUrl(url) {
  const p = trustedPolicy();
  return p ? p.createScriptURL(String(url)) : url;
}
