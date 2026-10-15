/**
 * Auth0 post-login Action for the identity-provider checks (MAC-07, MAC-16).
 *
 * When a person signs in for one of the check resources, Auth0 sends them to our consent page, which
 * names the tool, shows where it returns, and asks which organisation the connection is for. The
 * choice comes back in a signed token and becomes the access token's organisation claim. Renewals
 * cannot open a page, so they reuse the choice stored for that tool. Each Allow creates a grant on our
 * side; its ID travels in the token, so revoking the grant stops every pass resting on it.
 *
 * Secrets (set by trials/idp_check/setup.py): CONSENT_SECRET, CONSENT_URL, RESOURCES (comma-separated).
 */

const CLAIM = "https://open.alvary.ai/org";
const GRANT = "https://open.alvary.ai/grant";
// Every sign-in for our resources requests reading, whatever scopes the tool asked for (Codex asks only
// for the provider's profile scopes, never the resource's corpus:read). A target scope, unlike a scope
// added to the token, still passes Auth0's client-grant and consent checks, so it works for
// third-party tools too.
const SCOPE = "corpus:read";

function resourceOf(event) {
  const query = event.request && event.request.query ? event.request.query : {};
  return (event.resource_server && event.resource_server.identifier) || query.resource || query.audience || "";
}

function ours(event) {
  const resources = (event.secrets.RESOURCES || "").split(",").filter(Boolean);
  return resources.includes(resourceOf(event));
}

function stored(event) {
  const orgs = (event.user.app_metadata && event.user.app_metadata.oa_check_orgs) || {};
  return orgs[event.client.client_id];
}

exports.onExecutePostLogin = async (event, api) => {
  if (!ours(event)) return;
  if (api.transaction && api.transaction.addTargetScope) api.transaction.addTargetScope(SCOPE);
  const renewal = event.transaction && event.transaction.protocol === "oauth2-refresh-token";
  const canRedirect = !renewal && (!api.redirect.canRedirect || api.redirect.canRedirect());
  if (!canRedirect) {
    const choice = stored(event);
    if (!choice || !choice.grant) return api.access.deny("No organisation was chosen for this connection; connect again.");
    api.accessToken.setCustomClaim(CLAIM, choice.org);
    api.accessToken.setCustomClaim(GRANT, choice.grant);
    return;
  }
  const query = event.request && event.request.query ? event.request.query : {};
  const token = api.redirect.encodeToken({
    secret: event.secrets.CONSENT_SECRET,
    expiresInSeconds: 600,
    payload: {
      // The tool's own identifier (its metadata document address), not Auth0's internal ID for it.
      client_id: query.client_id || event.client.client_id,
      client_name: event.client.name,
      redirect_uri: query.redirect_uri || "",
      resource: resourceOf(event),
      email: event.user.email || "",
    },
  });
  api.redirect.sendUserTo(event.secrets.CONSENT_URL, { query: { session_token: token } });
};

exports.onContinuePostLogin = async (event, api) => {
  const answer = api.redirect.validateToken({ secret: event.secrets.CONSENT_SECRET, tokenParameterName: "consent_token" });
  if (answer.decision !== "allow" || !answer.org || !answer.grant) {
    return api.access.deny("The connection was not allowed.");
  }
  const orgs = Object.assign({}, (event.user.app_metadata && event.user.app_metadata.oa_check_orgs) || {});
  orgs[event.client.client_id] = { org: answer.org, grant: answer.grant };
  api.user.setAppMetadata("oa_check_orgs", orgs);
  api.accessToken.setCustomClaim(CLAIM, answer.org);
  api.accessToken.setCustomClaim(GRANT, answer.grant);
};
