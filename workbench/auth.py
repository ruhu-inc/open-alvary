"""Operator sign-in for the workbench.

Production sign-in is OpenID Connect (authorization code with PKCE) against the configured
issuer — Google by default — and admits only active operators on the allow-list. The ID token is
taken straight from the issuer's token endpoint over TLS by this server (a confidential client),
so its claims are checked (issuer, audience, expiry, nonce, verified email) without a local
signature check, as OpenID Connect Core 3.1.3.7 permits for that channel.

A development sign-in exists for a local trial only: it must be switched on explicitly, and it
answers only when the app is reached on a loopback address from a loopback client.

Sessions are random tokens kept hashed server-side; the cookie is HttpOnly, SameSite=Strict and
Secure outside loopback. Every state-changing request must carry the session's CSRF token.
"""

import base64
import hashlib
import json
import os
import secrets
import time
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx
from fastapi import HTTPException, Request

from ingestion import settings
from workbench.store import Store

COOKIE = "oa_review_session"
LOOPBACK = {"127.0.0.1", "::1", "localhost"}


@dataclass(frozen=True)
class OIDCConfig:
    issuer: str
    client_id: str
    client_secret: str
    base_url: str

    @classmethod
    def from_environment(cls) -> "OIDCConfig | None":
        settings.load()
        client = os.environ.get("ALVARY_REVIEW_OIDC_CLIENT_ID")
        if not client:
            return None
        return cls(
            issuer=os.environ.get("ALVARY_REVIEW_OIDC_ISSUER", "https://accounts.google.com").rstrip("/"),
            client_id=client,
            client_secret=os.environ["ALVARY_REVIEW_OIDC_CLIENT_SECRET"],
            base_url=os.environ["ALVARY_REVIEW_BASE_URL"].rstrip("/"),
        )

    @property
    def redirect_uri(self) -> str:
        return f"{self.base_url}/auth/callback"


def dev_login_enabled() -> bool:
    settings.load()
    return os.environ.get("ALVARY_REVIEW_DEV_LOGIN") == "1"


def is_loopback(request: Request) -> bool:
    host = (request.url.hostname or "").lower()
    client = request.client.host if request.client else ""
    return host in LOOPBACK and client in LOOPBACK | {"testclient"}


class OIDC:
    def __init__(self, config: OIDCConfig, client: httpx.Client | None = None):
        self.config = config
        self.http = client or httpx.Client(timeout=15)
        self._discovery: dict | None = None

    def discovery(self) -> dict:
        if self._discovery is None:
            response = self.http.get(f"{self.config.issuer}/.well-known/openid-configuration")
            response.raise_for_status()
            self._discovery = response.json()
        return self._discovery

    def authorize_url(self, store: Store) -> str:
        nonce, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state = store.begin_flow(nonce, verifier)
        query = {
            "response_type": "code",
            "client_id": self.config.client_id,
            "redirect_uri": self.config.redirect_uri,
            "scope": "openid email profile",
            "state": state,
            "nonce": nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "prompt": "select_account",
        }
        return f"{self.discovery()['authorization_endpoint']}?{urlencode(query)}"

    def finish(self, store: Store, state: str, code: str) -> str:
        """Exchange the code; return the verified email of an allowed operator."""
        flow = store.take_flow(state)
        if not flow:
            raise HTTPException(400, "Sign-in expired or was already used; start again")
        response = self.http.post(
            self.discovery()["token_endpoint"],
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.config.redirect_uri,
                "client_id": self.config.client_id,
                "client_secret": self.config.client_secret,
                "code_verifier": flow["verifier"],
            },
        )
        if response.status_code != 200:
            raise HTTPException(401, "The identity provider refused the sign-in")
        claims = self._claims(response.json().get("id_token", ""))
        if claims.get("iss") not in {self.config.issuer, self.config.issuer.removeprefix("https://")}:
            raise HTTPException(401, "Wrong issuer")
        audience = claims.get("aud")
        if self.config.client_id not in (audience if isinstance(audience, list) else [audience]):
            raise HTTPException(401, "Wrong audience")
        if float(claims.get("exp", 0)) < time.time() or claims.get("nonce") != flow["nonce"]:
            raise HTTPException(401, "Expired or replayed sign-in")
        if claims.get("email_verified") not in (True, "true") or not claims.get("email"):
            raise HTTPException(401, "The account's email is not verified")
        email = claims["email"].lower()
        if not store.operator(email):
            store.audit(email, "sign_in_refused")
            raise HTTPException(403, "This account is not an operator")
        return email

    @staticmethod
    def _claims(token: str) -> dict:
        try:
            payload = token.split(".")[1]
            return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        except (IndexError, ValueError) as error:
            raise HTTPException(401, "Malformed identity token") from error


def current(request: Request, store: Store, *, write: bool = False, role: str | None = None) -> dict:
    """The signed-in operator; for writes the CSRF header must match; optionally a role."""
    token = request.cookies.get(COOKIE)
    found = store.resume(token) if token else None
    if not found:
        raise HTTPException(401, "Sign in first")
    if write and not secrets.compare_digest(request.headers.get("X-CSRF-Token", ""), found["csrf"]):
        raise HTTPException(403, "Missing or wrong CSRF token")
    if role and role not in found["operator"]["roles"]:
        raise HTTPException(403, f"This needs the {role} role")
    return found


def cookie_options(request: Request) -> dict:
    return {
        "httponly": True,
        "samesite": "strict",
        "secure": not is_loopback(request),
        "path": "/",
        "max_age": 12 * 3600,
    }
