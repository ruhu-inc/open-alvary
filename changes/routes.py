"""Change-feed routes, mounted on the public API, the keyed API and the website's data API
(changes-contracts.md C1). Every surface answers the same events; only admission differs."""

import os
from collections.abc import Callable
from contextlib import AbstractContextManager
from functools import cache

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy.engine import Connection

from changes.feed import Feed, FeedError, Filters, Tokens, atom, rss


def site_origin(request: Request) -> str:
    """The website's origin for links in events and feeds."""
    domain = os.getenv("PUBLIC_DOMAIN")
    return f"https://{domain}" if domain else f"{request.url.scheme}://{request.url.netloc}"


PARAMETERS = {"jurisdiction", "work", "type", "class", "language", "q", "issuer", "collection", "topic"}


@cache
def tokens() -> Tokens | None:
    return Tokens.from_environment()


def problem(error: FeedError, request: Request) -> JSONResponse:
    body = {
        "type": f"https://open.alvary.ai/docs/errors#{error.code}",
        "title": "Request refused",
        "status": error.status,
        "detail": error.detail,
        "code": error.code,
        "retryable": error.status in (429, 503),
    }
    request_id = getattr(request.state, "request_id", None)
    if request_id:
        body["request_id"] = request_id
    headers = {"Cache-Control": "no-store"}
    if error.retry_after:
        headers["Retry-After"] = str(error.retry_after)
    return JSONResponse(
        body, status_code=error.status, media_type="application/problem+json", headers=headers
    )


def _values(request: Request, allowed: set[str]) -> dict[str, list[str]]:
    values: dict[str, list[str]] = {}
    for key, value in request.query_params.multi_items():
        if key not in allowed:
            raise FeedError(400, "invalid_input", f"Unknown parameter: {key[:40]}.")
        values.setdefault(key, []).append(value)
    return values


def _one(values, name):
    given = values.pop(name, [])
    if len(given) > 1:
        raise FeedError(400, "invalid_input", f"Send {name} once.")
    return given[0] if given else None


def create_router(
    connect: Callable[[], AbstractContextManager[Connection]], origin: Callable[[Request], str] = site_origin
) -> APIRouter:
    router = APIRouter()

    def run(request: Request, work):
        try:
            with connect() as conn:
                return work(Feed(conn, tokens(), origin(request)))
        except FeedError as error:
            return problem(error, request)

    @router.get("/changes")
    def changes(request: Request):
        def work(feed: Feed):
            values = _values(request, PARAMETERS | {"limit", "cursor", "resume_token", "start"})
            limit = _one(values, "limit")
            cursor, resume, start = (
                _one(values, "cursor"),
                _one(values, "resume_token"),
                _one(values, "start"),
            )
            if start not in (None, "now"):
                raise FeedError(400, "invalid_input", "start accepts only now.")
            try:
                size = int(limit) if limit is not None else 25
            except ValueError:
                size = 0
            if not 1 <= size <= 100:
                raise FeedError(400, "invalid_input", "limit must be 1 to 100.")
            return feed.page(
                Filters.parse(values), size, cursor=cursor, resume=resume, start_now=start == "now"
            )

        return run(request, work)

    def window(request: Request, render, media):
        def work(feed: Feed):
            events = feed.recent(Filters.parse(_values(request, PARAMETERS)))
            body = render(events, str(request.url), origin(request))
            return Response(body, media_type=media, headers={"Cache-Control": "no-store"})

        return run(request, work)

    @router.get("/changes/recent")
    def recent(request: Request):
        """Newest first, the same window as the feeds, for pages that show the latest changes."""
        return run(request, lambda feed: {"events": feed.recent(Filters.parse(_values(request, PARAMETERS)))})

    @router.get("/changes/feed.atom")
    def feed_atom(request: Request):
        return window(request, atom, "application/atom+xml")

    @router.get("/changes/feed.rss")
    def feed_rss(request: Request):
        return window(request, rss, "application/rss+xml")

    @router.get("/changes/{event_id}")
    def change(request: Request, event_id: str):
        return run(request, lambda feed: (_values(request, set()), feed.event(event_id))[1])

    @router.get("/change-coverage")
    def coverage(request: Request):
        return run(request, lambda feed: (_values(request, set()), feed.coverage())[1])

    return router
