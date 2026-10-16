"""The developer console's back end (MAC-08, MAC-10), on the website's origin under /console.

    /console/auth/start      begin a sign-in, fresh sign-in, second factor or link at the provider
    /console/auth/callback   finish it in the browser that started it, within 10 minutes
    /console/auth/sign-out   end this session
    /console/api/v1/...      the console API (machine-access-contracts, "Console endpoints")
    /console/...             the console's page, which reads the address itself

Sign-in is OpenID Connect (authorization code with PKCE, confidential client) against the identity
provider (Auth0, D1). The ID token comes straight from the provider's token endpoint over TLS, so
its claims are checked without a local signature check (OpenID Connect Core 3.1.3.7), as the
workbench does. People are found by the provider's issuer and subject, never by email.

Sessions are random tokens kept hashed in the accounts schema; the cookie is HttpOnly, Secure
outside loopback, SameSite=Lax (the provider's redirect back is a cross-site navigation) and
limited to /console. Every change needs the session's CSRF token and, when the browser sends one,
our own Origin. Sensitive actions need a sign-in within the last 10 minutes, and owners and admins
of an organisation with access also a second factor within the same window (MAC-08).

A development sign-in exists for a local trial only: it must be switched on explicitly
(ALVARY_CONSOLE_DEV_LOGIN=1) and answers only on a loopback address from a loopback client.
"""

import base64
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import time
import uuid
from dataclasses import dataclass
from datetime import timedelta
from html import escape
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import httpx
from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response

from accounts import mail, policies, roles
from accounts.store import AccountError, Accounts, Context
from api.admission import client_address

SESSION_COOKIE = "oa_console"
FLOW_COOKIE = "oa_console_flow"
FRESH = timedelta(minutes=10)
LOOPBACK = {"127.0.0.1", "::1", "localhost"}
RETURN = re.compile(r"^/console(/[A-Za-z0-9/_?=&%.-]*)?$")
EMAIL = re.compile(r"[^\s@<>\"']{1,64}@[A-Za-z0-9.-]{1,253}\.[A-Za-z]{2,63}")
# Provider connections offered at launch (D24); Microsoft waits for its MAC-16 check.
CONNECTIONS = {"google": "google-oauth2", "email": "email"}
METHODS = {"google-oauth2": "google", "email": "email"}
MFA_POLICY = "http://schemas.openid.net/pape/policies/2007/06/multi-factor"
DEV_ISSUER = "https://dev-login.invalid/"


@dataclass(frozen=True)
class ConsoleConfig:
    issuer: str  # with its trailing slash, as the provider writes it in tokens
    client_id: str
    client_secret: str
    base_url: str

    @classmethod
    def from_environment(cls) -> "ConsoleConfig | None":
        client = os.environ.get("ALVARY_CONSOLE_CLIENT_ID")
        if not client:
            return None
        return cls(
            issuer=os.environ["ALVARY_CONSOLE_ISSUER"].rstrip("/") + "/",
            client_id=client,
            client_secret=os.environ["ALVARY_CONSOLE_CLIENT_SECRET"],
            base_url=os.environ["ALVARY_CONSOLE_BASE_URL"].rstrip("/"),
        )

    @property
    def redirect_uri(self) -> str:
        return f"{self.base_url}/console/auth/callback"


class Provider:
    def __init__(self, config: ConsoleConfig, client: httpx.Client | None = None):
        self.config = config
        self.http = client or httpx.Client(timeout=15)
        self._discovery: dict | None = None

    def discovery(self) -> dict:
        if self._discovery is None:
            response = self.http.get(f"{self.config.issuer}.well-known/openid-configuration")
            response.raise_for_status()
            self._discovery = response.json()
        return self._discovery

    def authorize_url(
        self, flow: dict, purpose: str, method: str | None, login_hint: str | None = None
    ) -> str:
        challenge = base64.urlsafe_b64encode(hashlib.sha256(flow["verifier"].encode()).digest()).rstrip(b"=")
        query = {
            "response_type": "code",
            "client_id": self.config.client_id,
            "redirect_uri": self.config.redirect_uri,
            "scope": "openid email profile",
            "state": flow["state"],
            "nonce": flow["nonce"],
            "code_challenge": challenge.decode(),
            "code_challenge_method": "S256",
        }
        if method:
            query["connection"] = CONNECTIONS[method]
        if login_hint:
            query["login_hint"] = login_hint  # "Continue as …": the provider pre-fills the account
        if purpose != "sign_in":
            query["prompt"] = "login"  # a fresh sign-in, never the provider's remembered session
        if purpose == "second_factor":
            query["acr_values"] = MFA_POLICY
        return f"{self.discovery()['authorization_endpoint']}?{urlencode(query)}"

    def finish(self, code: str, flow: dict) -> dict:
        """Exchange the code; the checked ID token claims."""
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
            raise AccountError("sign_in_refused", 401, "The sign-in service refused the sign-in. Try again.")
        claims = _claims(response.json().get("id_token", ""))
        audience = claims.get("aud")
        if (
            claims.get("iss") != self.config.issuer
            or self.config.client_id not in (audience if isinstance(audience, list) else [audience])
            or float(claims.get("exp", 0)) < time.time()
            or claims.get("nonce") != flow["nonce"]
            or not claims.get("sub")
        ):
            raise AccountError("sign_in_refused", 401, "The sign-in could not be verified. Try again.")
        return claims


def _claims(token: str) -> dict:
    try:
        payload = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError) as error:
        raise AccountError("sign_in_refused", 401, "The sign-in could not be verified. Try again.") from error


# Tools reviewed by the operator and pinned by their client metadata document address (MAC-07):
# shown as verified publishers. Any other https client ID is "from <host>"; the rest unverified.
VERIFIED = {
    "https://claude.ai/oauth/mcp-oauth-client-metadata": ("Claude", "Anthropic"),
    "https://claude.ai/oauth/claude-code-client-metadata": ("Claude Code", "Anthropic"),
    "https://chatgpt.com/oauth/client.json": ("ChatGPT", "OpenAI"),
    "https://chatgpt.com/oauth/codex/client.json": ("Codex", "OpenAI"),
}


def _b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def sign_hs256(payload: dict, secret: bytes) -> str:
    head = _b64e(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    body = _b64e(json.dumps(payload).encode())
    return f"{head}.{body}.{_b64e(hmac.new(secret, f'{head}.{body}'.encode(), hashlib.sha256).digest())}"


def verify_hs256(token: str, secret: bytes) -> dict | None:
    """The payload of a token signed with the shared secret and not expired, or None."""
    try:
        head, body, signature = token.split(".")
        header = json.loads(base64.urlsafe_b64decode(head + "=" * (-len(head) % 4)))
        payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        given = base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))
    except ValueError:
        return None
    expected = hmac.new(secret, f"{head}.{body}".encode(), hashlib.sha256).digest()
    if header.get("alg") != "HS256" or not hmac.compare_digest(expected, given):
        return None
    if not isinstance(payload, dict) or float(payload.get("exp", 0)) < time.time():
        return None
    return payload


def is_loopback(request: Request) -> bool:
    host = (request.url.hostname or "").lower()
    client = request.client.host if request.client else ""
    return host in LOOPBACK and client in LOOPBACK | {"testclient"}


def network_prefix(address: str | None) -> str | None:
    """IPv4 /24 or IPv6 /48: enough to recognise a network in the audit log, never a person's address."""
    try:
        parsed = ipaddress.ip_address(address or "")
    except ValueError:
        return None
    if isinstance(parsed, ipaddress.IPv6Address):
        if parsed.ipv4_mapped is None:
            return str(ipaddress.ip_network(f"{parsed}/48", strict=False))
        parsed = parsed.ipv4_mapped
    return str(ipaddress.ip_network(f"{parsed}/24", strict=False))


def client_family(user_agent: str) -> str:
    ua = user_agent or ""
    for marker, family in (
        ("Edg/", "Edge"),
        ("Firefox/", "Firefox"),
        ("Chrome/", "Chrome"),
        ("Safari/", "Safari"),
        ("curl/", "curl"),
    ):
        if marker in ua:
            return family
    return "Other"


def create_console_app(
    accounts: Accounts,
    provider: Provider | None,
    *,
    frontend: Path | None = None,
    trusted_proxies: int = 0,
    dev_login: bool = False,
    consent_secret: bytes | None = None,
    mailer: mail.Mailer | None = None,
) -> FastAPI:
    """``consent_secret`` is shared with the provider's post-login step, which signs the hand-off
    to the approval page and verifies our answer (MAC-07)."""
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    base_url = provider.config.base_url if provider else None
    mailer = mailer or mail.Mailer.from_environment()
    site = base_url or "https://open.alvary.ai"
    operator_email = os.getenv("ALVARY_OPERATOR_EMAIL", "hello@ruhu.ai")

    def later(response: Response, *messages: mail.Message) -> Response:
        """Send after the response: the change is committed, and nothing waits for the mail."""
        tasks = BackgroundTasks()
        for message in messages:
            tasks.add_task(mailer.send, message)
        response.background = tasks
        return response

    def context(request: Request) -> Context:
        peer = request.client.host if request.client else None
        address = client_address(list(request.scope.get("headers", [])), peer, trusted_proxies)
        return Context(
            network_prefix(address),
            client_family(request.headers.get("user-agent", "")),
            request.state.request_id,
        )

    def secure(request: Request) -> bool:
        return not is_loopback(request)

    def set_session(response: Response, request: Request, token: str) -> None:
        response.set_cookie(
            SESSION_COOKIE,
            token,
            max_age=30 * 86400,
            path="/console",
            httponly=True,
            secure=secure(request),
            samesite="lax",
        )

    def error(request: Request, status: int, code: str, message: str, **extra) -> JSONResponse:
        body = {"error": {"code": code, "message": message, "request_id": request.state.request_id, **extra}}
        return JSONResponse(body, status_code=status, headers={"Cache-Control": "no-store"})

    @app.middleware("http")
    async def request_id(request: Request, call_next):
        request.state.request_id = uuid.uuid4().hex[:16]
        response = await call_next(request)
        path = request.url.path.removeprefix(request.scope.get("root_path", ""))
        if path.startswith(("/api/", "/auth/")):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(AccountError)
    async def account_error(request: Request, exc: AccountError):
        return error(request, exc.status, exc.code, exc.message)

    # ---------- sign-in ----------
    @app.get("/auth/start")
    def start(
        request: Request,
        purpose: str = "sign_in",
        method: str | None = None,
        return_to: str = "/console",
        login_hint: str | None = None,
    ):
        if purpose not in ("sign_in", "reauthenticate", "second_factor", "link") or (
            method is not None and method not in CONNECTIONS
        ):
            return RedirectResponse("/console/sign-in?error=invalid", status_code=303)
        if not RETURN.match(return_to) or len(return_to) > 500:
            return_to = "/console"
        session = request.cookies.get(SESSION_COOKIE)
        if purpose != "sign_in" and not accounts.resume(session or ""):
            return RedirectResponse("/console/sign-in?error=signed_out", status_code=303)
        if provider is None:
            if dev_login and is_loopback(request):
                query = urlencode({"purpose": purpose, "return_to": return_to})
                return RedirectResponse(f"/console/auth/dev?{query}", status_code=303)
            return RedirectResponse("/console/sign-in?error=unavailable", status_code=303)
        flow = accounts.begin_flow(purpose, return_to, session if purpose != "sign_in" else None)
        response = RedirectResponse(
            provider.authorize_url(
                flow, purpose, method, login_hint if login_hint and EMAIL.fullmatch(login_hint) else None
            ),
            status_code=303,
        )
        response.set_cookie(
            FLOW_COOKIE,
            flow["browser"],
            max_age=600,
            path="/console/auth",
            httponly=True,
            secure=secure(request),
            samesite="lax",
        )
        return response

    @app.get("/auth/callback")
    def callback(request: Request, state: str = "", code: str = ""):
        if provider is None:
            return RedirectResponse("/console/sign-in?error=unavailable", status_code=303)
        flow = accounts.take_flow(state, request.cookies.get(FLOW_COOKIE, ""))
        if flow is None:
            return _back(request, "expired")
        if request.query_params.get("error") or not code:
            return _back(request, "cancelled")
        try:
            claims = provider.finish(code, flow)
            connection = claims["sub"].split("|", 1)[0]
            method = METHODS.get(connection)
            if (
                method is None
                or claims.get("email_verified") not in (True, "true")
                or not claims.get("email")
            ):
                raise AccountError("method_not_supported", 403, "Sign in with Google or an email code.")
            mfa = "mfa" in (claims.get("amr") or [])
            return _complete(
                request, flow, provider.config.issuer, claims["sub"], method, claims["email"], mfa
            )
        except AccountError as refused:
            return _back(request, refused.code)

    def _complete(request, flow, issuer, subject, method, email, mfa) -> Response:
        ctx = context(request)
        notices: list[mail.Message] = []
        purpose = flow["purpose"]
        if purpose == "sign_in":
            signed = accounts.sign_in(
                issuer=issuer, subject=subject, method=method, email=email, label=email, context=ctx
            )
            token, _ = accounts.create_session(
                signed["id"],
                second_factor=mfa,
                client_family=ctx.client_family,
                network_prefix=ctx.network_prefix,
            )
            person = accounts.person(signed["id"])
            needs_welcome = person["policies_pending"] or not accounts.organisations(signed["id"])
            response = RedirectResponse(
                "/console/welcome" if needs_welcome else flow["return_to"], status_code=303
            )
            set_session(response, request, token)
        else:
            token = request.cookies.get(SESSION_COOKIE, "")
            session = accounts.resume(token)
            if (
                session is None
                or flow["session_hash"] is None
                or bytes(flow["session_hash"]) != hashlib.sha256(token.encode()).digest()
            ):
                raise AccountError("signed_out", 401, "Your session ended. Sign in again.")
            if purpose == "link":
                accounts.link_identity(
                    session["person_id"],
                    issuer=issuer,
                    subject=subject,
                    method=method,
                    label=email,
                    context=ctx,
                )
                notices.append(
                    mail.sign_in_changed(accounts.person(session["person_id"])["email"], True, method, site)
                )
            elif accounts.person_for_identity(issuer, subject) != session["person_id"]:
                raise AccountError(
                    "wrong_account", 403, "That is a different account. Use the one you signed in with."
                )
            if purpose == "second_factor" and not mfa:
                raise AccountError("second_factor_required", 403, "The second factor was not completed.")
            accounts.refresh_session(token, second_factor=mfa)
            response = RedirectResponse(flow["return_to"], status_code=303)
        response.delete_cookie(FLOW_COOKIE, path="/console/auth")
        return later(response, *notices)

    def _back(request: Request, code: str) -> Response:
        response = RedirectResponse(f"/console/sign-in?error={code}", status_code=303)
        response.delete_cookie(FLOW_COOKIE, path="/console/auth")
        return response

    @app.get("/auth/dev")
    def dev(
        request: Request, purpose: str = "sign_in", return_to: str = "/console", email: str = "", mfa: int = 0
    ):
        """Local trial only: a sign-in without the provider, refused anywhere but loopback."""
        if not (dev_login and is_loopback(request)):
            return Response(status_code=404)
        if not email:
            safe_return = escape(return_to if RETURN.match(return_to) else "/console")
            return Response(
                "<!doctype html><title>Development sign-in</title><form>"
                f'<input type="hidden" name="purpose" value="{escape(purpose)}">'
                f'<input type="hidden" name="return_to" value="{safe_return}">'
                '<p><label>Email <input name="email" type="email" required></label></p>'
                '<p><label><input name="mfa" type="checkbox" value="1"> Second factor</label></p>'
                "<button>Sign in</button></form>",
                media_type="text/html",
                headers={"Content-Security-Policy": "default-src 'none'; form-action 'self'"},
            )
        flow = {
            "purpose": purpose if purpose in ("sign_in", "reauthenticate", "second_factor") else "sign_in",
            "return_to": return_to if RETURN.match(return_to) else "/console",
        }
        if flow["purpose"] != "sign_in":
            token = request.cookies.get(SESSION_COOKIE, "")
            flow["session_hash"] = hashlib.sha256(token.encode()).digest()
        try:
            return _complete(request, flow, DEV_ISSUER, "dev|" + email.lower(), "email", email, bool(mfa))
        except AccountError as refused:
            return _back(request, refused.code)

    @app.post("/auth/sign-out")
    def sign_out(request: Request):
        guard(request, write=True)
        accounts.end_session(request.cookies.get(SESSION_COOKIE, ""))
        response = Response(status_code=204)
        response.delete_cookie(SESSION_COOKIE, path="/console")
        return response

    # ---------- session checks ----------
    def guard(request: Request, *, write: bool = False) -> dict:
        session = accounts.resume(request.cookies.get(SESSION_COOKIE, ""))
        if session is None:
            raise AccountError("credential_required", 401, "Sign in to continue.")
        if write:
            origin = request.headers.get("origin")
            if origin and base_url and origin != base_url and not is_loopback(request):
                raise AccountError("forbidden_origin", 403, "This request came from another site.")
            sent = request.headers.get("x-csrf-token", "")
            if not secrets.compare_digest(sent.encode(), session["csrf"].encode()):
                raise AccountError("csrf_failed", 403, "Reload the page and try again.")
        return session

    def fresh(session: dict, *, second_factor: bool) -> None:
        """MAC-08: a sign-in within 10 minutes, and a second factor too where the role requires it."""
        if second_factor and (
            session["second_factor_at"] is None or session["now"] - session["second_factor_at"] > FRESH
        ):
            raise AccountError("second_factor_required", 403, "Confirm with your second factor to continue.")
        if session["now"] - session["authenticated_at"] > FRESH:
            raise AccountError("reauthentication_required", 401, "Sign in again to confirm it's you.")

    def needs_second_factor(person_id: str, organisation_id: str) -> bool:
        """Owners and admins of an organisation with approved or paid access (D22 revised): not the
        free starter tier, as at OpenAI and Anthropic."""
        organisation = accounts.organisation(person_id, organisation_id)
        access = organisation["access"]
        return (
            organisation["role"] in roles.MANAGERS
            and access["state"] == "active"
            and not access.get("starter")
        )

    async def body(request: Request) -> dict:
        try:
            data = await request.json()
        except ValueError:
            raise AccountError("invalid_input", 400, "Send a JSON object.") from None
        if not isinstance(data, dict):
            raise AccountError("invalid_input", 400, "Send a JSON object.")
        return data

    # ---------- you ----------
    @app.get("/api/v1/me")
    def me(request: Request):
        session = guard(request)
        person_id = session["person_id"]
        person = accounts.person(person_id)
        return {
            "person": {k: person[k] for k in ("id", "email", "display_name", "second_factor", "created_at")},
            "policies": {
                "pending": person["policies_pending"],
                "current": policies.CURRENT,
                "paths": policies.PATHS,
            },
            "organisations": accounts.organisations(person_id),
            "invitations": accounts.invitations_for(person_id),
            "csrf": session["csrf"],
            "fresh_until": session["authenticated_at"] + FRESH,
            "second_factor_until": session["second_factor_at"] and session["second_factor_at"] + FRESH,
            "dev_login": provider is None and dev_login,
        }

    @app.patch("/api/v1/me")
    async def update_me(request: Request):
        session = guard(request, write=True)
        accounts.update_profile(
            session["person_id"], (await body(request)).get("display_name", ""), context(request)
        )
        return Response(status_code=204)

    @app.post("/api/v1/me/policies")
    async def accept(request: Request):
        session = guard(request, write=True)
        accounts.accept_policies(
            session["person_id"], (await body(request)).get("versions") or {}, context(request)
        )
        return Response(status_code=204)

    @app.post("/api/v1/me/welcome")
    async def welcome(request: Request):
        session = guard(request, write=True)
        data = await body(request)
        organisation = accounts.welcome(
            session["person_id"],
            data.get("display_name", ""),
            data.get("organisation_name", ""),
            context(request),
        )
        return {"organisation": organisation}

    @app.get("/api/v1/me/identities")
    def identities(request: Request):
        return {"identities": accounts.identities(guard(request)["person_id"])}

    @app.delete("/api/v1/me/identities/{identity_id}")
    def unlink(request: Request, identity_id: str):
        session = guard(request, write=True)
        fresh(session, second_factor=False)
        method = next(
            (i["method"] for i in accounts.identities(session["person_id"]) if i["id"] == identity_id), ""
        )
        accounts.unlink_identity(session["person_id"], identity_id, context(request))
        return later(
            Response(status_code=204),
            mail.sign_in_changed(accounts.person(session["person_id"])["email"], False, method, site),
        )

    @app.get("/api/v1/me/sessions")
    def sessions(request: Request):
        return {"sessions": accounts.sessions(guard(request)["person_id"])}

    @app.post("/api/v1/me/sessions/revoke-all")
    def revoke_all(request: Request):
        session = guard(request, write=True)
        accounts.end_all_sessions(session["person_id"], context(request))
        response = Response(status_code=204)
        response.delete_cookie(SESSION_COOKIE, path="/console")
        return response

    # ---------- organisations ----------
    @app.post("/api/v1/organisations")
    async def create_organisation(request: Request):
        session = guard(request, write=True)
        data = await body(request)
        organisation = accounts.create_organisation(
            session["person_id"],
            data.get("name", ""),
            data.get("kind", "other"),
            data.get("country"),
            context(request),
        )
        return JSONResponse({"id": organisation}, status_code=201)

    @app.get("/api/v1/organisations/{organisation_id}")
    def organisation(request: Request, organisation_id: str):
        return accounts.organisation(guard(request)["person_id"], organisation_id)

    @app.patch("/api/v1/organisations/{organisation_id}")
    async def update_organisation(request: Request, organisation_id: str):
        session = guard(request, write=True)
        data = await body(request)
        revision = data.pop("revision", None)
        if not isinstance(revision, int):
            raise AccountError("precondition_required", 428, "Send the revision you are changing.")
        accounts.update_organisation(session["person_id"], organisation_id, revision, data, context(request))
        return Response(status_code=204)

    @app.delete("/api/v1/organisations/{organisation_id}")
    def delete_organisation(request: Request, organisation_id: str):
        session = guard(request, write=True)
        fresh(session, second_factor=needs_second_factor(session["person_id"], organisation_id))
        accounts.delete_organisation(session["person_id"], organisation_id, context(request))
        return Response(status_code=204)

    # ---------- access ----------
    @app.get("/api/v1/organisations/{organisation_id}/access")
    def access(request: Request, organisation_id: str):
        return accounts.access_details(guard(request)["person_id"], organisation_id)

    @app.post("/api/v1/organisations/{organisation_id}/access-requests")
    async def request_access(request: Request, organisation_id: str):
        session = guard(request, write=True)
        data = await body(request)
        request_id = accounts.request_access(session["person_id"], organisation_id, data, context(request))
        notice = accounts.notice_recipients(organisation_id, request_id)
        plan = next((t["name"] for t in accounts.tiers() if t["id"] == data.get("tier")), None)
        return later(
            JSONResponse({"id": request_id}, status_code=201),
            *(mail.request_received(to, notice["organisation"], site, plan) for to in notice["to"]),
            mail.request_for_operator(
                operator_email, notice["organisation"], bool(data.get("reduced")), site, plan
            ),
        )

    @app.post("/api/v1/organisations/{organisation_id}/access-requests/{request_id}/withdraw")
    def withdraw(request: Request, organisation_id: str, request_id: str):
        session = guard(request, write=True)
        accounts.withdraw_request(session["person_id"], organisation_id, request_id, context(request))
        return Response(status_code=204)

    @app.post("/api/v1/organisations/{organisation_id}/access/end")
    def end_access(request: Request, organisation_id: str):
        session = guard(request, write=True)
        fresh(session, second_factor=needs_second_factor(session["person_id"], organisation_id))
        accounts.end_access(session["person_id"], organisation_id, context(request))
        return Response(status_code=204)

    @app.get("/api/v1/tiers")
    def tiers(request: Request):
        guard(request)
        return {"tiers": accounts.tiers(published_only=False)}

    # ---------- keys ----------
    @app.get("/api/v1/organisations/{organisation_id}/keys")
    def list_keys(request: Request, organisation_id: str):
        return {"keys": accounts.list_keys(guard(request)["person_id"], organisation_id)}

    @app.post("/api/v1/organisations/{organisation_id}/keys")
    async def create_key(request: Request, organisation_id: str):
        session = guard(request, write=True)
        data = await body(request)
        fresh(session, second_factor=needs_second_factor(session["person_id"], organisation_id))
        created = accounts.create_key(
            session["person_id"],
            organisation_id,
            kind=data.get("kind", "personal"),
            name=data.get("name", ""),
            days=data.get("days", 90),
            context=context(request),
        )
        return JSONResponse(json.loads(json.dumps(created, default=str)), status_code=201)

    @app.post("/api/v1/organisations/{organisation_id}/keys/{key_id}/rotate")
    async def rotate_key(request: Request, organisation_id: str, key_id: str):
        session = guard(request, write=True)
        data = await body(request)
        fresh(session, second_factor=needs_second_factor(session["person_id"], organisation_id))
        created = accounts.rotate_key(
            session["person_id"],
            organisation_id,
            key_id,
            overlap_hours=data.get("overlap_hours", 24),
            context=context(request),
        )
        return JSONResponse(json.loads(json.dumps(created, default=str)), status_code=201)

    @app.post("/api/v1/organisations/{organisation_id}/keys/{key_id}/revoke")
    def revoke_key(request: Request, organisation_id: str, key_id: str):
        session = guard(request, write=True)
        accounts.revoke_key(session["person_id"], organisation_id, key_id, context(request))
        return Response(status_code=204)

    # ---------- connected apps ----------
    @app.get("/api/v1/organisations/{organisation_id}/clients")
    def clients(request: Request, organisation_id: str):
        return {"clients": accounts.list_grants(guard(request)["person_id"], organisation_id)}

    @app.post("/api/v1/organisations/{organisation_id}/clients/{grant_id}/revoke")
    def revoke_client(request: Request, organisation_id: str, grant_id: str):
        session = guard(request, write=True)
        accounts.revoke_grant(session["person_id"], organisation_id, grant_id, context(request))
        return Response(status_code=204)

    # ---------- members ----------
    @app.get("/api/v1/organisations/{organisation_id}/members")
    def members(request: Request, organisation_id: str):
        return accounts.members(guard(request)["person_id"], organisation_id)

    @app.post("/api/v1/organisations/{organisation_id}/invitations")
    async def invite(request: Request, organisation_id: str):
        session = guard(request, write=True)
        data = await body(request)
        invitation = accounts.invite(
            session["person_id"],
            organisation_id,
            data.get("email", ""),
            data.get("role", ""),
            context(request),
        )
        sent = accounts.invitation_notice(invitation)
        return later(
            JSONResponse({"id": invitation}, status_code=201),
            mail.invitation(sent["email"], sent["organisation"], sent["inviter"], sent["role"], site),
        )

    @app.delete("/api/v1/organisations/{organisation_id}/invitations/{invitation_id}")
    def revoke_invitation(request: Request, organisation_id: str, invitation_id: str):
        session = guard(request, write=True)
        accounts.revoke_invitation(session["person_id"], organisation_id, invitation_id, context(request))
        return Response(status_code=204)

    @app.post("/api/v1/invitations/{invitation_id}/accept")
    def accept_invitation(request: Request, invitation_id: str):
        session = guard(request, write=True)
        return {
            "organisation": accounts.accept_invitation(session["person_id"], invitation_id, context(request))
        }

    @app.patch("/api/v1/organisations/{organisation_id}/members/{membership_id}")
    async def change_role(request: Request, organisation_id: str, membership_id: str):
        session = guard(request, write=True)
        data = await body(request)
        fresh(session, second_factor=needs_second_factor(session["person_id"], organisation_id))
        accounts.change_role(
            session["person_id"], organisation_id, membership_id, data.get("role", ""), context(request)
        )
        return Response(status_code=204)

    @app.delete("/api/v1/organisations/{organisation_id}/members/{membership_id}")
    def remove_member(request: Request, organisation_id: str, membership_id: str):
        session = guard(request, write=True)
        fresh(session, second_factor=needs_second_factor(session["person_id"], organisation_id))
        accounts.remove_member(session["person_id"], organisation_id, membership_id, context(request))
        return Response(status_code=204)

    @app.post("/api/v1/organisations/{organisation_id}/transfer-ownership")
    async def transfer(request: Request, organisation_id: str):
        session = guard(request, write=True)
        data = await body(request)
        fresh(session, second_factor=needs_second_factor(session["person_id"], organisation_id))
        accounts.transfer_ownership(
            session["person_id"],
            organisation_id,
            data.get("member_id", ""),
            bool(data.get("keep_owner")),
            context(request),
        )
        return Response(status_code=204)

    # ---------- usage and audit ----------
    @app.get("/api/v1/organisations/{organisation_id}/usage")
    def usage(request: Request, organisation_id: str, days: int = 30):
        return {"days": accounts.usage(guard(request)["person_id"], organisation_id, days)}

    @app.get("/api/v1/organisations/{organisation_id}/audit")
    def audit(request: Request, organisation_id: str, before: int | None = None):
        return {"events": accounts.audit_log(guard(request)["person_id"], organisation_id, before=before)}

    @app.api_route("/api/{rest:path}", methods=["GET", "POST", "PATCH", "DELETE", "PUT"])
    def unknown(request: Request, rest: str):
        return error(request, 404, "not_found", "No such console route.")

    # ---------- approving an app (MAC-07) ----------
    def hand_off(token: str) -> dict:
        """The provider's signed hand-off: who signed in, which app, for which resource."""
        if consent_secret is None or provider is None:
            raise AccountError("unavailable", 503, "Connecting apps isn't available right now.")
        payload = verify_hs256(token, consent_secret)
        if payload is None or not payload.get("sub"):
            raise AccountError(
                "consent_expired", 400, "This approval link has expired. Start again from your app."
            )
        return payload

    def app_label(payload: dict) -> dict:
        client_id = str(payload.get("client_id") or "")
        name, publisher = VERIFIED.get(client_id, (None, None))
        host = urlsplit(client_id).hostname if client_id.startswith("https://") else None
        returns = urlsplit(str(payload.get("redirect_uri") or "")).hostname or ""
        return {
            "client_id": client_id,
            "name": name or str(payload.get("client_name") or "This app")[:200],
            "label": "verified_publisher" if publisher else "from_host" if host else "unverified",
            "publisher": publisher,
            "host": host,
            "returns_to": returns,
            "loopback": returns in ("localhost", "127.0.0.1", "::1"),
            "resource": "mcp" if str(payload.get("resource", "")).rstrip("/").endswith("/mcp") else "api",
        }

    def eligible(person_id: str, resource: str) -> list[dict]:
        wanted = "mcp" if resource == "mcp" else "rest"
        result = []
        for organisation in accounts.organisations(person_id):
            access = organisation["access"]
            reason = None
            if not roles.can(organisation["role"], "client.connect"):
                reason = "Your role here doesn't allow connecting apps"
            elif access["state"] != "active":
                reason = "No access yet"
            elif wanted not in (access.get("tier") or {}).get("includes", []):
                reason = "Its plan doesn't include this"
            result.append(
                {
                    "id": organisation["id"],
                    "name": organisation["name"],
                    "eligible": reason is None,
                    "reason": reason,
                }
            )
        return result

    @app.get("/oauth/api/session")
    def consent_session(session_token: str = ""):
        payload = hand_off(session_token)
        person_id = accounts.person_for_identity(provider.config.issuer, payload["sub"])
        person = accounts.person(person_id) if person_id else None
        client = app_label(payload)
        return {
            "client": client,
            "email": payload.get("email") or "",
            "account": person is not None,
            "policies_pending": bool(person and person["policies_pending"]),
            "organisations": eligible(person_id, client["resource"])
            if person and not person["policies_pending"]
            else [],
        }

    @app.post("/oauth/api/decision")
    async def consent_decision(request: Request):
        data = await body(request)
        payload = hand_off(str(data.get("session_token") or ""))
        state = str(data.get("state") or "")
        if not re.fullmatch(r"[A-Za-z0-9._~-]{1,2048}", state):
            raise AccountError(
                "invalid_input", 400, "This approval link is incomplete. Start again from your app."
            )
        person_id = accounts.person_for_identity(provider.config.issuer, payload["sub"])
        client = app_label(payload)
        decision, organisation, grant = "deny", None, None
        if data.get("decision") == "allow" and person_id:
            chosen = str(data.get("organisation") or "")
            if not any(o["id"] == chosen and o["eligible"] for o in eligible(person_id, client["resource"])):
                raise AccountError(
                    "organisation_not_eligible",
                    409,
                    "Choose an organisation with access where you may connect apps.",
                )
            grant = accounts.create_grant(
                person_id,
                chosen,
                client_id=client["client_id"] or client["name"],
                client_name=client["name"],
                client_label=client["label"],
                client_host=client["host"],
                resource=client["resource"],
                context=context(request),
            )
            decision, organisation = "allow", chosen
        now = int(time.time())
        answer = sign_hs256(
            {
                "sub": payload["sub"],
                "iss": provider.config.base_url,
                "iat": now,
                "exp": now + 120,
                "state": state,
                "decision": decision,
                "org": organisation,
                "grant": grant,
                # The provider asks for a second factor after this page when the role needs one.
                "mfa": bool(organisation and needs_second_factor(person_id, organisation)),
            },
            consent_secret,
        )
        return {
            "redirect": f"{provider.config.issuer}continue?{urlencode({'state': state, 'consent_token': answer})}"
        }

    # ---------- the console page ----------
    @app.get("/{path:path}")
    def page(path: str):
        if frontend is None or not (frontend / "console.html").is_file():
            return Response("The console is not built.", status_code=404, media_type="text/plain")
        return FileResponse(frontend / "console.html", headers={"Cache-Control": "no-cache"})

    return app
