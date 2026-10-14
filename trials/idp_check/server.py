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
import json
import os
import time

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from trials.idp_check.auth0 import ROOT, environment

ENV = environment()
BASE = os.environ.get("IDP_CHECK_BASE_URL", "http://localhost:8766").rstrip("/")
ISSUER = f"https://{ENV['AUTH0_DOMAIN'].removeprefix('https://').rstrip('/')}/"
SCOPE = "corpus:read"
RESOURCES = {"mcp": f"{BASE}/mcp", "api": f"{BASE}/api/v1"}
VERSIONS = ["2026-07-28", "2025-11-25", "2025-06-18", "2025-03-26"]
EVENTS = ROOT / "private-staging" / "idp-check" / "events.jsonl"

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
    EVENTS.parent.mkdir(parents=True, exist_ok=True)
    with EVENTS.open("a", encoding="utf-8") as out:
        out.write(json.dumps({"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **event}) + "\n")


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
    return claims


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
        "subject": hashlib.sha256(str(claims.get("sub")).encode()).hexdigest()[:12],
        "audience": claims.get("aud"),
        "expires_in": int(claims.get("exp", 0) - time.time()),
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
        result = {"content": [{"type": "text", "text": text}], "isError": False}
    else:
        return JSONResponse(
            {"jsonrpc": "2.0", "id": ident, "error": {"code": -32601, "message": "Method not found"}}
        )
    return JSONResponse({"jsonrpc": "2.0", "id": ident, "result": result})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("IDP_CHECK_PORT", "8766")))
