"""REST v1 (MAC-06): the read routes for entitled organisations, at https://api.<domain>/api/v1.

The same response shapes as today's read routes (API-01–06), behind the credential gate
(api.credentials). A plan's countries are read at its own limits and every other one at the Starter
rate (api.reach); a search or listing without a jurisdiction covers the plan's. Public without a
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

from api.admission import AdmissionUnavailable
from api.catalogue import InvalidSearch, LegacyCatalogue, SourceUnavailable
from api.credentials import problem
from api.reach import EVERYWHERE, OutsidePlanLimited, Reach
from api.release_adapter import LegacyReleases
from api.repositories import LegacyRepository, PublicUnavailable, ResultTooLarge
from api.store import Store, check_legacy_authority
from changes.routes import create_router as create_changes_router
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

    @app.exception_handler(OutsidePlanLimited)
    async def outside_plan(request, exception):
        return problem(
            "rate_limited", request_id(request), retry_after=exception.retry_after, detail=str(exception)
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
    @app.exception_handler(AdmissionUnavailable)
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

    def reach(request: Request) -> Reach:
        return getattr(request.state, "access", {}).get("reach", EVERYWHERE)

    def within(request: Request, service: LegacyCatalogue, jurisdiction: str | None):
        """The jurisdiction filter for a list or search: one named jurisdiction (outside the plan, at
        the Starter rate), or else the plan's."""
        plan = reach(request)
        if jurisdiction is not None:
            plan.admit(jurisdiction)
            return jurisdiction
        return plan.within(j.id for j in service.jurisdictions())

    def check_source(request: Request, service: LegacyCatalogue, source_id: str) -> SourceView:
        view = service.source(source_id)
        reach(request).admit(view.jurisdiction_id)
        return view

    @contextmanager
    def change_session():
        with store.engine.begin() as connection:
            check_legacy_authority(connection, write=False)
            yield connection

    # Changes are public: plan countries do not narrow them (changes-contracts.md).
    if store.engine.dialect.name == "postgresql":
        app.include_router(create_changes_router(change_session))

    @app.get("/quota-policy")
    def quota_policy():
        return {
            "enforcement": "organisation",
            "algorithm": "token_bucket",
            "grouping": "organisation",
            "note": (
                "Each organisation's access tier sets its requests a minute, burst and concurrent requests "
                "(see /tiers), shared by every key and app of the organisation across REST and MCP. "
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
        return service.jurisdictions()

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
            jurisdiction=within(request, service, jurisdiction), limit=limit, offset=offset, access=access
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
            q, jurisdiction=within(request, service, jurisdiction), limit=limit, offset=offset, access=access
        )

    @app.get("/citations/{citation:path}")
    def citations(
        request: Request, citation: str, service: LegacyCatalogue = Depends(catalogue)
    ) -> CitationView:
        view = service.citations(citation)
        reach(request).admit(*(m.jurisdiction_id for m in view.matches))
        return view

    @app.get("/coverage")
    def coverage(service: LegacyCatalogue = Depends(catalogue)) -> CoverageView:
        return service.coverage()

    @app.get("/rights/{source_id}")
    def rights(request: Request, source_id: str, service: LegacyCatalogue = Depends(catalogue)) -> RightsView:
        check_source(request, service, source_id)
        return service.rights(source_id)

    @app.get("/releases")
    def release_listing(request: Request):
        return LegacyReleases(store, releases).listing()

    return app
