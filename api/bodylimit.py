"""A limit on every request body, applied as it is read (consolidated findings, item 8).

Without it a route that reads its body before any check (the console's consent decision, the mail
provider's events) would take in whatever was sent, up to the platform's own 32 MiB. A request whose
Content-Length is over its route's limit is refused before anything is read; one without it is read
only up to the limit, then refused with 413: as a problem document, request_too_large, when nothing
has been read, and in the app's own shape for a 413 when a route was reading it.
"""

import json
import uuid

from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse

# The largest body any route accepts, by path prefix, longest first; DEFAULT for the rest. A JSON-RPC
# call or a currentness check of 100 items is well under the default.
DEFAULT = 1024 * 1024
LIMITS = (
    ("/console/mail-events", 64 * 1024),
    ("/console/oauth/api/decision", 16 * 1024),
    ("/mcp", 32 * 1024),
)


def limit_for(path: str) -> int:
    for prefix, size in LIMITS:
        if path == prefix or path.startswith(prefix + "/"):
            return size
    return DEFAULT


def _problem(request_id: str, limit: int) -> tuple[dict, bytes]:
    body = json.dumps(
        {
            "type": "https://open.alvary.ai/docs/errors#request_too_large",
            "title": "Request refused",
            "status": 413,
            "detail": f"The request body is larger than this address accepts ({limit:,} bytes).",
            "code": "request_too_large",
            "retryable": False,
            "request_id": request_id,
        }
    ).encode()
    start = {
        "type": "http.response.start",
        "status": 413,
        "headers": [
            (b"content-type", b"application/problem+json"),
            (b"content-length", str(len(body)).encode()),
            (b"cache-control", b"no-store"),
            (b"x-request-id", request_id.encode()),
            (b"connection", b"close"),
        ],
    }
    return start, body


def _mcp_too_large_payload(request_id: str, limit: int = 32 * 1024) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": None,
        "error": {
            "code": 10005,
            "message": f"Request body exceeds {limit:,} bytes.",
            "data": {
                "code": "request_too_large",
                "retryable": False,
                "request_id": request_id,
                "schema_version": "1",
            },
        },
    }


def _mcp_problem(request_id: str, limit: int = 32 * 1024) -> tuple[dict, bytes]:
    body = json.dumps(_mcp_too_large_payload(request_id, limit)).encode()
    start = {
        "type": "http.response.start",
        "status": 413,
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
            (b"cache-control", b"no-store"),
            (b"x-request-id", request_id.encode()),
            (b"connection", b"close"),
        ],
    }
    return start, body


def mcp_too_large_response(request_id: str, limit: int = 32 * 1024) -> JSONResponse:
    return JSONResponse(
        _mcp_too_large_payload(request_id, limit),
        status_code=413,
        headers={
            "Cache-Control": "no-store",
            "X-Request-ID": request_id,
            "Connection": "close",
        },
    )


class TooLarge(HTTPException):
    def __init__(self, limit: int):
        super().__init__(413, f"The request body is larger than this address accepts ({limit:,} bytes).")
        self.limit = limit


class BodyLimit:
    def __init__(self, app, limit=limit_for):
        self.app, self.limit = app, limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        limit = self.limit(path)
        declared = [v for k, v in scope.get("headers", []) if k.lower() == b"content-length"]
        if declared:
            try:
                size = int(declared[0])
            except ValueError:
                size = limit + 1
            if size > limit or len(declared) > 1:
                request_id = (scope.get("state") or {}).get("request_id") or uuid.uuid4().hex[:16]
                return await self._refuse(send, limit, path, request_id)
        received = 0
        started = False

        async def counted():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise TooLarge(limit)
            return message

        async def watched(message):
            nonlocal started
            started = started or message["type"] == "http.response.start"
            await send(message)

        try:
            await self.app(scope, counted, watched)
        except TooLarge:
            # Raised where the app did not turn it into an answer itself (a route reading the body
            # outside its own exception handling).
            if not started:
                request_id = (scope.get("state") or {}).get("request_id") or uuid.uuid4().hex[:16]
                await self._refuse(send, limit, path, request_id)

    @staticmethod
    async def _refuse(send, limit: int, path: str = "", request_id: str | None = None):
        rid = request_id or uuid.uuid4().hex[:16]
        start, body = _mcp_problem(rid, limit) if path == "/mcp" else _problem(rid, limit)
        await send(start)
        await send({"type": "http.response.body", "body": body})
