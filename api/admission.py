"""ACS-05 anonymous admission: network-group token buckets, concurrency and global leases.

Every allowance is an operator decision (D3) loaded from ``ALVARY_ADMISSION_CONFIG``; no
default capacity exists. Admission runs in one short database transaction before any corpus
lookup, the connection returns to the pool before the request is handled, and a quota-store
failure fails closed with 503 (never 429, never an in-memory fallback). Requests blocked only
by the global ceiling wait in a bounded ingress queue without being charged.
"""

import asyncio
import hashlib
import hmac
import ipaddress
import json
import math
import os
from dataclasses import dataclass
from pathlib import Path

from anyio import to_thread
from sqlalchemy import text
from sqlalchemy.engine import Engine
from starlette.responses import JSONResponse

from rights.database import PolicyNamespace

EXEMPT_PATHS = {"/healthz"}
GLOBAL_KEY = "global:all"


class AdmissionUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class ByteLimits:
    """BUD identity and global byte budgets (D3/D18). Text delivery is refused without them."""

    capacity: int
    refill_per_minute: float
    global_capacity: int
    global_refill_per_minute: float
    emission_lease_seconds: float

    def __post_init__(self):
        # MIN: a byte burst of at least one maximum single response (10 MiB).
        if (
            min(self.capacity, self.global_capacity) < 10 * 1024 * 1024
            or min(self.refill_per_minute, self.global_refill_per_minute, self.emission_lease_seconds) <= 0
            or self.emission_lease_seconds > 3600
        ):
            raise ValueError("Invalid byte limits")


@dataclass(frozen=True)
class AdmissionConfig:
    policy_revision: str
    capacity: int
    refill_per_minute: float
    identity_concurrency: int
    global_concurrency: int
    lease_seconds: int
    queue_size: int
    queue_deadline_seconds: float
    trusted_proxies: int
    hmac_secret: bytes
    previous_hmac_secret: bytes | None
    db_connections: int
    operator_reserve: int
    other_workers: int
    public_pool_size: int
    api_replicas: int
    byte_limits: "ByteLimits | None" = None

    def __post_init__(self):
        positive = (
            self.capacity,
            self.refill_per_minute,
            self.identity_concurrency,
            self.global_concurrency,
            self.lease_seconds,
            self.queue_deadline_seconds,
            self.public_pool_size,
            self.db_connections,
        )
        if (
            any(v <= 0 for v in positive)
            or min(self.queue_size, self.trusted_proxies, self.operator_reserve, self.other_workers) < 0
            or self.lease_seconds > 600
            or len(self.hmac_secret) < 32
            or (self.previous_hmac_secret is not None and len(self.previous_hmac_secret) < 32)
            or not 1 <= len(self.policy_revision) <= 64
        ):
            raise ValueError("Invalid admission configuration")
        # P <= C - R - O: the public pool can never consume the operator/recovery reserve.
        if self.public_pool_size > self.db_connections - self.operator_reserve - self.other_workers:
            raise ValueError("Public connection pool exceeds the database budget")
        if self.api_replicas < 1 or self.public_pool_size // self.api_replicas < 1:
            raise ValueError("Each API replica needs at least one pooled connection")

    @classmethod
    def load(cls, path: Path) -> "AdmissionConfig":
        data = json.loads(path.read_text())
        data["hmac_secret"] = bytes.fromhex(data["hmac_secret"])
        previous = data.get("previous_hmac_secret")
        data["previous_hmac_secret"] = bytes.fromhex(previous) if previous else None
        if data.get("byte_limits") is not None:
            data["byte_limits"] = ByteLimits(**data["byte_limits"])
        return cls(**data)

    @classmethod
    def from_environment(cls, variable: str = "ALVARY_ADMISSION_CONFIG") -> "AdmissionConfig | None":
        path = os.environ.get(variable)
        return cls.load(Path(path)) if path else None

    @property
    def instance_pool_size(self) -> int:
        """P is the aggregate across replicas; each instance gets an equal share, no overflow."""
        return self.public_pool_size // self.api_replicas

    @property
    def window_seconds(self) -> int:
        return math.ceil(60 * self.capacity / self.refill_per_minute)


def client_address(headers: list[tuple[bytes, bytes]], peer: str | None, trusted_proxies: int) -> str | None:
    """The address appended by the outermost trusted proxy; client-supplied entries are ignored."""
    if trusted_proxies == 0:
        return peer
    forwarded = [
        part.strip()
        for name, value in headers
        if name.lower() == b"x-forwarded-for"
        for part in value.decode("latin-1").split(",")
    ]
    return forwarded[-trusted_proxies] if len(forwarded) >= trusted_proxies else None


def network_group(address: str) -> str:
    """IPv4 address or IPv6 /64 (a documented guess owned by the platform maintainer)."""
    parsed = ipaddress.ip_address(address)
    if isinstance(parsed, ipaddress.IPv6Address):
        if parsed.ipv4_mapped is not None:
            return str(parsed.ipv4_mapped)
        return str(ipaddress.ip_network(f"{parsed}/64", strict=False))
    return str(parsed)


def pseudonym(secret: bytes, group: str) -> str:
    return "ip:" + hmac.new(secret, group.encode(), hashlib.sha256).hexdigest()[:32]


class AdmissionStore:
    def __init__(self, engine: Engine, namespace: PolicyNamespace):
        self.engine = engine
        self.namespace = namespace

    def _call(self, name, sql, parameters):
        try:
            with self.engine.begin() as conn:
                fn = self.namespace.qualified(conn, "policy", name)
                return conn.scalar(text(sql.format(fn=fn)), parameters)
        except Exception:
            raise AdmissionUnavailable("Quota store unavailable") from None

    def admit(self, request: dict) -> dict:
        return self._call("admit", "SELECT {fn}(CAST(:r AS jsonb))", {"r": json.dumps(request)})

    def release(self, lease_id: str) -> None:
        self._call("release_admission", "SELECT {fn}(CAST(:l AS uuid))", {"l": lease_id})


# The site class answers in RFC 9457 problem details with the MAC-06 codes; the legacy API keeps
# its existing body.
SITE_CODES = {
    "Client address unavailable": "invalid_input",
    "Quota store unavailable": "temporarily_unavailable",
    "Rate limit exceeded": "rate_limited",
    "capacity_exhausted": "capacity_exhausted",
}


def failure(
    status: int, error: str, retry_after: int | None, config: AdmissionConfig, route_class: str = "anonymous"
):
    headers = {"Cache-Control": "no-store", "Open-Alvary-Quota-Policy": config.policy_revision}
    headers["Open-Alvary-Quota-Tier"] = route_class
    if route_class == "anonymous":
        body = {"detail": error}
        media_type = "application/json"
    else:
        code = SITE_CODES.get(error, "temporarily_unavailable")
        body = {
            "type": f"https://open.alvary.ai/docs/errors#{code}",
            "title": "Request refused",
            "status": status,
            "detail": error,
            "code": code,
            "retryable": status in (429, 503),
        }
        media_type = "application/problem+json"
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)
        body["retry_after_seconds"] = retry_after
    return JSONResponse(status_code=status, content=body, headers=headers, media_type=media_type)


class AdmissionMiddleware:
    """One admission class. "anonymous" is the legacy API's; any other class (the website's "site")
    keeps its own buckets, concurrency and global ceiling under keys in its own namespace. The
    database accepts keys of one shape, ``name:value`` with a single colon, so "ip:<hash>" becomes
    "site:ip-<hash>" and "global:all" becomes "site:global-all"."""

    def __init__(self, app, config: AdmissionConfig, store: AdmissionStore, route_class: str = "anonymous"):
        self.app = app
        self.config = config
        self.store = store
        self.route_class = route_class
        self.waiting = 0

    def key(self, key: str) -> str:
        return key if self.route_class == "anonymous" else f"{self.route_class}:{key.replace(':', '-')}"

    def _failure(self, status, error, retry_after):
        return failure(status, error, retry_after, self.config, self.route_class)

    def request_for(self, scope) -> dict | None:
        peer = scope.get("client")[0] if scope.get("client") else None
        address = client_address(scope.get("headers", []), peer, self.config.trusted_proxies)
        try:
            group = network_group(address) if address else None
        except ValueError:
            group = None
        if group is None:
            return None
        keys = [self.key(pseudonym(self.config.hmac_secret, group))]
        if self.config.previous_hmac_secret:
            # Rotation keeps the previous bucket's debt until it refills; it never resets quota.
            keys.append(self.key(pseudonym(self.config.previous_hmac_secret, group)))
        limits = {"capacity": self.config.capacity, "refill_per_minute": self.config.refill_per_minute}
        return {
            "buckets": [{"key": k, **limits} for k in keys],
            "concurrency": [
                {"key": keys[0], "limit": self.config.identity_concurrency},
                {"key": self.key(GLOBAL_KEY), "limit": self.config.global_concurrency},
            ],
            "lease_seconds": self.config.lease_seconds,
        }

    async def __call__(self, scope, receive, send):
        # Mounted apps see the full path; exemptions apply to the path within this app.
        path = scope.get("path", "")
        root = scope.get("root_path", "")
        if root and path.startswith(root):
            path = path[len(root) :] or "/"
        if scope["type"] != "http" or path in EXEMPT_PATHS:
            return await self.app(scope, receive, send)
        request = self.request_for(scope)
        if request is None:
            return await self._failure(400, "Client address unavailable", None)(scope, receive, send)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.config.queue_deadline_seconds
        queued, delay = False, 0.02
        try:
            while True:
                try:
                    result = await to_thread.run_sync(self.store.admit, request)
                except AdmissionUnavailable:
                    response = self._failure(503, "Quota store unavailable", None)
                    return await response(scope, receive, send)
                if result["admitted"]:
                    break
                if set(result["blocked"]) != {self.key(GLOBAL_KEY)}:
                    response = self._failure(429, "Rate limit exceeded", result["retry_after"])
                    return await response(scope, receive, send)
                # Only global capacity is exhausted: wait in the bounded ingress queue, uncharged.
                if not queued:
                    if self.waiting >= self.config.queue_size:
                        response = self._failure(503, "capacity_exhausted", result["retry_after"])
                        return await response(scope, receive, send)
                    self.waiting, queued = self.waiting + 1, True
                if loop.time() + delay > deadline:
                    response = self._failure(503, "capacity_exhausted", result["retry_after"])
                    return await response(scope, receive, send)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 0.5)
        finally:
            if queued:
                self.waiting -= 1
        # Routes charge response bytes to the same pseudonymous identity scope.
        scope.setdefault("state", {})["admission_identity"] = request["buckets"][0]["key"]
        bucket = result["buckets"][request["buckets"][0]["key"]]
        headers = [
            (
                b"ratelimit-policy",
                f'"{self.route_class}";q={self.config.capacity};w={self.config.window_seconds}'.encode(),
            ),
            (b"ratelimit", f'"{self.route_class}";r={bucket["remaining"]};t={bucket["reset"]}'.encode()),
            (b"open-alvary-quota-policy", self.config.policy_revision.encode()),
            (b"open-alvary-quota-tier", self.route_class.encode()),
        ]

        async def with_headers(message):
            if message["type"] == "http.response.start":
                message = {**message, "headers": [*message.get("headers", []), *headers]}
            await send(message)

        try:
            await self.app(scope, receive, with_headers)
        finally:
            try:
                await to_thread.run_sync(self.store.release, result["lease_id"])
            except AdmissionUnavailable:
                pass  # The lease expires on its own; the request debit is never refunded.


def published_policy(config: "AdmissionConfig | None") -> dict:
    """The public /quota-policy document; never includes secrets, addresses or pool sizes."""
    if config is None:
        return {"enforcement": "none", "note": "Metadata pilot: no ACS quota policy is configured"}
    policy = {
        "enforcement": "anonymous",
        "policy_revision": config.policy_revision,
        "algorithm": "token_bucket",
        "grouping": "ipv4_address_or_ipv6_64",
        "request_units": {
            "burst": config.capacity,
            "refill_per_minute": config.refill_per_minute,
            "window_seconds": config.window_seconds,
        },
        "concurrency": {"per_identity": config.identity_concurrency},
        "max_single_response_bytes": 10 * 1024 * 1024,
    }
    if config.byte_limits is not None:
        policy["response_bytes"] = {
            "burst": config.byte_limits.capacity,
            "refill_per_minute": config.byte_limits.refill_per_minute,
        }
    return policy
