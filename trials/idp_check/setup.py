"""Configure the Auth0 test tenant for the identity-provider checks (MAC-16). Safe to run again.

    python -m trials.idp_check.setup [--public-url https://…]

Creates or updates: the test MCP resource and a test API resource (scope `corpus:read`, 15-minute
access tokens, refresh allowed, RFC 9068 tokens); Google and email-code sign-in available to
third-party tools; dynamic registration on as the fallback; Claude Code's client metadata document
imported. Prints what it did, never a credential.
"""

import argparse

from trials.idp_check.auth0 import Auth0Error, Management

LOCAL = "http://localhost:8766"
CLAUDE_CODE = "https://claude.ai/oauth/claude-code-client-metadata"


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


def ensure_registration(m):
    m.patch("tenants/settings", {"flags": {"enable_dynamic_client_registration": True}})
    preview = m.post("clients/cimd/preview", {"external_client_id": CLAUDE_CODE})
    if preview.get("client_id"):
        return [f"Claude Code already imported ({preview['client_id']})", "dynamic registration on"]
    created = m.post("clients/cimd/register", {"external_client_id": CLAUDE_CODE})
    return [f"Claude Code imported ({created['client_id']})", "dynamic registration on"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--public-url", help="the public test address, once approved and created")
    args = parser.parse_args()
    m = Management()
    for base in [LOCAL] + ([args.public_url.rstrip("/")] if args.public_url else []):
        for identifier, name in resources(base).items():
            print(ensure_resource(m, identifier, name))
    for line in ensure_connections(m) + ensure_registration(m):
        print(line)
    print("issuer:", m.issuer)


if __name__ == "__main__":
    try:
        main()
    except Auth0Error as error:
        raise SystemExit(str(error)) from None
