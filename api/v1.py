"""REST v1 (MAC-06): the read routes for entitled organisations, at https://api.<domain>/api/v1.

The same response shapes as today's read routes (API-01–06), behind the credential gate
(api.credentials), limited to the jurisdictions the organisation's access covers. Public without a
credential, and open to any origin without credentials: the OpenAPI document, the quota policy and
the tier catalogue. Data routes send no CORS headers: keys never belong in a browser (N18).
"""

from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from api.catalogue import InvalidSearch, LegacyCatalogue, SourceUnavailable
from api.credentials import problem
from api.release_adapter import LegacyReleases
from api.repositories import LegacyRepository, PublicUnavailable, ResultTooLarge
from api.store import Store, check_legacy_authority
from schema.public import (
    CitationView,
    CoverageView,
    JurisdictionView,
    RightsView,
    SearchPage,
    SourcePage,
    SourceView,
    VersionPage,
)

PUBLIC_PATHS = frozenset({"/openapi.json", "/quota-policy", "/tiers"})


class OutOfScope(Exception):
    pass


class Unbounded(Exception):
    pass


def create_v1_app(store: Store, releases: Path, tiers: Callable[[], list[dict]]) -> FastAPI:
    app = FastAPI(
        title="Open Alvary API",
        version="1.0.0",
        description=(
            "Read-only access to the published Open Alvary corpus for organisations with access. "
            "Send an API key or access token as `Authorization: Bearer <credential>`."
        ),
        docs_url=None,
        redoc_url=None,
        openapi_url="/openapi.json",
    )

    @app.middleware("http")
    async def headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        path = request.url.path.removeprefix(request.scope.get("root_path", ""))
        if path in PUBLIC_PATHS:
            response.headers["Access-Control-Allow-Origin"] = "*"
        return response

    def request_id(request: Request) -> str:
        return getattr(request.state, "request_id", "-")

    @app.exception_handler(OutOfScope)
    async def out_of_scope(request, exception):
        return problem("jurisdiction_not_included", request_id(request))

    @app.exception_handler(Unbounded)
    async def unbounded(request, exception):
        return problem(
            "invalid_input",
            request_id(request),
            detail="Your access covers several jurisdictions: ask for one at a time with ?jurisdiction=.",
        )

    @app.exception_handler(SourceUnavailable)
    async def missing(request, exception):
        return JSONResponse({"detail": "Source not available", "code": "unavailable"}, status_code=404)

    @app.exception_handler(InvalidSearch)
    async def invalid_search(request, exception):
        return problem("invalid_input", request_id(request), detail=str(exception))

    @app.exception_handler(ResultTooLarge)
    async def too_large(request, exception):
        return JSONResponse(
            {"detail": "Result exceeds the bounded interface", "code": "result_too_large"}, status_code=413
        )

    @app.exception_handler(SQLAlchemyError)
    @app.exception_handler(ValidationError)
    @app.exception_handler(PublicUnavailable)
    async def unavailable(request, exception):
        return problem("temporarily_unavailable", request_id(request))

    @contextmanager
    def session():
        with store.engine.begin() as connection:
            check_legacy_authority(connection, write=False)
            yield LegacyCatalogue(LegacyRepository(connection))

    def catalogue():
        with session() as service:
            yield service

    def allowed(request: Request) -> set[str] | None:
        """The jurisdictions this request may reach; None means all."""
        scope = request.state.access["scope"]
        return None if scope.get("all") else set(scope.get("jurisdictions") or [])

    def within(request: Request, jurisdiction: str | None) -> str | None:
        """The jurisdiction filter to apply for a list or search."""
        reach = allowed(request)
        if reach is None:
            return jurisdiction
        if jurisdiction is not None:
            if jurisdiction not in reach:
                raise OutOfScope
            return jurisdiction
        if len(reach) == 1:
            return next(iter(reach))
        raise Unbounded

    def check_source(request: Request, service: LegacyCatalogue, source_id: str) -> SourceView:
        view = service.source(source_id)
        reach = allowed(request)
        if reach is not None and view.jurisdiction_id not in reach:
            raise OutOfScope
        return view

    @app.get("/quota-policy")
    def quota_policy():
        return {
            "enforcement": "organisation",
            "algorithm": "token_bucket",
            "grouping": "organisation",
            "note": (
                "Each organisation's access tier sets its requests a minute, burst and concurrent requests "
                "(see /tiers), shared by every key and app of the organisation across REST, MCP and bulk. "
                "One key or app may use at most half the organisation's concurrent requests. A refused "
                "request answers 429 with Retry-After."
            ),
        }

    @app.get("/tiers")
    def tier_catalogue():
        return {"tiers": tiers()}

    @app.get("/jurisdictions")
    def jurisdictions(
        request: Request, service: LegacyCatalogue = Depends(catalogue)
    ) -> tuple[JurisdictionView, ...]:
        reach = allowed(request)
        return tuple(j for j in service.jurisdictions() if reach is None or j.id in reach)

    @app.get("/sources")
    def sources(
        request: Request,
        jurisdiction: str | None = None,
        access: Literal["all", "open", "metadata"] = "all",
        limit: int = Query(50, ge=1, le=100),
        offset: int = Query(0, ge=0),
        service: LegacyCatalogue = Depends(catalogue),
    ) -> SourcePage:
        return service.sources(
            jurisdiction=within(request, jurisdiction), limit=limit, offset=offset, access=access
        )

    @app.get("/sources/{source_id}")
    def source(request: Request, source_id: str, service: LegacyCatalogue = Depends(catalogue)) -> SourceView:
        return check_source(request, service, source_id)

    @app.get("/sources/{source_id}/versions")
    def versions(
        request: Request, source_id: str, service: LegacyCatalogue = Depends(catalogue)
    ) -> VersionPage:
        check_source(request, service, source_id)
        return service.versions(source_id)

    @app.get("/search")
    def search(
        request: Request,
        q: str = Query(min_length=1, max_length=200),
        jurisdiction: str | None = None,
        access: Literal["all", "open", "metadata"] = "all",
        limit: int = Query(25, ge=1, le=100),
        offset: int = Query(0, ge=0),
        service: LegacyCatalogue = Depends(catalogue),
    ) -> SearchPage:
        return service.search(
            q, jurisdiction=within(request, jurisdiction), limit=limit, offset=offset, access=access
        )

    @app.get("/citations/{citation:path}")
    def citations(
        request: Request, citation: str, service: LegacyCatalogue = Depends(catalogue)
    ) -> CitationView:
        view = service.citations(citation)
        reach = allowed(request)
        if reach is None:
            return view
        matches = tuple(m for m in view.matches if m.jurisdiction_id in reach)
        status = (
            view.status
            if len(matches) == len(view.matches)
            else ("resolved" if len(matches) == 1 else "ambiguous" if matches else "unresolved")
        )
        return CitationView(status=status, matches=matches)

    @app.get("/coverage")
    def coverage(service: LegacyCatalogue = Depends(catalogue)) -> CoverageView:
        return service.coverage()

    @app.get("/rights/{source_id}")
    def rights(request: Request, source_id: str, service: LegacyCatalogue = Depends(catalogue)) -> RightsView:
        check_source(request, service, source_id)
        return service.rights(source_id)

    @app.get("/releases")
    def release_listing(request: Request):
        listing = LegacyReleases(store, releases).listing()
        reach = allowed(request)
        return listing if reach is None else [m for m in listing if m.get("jurisdiction") in reach]

    return app
