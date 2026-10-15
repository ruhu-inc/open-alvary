"""Bring the Auth0 tenant in line with Open Alvary's production design (MAC-07, MAC-08, MAC-16).

    python -m deployment.auth0.configure            show what would change (default; nothing written)
    python -m deployment.auth0.configure --apply    make the changes

What it keeps in line, by name, so reruns change nothing:
  - the API and MCP resources (https://api.<domain>/api/v1 and /mcp): corpus:read, 15-minute access
    tokens, refresh tokens allowed;
  - each reviewed tool (Claude, Claude Code, ChatGPT, Codex: imported earlier by their metadata
    documents) allowed on both resources for people signing in;
  - the console's web application ("Open Alvary console") with its callback, using Google and email
    codes only;
  - for a local trial (--local-origin, http://localhost:8022 by default): local API and MCP resources
    and the console's local callback, so the whole flow can be tried before a deploy;
  - the post-login Action (deployment/auth0/post_login.js) with its secrets, bound in place of the
    sign-in check's.

New secrets (the console's client secret and the hand-off secret shared with the console) are written
to private-staging/auth0/ (git-ignored), never printed, for Secret Manager. Credentials for the
Management API come from the git-ignored .env (AUTH0_DOMAIN, AUTH0_CLIENT_ID, AUTH0_CLIENT_SECRET).
"""

import argparse
import secrets
import time
from pathlib import Path

from trials.idp_check.auth0 import Management

ROOT = Path(__file__).resolve().parents[2]
PRIVATE = ROOT / "private-staging" / "auth0"
ACTION = "open-alvary-post-login"
CONSOLE = "Open Alvary console"
TOOLS = ("Claude", "Claude Code", "ChatGPT", "Codex")
CONNECTIONS = ("google-oauth2", "email")


def plan(
    m: Management, api_origin: str, site: str, local: list[str], local_origin: str | None = None
) -> list[tuple[str, callable]]:
    steps = []
    resources = {"Open Alvary API": api_origin + "/api/v1", "Open Alvary MCP": api_origin + "/mcp"}
    if local_origin:
        # A local trial of the whole flow (console, approval page, Claude Code) before any deploy.
        resources |= {
            "Open Alvary API (local trial)": local_origin + "/api/v1",
            "Open Alvary MCP (local trial)": local_origin + "/mcp",
        }
    existing = {r["identifier"]: r for r in m.call("GET", "resource-servers")}
    for name, identifier in resources.items():
        wanted = {
            "name": name,
            "scopes": [{"value": "corpus:read", "description": "Read the published Open Alvary corpus"}],
            "token_lifetime": 900,
            "token_lifetime_for_web": 900,
            "allow_offline_access": True,
            "signing_alg": "RS256",
            "skip_consent_for_verifiable_first_party_clients": True,
        }
        if identifier not in existing:
            steps.append(
                (
                    f"create resource {identifier}",
                    lambda w=wanted, i=identifier: m.call("POST", "resource-servers", {**w, "identifier": i}),
                )
            )
        else:
            current = existing[identifier]
            changed = {k: v for k, v in wanted.items() if current.get(k) != v}
            if changed:
                steps.append(
                    (
                        f"update resource {identifier}: {sorted(changed)}",
                        lambda c=changed, r=current: m.call("PATCH", f"resource-servers/{r['id']}", c),
                    )
                )

    clients = m.call("GET", "clients", fields="name,client_id,app_type,callbacks", include_fields="true")
    tools = [c for c in clients if c["name"] in TOOLS and c["client_id"].startswith(("tpc_", "https://"))]
    grants = {(g["client_id"], g["audience"]) for g in m.call("GET", "client-grants")}
    for tool in tools:
        for identifier in resources.values():
            if (tool["client_id"], identifier) not in grants:
                steps.append(
                    (
                        f"allow {tool['name']} on {identifier}",
                        lambda t=tool, i=identifier: m.call(
                            "POST",
                            "client-grants",
                            {
                                "client_id": t["client_id"],
                                "audience": i,
                                "scope": ["corpus:read"],
                                "subject_type": "user",
                            },
                        ),
                    )
                )

    callbacks = [site + "/console/auth/callback", *local]
    console = next((c for c in clients if c["name"] == CONSOLE), None)
    if console is None:

        def create_console():
            created = m.call(
                "POST",
                "clients",
                {
                    "name": CONSOLE,
                    "app_type": "regular_web",
                    "callbacks": callbacks,
                    "allowed_logout_urls": [site + "/console/sign-in"],
                    "web_origins": [],
                    "grant_types": ["authorization_code"],
                    "token_endpoint_auth_method": "client_secret_post",
                    "oidc_conformant": True,
                    "is_first_party": True,
                },
            )
            _store("console-client-id", created["client_id"])
            _store("console-client-secret", created["client_secret"])
            return created

        steps.append((f"create the console application ({callbacks[0]})", create_console))
    elif sorted(console.get("callbacks") or []) != sorted(callbacks):
        steps.append(
            (
                "update the console's callbacks",
                lambda c=console: m.call("PATCH", f"clients/{c['client_id']}", {"callbacks": callbacks}),
            )
        )

    def enable_connections():
        console_id = next(
            c["client_id"]
            for c in m.call("GET", "clients", fields="name,client_id", include_fields="true")
            if c["name"] == CONSOLE
        )
        for connection in m.call("GET", "connections", fields="id,name", include_fields="true"):
            if connection["name"] in CONNECTIONS:
                enabled = m.call("GET", f"connections/{connection['id']}/clients").get("clients", [])
                if console_id not in {c["client_id"] for c in enabled}:
                    m.call(
                        "PATCH",
                        f"connections/{connection['id']}/clients",
                        [{"client_id": console_id, "status": True}],
                    )

    steps.append(("enable Google and email codes for the console (where not already)", enable_connections))

    code = (Path(__file__).parent / "post_login.js").read_text()
    actions = {a["name"]: a for a in m.call("GET", "actions/actions").get("actions", [])}

    def deploy_action():
        consent = _secret("consent-secret")
        body = {
            "name": ACTION,
            "supported_triggers": [{"id": "post-login", "version": "v3"}],
            "code": code,
            "runtime": "node22",
            "secrets": [
                {"name": "CONSENT_SECRET", "value": consent},
                {"name": "CONSENT_URL", "value": site + "/console/oauth/consent"},
                {"name": "LOCAL_CONSENT_URL", "value": (local_origin or site) + "/console/oauth/consent"},
                {"name": "RESOURCES", "value": ",".join(resources.values())},
            ],
        }
        action = actions.get(ACTION)
        action = (
            m.call("PATCH", f"actions/actions/{action['id']}", body)
            if action
            else m.call("POST", "actions/actions", body)
        )
        for _ in range(30):
            if m.call("GET", f"actions/actions/{action['id']}").get("status") == "built":
                break
            time.sleep(2)
        m.call("POST", f"actions/actions/{action['id']}/deploy")
        m.call(
            "PATCH",
            "actions/triggers/post-login/bindings",
            {"bindings": [{"ref": {"type": "action_name", "value": ACTION}, "display_name": ACTION}]},
        )

    current = actions.get(ACTION)
    wanted_secrets = {"CONSENT_URL", "LOCAL_CONSENT_URL", "CONSENT_SECRET", "RESOURCES"}
    if (
        current is None
        or current.get("code") != code
        or {x["name"] for x in current.get("secrets", [])} != wanted_secrets
    ):
        steps.append(
            ("deploy the post-login Action and bind it (replacing the sign-in check's)", deploy_action)
        )
    return steps


def _store(name: str, value: str) -> None:
    PRIVATE.mkdir(parents=True, exist_ok=True)
    path = PRIVATE / name
    path.write_text(value)
    path.chmod(0o600)


def _secret(name: str) -> str:
    path = PRIVATE / name
    if not path.exists():
        _store(name, secrets.token_urlsafe(48))
    return path.read_text().strip()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--apply", action="store_true", help="Make the changes (default: show them)")
    parser.add_argument("--site", default="https://open.alvary.ai")
    parser.add_argument("--api-origin", default="https://api.open.alvary.ai")
    parser.add_argument(
        "--local-origin",
        default="http://localhost:8022",
        help="Origin of a local trial (its console callback, API and MCP resources); '' for none",
    )
    args = parser.parse_args(argv)
    m = Management()
    local = args.local_origin.rstrip("/") or None
    steps = plan(
        m,
        args.api_origin.rstrip("/"),
        args.site.rstrip("/"),
        [local + "/console/auth/callback"] if local else [],
        local,
    )
    for description, _ in steps:
        print(("applying: " if args.apply else "would ") + description)
    if args.apply:
        for _, action in steps:
            action()
        print(f"Done. Secrets for Secret Manager are in {PRIVATE.relative_to(ROOT)}/ (never commit them).")
    elif not steps:
        print("Nothing to change.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
