"""The credential gate for machine access (MAC-05–07): REST v1 and MCP share it.

Every request carries one bearer credential in the Authorization header: an API key
(`oa_live_…`) or an access token from the identity provider whose audience is exactly this
resource. The gate checks it, and the organisation's access, through the accounts schema's narrow
check; admits the request against the organisation's allowance; checks the credential again just
before the response starts, so a change committed meanwhile (a revoked key, a removed member, a
lapsed or narrowed access) stops it; reserves the response's bytes against the organisation's data a
day; and counts the request and its bytes for the organisation.

Data a day (MAC F11) is a byte bucket in the BUD ledger (rights.bytes, migration 0017): it holds one
day's bytes_per_day and refills continuously over 24 hours, shared by every key and app across REST
and MCP. A response's bytes (its Content-Length, or the single-response maximum when it has none) are
reserved before its first byte and settled with what was sent; a response that would go over is
refused with 429 and the seconds until enough has refilled.

Refusals use MAC-06's codes at the HTTP boundary, as RFC 9457 problem details. Only a missing or
invalid credential carries a WWW-Authenticate challenge (and insufficient_scope); a 403 never sends
anyone through sign-in again for something sign-in cannot change (N09).
"""

import base64
import json
import logging
import math
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import parse_qsl

import httpx
from anyio import to_thread
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi.responses import JSONResponse

from accounts import keys
from api.admission import AdmissionStore, AdmissionUnavailable
from api.emission import MAX_SINGLE_RESPONSE_BYTES
from api.reach import OutsidePlanLimited, Reach
from rights.bytes import ByteLedger, LedgerConflict, LedgerUnavailable, Scope

SCOPE = "corpus:read"
GRANT_CLAIM = "https://open.alvary.ai/grant"
STATUS = {
    "invalid_input": 400,
    "unavailable": 404,
    "not_found_route": 404,
    "withdrawn": 410,
    "result_too_large": 413,
    "credential_not_accepted": 400,
    "credential_required": 401,
    "invalid_credentials": 401,
    "insufficient_scope": 403,
    "entitlement_required": 403,
    "entitlement_lapsed": 403,
    "jurisdiction_not_included": 403,
    "account_inactive": 403,
    "organisation_suspended": 403,
    "rate_limited": 429,
    "temporarily_unavailable": 503,
    "capacity_exhausted": 503,
}
# Some hosts show only the message, so each says what to do.
DETAIL = {
    "credential_required": "Send an API key or access token: Authorization: Bearer <credential>.",
    "invalid_credentials": "This key or token is unknown, revoked or expired. Create a new key in the console.",
    "insufficient_scope": "This token does not allow reading the corpus. Connect the app again.",
    "entitlement_required": "Your organisation has no access yet. Request access in the console.",
    "entitlement_lapsed": "Your organisation's access has ended. Renew it in the console.",
    "jurisdiction_not_included": "That jurisdiction is outside your organisation's access. Add it in the console.",
    "account_inactive": "This account is suspended or closed.",
    "organisation_suspended": "This organisation is suspended.",
    "rate_limited": "Too many requests. Wait for the time in Retry-After.",
    "bytes_per_day": "Your organisation's data a day is used up for now. Wait for the time in Retry-After, "
    "or keep a local copy and refresh it from the change feed.",
    "temporarily_unavailable": "Access cannot be checked right now. Try again shortly.",
    "credential_not_accepted": "Send the key in the Authorization header, never in the address.",
    "unavailable": "There is no such record, or it is not public.",
    "not_found_route": "There is no such route. Every route is listed at https://open.alvary.ai/docs/api.",
    "withdrawn": "This release was withdrawn and is no longer served. Use the latest release.",
    "result_too_large": "The answer would be larger than one response may be.",
}


# What a refusal says when the plan does not include the class asked for.
NOT_INCLUDED = {
    "bulk": "Your organisation's access does not include bulk releases with full text. They are granted "
    "by agreement: write to the address on https://open.alvary.ai/docs/plans.",
}
NOT_INCLUDED_DEFAULT = "Your organisation's plan does not include this. See the console."


# MCP carries the same codes as a JSON-RPC error (agent-access application numbers), at the same
# HTTP status, so a host starts sign-in on the 401 exactly as for REST.
MCP_NUMBERS = {400: -32600, 401: 10001, 403: 10002, 429: 10003, 503: 10004}
DAY_MINUTES = 24 * 60


def _length(message: dict) -> int | None:
    """A response's size from its start message: 0 for one without a body, its Content-Length when it
    has one, else None."""
    status = message.get("status", 200)
    if status < 200 or status in (204, 304):
        return 0
    for name, value in message.get("headers", []):
        if name.lower() == b"content-length":
            try:
                return max(0, int(value))
            except ValueError:
                return None
    return None


def _stamp(message: dict, headers: list[tuple[bytes, bytes]]) -> dict:
    """A response's start message with these headers added, each only where the answer has not set it
    already (a refusal from the app carries its own request ID)."""
    present = {name.lower() for name, _ in message.get("headers", [])}
    extra = [(name, value) for name, value in headers if name not in present]
    return {**message, "headers": [*message.get("headers", []), *extra]} if extra else message


def _stamped(send, headers: list[tuple[bytes, bytes]]):
    async def stamped(message):
        await send(_stamp(message, headers) if message["type"] == "http.response.start" else message)

    return stamped


def jsonrpc_problem(
    code: str,
    request_id: str,
    *,
    detail: str | None = None,
    challenge: str | None = None,
    retry_after: int | None = None,
) -> JSONResponse:
    status = STATUS[code]
    data = {"code": code, "request_id": request_id}
    headers = {"Cache-Control": "no-store", "X-Request-ID": request_id}
    if challenge:
        headers["WWW-Authenticate"] = challenge
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)
        data["retry_after_seconds"] = retry_after
    message = detail or DETAIL.get(code, "Request refused.")
    body = {
        "jsonrpc": "2.0",
        "id": None,
        "error": {"code": MCP_NUMBERS[status], "message": message, "data": data},
    }
    return JSONResponse(body, status_code=status, headers=headers)


def problem(
    code: str,
    request_id: str,
    *,
    detail: str | None = None,
    challenge: str | None = None,
    retry_after: int | None = None,
) -> JSONResponse:
    status = STATUS[code]
    body = {
        "type": f"https://open.alvary.ai/docs/errors#{code}",
        "title": "Request refused",
        "status": status,
        "detail": detail or DETAIL.get(code, "Request refused."),
        "code": code,
        "retryable": status in (429, 503),
        "request_id": request_id,
    }
    headers = {"Cache-Control": "no-store", "X-Request-ID": request_id}
    if challenge:
        headers["WWW-Authenticate"] = challenge
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)
        body["retry_after_seconds"] = retry_after
    return JSONResponse(body, status_code=status, headers=headers, media_type="application/problem+json")


class TokenVerifier:
    """RS256 access tokens from the identity provider, checked against its published keys."""

    def __init__(self, issuer: str, client: httpx.Client | None = None, jwks_uri: str | None = None):
        self.issuer = issuer
        self.http = client or httpx.Client(timeout=10)
        self.jwks_uri = jwks_uri or issuer + ".well-known/jwks.json"
        self._keys: dict = {}
        self._fetched = 0.0

    def _signing_keys(self, refresh: bool = False) -> dict:
        if refresh and time.time() - self._fetched < 30:
            return self._keys  # an unknown key ID refreshes at most every 30 seconds
        if refresh or not self._keys or time.time() - self._fetched > 3600:
            response = self.http.get(self.jwks_uri)
            response.raise_for_status()
            self._keys = {
                k["kid"]: rsa.RSAPublicNumbers(_int(k["e"]), _int(k["n"])).public_key()
                for k in response.json().get("keys", [])
                if k.get("kty") == "RSA" and k.get("kid")
            }
            self._fetched = time.time()
        return self._keys

    def verify(self, token: str, resource: str) -> dict:
        """The token's claims; ValueError if invalid, PermissionError if it lacks corpus:read."""
        try:
            head, body, signature = token.split(".")
            header, claims = json.loads(_b64(head)), json.loads(_b64(body))
        except ValueError as error:
            raise ValueError("not a token") from error
        if header.get("alg") != "RS256":
            raise ValueError("unexpected algorithm")
        key = self._signing_keys().get(header.get("kid")) or self._signing_keys(refresh=True).get(
            header.get("kid")
        )
        if key is None:
            raise ValueError("unknown signing key")
        try:
            key.verify(_b64(signature), f"{head}.{body}".encode(), padding.PKCS1v15(), hashes.SHA256())
        except InvalidSignature as error:
            raise ValueError("bad signature") from error
        audience = claims.get("aud")
        now = time.time()
        if claims.get("iss") != self.issuer or resource not in (
            audience if isinstance(audience, list) else [audience]
        ):
            raise ValueError("wrong issuer or audience")
        if float(claims.get("exp", 0)) < now - 30 or float(claims.get("nbf", 0)) > now + 30:
            raise ValueError("expired")
        if SCOPE not in str(claims.get("scope", "")).split():
            raise PermissionError("insufficient scope")
        return claims


def _b64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _int(value: str) -> int:
    return int.from_bytes(_b64(value), "big")


@dataclass(frozen=True)
class GateConfig:
    resource: str  # exact resource URL, the token audience
    resource_class: str  # "rest" or "mcp": the tier must include it; usage is counted under it
    grant_resource: str  # "api" or "mcp", as consent records it
    metadata_url: str  # this resource's protected-resource metadata (RFC 9728)
    public_paths: frozenset[str] = frozenset()
    # Paths of another class, by prefix: (("/bulk/", "bulk"),) makes bulk downloads need a tier that
    # includes bulk, and counts them as bulk.
    classes: tuple[tuple[str, str], ...] = ()
    global_concurrency: int = 512
    lease_seconds: int = 60
    jsonrpc: bool = False  # MCP: refusals as JSON-RPC errors
    # Requests outside a plan's countries also draw on this, the Starter rate (api.reach).
    outside_per_minute: int = 60
    outside_burst: int = 30


class CredentialGate:
    def __init__(
        self,
        app,
        *,
        config: GateConfig,
        check: Callable[[dict], dict],
        record_usage: Callable[[dict], None],
        admission: AdmissionStore | None,
        verifier: TokenVerifier | None,
        key_pepper: bytes | None = None,
        ledger: ByteLedger | None = None,
    ):
        self.app, self.config = app, config
        self.check, self.record_usage, self.admission, self.verifier = (
            check,
            record_usage,
            admission,
            verifier,
        )
        self.ledger = ledger
        self.pepper = key_pepper
        self.challenge = f'Bearer resource_metadata="{config.metadata_url}", scope="{SCOPE}"'

    def _refuse(self, code: str, request_id: str, **kwargs) -> JSONResponse:
        if code == "credential_required":
            kwargs["challenge"] = self.challenge
        elif code == "invalid_credentials":
            kwargs["challenge"] = self.challenge + ', error="invalid_token"'
        elif code == "insufficient_scope":
            kwargs["challenge"] = (
                f'Bearer error="insufficient_scope", scope="{SCOPE}", resource_metadata="{self.config.metadata_url}"'
            )
        return (jsonrpc_problem if self.config.jsonrpc else problem)(code, request_id, **kwargs)

    def credential_request(self, token: str) -> dict:
        """What to ask the accounts check for this bearer value; raises ValueError/PermissionError."""
        if token.startswith(keys.PREFIX):
            key_id = keys.parse(token)
            if key_id is None:
                raise ValueError("malformed key")
            return {
                "kind": "key",
                "key_id": key_id,
                "secret_hash": keys.secret_hash(token, self.pepper).hex(),
            }
        if self.verifier is None:
            raise ValueError("tokens are not accepted here yet")
        claims = self.verifier.verify(token, self.config.resource)
        return {
            "kind": "grant",
            "grant_id": str(claims.get(GRANT_CLAIM, "")),
            "resource": self.config.grant_resource,
            "issuer": claims["iss"],
            "subject": str(claims.get("sub", "")),
        }

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        root = scope.get("root_path", "")
        if root and path.startswith(root):
            path = path[len(root) :] or "/"
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        request_id = uuid.uuid4().hex[:16]
        scope.setdefault("state", {})["request_id"] = request_id
        if path in self.config.public_paths or scope.get("method") == "OPTIONS":
            # Public answers need no credential, and carry a request ID like every other answer.
            return await self.app(scope, receive, _stamped(send, [(b"x-request-id", request_id.encode())]))
        # A credential in the address: a parameter named as a key or a token (api_key, key, access_token,
        # token and the like), but not the change feed's own resume_token, which is no credential.
        query = parse_qsl(scope.get("query_string", b"").decode("latin-1"), keep_blank_values=True)
        names = {name.lower() for name, _ in query} - {"resume_token"}
        if any(name.endswith(("key", "token")) for name in names):
            return await self._refuse("credential_not_accepted", request_id)(scope, receive, send)
        authorization = [
            v.decode("latin-1") for k, v in scope.get("headers", []) if k.lower() == b"authorization"
        ]
        if not authorization:
            return await self._refuse("credential_required", request_id)(scope, receive, send)
        scheme, _, token = authorization[0].partition(" ")
        if len(authorization) > 1 or scheme.lower() != "bearer" or not token.strip():
            return await self._refuse("invalid_credentials", request_id)(scope, receive, send)
        token = token.strip()
        try:
            asked = await to_thread.run_sync(self.credential_request, token)
            result = await to_thread.run_sync(self.check, asked)
        except PermissionError:
            return await self._refuse("insufficient_scope", request_id)(scope, receive, send)
        except (ValueError, KeyError):
            return await self._refuse("invalid_credentials", request_id)(scope, receive, send)
        except Exception:
            logging.getLogger("open_alvary.credentials").exception("credential check failed")
            return await self._refuse("temporarily_unavailable", request_id)(scope, receive, send)
        if not result.get("ok"):
            return await self._refuse(result.get("code", "invalid_credentials"), request_id)(
                scope, receive, send
            )
        organisation, credential = result["organisation"], result["credential"]
        resource_class = next(
            (name for prefix, name in self.config.classes if path.startswith(prefix)),
            self.config.resource_class,
        )
        usage = {"organisation": organisation, "credential": credential, "class": resource_class}
        if resource_class not in (result.get("tier") or {}).get("includes", []):
            return await self._refuse(
                "entitlement_required",
                request_id,
                detail=NOT_INCLUDED.get(resource_class, NOT_INCLUDED_DEFAULT),
            )(scope, receive, send)
        lease = None
        stamped = [(b"x-request-id", request_id.encode())]
        if self.admission is not None:
            allowance = result["tier"].get("allowance") or {}
            rate = max(1, int(allowance.get("requests_per_minute", 60)))
            burst = max(1, int(allowance.get("burst", rate)))
            concurrency = max(1, int(allowance.get("concurrency", 8)))
            credential_key = (
                credential.replace("key_", "key:", 1)
                if credential.startswith("key_")
                else ("grant:" + credential.removeprefix("grt_"))
            )
            bucket_key = "org:" + organisation.removeprefix("org_")
            admit = {
                "buckets": [{"key": bucket_key, "capacity": burst, "refill_per_minute": rate}],
                "concurrency": [
                    {"key": bucket_key, "limit": concurrency},
                    {"key": credential_key, "limit": max(1, math.ceil(concurrency / 2))},
                    {"key": "global:api", "limit": self.config.global_concurrency},
                ],
                "lease_seconds": self.config.lease_seconds,
            }
            try:
                decision = await to_thread.run_sync(self.admission.admit, admit)
            except AdmissionUnavailable:
                return await self._refuse("temporarily_unavailable", request_id)(scope, receive, send)
            if not decision["admitted"]:
                await self._count({**usage, "refusal": "rate_limited"})
                return await self._refuse(
                    "rate_limited", request_id, retry_after=int(decision["retry_after"])
                )(scope, receive, send)
            lease = decision["lease_id"]
            # The organisation's request allowance, as the website's data path states its own
            # (api.admission): the burst over the time it takes to refill, and what is left of it now.
            bucket = (decision.get("buckets") or {}).get(bucket_key)
            if bucket is not None:
                window = math.ceil(60 * burst / rate)
                left, reset = int(bucket["remaining"]), int(bucket["reset"])
                stamped += [
                    (b"ratelimit-policy", f'"organisation";q={burst};w={window}'.encode()),
                    (b"ratelimit", f'"organisation";r={left};t={reset}'.encode()),
                ]
        scope["state"]["access"] = {
            "organisation": organisation,
            "credential": credential,
            "scope": result.get("scope") or {},
            "tier": result.get("tier"),
            "ends_at": result.get("ends_at"),
            "reach": Reach.of(result.get("scope"), lambda: self._outside(organisation)),
        }
        sent = {"bytes": 0, "refused": None, "status": 0, "reservation": None, "reserved": 0}
        daily = int(((result.get("tier") or {}).get("allowance") or {}).get("bytes_per_day") or 0)
        # A tier without bytes_per_day has no data a day to enforce; every published tier has one.
        bytes_scope = (
            Scope("org:" + organisation.removeprefix("org_"), daily, daily / DAY_MINUTES)
            if self.ledger is not None and daily > 0
            else None
        )

        async def refuse_inside(code: str, **kwargs):
            sent["refused"] = code
            await self._refuse(code, request_id, **kwargs)(scope, receive, send)

        async def reserve(message) -> bool:
            """Reserve the response's bytes before its first byte; False when it was refused instead."""
            amount = _length(message)
            amount = MAX_SINGLE_RESPONSE_BYTES if amount is None else amount
            if bytes_scope is None or amount == 0:
                return True
            try:
                held = await to_thread.run_sync(
                    lambda: self.ledger.reserve(
                        uuid.uuid4(), amount, [bytes_scope], self.config.lease_seconds
                    )
                )
            except (LedgerUnavailable, LedgerConflict):
                await refuse_inside("temporarily_unavailable")
                return False
            if not held.reserved:
                retry = max(1, int(held.retry_after or 1))
                await refuse_inside("rate_limited", retry_after=retry, detail=DETAIL["bytes_per_day"])
                return False
            sent["reservation"], sent["reserved"] = held.reservation_id, amount
            try:
                started = await to_thread.run_sync(self.ledger.begin_emission, held.reservation_id)
            except (LedgerUnavailable, LedgerConflict):
                started = False
            if not started:
                await refuse_inside("temporarily_unavailable")
                return False
            return True

        async def guarded(message):
            if message["type"] == "http.response.start":
                # The second check (F10): stop the response if access changed during the request.
                try:
                    again = await to_thread.run_sync(self.check, asked)
                except Exception:
                    again = {"ok": False, "code": "temporarily_unavailable"}
                if not again.get("ok"):
                    await refuse_inside(again.get("code", "invalid_credentials"))
                    return
                if resource_class not in (again.get("tier") or {}).get("includes", []):
                    detail = NOT_INCLUDED.get(resource_class, NOT_INCLUDED_DEFAULT)
                    await refuse_inside("entitlement_required", detail=detail)
                    return
                if not await reserve(message):
                    return
                sent["status"] = message["status"]
                message = _stamp(message, stamped)
            elif message["type"] == "http.response.body":
                if sent["refused"]:
                    return
                sent["bytes"] += len(message.get("body", b""))
            await send(message)

        try:
            await self.app(scope, receive, guarded)
        finally:
            if sent["reservation"] is not None:
                await self._settle(sent)
            if lease is not None:
                try:
                    await to_thread.run_sync(self.admission.release, lease)
                except AdmissionUnavailable:
                    pass  # the lease expires on its own
            refusal = sent["refused"] or ("rate_limited" if sent["status"] == 429 else None)
            await self._count({**usage, **({"refusal": refusal} if refusal else {"bytes": sent["bytes"]})})

    def _outside(self, organisation: str) -> None:
        """Admit one request outside the plan's countries at the Starter rate (called from a worker
        thread by the route); raises OutsidePlanLimited, or AdmissionUnavailable."""
        if self.admission is None:
            return
        decision = self.admission.admit(
            {
                "buckets": [
                    {
                        "key": "outside:" + organisation.removeprefix("org_"),
                        "capacity": self.config.outside_burst,
                        "refill_per_minute": self.config.outside_per_minute,
                    }
                ],
                "concurrency": [],
                "lease_seconds": self.config.lease_seconds,
            }
        )
        if not decision["admitted"]:
            raise OutsidePlanLimited(int(decision["retry_after"]))

    async def _settle(self, sent: dict) -> None:
        """Settle a reservation with the bytes handed to transport. A reservation refused before its
        first byte (begin_emission failed) is unsent and released."""
        reservation = sent["reservation"]
        try:
            if sent["refused"] and not sent["bytes"]:
                await to_thread.run_sync(self.ledger.release, reservation)
                return
            used = min(sent["bytes"], sent["reserved"])
            await to_thread.run_sync(self.ledger.settle, reservation, used, f"sent:{used}")
        except (LedgerUnavailable, LedgerConflict):
            # The reservation stays charged; the maintenance reconciler settles it conservatively.
            logging.getLogger("open_alvary.credentials").warning("byte reservation not settled")

    async def _count(self, entry: dict) -> None:
        try:
            await to_thread.run_sync(self.record_usage, entry)
        except Exception:
            logging.getLogger("open_alvary.credentials").warning("usage not recorded")
