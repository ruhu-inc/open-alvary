"""A small MCP server protected by Auth0, for the identity-provider checks (MAC-16).

    python -m trials.idp_check.server            # http://localhost:8766/mcp
    IDP_CHECK_BASE_URL=https://… python -m trials.idp_check.server

It exposes one read-only tool over a made-up test corpus and does only what the checks need:
protected-resource metadata (RFC 9728), the 401 challenge that starts sign-in, and access-token
checks (RS256 signature against the issuer's keys, issuer, audience equal to this resource, expiry,
scope `corpus:read`). A second resource, `/api/v1/whoami`, accepts only tokens for the API resource,
so the resource-binding check can show that a token for one is refused by the other.

Each request's outcome is appended to private-staging/idp-check/events.jsonl: never a token, only
the outcome, the client ID and a shortened subject.
"""

import base64
import hashlib
import hmac
import html
import json
import os
import secrets
import time
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from trials.idp_check.auth0 import ROOT, environment

ENV = environment()
BASE = os.environ.get("IDP_CHECK_BASE_URL", "http://localhost:8766").rstrip("/")
ISSUER = f"https://{ENV['AUTH0_DOMAIN'].removeprefix('https://').rstrip('/')}/"
SCOPE = "corpus:read"
RESOURCES = {"mcp": f"{BASE}/mcp", "api": f"{BASE}/api/v1"}
VERSIONS = ["2026-07-28", "2025-11-25", "2025-06-18", "2025-03-26"]
EVENTS = ROOT / "private-staging" / "idp-check" / "events.jsonl"
# Our own grant check (MAC-04, MAC-09): each Allow on the consent page creates a grant; its ID travels
# in every access token, renewals included, and a revoked or unknown grant is refused at once.
GRANTS = ROOT / "private-staging" / "idp-check" / "grants.json"
GRANT_CLAIM = "https://open.alvary.ai/grant"
CONSENT_SECRET = ROOT / "private-staging" / "idp-check" / "consent-secret"
ORG_CLAIM = "https://open.alvary.ai/org"
# Test organisations of the signed-in person: only entitled ones may be chosen (MAC-07).
ORGS = [
    {"id": "org_test_personal", "name": "Personal (test)", "entitled": True},
    {"id": "org_test_firm", "name": "Example Law Firm (test)", "entitled": True},
    {"id": "org_test_lapsed", "name": "Lapsed Ltd (test)", "entitled": False},
]
PUBLISHERS = {"claude.ai": "Anthropic", "chatgpt.com": "OpenAI", "openai.com": "OpenAI"}

CORPUS = [
    {
        "id": "test-act-1",
        "title": "Test Act No. 1 (made-up text for sign-in checks)",
        "text": "Section 1. This Act exists only to test sign-in. It is not law.",
    },
    {
        "id": "test-act-2",
        "title": "Test Regulation No. 2 (made-up text for sign-in checks)",
        "text": "Article 1. Nothing here is a real legal text.",
    },
]
TOOL = {
    "name": "search_test_corpus",
    "title": "Search the test corpus",
    "description": "Search a tiny made-up corpus used only to check Open Alvary sign-in.",
    "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
    "annotations": {"readOnlyHint": True},
}

app = FastAPI(title="Open Alvary sign-in check", docs_url=None, redoc_url=None, openapi_url=None)
_keys = {"at": 0.0, "keys": {}}


def record(event):
    line = json.dumps({"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "check": "idp", **event})
    print(line, flush=True)  # on Cloud Run this is the record (Cloud Logging); never a token
    EVENTS.parent.mkdir(parents=True, exist_ok=True)
    with EVENTS.open("a", encoding="utf-8") as out:
        out.write(line + "\n")


def b64(data):
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def signing_keys(refresh=False):
    if refresh or time.time() - _keys["at"] > 600:
        jwks = httpx.get(f"{ISSUER}.well-known/jwks.json", timeout=10).json()
        _keys["keys"] = {
            k["kid"]: rsa.RSAPublicNumbers(
                int.from_bytes(b64(k["e"]), "big"), int.from_bytes(b64(k["n"]), "big")
            ).public_key()
            for k in jwks["keys"]
            if k.get("kty") == "RSA"
        }
        _keys["at"] = time.time()
    return _keys["keys"]


def check_token(token, resource):
    """Return the token's claims, or raise ValueError with the reason it is refused."""
    try:
        head, body, signature = token.split(".")
        header, claims = json.loads(b64(head)), json.loads(b64(body))
    except ValueError as error:
        raise ValueError("not a JWT") from error
    if header.get("alg") != "RS256":
        raise ValueError("unexpected algorithm")
    key = signing_keys().get(header.get("kid")) or signing_keys(refresh=True).get(header.get("kid"))
    if key is None:
        raise ValueError("unknown signing key")
    try:
        key.verify(b64(signature), f"{head}.{body}".encode(), padding.PKCS1v15(), hashes.SHA256())
    except InvalidSignature as error:
        raise ValueError("bad signature") from error
    now = time.time()
    audience = claims.get("aud")
    audiences = audience if isinstance(audience, list) else [audience]
    if claims.get("iss") != ISSUER:
        raise ValueError("wrong issuer")
    if resource not in audiences:
        raise ValueError("wrong audience")
    if claims.get("exp", 0) < now - 30 or claims.get("nbf", 0) > now + 30:
        raise ValueError("expired or not yet valid")
    if SCOPE not in str(claims.get("scope", "")).split():
        raise PermissionError("insufficient scope")
    if str(claims.get("sub", "")).endswith("@clients"):  # the self-test's machine tokens: no person, no grant
        return claims
    grant = grants().get(claims.get(GRANT_CLAIM) or "")
    if not grant or grant.get("revoked_at") or grant.get("subject") != subject(claims):
        raise ValueError("no active grant")
    return claims


def grants():
    return json.loads(GRANTS.read_text()) if GRANTS.exists() else {}


def save_grants(data):
    GRANTS.parent.mkdir(parents=True, exist_ok=True)
    GRANTS.write_text(json.dumps(data, indent=1))


def subject(claims):
    return hashlib.sha256(str(claims.get("sub")).encode()).hexdigest()[:12]


def challenge(kind, error=None):
    metadata = f"{BASE}/.well-known/oauth-protected-resource/{'mcp' if kind == 'mcp' else 'api/v1'}"
    parts = [f'resource_metadata="{metadata}"', f'scope="{SCOPE}"']
    if error:
        parts.insert(0, f'error="{error}"')
    return "Bearer " + ", ".join(parts)


def authenticate(request, kind):
    """Return (claims, None) or (None, refusal response)."""
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        record({"path": request.url.path, "outcome": "credential_required"})
        return None, JSONResponse(
            {"error": {"code": "credential_required", "message": "Sign in to use Open Alvary."}},
            status_code=401,
            headers={"WWW-Authenticate": challenge(kind)},
        )
    try:
        claims = check_token(header[7:].strip(), RESOURCES[kind])
    except PermissionError:
        record({"path": request.url.path, "outcome": "insufficient_scope"})
        return None, JSONResponse(
            {"error": {"code": "insufficient_scope", "message": "This sign-in lacks corpus:read."}},
            status_code=403,
            headers={"WWW-Authenticate": challenge(kind, "insufficient_scope")},
        )
    except ValueError as reason:
        record({"path": request.url.path, "outcome": "invalid_credentials", "reason": str(reason)})
        return None, JSONResponse(
            {"error": {"code": "invalid_credentials", "message": "Sign in again."}},
            status_code=401,
            headers={"WWW-Authenticate": challenge(kind, "invalid_token")},
        )
    return claims, None


def who(claims):
    return {
        "client": claims.get("client_id") or claims.get("azp"),
        "subject": subject(claims),
        "audience": claims.get("aud"),
        "expires_in": int(claims.get("exp", 0) - time.time()),
        "signed_in_at": claims.get("auth_time"),
        "org": claims.get(ORG_CLAIM),
        "grant": claims.get(GRANT_CLAIM),
        "claim_names": sorted(claims),
    }


@app.get("/.well-known/oauth-protected-resource")
@app.get("/.well-known/oauth-protected-resource/mcp")
def mcp_metadata():
    return {
        "resource": RESOURCES["mcp"],
        "authorization_servers": [ISSUER],
        "scopes_supported": [SCOPE],
        "bearer_methods_supported": ["header"],
        "resource_name": "Open Alvary sign-in check (MCP)",
    }


@app.get("/.well-known/oauth-protected-resource/api/v1")
def api_metadata():
    return {
        "resource": RESOURCES["api"],
        "authorization_servers": [ISSUER],
        "scopes_supported": [SCOPE],
        "bearer_methods_supported": ["header"],
        "resource_name": "Open Alvary sign-in check (API)",
    }


@app.get("/api/v1/whoami")
def whoami(request: Request):
    claims, refusal = authenticate(request, "api")
    if refusal:
        return refusal
    record({"path": "/api/v1/whoami", "outcome": "ok", **who(claims)})
    return who(claims)


@app.get("/mcp")
def mcp_stream():
    return Response(status_code=405, headers={"Allow": "POST"})


@app.post("/mcp")
async def mcp(request: Request):
    claims, refusal = authenticate(request, "mcp")
    if refusal:
        return refusal
    try:
        message = await request.json()
    except ValueError:
        return JSONResponse(
            {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}},
            status_code=400,
        )
    method, ident, params = message.get("method"), message.get("id"), message.get("params") or {}
    record({"path": "/mcp", "method": method, "outcome": "ok", **who(claims)})
    if ident is None:  # a notification, such as notifications/initialized
        return Response(status_code=202)
    if method == "initialize":
        asked = params.get("protocolVersion")
        result = {
            "protocolVersion": asked if asked in VERSIONS else VERSIONS[0],
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "open-alvary-check", "version": "0.1"},
            "instructions": "A made-up test corpus for checking Open Alvary sign-in. Not law.",
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": [TOOL]}
    elif method == "tools/call" and params.get("name") == TOOL["name"]:
        query = str((params.get("arguments") or {}).get("query", "")).lower()
        hits = [
            d
            for d in CORPUS
            if not query or any(w in (d["title"] + d["text"]).lower() for w in query.split())
        ]
        text = "\n\n".join(f"{d['title']}\n{d['text']}" for d in hits) or "No match in the test corpus."
        org = {o["id"]: o["name"] for o in ORGS}.get(claims.get(ORG_CLAIM), "none chosen")
        text += f"\n\n(Connected for organisation: {org})"
        result = {"content": [{"type": "text", "text": text}], "isError": False}
    else:
        return JSONResponse(
            {"jsonrpc": "2.0", "id": ident, "error": {"code": -32601, "message": "Method not found"}}
        )
    return JSONResponse({"jsonrpc": "2.0", "id": ident, "result": result})


def _secret():
    shared = os.environ.get("IDP_CHECK_CONSENT_SECRET")
    return (shared or CONSENT_SECRET.read_text()).strip().encode()


def _b64e(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def sign_hs256(payload):
    head = _b64e(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    body = _b64e(json.dumps(payload).encode())
    signature = hmac.new(_secret(), f"{head}.{body}".encode(), hashlib.sha256).digest()
    return f"{head}.{body}.{_b64e(signature)}"


def verify_hs256(token):
    """Return the payload of a token Auth0's Action signed with the shared secret, or raise ValueError."""
    try:
        head, body, signature = token.split(".")
        header, payload = json.loads(b64(head)), json.loads(b64(body))
    except ValueError as error:
        raise ValueError("malformed session token") from error
    expected = hmac.new(_secret(), f"{head}.{body}".encode(), hashlib.sha256).digest()
    if header.get("alg") != "HS256" or not hmac.compare_digest(expected, b64(signature)):
        raise ValueError("bad session token signature")
    if payload.get("exp", 0) < time.time():
        raise ValueError("session token expired")
    return payload


def label(client_id, name):
    host = urlsplit(client_id).hostname if client_id.startswith("https://") else None
    if host in PUBLISHERS:
        return f"Verified publisher: {PUBLISHERS[host]}", True
    if host:
        return f"From {host}", True
    return f"Unverified client “{name}”", False


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Allow access · Open Alvary (test)</title>
<style>body{{font:16px/1.5 system-ui,sans-serif;max-width:34rem;margin:2rem auto;padding:0 1rem;color:#1b1b1b}}
.warn{{background:#fff4e5;border-left:4px solid #b25e00;padding:.6rem .8rem}}fieldset{{border:1px solid #ccc;border-radius:6px}}
label{{display:block;margin:.3rem 0}}.muted{{color:#5c5c5c}}button{{font:inherit;padding:.5rem 1rem;margin-right:.5rem}}</style>
</head><body><main><p class="muted">Open Alvary (sign-in check, test data only)</p>
<h1>Allow {name} to read Open Alvary?</h1>
<p><strong>{publisher}</strong></p>
<p>It will be able to: read the published Open Alvary corpus within your organisation's access.</p>
<p>After you choose, you return to: <strong>{host}</strong></p>{warning}
<form method="post" action="/consent">
<input type="hidden" name="session_token" value="{session}"><input type="hidden" name="state" value="{state}">
<fieldset><legend>Which organisation is this connection for?</legend>{orgs}</fieldset>
<p><button type="submit" name="decision" value="allow">Allow</button>
<button type="submit" name="decision" value="deny">Cancel</button></p></form>
<p class="muted">Signed in as {email}. You can disconnect it at any time in the console.</p></main></body></html>"""


@app.get("/consent")
def consent_page(session_token: str = "", state: str = ""):
    try:
        session = verify_hs256(session_token)
    except ValueError as reason:
        record({"path": "/consent", "outcome": "refused", "reason": str(reason)})
        return HTMLResponse(
            "<p>This sign-in link is not valid any more. Start again from your tool.</p>", 400
        )
    publisher, known = label(session.get("client_id", ""), session.get("client_name", "this tool"))
    host = urlsplit(session.get("redirect_uri", "")).hostname or "an unknown address"
    loopback = host in ("localhost", "127.0.0.1", "::1")
    warning = (
        '<p class="warn">This tool returns to an address on your own computer. Any program on your computer '
        "could receive it: continue only if you started this connection yourself.</p>"
        if loopback
        else ""
    )
    if not known:
        warning += '<p class="warn">We have not reviewed this tool. Allow it only if you trust it.</p>'
    orgs = "".join(
        f'<label><input type="radio" name="org" value="{o["id"]}" required{" checked" if i == 0 else ""}> '
        f"{html.escape(o['name'])}</label>"
        if o["entitled"]
        else f'<label class="muted"><input type="radio" disabled> {html.escape(o["name"])} — no active access</label>'
        for i, o in enumerate(ORGS)
    )
    record({"path": "/consent", "outcome": "shown", "client": session.get("client_id"), "return_host": host})
    return HTMLResponse(
        PAGE.format(
            name=html.escape(session.get("client_name") or "this tool"),
            publisher=html.escape(publisher),
            host=html.escape(host),
            warning=warning,
            session=html.escape(session_token),
            state=html.escape(state),
            orgs=orgs,
            email=html.escape(session.get("email") or "your account"),
        ),
        headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY"},
    )


@app.post("/consent")
async def consent_answer(request: Request):
    form = {k: v[0] for k, v in parse_qs((await request.body()).decode()).items()}
    try:
        session = verify_hs256(form.get("session_token", ""))
    except ValueError as reason:
        return HTMLResponse(f"<p>{html.escape(str(reason))}. Start again from your tool.</p>", 400)
    allowed = {o["id"] for o in ORGS if o["entitled"]}
    decision = "allow" if form.get("decision") == "allow" and form.get("org") in allowed else "deny"
    now = int(time.time())
    grant_id = None
    if decision == "allow":
        grant_id = "grt_" + secrets.token_hex(8)
        data = grants()
        data[grant_id] = {
            "subject": hashlib.sha256(session["sub"].encode()).hexdigest()[:12],
            "client": session.get("client_id"),
            "org": form.get("org"),
            "created_at": now,
            "revoked_at": None,
        }
        save_grants(data)
    answer = sign_hs256(
        {
            "sub": session["sub"],
            "iss": BASE,
            "iat": now,
            "exp": now + 120,
            "state": form.get("state", ""),
            "decision": decision,
            "org": form.get("org") if decision == "allow" else None,
            "grant": grant_id,
        }
    )
    record(
        {"path": "/consent", "outcome": decision, "client": session.get("client_id"), "org": form.get("org")}
    )
    query = urlencode({"state": form.get("state", ""), "consent_token": answer})
    return RedirectResponse(f"{ISSUER}continue?{query}", status_code=303)


if __name__ == "__main__":
    import uvicorn

    # Locally only on loopback; on Cloud Run (PORT set) on all interfaces behind Google's front end.
    cloud = "PORT" in os.environ
    uvicorn.run(
        app,
        host="0.0.0.0" if cloud else "127.0.0.1",  # noqa: S104
        port=int(os.environ.get("PORT") or os.environ.get("IDP_CHECK_PORT", "8766")),
        proxy_headers=cloud,
    )
