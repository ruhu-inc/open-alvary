"""Self-test of the check server's token handling, without a person signing in (MAC-16).

Gives the management application a machine token for each test resource and shows that each is
accepted only by its own resource: the resource-binding check. Prints outcomes, never a token.
"""

import httpx

from trials.idp_check.auth0 import Auth0Error, Management, environment

BASE = "http://localhost:8766"


def machine_token(m, env, audience):
    grants = m.get("client-grants", audience=audience, client_id=env["AUTH0_CLIENT_ID"])
    if not grants:
        m.post(
            "client-grants",
            {"client_id": env["AUTH0_CLIENT_ID"], "audience": audience, "scope": ["corpus:read"]},
        )
    answer = httpx.post(
        f"{m.issuer}oauth/token",
        timeout=20,
        data={
            "grant_type": "client_credentials",
            "client_id": env["AUTH0_CLIENT_ID"],
            "client_secret": env["AUTH0_CLIENT_SECRET"],
            "audience": audience,
        },
    )
    answer.raise_for_status()
    return answer.json()["access_token"]


def mcp(token, method, params=None, ident=1):
    return httpx.post(
        f"{BASE}/mcp",
        timeout=20,
        headers={"Authorization": f"Bearer {token}"},
        json={"jsonrpc": "2.0", "id": ident, "method": method, "params": params or {}},
    )


def main():
    env, m = environment(), Management()
    mcp_token = machine_token(m, env, f"{BASE}/mcp")
    api_token = machine_token(m, env, f"{BASE}/api/v1")
    checks = [
        (
            "MCP token on MCP: initialize",
            mcp(mcp_token, "initialize", {"protocolVersion": "2025-11-25"}).status_code,
            200,
        ),
        ("MCP token on MCP: tools/list", mcp(mcp_token, "tools/list").status_code, 200),
        (
            "MCP token on MCP: tools/call",
            mcp(
                mcp_token, "tools/call", {"name": "search_test_corpus", "arguments": {"query": "test"}}
            ).status_code,
            200,
        ),
        (
            "MCP token on API: refused",
            httpx.get(f"{BASE}/api/v1/whoami", headers={"Authorization": f"Bearer {mcp_token}"}).status_code,
            401,
        ),
        (
            "API token on API: accepted",
            httpx.get(f"{BASE}/api/v1/whoami", headers={"Authorization": f"Bearer {api_token}"}).status_code,
            200,
        ),
        ("API token on MCP: refused", mcp(api_token, "tools/list").status_code, 401),
        ("tampered token: refused", mcp(mcp_token[:-4] + "AAAA", "tools/list").status_code, 401),
    ]
    for name, got, want in checks:
        print(f"{'PASS' if got == want else 'FAIL'}  {name}  (HTTP {got}, expected {want})")
    return all(got == want for _, got, want in checks)


if __name__ == "__main__":
    try:
        raise SystemExit(0 if main() else 1)
    except Auth0Error as error:
        raise SystemExit(str(error)) from None
