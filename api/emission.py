"""Final policy check immediately before emission, with optional BUD byte charging.

A response is fully buffered (bounded) before it is sent. Immediately before the first byte is
handed to transport, ``recheck`` runs in its own fresh database transaction; if the looked-up
content is no longer permitted, nothing is sent. Bytes already handed to transport cannot be
recalled: a withdrawal that commits after this check takes effect on the next request.

When byte limits are configured, the order is: reserve (charged) → recheck → begin emission
(durable) → send → settle with the payload length as receipt. A denied recheck releases the
unsent reservation; a failed send stays charged until reconciliation.
"""

import json
from collections.abc import Callable
from uuid import uuid4

from anyio import to_thread
from starlette.responses import FileResponse, Response

from rights.bytes import ByteLedger, LedgerConflict, LedgerUnavailable, Scope

MAX_SINGLE_RESPONSE_BYTES = 10 * 1024 * 1024


class Withdrawn(RuntimeError):
    """The recheck found the content no longer permitted."""


class CheckedResponse(Response):
    media_type = "application/json"

    def __init__(
        self,
        payload: bytes,
        recheck: Callable[[], bool],
        *,
        ledger: ByteLedger | None = None,
        scopes: list[Scope] | None = None,
        lease_seconds: float | None = None,
        charged: bool = False,
        refusal: Callable[[int, str], Response] | None = None,
    ):
        if len(payload) > MAX_SINGLE_RESPONSE_BYTES:
            raise ValueError("Response exceeds the bounded single-response profile")
        super().__init__(content=payload, media_type=self.media_type)
        self.payload = payload
        self.recheck = recheck
        self.ledger = ledger
        self.scopes = scopes or []
        self.lease_seconds = lease_seconds
        self.charged = charged
        self.refusal = refusal  # the interface's own refusal shape (REST v1: a problem document)

    async def _refuse(self, scope, receive, send, status, detail):
        if self.refusal is not None:
            return await self.refusal(status, detail)(scope, receive, send)
        body = json.dumps({"detail": detail}).encode()
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope, receive, send):
        reservation = None
        if self.charged:
            if self.ledger is None or not self.scopes or not self.lease_seconds:
                # Missing byte limits block text delivery rather than defaulting an allowance.
                return await self._refuse(scope, receive, send, 503, "Byte limits are not configured")
            try:
                result = await to_thread.run_sync(
                    lambda: self.ledger.reserve(uuid4(), len(self.payload), self.scopes, self.lease_seconds)
                )
            except LedgerUnavailable:
                return await self._refuse(scope, receive, send, 503, "Quota store unavailable")
            if not result.reserved:
                global_only = all(key.startswith("global:") for key in result.blocked)
                status, detail = (503, "capacity_exhausted") if global_only else (429, "Byte limit exceeded")
                body = json.dumps({"detail": detail, "retry_after_seconds": result.retry_after}).encode()
                await send(
                    {
                        "type": "http.response.start",
                        "status": status,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"retry-after", str(result.retry_after).encode()),
                        ],
                    }
                )
                return await send({"type": "http.response.body", "body": body})
            reservation = result.reservation_id
        try:
            permitted = await to_thread.run_sync(self.recheck)
        except Exception:
            permitted = None
        if not permitted:
            if reservation is not None:
                try:
                    await to_thread.run_sync(self.ledger.release, reservation)
                except (LedgerUnavailable, LedgerConflict):
                    pass  # unsent reservation expires and is released by reconciliation
            if permitted is None:
                return await self._refuse(scope, receive, send, 503, "Catalogue authority unavailable")
            return await self._refuse(scope, receive, send, 409, "Publication changed; retry the request")
        if reservation is not None:
            try:
                started = await to_thread.run_sync(self.ledger.begin_emission, reservation)
            except (LedgerUnavailable, LedgerConflict):
                started = False
            if not started:
                return await self._refuse(scope, receive, send, 503, "Delivery lease unavailable")
        await super().__call__(scope, receive, send)
        if reservation is not None:
            # Receipt: the full uncompressed payload was accepted by the upstream transport.
            receipt = "content-length:" + str(len(self.payload))
            try:
                await to_thread.run_sync(self.ledger.settle, reservation, len(self.payload), receipt)
            except (LedgerUnavailable, LedgerConflict):
                pass  # remains emitting; reconciliation settles conservatively


class CheckedText(CheckedResponse):
    """A canonical text as text/plain; charset=utf-8, rechecked the same way (API-02 /versions/{id}/text)."""

    media_type = "text/plain; charset=utf-8"


class CheckedFileResponse(FileResponse):
    """A release file: recheck availability before sending. It charges no bytes itself; behind the
    credential gate (REST v1's /bulk), the gate charges them to the organisation's data a day."""

    def __init__(
        self,
        path,
        recheck: Callable[[], bool],
        refusal: Callable[[int, str], Response] | None = None,
        **kwargs,
    ):
        super().__init__(path, **kwargs)
        self.recheck = recheck
        self.refusal = refusal  # the interface's own refusal shape (REST v1: a problem document)

    async def __call__(self, scope, receive, send):
        try:
            permitted = await to_thread.run_sync(self.recheck)
        except Exception:
            permitted = None
        if not permitted:
            status, detail = (
                (503, "Catalogue authority unavailable")
                if permitted is None
                else (
                    410,
                    "Release withdrawn or invalid; consult the latest release",
                )
            )
            if self.refusal is not None:
                return await self.refusal(status, detail)(scope, receive, send)
            body = json.dumps({"detail": detail}).encode()
            await send(
                {
                    "type": "http.response.start",
                    "status": status,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode()),
                    ],
                }
            )
            return await send({"type": "http.response.body", "body": body})
        await super().__call__(scope, receive, send)
