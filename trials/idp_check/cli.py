"""Our reference command-line tool for the identity-provider checks (MAC-09, MAC-16).

    python -m trials.idp_check.cli login            # browser sign-in, loopback callback on a random port
    python -m trials.idp_check.cli login --device   # device code: sign in on any device
    python -m trials.idp_check.cli search <words>   # call the test MCP server with the stored pass
    python -m trials.idp_check.cli reuse-check      # a used renewal pass must be refused (invalid_grant)

A pre-registered public client (no secret) with PKCE. Passes are kept for the check only in
private-staging/idp-check/cli-tokens.json (mode 600) and are never printed.
"""

import argparse
import base64
import hashlib
import http.server
import json
import secrets
import threading
import time
import webbrowser
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from trials.idp_check.auth0 import ROOT, Management, environment

BASE = "http://localhost:8766"
RESOURCE = f"{BASE}/mcp"
SCOPE = "openid corpus:read offline_access"
STATE = ROOT / "private-staging" / "idp-check"
CLIENT_FILE = STATE / "cli-client.json"
TOKENS = STATE / "cli-tokens.json"
NAME = "Open Alvary CLI (check)"
# Auth0 matches loopback ports loosely only for tools identified by a client metadata document; a
# pre-registered client needs its exact callback, so our tool listens on a fixed port.
FIXED_PORT = 53682


def issuer():
    return f"https://{environment()['AUTH0_DOMAIN'].removeprefix('https://').rstrip('/')}/"


def client_id():
    """Create our pre-registered public client once (needs the management application)."""
    if CLIENT_FILE.exists():
        return json.loads(CLIENT_FILE.read_text())["client_id"]
    m = Management()
    found = [c for c in m.get("clients", fields="client_id,name", per_page=100) if c.get("name") == NAME]
    client = (
        found[0]
        if found
        else m.post(
            "clients",
            {
                "name": NAME,
                "app_type": "native",
                "token_endpoint_auth_method": "none",
                "grant_types": [
                    "authorization_code",
                    "refresh_token",
                    "urn:ietf:params:oauth:grant-type:device_code",
                ],
                "callbacks": [f"http://127.0.0.1:{FIXED_PORT}/callback"],
                "oidc_conformant": True,
                "refresh_token": {
                    "rotation_type": "rotating",
                    "expiration_type": "expiring",
                    "leeway": 0,
                    "token_lifetime": 2592000,
                    "idle_token_lifetime": 1296000,
                    "infinite_token_lifetime": False,
                    "infinite_idle_token_lifetime": False,
                },
            },
        )
    )
    STATE.mkdir(parents=True, exist_ok=True)
    CLIENT_FILE.write_text(json.dumps({"client_id": client["client_id"], "name": NAME}))
    return client["client_id"]


def save(tokens):
    STATE.mkdir(parents=True, exist_ok=True)
    TOKENS.write_text(json.dumps(tokens))
    TOKENS.chmod(0o600)


def token_request(data):
    return httpx.post(f"{issuer()}oauth/token", data=data, timeout=20)


def login_loopback(fresh=False, open_browser=True, port=FIXED_PORT, mfa=False):
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state, answer = secrets.token_urlsafe(16), {}

    class Callback(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            query = {k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()}
            if urlsplit(self.path).path == "/callback":
                answer.update(query)
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Signed in. You can close this tab and return to the terminal.")

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", port), Callback)
    redirect = f"http://127.0.0.1:{server.server_port}/callback"
    threading.Thread(target=server.handle_request, daemon=True).start()
    url = f"{issuer()}authorize?" + urlencode(
        {
            "response_type": "code",
            "client_id": client_id(),
            "redirect_uri": redirect,
            "scope": SCOPE,
            "resource": RESOURCE,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            **({"prompt": "login"} if fresh else {}),
            **({"acr_values": "http://schemas.openid.net/pape/policies/2007/06/multi-factor"} if mfa else {}),
        }
    )
    print(
        f"Opening the browser to sign in (callback on port {server.server_port}).\nIf it does not open: {url}"
    )
    if open_browser:
        webbrowser.open(url)
    deadline = time.time() + 300
    while "code" not in answer and "error" not in answer and time.time() < deadline:
        time.sleep(0.3)
    if answer.get("state") != state or "code" not in answer:
        raise SystemExit(
            f"sign-in failed: {answer.get('error', 'timed out')} {answer.get('error_description', '')}"
        )
    reply = token_request(
        {
            "grant_type": "authorization_code",
            "client_id": client_id(),
            "code": answer["code"],
            "redirect_uri": redirect,
            "code_verifier": verifier,
        }
    )
    if reply.status_code != 200:
        raise SystemExit(f"code exchange refused: {reply.json()}")
    save(reply.json())
    claims = json.loads(base64.urlsafe_b64decode(reply.json()["id_token"].split(".")[1] + "=="))
    print(
        f"Signed in; pass stored for the check. Sign-in methods (amr): {claims.get('amr', ['none reported'])}"
    )


def login_device():
    reply = httpx.post(
        f"{issuer()}oauth/device/code",
        timeout=20,
        data={"client_id": client_id(), "scope": SCOPE, "audience": RESOURCE},
    )
    if reply.status_code != 200:
        raise SystemExit(f"device code refused: {reply.json()}")
    grant = reply.json()
    print(f"On any device, open {grant['verification_uri']} and enter the code {grant['user_code']}")
    print(
        f"(or open {grant['verification_uri_complete']}). Check the code matches; never enter a code someone sent you."
    )
    interval, deadline, seen = grant.get("interval", 5), time.time() + grant["expires_in"], []
    while time.time() < deadline:
        time.sleep(interval)
        reply = token_request(
            {
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "client_id": client_id(),
                "device_code": grant["device_code"],
            }
        )
        error = reply.json().get("error") if reply.status_code != 200 else None
        if error and (not seen or seen[-1] != error):
            seen.append(error)
            print(f"  waiting: {error}")
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            interval += 5
            continue
        if error:
            raise SystemExit(f"device sign-in ended: {error}")
        save(reply.json())
        print("Signed in on the other device; pass stored for the check.")
        return
    raise SystemExit("device code expired")


def search(words):
    tokens = json.loads(TOKENS.read_text())
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    call = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "search_test_corpus", "arguments": {"query": " ".join(words)}},
    }
    reply = httpx.post(RESOURCE, json=call, headers=headers, timeout=20)
    if reply.status_code != 200:
        raise SystemExit(f"HTTP {reply.status_code}: {reply.text[:300]}")
    print(reply.json()["result"]["content"][0]["text"])


def reuse_check():
    first = json.loads(TOKENS.read_text())["refresh_token"]

    def renew(token):
        return token_request(
            {"grant_type": "refresh_token", "client_id": client_id(), "refresh_token": token}
        )

    one = renew(first)
    print(f"1. renew with the pass:          HTTP {one.status_code} (expect 200)")
    if one.status_code != 200:
        raise SystemExit(one.json())
    second = one.json()
    save({**json.loads(TOKENS.read_text()), **second})
    again = renew(first)
    print(
        f"2. renew with the used pass:     HTTP {again.status_code} {again.json().get('error')} (expect 4xx invalid_grant)"
    )
    after = renew(second.get("refresh_token", ""))
    print(
        f"3. renew with the newest pass:   HTTP {after.status_code} {after.json().get('error')} "
        "(expect invalid_grant: reuse ends the whole family)"
    )


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    login = sub.add_parser("login")
    login.add_argument("--device", action="store_true")
    login.add_argument("--fresh", action="store_true", help="ask to sign in even if a session exists")
    login.add_argument("--no-browser", action="store_true", help="only print the sign-in address")
    login.add_argument("--mfa", action="store_true", help="require a second factor for this sign-in")
    found = sub.add_parser("search")
    found.add_argument("words", nargs="+")
    sub.add_parser("reuse-check")
    args = parser.parse_args()
    if args.command == "login":
        login_device() if args.device else login_loopback(args.fresh, not args.no_browser, mfa=args.mfa)
    elif args.command == "search":
        search(args.words)
    else:
        reuse_check()


if __name__ == "__main__":
    main()
