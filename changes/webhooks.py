"""Webhooks (SUB C3, changes-contracts.md): signed deliveries of monitor matches to developers'
own HTTPS endpoints.

Network safety: an endpoint's host is resolved at every attempt, and every address it resolves to
must be public; the connection goes to the vetted address while TLS still checks the hostname, so a
second lookup cannot steer it elsewhere. Redirects are never followed. Requests come from the
worker, never from the API that serves corpus reads.

Signing: HMAC-SHA256 over "<timestamp>.<exact body bytes>" with the endpoint's secret, sent as
Open-Alvary-Signature: v1=<hex> (one per current key during a rotation) with Open-Alvary-Key,
Open-Alvary-Timestamp and Open-Alvary-Delivery. Secrets are stored sealed (AES-256-GCM) because
HMAC needs them back; each is shown once.
"""

import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import secrets
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

import httpcore
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import text

from accounts import mail
from accounts.ids import new_id, valid
from accounts.store import AccountError, Accounts
from changes.feed import Feed, _Visibility
from changes.watches import plan_limits

log = logging.getLogger("open_alvary.webhooks")
TIMEOUT = 10.0  # seconds for the whole delivery, and for each phase within it (D19)
MAX_BODY = 64 * 1024
MAX_ANSWER = 8 * 1024
RETRIES = (60, 300, 1800, 7200, 28800)  # after attempts 1-5; terminal at 24 h (D19)
TERMINAL = timedelta(hours=24)
FAILING_AFTER, PAUSE_AFTER = 3, 20  # consecutive failed attempts (D19)
OVERLAP = timedelta(hours=24)  # an old secret keeps signing after a rotation (D19)
VERIFY_PER_HOUR, REPLAYS_PER_HOUR = 5, 10
USER_AGENT = "Open-Alvary-Webhooks/1"
BLOCKED_NAMES = ("localhost", ".localhost", ".local", ".internal", ".home.arpa")


# ---------- sealed secrets ----------
class Sealer:
    def __init__(self, keys: dict[str, bytes], current: str):
        if current not in keys or any(len(k) != 32 for k in keys.values()):
            raise ValueError("Webhook keys must be 32 bytes and include the current key")
        self.keys, self.current = keys, current

    @classmethod
    def from_environment(cls) -> "Sealer | None":
        """Shared keys from ALVARY_WEBHOOK_KEYS. A development process uses a fixed local key; a
        persistent deployment without keys has webhooks off (secrets must outlive the process)."""
        raw = os.getenv("ALVARY_WEBHOOK_KEYS")
        if raw:
            keys = {kid: base64.b64decode(value) for kid, value in json.loads(raw).items()}
            return cls(keys, os.environ["ALVARY_WEBHOOK_KEY_ID"])
        if os.getenv("ALVARY_RUNTIME_MODE") == "persistent":
            return None
        log.warning("ALVARY_WEBHOOK_KEYS is not set: using the development key")
        return cls({"dev": hashlib.sha256(b"open-alvary development webhook key").digest()}, "dev")

    def seal(self, secret: str) -> str:
        nonce = secrets.token_bytes(12)
        sealed = AESGCM(self.keys[self.current]).encrypt(nonce, secret.encode(), b"webhook-secret")
        return self.current + "." + base64.urlsafe_b64encode(nonce + sealed).decode()

    def open(self, value: str) -> str:
        kid, _, data = value.partition(".")
        raw = base64.urlsafe_b64decode(data)
        try:
            return AESGCM(self.keys[kid]).decrypt(raw[:12], raw[12:], b"webhook-secret").decode()
        except (KeyError, InvalidTag) as error:
            raise RuntimeError("A webhook secret cannot be opened with the configured keys") from error


# ---------- addresses ----------
class Refused(ValueError):
    pass


def check_url(url) -> str:
    """An endpoint URL anyone may register: HTTPS on 443, a public host, no credentials."""
    if not isinstance(url, str) or not 9 <= len(url) <= 2000:
        raise Refused("Give an HTTPS URL of up to 2,000 characters.")
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not host:
        raise Refused("Webhooks are delivered over HTTPS only.")
    if parts.username or parts.password or "@" in parts.netloc:
        raise Refused("The URL must not contain a user name or password.")
    if parts.fragment:
        raise Refused("The URL must not have a fragment (#).")
    if parts.port not in (None, 443):
        raise Refused("Webhooks are delivered to port 443.")
    if host in BLOCKED_NAMES or host.endswith(BLOCKED_NAMES[1:]):
        raise Refused("That address is not reachable from the internet.")
    try:
        literal = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        literal = None
    if literal is not None and not public(str(literal)):
        raise Refused("That address is not reachable from the internet.")
    return parts._replace(netloc=parts.netloc.replace(":443", ""), fragment="").geturl()


def public(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def resolve(host: str) -> list[str]:
    return sorted({info[4][0] for info in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)})


@dataclass
class Outcome:
    status: int | None
    body: bytes = b""
    retry_after: int | None = None
    error: str | None = None


class _Pinned(httpcore.SyncBackend):
    """Connects to the vetted address; TLS (SNI and certificate) still uses the URL's hostname."""

    def __init__(self, address: str):
        super().__init__()
        self.address = address

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        return super().connect_tcp(self.address, port, timeout, local_address, socket_options)


def _send(url: str, address: str, body: bytes, headers: dict) -> tuple[int, dict, bytes]:
    pool = httpcore.ConnectionPool(network_backend=_Pinned(address), retries=0, max_connections=1)
    try:
        with pool.stream(
            "POST",
            url,
            headers=list(headers.items()),
            content=body,
            extensions={"timeout": {"connect": TIMEOUT, "read": TIMEOUT, "write": TIMEOUT, "pool": TIMEOUT}},
        ) as response:
            answer = b""
            for chunk in response.iter_stream():
                answer += chunk
                if len(answer) > MAX_ANSWER:
                    break
            return (
                response.status,
                {k.decode().lower(): v.decode() for k, v in response.headers},
                answer[:MAX_ANSWER],
            )
    finally:
        pool.close()


def _within(limit: float, work, *args):
    """Run one delivery with a deadline on the whole of it. A late attempt is abandoned to its own
    per-phase timeouts in a daemon thread (it ends on its own and its answer is ignored)."""
    outcome: dict = {}

    def run():
        try:
            outcome["value"] = work(*args)
        except BaseException as error:  # handed back to the caller
            outcome["error"] = error

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(limit)
    if thread.is_alive():
        raise TimeoutError
    if "error" in outcome:
        raise outcome["error"]
    return outcome["value"]


class Sender:
    """POSTs to an endpoint after vetting every address its host resolves to, at each attempt."""

    def __init__(self, resolver: Callable[[str], list[str]] = resolve, transport=_send):
        self.resolver, self.transport = resolver, transport

    def post(self, url: str, body: bytes, headers: dict) -> Outcome:
        host = urlsplit(url).hostname or ""
        try:
            check_url(url)
            addresses = self.resolver(host)
        except Refused as refused:
            return Outcome(None, error=str(refused))
        except OSError:
            return Outcome(None, error="The host name could not be resolved.")
        if not addresses or not all(public(a) for a in addresses):
            return Outcome(None, error="That address is not reachable from the internet.")
        try:
            status, answer_headers, answer = _within(
                TIMEOUT, self.transport, url, addresses[0], body, headers
            )
        except (httpcore.TimeoutException, TimeoutError):
            return Outcome(None, error=f"No answer within {int(TIMEOUT)} seconds.")
        except (httpcore.ConnectError, ConnectionError):
            return Outcome(None, error="The connection was refused or could not be made.")
        except (httpcore.NetworkError, httpcore.ProtocolError, OSError):
            return Outcome(None, error="The connection failed (network or TLS).")
        retry_after = answer_headers.get("retry-after")
        return Outcome(status, answer, int(retry_after) if (retry_after or "").isdigit() else None)


# ---------- signing ----------
def signature_headers(
    body: bytes, keys: list[tuple[str, str]], delivery_id: str, timestamp: int | None = None
) -> dict:
    stamp = int(timestamp if timestamp is not None else time.time())
    signed = f"{stamp}.".encode() + body
    return {
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
        "Open-Alvary-Delivery": delivery_id,
        "Open-Alvary-Timestamp": str(stamp),
        "Open-Alvary-Key": ",".join(kid for kid, _ in keys),
        "Open-Alvary-Signature": ",".join(
            "v1=" + hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest() for _, secret in keys
        ),
    }


def verify_signature(secret: str, body: bytes, timestamp: str, signature: str, tolerance: int = 300) -> bool:
    """What a receiver does (published in the developer docs): constant-time, with a time window."""
    if abs(time.time() - int(timestamp)) > tolerance:
        return False
    expected = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    return any(
        hmac.compare_digest(expected, part.strip().removeprefix("v1=")) for part in signature.split(",")
    )


def svix_valid(secret: str | None, headers, body: bytes, tolerance: int = 300) -> bool:
    """Inbound events signed the Svix way (Resend): base64 HMAC-SHA256 of "id.timestamp.body" with
    the base64 key after "whsec_", one or more "v1,<signature>" values, within five minutes."""
    if not secret or not secret.startswith("whsec_"):
        return False
    ident, stamp, given = headers.get("svix-id"), headers.get("svix-timestamp"), headers.get("svix-signature")
    if (
        not ident
        or not stamp
        or not given
        or not stamp.isdigit()
        or abs(time.time() - int(stamp)) > tolerance
    ):
        return False
    try:
        key = base64.b64decode(secret.removeprefix("whsec_"))
    except ValueError:
        return False
    expected = base64.b64encode(
        hmac.new(key, f"{ident}.{stamp}.".encode() + body, hashlib.sha256).digest()
    ).decode()
    return any(
        hmac.compare_digest(expected, part.split(",", 1)[1])
        for part in given.split()
        if part.startswith("v1,")
    )


def _envelope(kind: str, delivery_id: str, **fields) -> bytes:
    body = {
        "type": kind,
        "payload_version": 1,
        "delivery_id": delivery_id,
        "attempted_at": _now_text(),
    } | fields
    return json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()


def _now_text():
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


# ---------- endpoints (console) ----------
MANAGERS = ("owner", "admin")


class Webhooks:
    def __init__(self, accounts: Accounts, sender: Sender | None = None, sealer: Sealer | None = None):
        self.accounts, self.engine = accounts, accounts.engine
        self.sender = sender or Sender()
        self._sealer = sealer

    @property
    def sealer(self) -> Sealer:
        if self._sealer is None:
            self._sealer = Sealer.from_environment()
        if self._sealer is None:
            raise AccountError(
                "temporarily_unavailable", 503, "Webhooks are not configured on this server yet."
            )
        return self._sealer

    def _t(self, conn, name):
        return self.accounts._t(conn, name)

    def _membership(self, conn, person_id, organisation_id, lock=False):
        membership = self.accounts._membership(conn, person_id, organisation_id, lock=lock)
        if membership["role"] not in (*MANAGERS, "developer"):
            raise AccountError("role_forbidden", 403, "Owners, admins and developers manage webhooks.")
        return membership

    def _endpoint(self, conn, person_id, organisation_id, endpoint_id, *, lock=False):
        membership = self._membership(conn, person_id, organisation_id)
        if not valid(endpoint_id, "whe"):
            raise AccountError("not_found", 404, "No such webhook endpoint.")
        row = (
            conn.execute(
                text(
                    f"SELECT * FROM {self._t(conn, 'webhook_endpoint')} WHERE id = :e AND organisation_id = :o"
                    + (" FOR UPDATE" if lock else "")
                ),
                {"e": endpoint_id, "o": organisation_id},
            )
            .mappings()
            .first()
        )
        if row is None or (membership["role"] not in MANAGERS and row["created_by"] != person_id):
            raise AccountError("not_found", 404, "No such webhook endpoint.")
        return dict(row)

    def _view(self, conn, row, person_id) -> dict:
        keys = conn.execute(
            text(
                f"SELECT kid, created_at, retires_at FROM {self._t(conn, 'webhook_key')} WHERE endpoint_id = :e"
                " ORDER BY created_at"
            ),
            {"e": row["id"]},
        ).mappings()
        watches = conn.scalars(
            text(
                f"SELECT watch_id FROM {self._t(conn, 'webhook_endpoint_watch')} WHERE endpoint_id = :e ORDER BY 1"
            ),
            {"e": row["id"]},
        ).all()
        last = (
            conn.execute(
                text(
                    f"SELECT state, last_status, created_at FROM {self._t(conn, 'webhook_delivery')}"
                    " WHERE endpoint_id = :e ORDER BY created_at DESC LIMIT 1"
                ),
                {"e": row["id"]},
            )
            .mappings()
            .first()
        )
        return {
            "id": row["id"],
            "revision": row["revision"],
            "url": row["url"],
            "description": row["description"],
            "state": row["state"],
            "watch_ids": list(watches),
            "signing_keys": [dict(k) for k in keys],
            "failure": {
                "consecutive_failures": row["consecutive_failures"],
                "first_failure_at": row["first_failure_at"],
                "last_error": row["last_error"],
            },
            "last_delivery": dict(last) if last else None,
            "mine": row["created_by"] == person_id,
            "created_at": row["created_at"],
            "verified_at": row["verified_at"],
        }

    def listing(self, person_id: str, organisation_id: str) -> dict:
        with self.engine.connect() as conn:
            membership = self._membership(conn, person_id, organisation_id)
            rows = conn.execute(
                text(
                    f"SELECT * FROM {self._t(conn, 'webhook_endpoint')} WHERE organisation_id = :o"
                    + ("" if membership["role"] in MANAGERS else " AND created_by = :p")
                    + " ORDER BY created_at"
                ),
                {"o": organisation_id, "p": person_id},
            ).mappings()
            endpoints = [self._view(conn, dict(r), person_id) for r in rows]
            used = conn.scalar(
                text(f"SELECT count(*) FROM {self._t(conn, 'webhook_endpoint')} WHERE organisation_id = :o"),
                {"o": organisation_id},
            )
            allowed = self._allowance(conn, organisation_id)
            return {"endpoints": endpoints, "limit": allowed, "used": used}

    def _allowance(self, conn, organisation_id) -> int:
        access = self.accounts.access(organisation_id, conn)
        if plan_limits(access)["watches"] == 0:
            return 0
        return int(((access.get("tier") or {}).get("allowance") or {}).get("webhook_endpoints", 0))

    def _watch_ids(self, conn, person_id, organisation_id, watch_ids) -> list[str]:
        if not isinstance(watch_ids, list) or not watch_ids or len(watch_ids) > 50:
            raise AccountError("invalid_input", 400, "Choose the monitors this endpoint receives.")
        found = conn.scalars(
            text(
                f"SELECT id FROM {self._t(conn, 'watch')} WHERE id = ANY(:ids) AND organisation_id = :o"
                " AND (owner_person_id = :p OR visibility = 'organisation')"
            ),
            {"ids": [w for w in watch_ids if isinstance(w, str)], "o": organisation_id, "p": person_id},
        ).all()
        if set(found) != set(watch_ids):
            raise AccountError("invalid_input", 400, "One of those monitors isn't available.")
        return sorted(found)

    def create(self, person_id: str, organisation_id: str, data: dict, context=None) -> dict:
        try:
            url = check_url(data.get("url"))
        except Refused as refused:
            raise AccountError("invalid_input", 400, str(refused)) from None
        description = " ".join(str(data.get("description") or "").split())[:200] or None
        secret = "whsec_" + base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")
        sealed = self.sealer.seal(secret)
        with self.engine.begin() as conn:
            self._membership(conn, person_id, organisation_id, lock=True)
            allowed = self._allowance(conn, organisation_id)
            used = conn.scalar(
                text(f"SELECT count(*) FROM {self._t(conn, 'webhook_endpoint')} WHERE organisation_id = :o"),
                {"o": organisation_id},
            )
            if used >= allowed:
                raise AccountError(
                    "limit_reached",
                    409,
                    "Webhooks come with paid plans. See Plan and limits."
                    if not allowed
                    else f"Your plan includes {allowed} webhook endpoints.",
                )
            watches = self._watch_ids(conn, person_id, organisation_id, data.get("watch_ids"))
            endpoint_id = new_id("whe")
            conn.execute(
                text(
                    f"INSERT INTO {self._t(conn, 'webhook_endpoint')} (id, organisation_id, created_by, url, description)"
                    " VALUES (:id, :o, :p, :url, :d)"
                ),
                {"id": endpoint_id, "o": organisation_id, "p": person_id, "url": url, "d": description},
            )
            conn.execute(
                text(
                    f"INSERT INTO {self._t(conn, 'webhook_key')} (endpoint_id, kid, secret_sealed) VALUES (:e, '1', :s)"
                ),
                {"e": endpoint_id, "s": sealed},
            )
            self._attach(conn, endpoint_id, watches)
            self.accounts._audit(
                conn,
                self.accounts._actor(conn, person_id),
                "webhook.created",
                organisation=organisation_id,
                target=("webhook_endpoint", endpoint_id),
                context=context,
            )
        verification = self.verify(person_id, organisation_id, endpoint_id)
        return {
            "endpoint": verification["endpoint"],
            "secret": secret,
            "verification": verification["result"],
        }

    def _attach(self, conn, endpoint_id, watches):
        conn.execute(
            text(f"DELETE FROM {self._t(conn, 'webhook_endpoint_watch')} WHERE endpoint_id = :e"),
            {"e": endpoint_id},
        )
        for watch_id in watches:
            conn.execute(
                text(
                    f"INSERT INTO {self._t(conn, 'webhook_endpoint_watch')} (endpoint_id, watch_id) VALUES (:e, :w)"
                ),
                {"e": endpoint_id, "w": watch_id},
            )

    def update(self, person_id, organisation_id, endpoint_id, data, context=None) -> dict:
        reverify = False
        with self.engine.begin() as conn:
            row = self._endpoint(conn, person_id, organisation_id, endpoint_id, lock=True)
            if data.get("revision") is not None and data["revision"] != row["revision"]:
                raise AccountError(
                    "revision_conflict", 409, "This endpoint changed meanwhile. Reload and try again."
                )
            changes = {}
            if "url" in data:
                try:
                    url = check_url(data["url"])
                except Refused as refused:
                    raise AccountError("invalid_input", 400, str(refused)) from None
                if url != row["url"]:
                    changes |= {"url": url, "state": "pending_verification", "verified_at": None}
                    reverify = True
            if "description" in data:
                changes["description"] = " ".join(str(data["description"] or "").split())[:200] or None
            if "watch_ids" in data:
                self._attach(
                    conn, endpoint_id, self._watch_ids(conn, person_id, organisation_id, data["watch_ids"])
                )
            if changes:
                assignments = ", ".join(f"{k} = :{k}" for k in changes)
                conn.execute(
                    text(
                        f"UPDATE {self._t(conn, 'webhook_endpoint')} SET {assignments}, revision = revision + 1,"
                        " updated_at = statement_timestamp() WHERE id = :id"
                    ),
                    changes | {"id": endpoint_id},
                )
            self.accounts._audit(
                conn,
                self.accounts._actor(conn, person_id),
                "webhook.updated",
                organisation=organisation_id,
                target=("webhook_endpoint", endpoint_id),
                context=context,
            )
        if reverify:
            return self.verify(person_id, organisation_id, endpoint_id)["endpoint"]
        return self.get(person_id, organisation_id, endpoint_id)

    def get(self, person_id, organisation_id, endpoint_id) -> dict:
        with self.engine.connect() as conn:
            return self._view(conn, self._endpoint(conn, person_id, organisation_id, endpoint_id), person_id)

    def delete(self, person_id, organisation_id, endpoint_id, context=None) -> None:
        with self.engine.begin() as conn:
            self._endpoint(conn, person_id, organisation_id, endpoint_id, lock=True)
            conn.execute(
                text(f"DELETE FROM {self._t(conn, 'webhook_endpoint')} WHERE id = :e"), {"e": endpoint_id}
            )
            self.accounts._audit(
                conn,
                self.accounts._actor(conn, person_id),
                "webhook.deleted",
                organisation=organisation_id,
                target=("webhook_endpoint", endpoint_id),
                context=context,
            )

    def rotate(self, person_id, organisation_id, endpoint_id, context=None) -> dict:
        """A new secret, shown once; the old one keeps signing for 24 hours beside it."""
        secret = "whsec_" + base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")
        sealed = self.sealer.seal(secret)
        with self.engine.begin() as conn:
            self._endpoint(conn, person_id, organisation_id, endpoint_id, lock=True)
            keys = self._t(conn, "webhook_key")
            conn.execute(
                text(f"DELETE FROM {keys} WHERE endpoint_id = :e AND retires_at IS NOT NULL"),
                {"e": endpoint_id},
            )
            kid = conn.scalar(
                text(f"SELECT max(kid::int) FROM {keys} WHERE endpoint_id = :e"), {"e": endpoint_id}
            )
            conn.execute(
                text(f"UPDATE {keys} SET retires_at = :r WHERE endpoint_id = :e AND retires_at IS NULL"),
                {"e": endpoint_id, "r": datetime.now(UTC) + OVERLAP},
            )
            conn.execute(
                text(f"INSERT INTO {keys} (endpoint_id, kid, secret_sealed) VALUES (:e, :k, :s)"),
                {"e": endpoint_id, "k": str((kid or 0) + 1), "s": sealed},
            )
            self.accounts._audit(
                conn,
                self.accounts._actor(conn, person_id),
                "webhook.rotated",
                organisation=organisation_id,
                target=("webhook_endpoint", endpoint_id),
                context=context,
            )
            return {
                "endpoint": self._view(
                    conn, self._endpoint(conn, person_id, organisation_id, endpoint_id), person_id
                ),
                "secret": secret,
            }

    def _keys(self, conn, endpoint_id) -> list[tuple[str, str]]:
        rows = conn.execute(
            text(
                f"SELECT kid, secret_sealed FROM {self._t(conn, 'webhook_key')} WHERE endpoint_id = :e"
                " AND (retires_at IS NULL OR retires_at > statement_timestamp()) ORDER BY kid::int DESC"
            ),
            {"e": endpoint_id},
        ).all()
        return [(kid, self.sealer.open(sealed)) for kid, sealed in rows]

    def _limited(self, conn, row, column_start, column_count, per_hour, message):
        now = datetime.now(UTC)
        start, count = row[column_start], row[column_count]
        if start is None or now - start > timedelta(hours=1):
            start, count = now, 0
        if count >= per_hour:
            raise AccountError("rate_limited", 429, message)
        conn.execute(
            text(
                f"UPDATE {self._t(conn, 'webhook_endpoint')} SET {column_start} = :s, {column_count} = :c WHERE id = :e"
            ),
            {"s": start, "c": count + 1, "e": row["id"]},
        )

    def verify(self, person_id, organisation_id, endpoint_id) -> dict:
        """The challenge handshake: the endpoint must answer 2xx with the same challenge."""
        with self.engine.begin() as conn:
            row = self._endpoint(conn, person_id, organisation_id, endpoint_id, lock=True)
            self._limited(
                conn,
                row,
                "verification_window_start",
                "verification_attempts",
                VERIFY_PER_HOUR,
                "Up to 5 verification attempts an hour. Try again later.",
            )
            keys = self._keys(conn, endpoint_id)
        challenge = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")
        delivery = "verification"
        body = _envelope("endpoint.verification", delivery, endpoint_id=endpoint_id, challenge=challenge)
        outcome = self.sender.post(row["url"], body, signature_headers(body, keys, delivery))
        answered = None
        if outcome.status is not None and 200 <= outcome.status < 300:
            try:
                answered = json.loads(outcome.body).get("challenge")
            except (ValueError, AttributeError):
                answered = None
        ok = answered == challenge
        if ok:
            error = None
        elif outcome.status is not None and 200 <= outcome.status < 300:
            error = 'The endpoint answered, but its body was not {"challenge": "<the challenge we sent>"}.'
        else:
            error = outcome.error or _status_error(outcome.status)
        with self.engine.begin() as conn:
            if ok:
                conn.execute(
                    text(
                        f"UPDATE {self._t(conn, 'webhook_endpoint')} SET state = 'active', verified_at ="
                        " statement_timestamp(), consecutive_failures = 0, first_failure_at = NULL, last_error = NULL,"
                        " revision = revision + 1 WHERE id = :e"
                    ),
                    {"e": endpoint_id},
                )
            else:
                conn.execute(
                    text(f"UPDATE {self._t(conn, 'webhook_endpoint')} SET last_error = :err WHERE id = :e"),
                    {"e": endpoint_id, "err": error[:300]},
                )
            view = self._view(conn, self._endpoint(conn, person_id, organisation_id, endpoint_id), person_id)
        return {"endpoint": view, "result": {"verified": ok, "status": outcome.status, "error": error}}

    def test(self, person_id, organisation_id, endpoint_id) -> dict:
        """A signed endpoint.test event, sent now; nothing is retried."""
        with self.engine.connect() as conn:
            row = self._endpoint(conn, person_id, organisation_id, endpoint_id)
            keys = self._keys(conn, endpoint_id)
        delivery = "test_" + secrets.token_hex(8)
        body = _envelope("endpoint.test", delivery, endpoint_id=endpoint_id)
        outcome = self.sender.post(row["url"], body, signature_headers(body, keys, delivery))
        ok = outcome.status is not None and 200 <= outcome.status < 300
        return {
            "delivered": ok,
            "status": outcome.status,
            "error": None if ok else (outcome.error or _status_error(outcome.status)),
        }

    def deliveries(self, person_id, organisation_id, endpoint_id) -> dict:
        with self.engine.connect() as conn:
            self._endpoint(conn, person_id, organisation_id, endpoint_id)
            events = self.accounts.namespace.qualified(conn, "changes", "change_event")
            rows = conn.execute(
                text(
                    f"SELECT d.id, d.event_id, d.event_revision, d.state, d.attempts, d.last_status, d.last_error,"
                    f" d.replay_of, d.created_at, d.finished_at, d.next_attempt_at, e.event_type"
                    f" FROM {self._t(conn, 'webhook_delivery')} d LEFT JOIN {events} e ON e.event_id = d.event_id"
                    " AND e.event_revision = d.event_revision WHERE d.endpoint_id = :e ORDER BY d.created_at DESC LIMIT 50"
                ),
                {"e": endpoint_id},
            ).mappings()
            return {"deliveries": [dict(r) | {"event_id": str(r["event_id"])} for r in rows]}

    def replay(self, person_id, organisation_id, endpoint_id, delivery_id) -> dict:
        """A new delivery of the same event; current eligibility applies when it is sent."""
        with self.engine.begin() as conn:
            row = self._endpoint(conn, person_id, organisation_id, endpoint_id, lock=True)
            if not valid(delivery_id, "whd"):
                raise AccountError("not_found", 404, "No such delivery.")
            original = (
                conn.execute(
                    text(
                        f"SELECT * FROM {self._t(conn, 'webhook_delivery')} WHERE id = :d AND endpoint_id = :e"
                    ),
                    {"d": delivery_id, "e": endpoint_id},
                )
                .mappings()
                .first()
            )
            if original is None:
                raise AccountError("not_found", 404, "No such delivery.")
            self._limited(
                conn, row, "replay_window_start", "replays", REPLAYS_PER_HOUR, "Up to 10 replays an hour."
            )
            replay_id = new_id("whd")
            conn.execute(
                text(
                    f"INSERT INTO {self._t(conn, 'webhook_delivery')} (id, endpoint_id, event_id, event_revision,"
                    " watch_ids, replay_of) VALUES (:id, :e, :ev, :v, :w, :of)"
                ),
                {
                    "id": replay_id,
                    "e": endpoint_id,
                    "ev": original["event_id"],
                    "v": original["event_revision"],
                    "w": original["watch_ids"],
                    "of": delivery_id,
                },
            )
            return {"id": replay_id}


def _status_error(status, fallback="The endpoint did not accept the delivery.") -> str:
    if status is None:
        return fallback
    if 300 <= status < 400:
        return f"Redirects are not followed (HTTP {status})."
    return f"The endpoint answered HTTP {status}."


# ---------- the worker's delivery step ----------
def enqueue(conn, accounts: Accounts, watch_id: str, event: dict, endpoints: dict[str, list[str]]) -> None:
    """Called by the matcher for each match: one delivery per endpoint per event revision."""
    for endpoint_id in endpoints.get(watch_id, []):
        conn.execute(
            text(
                f"INSERT INTO {accounts._t(conn, 'webhook_delivery')} AS d (id, endpoint_id, event_id, event_revision,"
                " watch_ids) VALUES (:id, :e, :ev, :v, ARRAY[:w]) ON CONFLICT (endpoint_id, event_id, event_revision)"
                " WHERE replay_of IS NULL DO UPDATE SET watch_ids = ARRAY(SELECT DISTINCT unnest(d.watch_ids ||"
                " EXCLUDED.watch_ids) ORDER BY 1)"
            ),
            {
                "id": new_id("whd"),
                "e": endpoint_id,
                "ev": event["event_id"],
                "v": event["event_revision"],
                "w": watch_id,
            },
        )


def attached(conn, accounts: Accounts) -> dict[str, list[str]]:
    """Monitor ID -> the endpoints that receive its matches (active or failing ones)."""
    found: dict[str, list[str]] = {}
    rows = conn.execute(
        text(
            f"SELECT w.watch_id, w.endpoint_id FROM {accounts._t(conn, 'webhook_endpoint_watch')} w"
            f" JOIN {accounts._t(conn, 'webhook_endpoint')} e ON e.id = w.endpoint_id"
            " WHERE e.state IN ('active', 'failing')"
        )
    )
    for watch_id, endpoint_id in rows:
        found.setdefault(watch_id, []).append(endpoint_id)
    return found


def deliver(
    accounts: Accounts, hooks: Webhooks, mailer, base: str, now: datetime | None = None, limit: int = 100
) -> dict:
    """Attempt due deliveries. Each is claimed in a short transaction, sent outside it, then
    recorded, so a slow endpoint holds no database locks."""
    now = now or datetime.now(UTC)
    counts = {"delivered": 0, "failed": 0, "withheld": 0, "abandoned": 0}
    with accounts.engine.begin() as conn:
        table = accounts._t(conn, "webhook_delivery")
        counts["abandoned"] = conn.execute(
            text(
                f"UPDATE {table} SET state = 'abandoned', finished_at = :now WHERE state = 'pending'"
                " AND created_at < :cut"
            ),
            {"now": now, "cut": now - TERMINAL},
        ).rowcount
        claimed = (
            conn.execute(
                text(
                    f"UPDATE {table} SET next_attempt_at = :lease WHERE id IN (SELECT d.id FROM {table} d"
                    f" JOIN {accounts._t(conn, 'webhook_endpoint')} e ON e.id = d.endpoint_id WHERE d.state = 'pending'"
                    " AND d.next_attempt_at <= :now AND e.state IN ('active', 'failing') ORDER BY d.next_attempt_at"
                    " LIMIT :n FOR UPDATE OF d SKIP LOCKED) RETURNING id"
                ),
                {"now": now, "lease": now + timedelta(minutes=15), "n": limit},
            )
            .scalars()
            .all()
        )
    for delivery_id in claimed:
        result = _attempt(accounts, hooks, mailer, base, delivery_id, now)
        counts[result] = counts.get(result, 0) + 1
    return counts


def _attempt(accounts, hooks, mailer, base, delivery_id, now) -> str:
    with accounts.engine.connect() as conn:
        row = dict(
            conn.execute(
                text(
                    f"SELECT d.*, e.url, e.organisation_id, e.created_by FROM {accounts._t(conn, 'webhook_delivery')} d"
                    f" JOIN {accounts._t(conn, 'webhook_endpoint')} e ON e.id = d.endpoint_id WHERE d.id = :d"
                ),
                {"d": delivery_id},
            )
            .mappings()
            .one()
        )
        events = accounts.namespace.qualified(conn, "changes", "change_event")
        source = (
            conn.execute(
                text(f"SELECT * FROM {events} WHERE event_id = :e AND event_revision = :v"),
                {"e": row["event_id"], "v": row["event_revision"]},
            )
            .mappings()
            .first()
        )
        event = Feed(conn, None, base).project(dict(source), _Visibility(conn)) if source else None
        keys = hooks._keys(conn, row["endpoint_id"]) if event else []
    if event is None:  # no longer public: nothing is sent (SUB-06 reauthorisation)
        _finish(accounts, delivery_id, "withheld", None, None, now)
        return "withheld"
    body = _envelope("change", delivery_id, watch_ids=row["watch_ids"], event=event)
    if len(body) > MAX_BODY:
        slim = {
            k: event[k] for k in ("event_id", "event_revision", "event_type", "feed_epoch", "committed_at")
        }
        body = _envelope("change", delivery_id, watch_ids=row["watch_ids"], event=slim, truncated=True)
    outcome = hooks.sender.post(row["url"], body, signature_headers(body, keys, delivery_id))
    attempts = row["attempts"] + 1
    status = outcome.status
    if status is not None and 200 <= status < 300:
        _finish(accounts, delivery_id, "delivered", status, None, now, attempts)
        _endpoint_succeeded(accounts, row["endpoint_id"])
        return "delivered"
    error = outcome.error or _status_error(status)
    if status == 410:
        _finish(
            accounts,
            delivery_id,
            "rejected",
            status,
            "The endpoint answered 410 Gone: it was disabled.",
            now,
            attempts,
        )
        _endpoint_state(accounts, mailer, base, row, "disabled", "answered 410 Gone")
        return "failed"
    if status is not None and 400 <= status < 500 and status != 429:
        _finish(accounts, delivery_id, "rejected", status, error, now, attempts)
        _endpoint_failed(accounts, mailer, base, row, error, now)
        return "failed"
    if attempts > len(RETRIES) or now - row["created_at"] >= TERMINAL:
        _finish(accounts, delivery_id, "abandoned", status, error, now, attempts)
    else:
        delay = RETRIES[attempts - 1]
        if status == 429 and outcome.retry_after:
            delay = max(delay, min(outcome.retry_after, RETRIES[-1]))
        with accounts.engine.begin() as conn:
            conn.execute(
                text(
                    f"UPDATE {accounts._t(conn, 'webhook_delivery')} SET attempts = :a, last_status = :s, last_error = :err,"
                    " next_attempt_at = :next WHERE id = :d"
                ),
                {
                    "a": attempts,
                    "s": status,
                    "err": error[:300],
                    "next": now + timedelta(seconds=delay),
                    "d": delivery_id,
                },
            )
    _endpoint_failed(accounts, mailer, base, row, error, now)
    return "failed"


def _finish(accounts, delivery_id, state, status, error, now, attempts=None):
    with accounts.engine.begin() as conn:
        conn.execute(
            text(
                f"UPDATE {accounts._t(conn, 'webhook_delivery')} SET state = :st, last_status = :s, last_error = :err,"
                " finished_at = :now, attempts = coalesce(:a, attempts) WHERE id = :d"
            ),
            {
                "st": state,
                "s": status,
                "err": error and error[:300],
                "now": now,
                "a": attempts,
                "d": delivery_id,
            },
        )


def _endpoint_succeeded(accounts, endpoint_id):
    with accounts.engine.begin() as conn:
        conn.execute(
            text(
                f"UPDATE {accounts._t(conn, 'webhook_endpoint')} SET consecutive_failures = 0, first_failure_at = NULL,"
                " last_error = NULL, state = CASE WHEN state = 'failing' THEN 'active' ELSE state END WHERE id = :e"
            ),
            {"e": endpoint_id},
        )


def _endpoint_failed(accounts, mailer, base, row, error, now):
    with accounts.engine.begin() as conn:
        updated = (
            conn.execute(
                text(
                    f"UPDATE {accounts._t(conn, 'webhook_endpoint')} SET consecutive_failures = consecutive_failures + 1,"
                    " first_failure_at = coalesce(first_failure_at, :now), last_error = :err WHERE id = :e"
                    " RETURNING state, consecutive_failures, first_failure_at"
                ),
                {"e": row["endpoint_id"], "now": now, "err": error[:300]},
            )
            .mappings()
            .one()
        )
    if updated["state"] not in ("active", "failing"):
        return
    if updated["consecutive_failures"] >= PAUSE_AFTER or now - updated["first_failure_at"] >= TERMINAL:
        _endpoint_state(accounts, mailer, base, row, "paused", "kept failing")
    elif updated["consecutive_failures"] >= FAILING_AFTER and updated["state"] == "active":
        with accounts.engine.begin() as conn:
            conn.execute(
                text(f"UPDATE {accounts._t(conn, 'webhook_endpoint')} SET state = 'failing' WHERE id = :e"),
                {"e": row["endpoint_id"]},
            )


def _endpoint_state(accounts, mailer, base, row, state, why):
    with accounts.engine.begin() as conn:
        conn.execute(
            text(
                f"UPDATE {accounts._t(conn, 'webhook_endpoint')} SET state = :s, revision = revision + 1,"
                " updated_at = statement_timestamp() WHERE id = :e"
            ),
            {"s": state, "e": row["endpoint_id"]},
        )
        accounts._audit(
            conn,
            ("system", "webhooks"),
            f"webhook.{state}",
            organisation=row["organisation_id"],
            target=("webhook_endpoint", row["endpoint_id"]),
        )
    notice = accounts.notice_recipients(row["organisation_id"])
    for to in notice["to"]:
        mailer.send(mail.webhook_stopped(to, notice["organisation"], row["url"], state, why, base))
