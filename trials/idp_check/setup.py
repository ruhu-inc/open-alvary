"""Configure the Auth0 test tenant for the identity-provider checks (MAC-16). Safe to run again.

    python -m trials.idp_check.setup [--public-url https://…]

Creates or updates: the test MCP resource and a test API resource (scope `corpus:read`, 15-minute
access tokens, refresh allowed, RFC 9068 tokens); Google and email-code sign-in available to
third-party tools; dynamic registration on as the fallback; Claude Code's client metadata document
imported. Prints what it did, never a credential.
"""

import argparse
import secrets
import time
from pathlib import Path

from trials.idp_check.auth0 import ROOT, Auth0Error, Management

LOCAL = "http://localhost:8766"
CLAUDE_CODE = "https://claude.ai/oauth/claude-code-client-metadata"
CLAUDE = (
    "https://claude.ai/oauth/mcp-oauth-client-metadata"  # claude.ai, desktop, mobile (seen in Auth0's log)
)
CHATGPT = "https://chatgpt.com/oauth/BpZIpxL-aggt/client.json"
CODEX = "https://chatgpt.com/oauth/codex/client.json"
# Tools reviewed and imported by their client metadata documents (MAC-07 "verified publisher").
REVIEWED = [CLAUDE_CODE, CLAUDE, CHATGPT, CODEX]


def resources(base):
    return {f"{base}/mcp": "Open Alvary check: MCP", f"{base}/api/v1": "Open Alvary check: API"}


def ensure_resource(m, identifier, name):
    settings = {
        "name": name,
        "scopes": [{"value": "corpus:read", "description": "Read the published Open Alvary corpus"}],
        "token_lifetime": 900,
        "token_lifetime_for_web": 900,
        "allow_offline_access": True,
        "skip_consent_for_verifiable_first_party_clients": False,
        "token_dialect": "rfc9068_profile",
    }
    existing = [r for r in m.get("resource-servers", per_page=100) if r.get("identifier") == identifier]
    if existing:
        m.patch(f"resource-servers/{existing[0]['id']}", settings)
        return f"updated {identifier}"
    m.post("resource-servers", {"identifier": identifier, "signing_alg": "RS256", **settings})
    return f"created {identifier}"


def ensure_connections(m):
    done = []
    connections = {c["name"]: c for c in m.get("connections", per_page=100)}
    google = connections.get("google-oauth2")
    if google and not google.get("is_domain_connection"):
        m.patch(f"connections/{google['id']}", {"is_domain_connection": True})
        done.append("google-oauth2 available to third-party tools")
    if "email" not in connections:
        try:
            create_email(m)
        except Auth0Error as error:
            done.append(f"email code sign-in NOT created ({error})")
        else:
            done.append("email code sign-in created")
    elif not connections["email"].get("is_domain_connection"):
        m.patch(f"connections/{connections['email']['id']}", {"is_domain_connection": True})
        done.append("email code sign-in available to third-party tools")
    return done or ["connections already set"]


def create_email(m):
    m.post(
        "connections",
        {
            "name": "email",
            "strategy": "email",
            "is_domain_connection": True,
            "options": {
                "name": "email",
                "disable_signup": False,
                "brute_force_protection": True,
                "totp": {"time_step": 300, "length": 6},
                "email": {
                    "syntax": "liquid",
                    "from": "",
                    "subject": "Your Open Alvary sign-in code",
                    "body": "<p>Your sign-in code is <strong>{{ code }}</strong>. It expires in 5 minutes.</p>",
                },
            },
        },
    )


def imported(m, document):
    """Auth0's client ID for a reviewed tool's metadata document, importing it if needed."""
    preview = m.post("clients/cimd/preview", {"external_client_id": document})
    return (
        preview.get("client_id")
        or m.post("clients/cimd/register", {"external_client_id": document})["client_id"]
    )


def ensure_registration(m):
    m.patch("tenants/settings", {"flags": {"enable_dynamic_client_registration": True}})
    return [f"imported {document} ({imported(m, document)})" for document in REVIEWED] + [
        "dynamic registration on"
    ]


def ensure_user_grant(m, client_id, audience):
    """Third-party tools need an explicit grant per resource, for people signing in only."""
    existing = m.get("client-grants", client_id=client_id, audience=audience)
    if any(g.get("subject_type") == "user" for g in existing):
        return f"{client_id} already allowed on {audience}"
    m.post(
        "client-grants",
        {"client_id": client_id, "audience": audience, "scope": ["corpus:read"], "subject_type": "user"},
    )
    return f"{client_id} allowed on {audience} (people signing in, corpus:read)"


CONSENT_SECRET = ROOT / "private-staging" / "idp-check" / "consent-secret"
ACTION = "open-alvary-check-consent"


def consent_secret():
    if not CONSENT_SECRET.exists():
        CONSENT_SECRET.parent.mkdir(parents=True, exist_ok=True)
        CONSENT_SECRET.write_text(secrets.token_urlsafe(32))
        CONSENT_SECRET.chmod(0o600)
    return CONSENT_SECRET.read_text().strip()


def ensure_consent_action(m, bases):
    """Deploy the post-login Action that sends people to our consent page, and bind it."""
    code = (Path(__file__).parent / "consent_action.js").read_text()
    settings = {
        "code": code,
        "runtime": "node22",
        "secrets": [
            {"name": "CONSENT_SECRET", "value": consent_secret()},
            {"name": "CONSENT_URL", "value": f"{bases[0]}/consent"},
            {"name": "RESOURCES", "value": ",".join(f"{b}/mcp,{b}/api/v1" for b in bases)},
        ],
    }
    found = m.get("actions/actions", actionName=ACTION).get("actions", [])
    if found:
        action = m.patch(f"actions/actions/{found[0]['id']}", settings)
    else:
        action = m.post(
            "actions/actions",
            {"name": ACTION, "supported_triggers": [{"id": "post-login", "version": "v3"}], **settings},
        )
    for _ in range(30):
        if m.get(f"actions/actions/{action['id']}").get("status") == "built":
            break
        time.sleep(1)
    m.post(f"actions/actions/{action['id']}/deploy", {})
    bindings = m.get("actions/triggers/post-login/bindings").get("bindings", [])
    refs = [
        {"ref": {"type": "action_id", "value": b["action"]["id"]}, "display_name": b["display_name"]}
        for b in bindings
        if b["action"]["id"] != action["id"]
    ]
    refs.append({"ref": {"type": "action_id", "value": action["id"]}, "display_name": ACTION})
    m.patch("actions/triggers/post-login/bindings", {"bindings": refs})
    return f"consent Action deployed and bound (consent page {bases[0]}/consent)"


FACTORS = ["otp", "webauthn-platform", "webauthn-roaming", "recovery-code"]  # MAC-08 second factors


def ensure_factors(m):
    """Authenticator apps, device biometrics (Touch ID, Windows Hello), security keys, recovery codes.
    Nobody is asked for them unless the consent Action requires it (MAC-08: by role, at the action)."""
    for factor in FACTORS:
        m.call("PUT", f"guardian/factors/{factor}", {"enabled": True})
    return f"second factors on: {', '.join(FACTORS)}"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--public-url", help="the public test address, once approved and created")
    args = parser.parse_args()
    m = Management()
    for base in [LOCAL] + ([args.public_url.rstrip("/")] if args.public_url else []):
        for identifier, name in resources(base).items():
            print(ensure_resource(m, identifier, name))
    for line in ensure_connections(m) + ensure_registration(m) + [ensure_factors(m)]:
        print(line)
    bases = [LOCAL] + ([args.public_url.rstrip("/")] if args.public_url else [])
    for document in REVIEWED:
        for base in bases:
            print(ensure_user_grant(m, imported(m, document), f"{base}/mcp"))
    print(ensure_consent_action(m, bases[::-1]))
    print("issuer:", m.issuer)


if __name__ == "__main__":
    try:
        main()
    except Auth0Error as error:
        raise SystemExit(str(error)) from None
