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
        return cls(**data)

    @classmethod
    def from_environment(cls) -> "AdmissionConfig | None":
        path = os.environ.get("ALVARY_ADMISSION_CONFIG")
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


def failure(status: int, error: str, retry_after: int | None, config: AdmissionConfig):
    headers = {"Cache-Control": "no-store", "Open-Alvary-Quota-Policy": config.policy_revision}
    headers["Open-Alvary-Quota-Tier"] = "anonymous"
    body = {"detail": error}
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)
        body["retry_after_seconds"] = retry_after
    return JSONResponse(status_code=status, content=body, headers=headers)


class AdmissionMiddleware:
    def __init__(self, app, config: AdmissionConfig, store: AdmissionStore):
        self.app = app
        self.config = config
        self.store = store
        self.waiting = 0

    def request_for(self, scope) -> dict | None:
        peer = scope.get("client")[0] if scope.get("client") else None
        address = client_address(scope.get("headers", []), peer, self.config.trusted_proxies)
        try:
            group = network_group(address) if address else None
        except ValueError:
            group = None
        if group is None:
            return None
        keys = [pseudonym(self.config.hmac_secret, group)]
        if self.config.previous_hmac_secret:
            # Rotation keeps the previous bucket's debt until it refills; it never resets quota.
            keys.append(pseudonym(self.config.previous_hmac_secret, group))
        limits = {"capacity": self.config.capacity, "refill_per_minute": self.config.refill_per_minute}
        return {
            "buckets": [{"key": k, **limits} for k in keys],
            "concurrency": [
                {"key": keys[0], "limit": self.config.identity_concurrency},
                {"key": GLOBAL_KEY, "limit": self.config.global_concurrency},
            ],
            "lease_seconds": self.config.lease_seconds,
        }

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"] in EXEMPT_PATHS:
            return await self.app(scope, receive, send)
        request = self.request_for(scope)
        if request is None:
            return await failure(400, "Client address unavailable", None, self.config)(scope, receive, send)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.config.queue_deadline_seconds
        queued, delay = False, 0.02
        try:
            while True:
                try:
                    result = await to_thread.run_sync(self.store.admit, request)
                except AdmissionUnavailable:
                    response = failure(503, "Quota store unavailable", None, self.config)
                    return await response(scope, receive, send)
                if result["admitted"]:
                    break
                if set(result["blocked"]) != {GLOBAL_KEY}:
                    response = failure(429, "Rate limit exceeded", result["retry_after"], self.config)
                    return await response(scope, receive, send)
                # Only global capacity is exhausted: wait in the bounded ingress queue, uncharged.
                if not queued:
                    if self.waiting >= self.config.queue_size:
                        response = failure(503, "capacity_exhausted", result["retry_after"], self.config)
                        return await response(scope, receive, send)
                    self.waiting, queued = self.waiting + 1, True
                if loop.time() + delay > deadline:
                    response = failure(503, "capacity_exhausted", result["retry_after"], self.config)
                    return await response(scope, receive, send)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 0.5)
        finally:
            if queued:
                self.waiting -= 1
        bucket = result["buckets"][request["buckets"][0]["key"]]
        headers = [
            (
                b"ratelimit-policy",
                f'"anonymous";q={self.config.capacity};w={self.config.window_seconds}'.encode(),
            ),
            (b"ratelimit", f'"anonymous";r={bucket["remaining"]};t={bucket["reset"]}'.encode()),
            (b"open-alvary-quota-policy", self.config.policy_revision.encode()),
            (b"open-alvary-quota-tier", b"anonymous"),
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
