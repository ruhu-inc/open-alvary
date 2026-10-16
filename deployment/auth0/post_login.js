/**
 * Auth0 post-login Action for Open Alvary (MAC-07, MAC-08).
 *
 * Console sign-in: when the console asks for a second factor (the standard multi-factor request,
 * acr_values), require one; the person enrols at that moment if they have none.
 *
 * Tools (Claude, ChatGPT, Codex, Claude Code) signing in for the API or MCP resource: send the
 * person to the console's approval page, which names the tool and asks which organisation the
 * connection is for. When the page says the person's role there needs a second factor (owners and
 * admins of organisations with approved or paid access), require one before the token is issued. The answer comes back signed; its grant ID goes into the access token, so
 * disconnecting the app in the console stops every token resting on it. Renewals cannot open a
 * page: they reuse the grant recorded for that tool, and our own check refuses it once revoked.
 *
 * Secrets (set by deployment/auth0/configure.py): CONSENT_SECRET (shared with the console),
 * CONSENT_URL, LOCAL_CONSENT_URL (the approval page of a local trial, for http://localhost resources),
 * RESOURCES (comma-separated resource URLs).
 */

const GRANT = "https://open.alvary.ai/grant";
const ORG = "https://open.alvary.ai/org";
// Every tool sign-in for our resources reads the corpus, whatever scopes the tool asked for (Codex
// asks only for profile scopes). A target scope still passes Auth0's grant checks for third-party
// tools, where an added token scope is ignored.
const SCOPE = "corpus:read";
const MFA = "http://schemas.openid.net/pape/policies/2007/06/multi-factor";

function query(event) {
  return (event.request && event.request.query) || {};
}

function resourceOf(event) {
  const q = query(event);
  return (event.resource_server && event.resource_server.identifier) || q.resource || q.audience || "";
}

function ours(event) {
  return (event.secrets.RESOURCES || "").split(",").filter(Boolean).includes(resourceOf(event));
}

function mfaRequested(event) {
  return String(query(event).acr_values || "").split(" ").includes(MFA);
}

function stored(event) {
  const connections = (event.user.app_metadata && event.user.app_metadata.oa_connections) || {};
  return connections[event.client.client_id];
}

exports.onExecutePostLogin = async (event, api) => {
  if (!ours(event)) {
    if (mfaRequested(event)) api.multifactor.enable("any", { allowRememberBrowser: false });
    return;
  }
  if (api.transaction && api.transaction.addTargetScope) api.transaction.addTargetScope(SCOPE);
  const renewal = event.transaction && event.transaction.protocol === "oauth2-refresh-token";
  const canRedirect = !renewal && (!api.redirect.canRedirect || api.redirect.canRedirect());
  if (!canRedirect) {
    const choice = stored(event);
    if (!choice || !choice.grant) return api.access.deny("This app isn't connected. Connect it again.");
    api.accessToken.setCustomClaim(ORG, choice.org);
    api.accessToken.setCustomClaim(GRANT, choice.grant);
    return;
  }
  const token = api.redirect.encodeToken({
    secret: event.secrets.CONSENT_SECRET,
    expiresInSeconds: 600,
    payload: {
      // The tool's own identifier (its metadata document address), not Auth0's internal ID for it.
      client_id: query(event).client_id || event.client.client_id,
      client_name: event.client.name,
      redirect_uri: query(event).redirect_uri || "",
      resource: resourceOf(event),
      email: event.user.email || "",
    },
  });
  const local = resourceOf(event).startsWith("http://localhost:");
  const consent = local && event.secrets.LOCAL_CONSENT_URL ? event.secrets.LOCAL_CONSENT_URL : event.secrets.CONSENT_URL;
  api.redirect.sendUserTo(consent, { query: { session_token: token } });
};

exports.onContinuePostLogin = async (event, api) => {
  const answer = api.redirect.validateToken({ secret: event.secrets.CONSENT_SECRET, tokenParameterName: "consent_token" });
  if (answer.decision !== "allow" || !answer.org || !answer.grant) {
    return api.access.deny("The connection was not approved.");
  }
  const connections = Object.assign({}, (event.user.app_metadata && event.user.app_metadata.oa_connections) || {});
  connections[event.client.client_id] = { org: answer.org, grant: answer.grant };
  api.user.setAppMetadata("oa_connections", connections);
  if (answer.mfa) api.multifactor.enable("any", { allowRememberBrowser: false });
  api.accessToken.setCustomClaim(ORG, answer.org);
  api.accessToken.setCustomClaim(GRANT, answer.grant);
};
