"""REST v1 (MAC-06): the read routes for entitled organisations, at https://api.<domain>/api/v1.

The same response shapes as today's read routes (API-01–06), behind the credential gate
(api.credentials). A plan's countries are read at its own limits and every other one at the Starter
rate (api.reach); a search or listing without a jurisdiction covers the plan's. Public without a
credential, and open to any origin without credentials: the OpenAPI document, the quota policy and
the tier catalogue. Data routes send no CORS headers: keys never belong in a browser (N18).
"""

import hashlib
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from api.admission import AdmissionUnavailable
from api.catalogue import InvalidSearch, LegacyCatalogue, SourceUnavailable
from api.credentials import problem
from api.emission import MAX_SINGLE_RESPONSE_BYTES, CheckedResponse, CheckedText
from api.openapi_docs import describe
from api.reach import EVERYWHERE, OutsidePlanLimited, Reach
from api.release_adapter import LegacyReleases
from api.repositories import LegacyRepository, PublicUnavailable, ResultTooLarge
from api.store import Store, check_legacy_authority
from changes.routes import create_router as create_changes_router
from schema.public import (
    AnchorView,
    CitationView,
    CoverageView,
    JurisdictionView,
    RelationKind,
    RightsView,
    SearchPage,
    SourcePage,
    SourceRelationships,
    SourceView,
    StructurePage,
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

    # Every refusal is a problem document with a code from the errors page.
    @app.exception_handler(RequestValidationError)
    async def invalid(request, exception):
        first = exception.errors()[0] if exception.errors() else {}
        where = ".".join(str(part) for part in first.get("loc", ())[1:]) or "request"
        return problem(
            "invalid_input", request_id(request), detail=f"{where}: {first.get('msg', 'not valid')}."
        )

    @app.exception_handler(SourceUnavailable)
    async def missing(request, exception):
        return problem("unavailable", request_id(request))

    @app.exception_handler(InvalidSearch)
    async def invalid_search(request, exception):
        return problem("invalid_input", request_id(request), detail=str(exception))

    @app.exception_handler(ResultTooLarge)
    async def too_large(request, exception):
        return problem("result_too_large", request_id(request))

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

    @app.get("/sources/{source_id}/relationships")
    def relationships(
        request: Request,
        source_id: str,
        kind: RelationKind | None = None,
        limit: int = Query(25, ge=1, le=100),
        cursor: str | None = Query(None, max_length=200),
        service: LegacyCatalogue = Depends(catalogue),
    ) -> SourceRelationships:
        check_source(request, service, source_id)
        return service.relationships(source_id, kinds=[kind] if kind else None, limit=limit, cursor=cursor)

    @app.get("/sources/{source_id}/versions")
    def versions(
        request: Request, source_id: str, service: LegacyCatalogue = Depends(catalogue)
    ) -> VersionPage:
        check_source(request, service, source_id)
        page = service.versions(source_id)
        payload = page.model_dump_json().encode()
        if len(payload) > MAX_SINGLE_RESPONSE_BYTES:
            raise ResultTooLarge("This record's texts are larger than one response may be")
        served = {(v.id, v.content_hash) for v in page.items}

        def recheck() -> bool:
            """Immediately before the first byte: every text read is still public (its rights record
            may have changed since it was read)."""
            try:
                with session() as fresh:
                    current = fresh.versions(source_id)
            except SourceUnavailable:
                return False
            return served <= {(v.id, v.content_hash) for v in current.items}

        # The tag covers the whole answer: the texts, their structure and the rights record.
        return checked(request, payload, recheck, f'"{hashlib.sha256(payload).hexdigest()}"')

    # Conditional requests (BUD-01): a matching If-None-Match answers 304 with no body, after the same
    # eligibility check as a full answer; nothing is cacheable by intermediaries (no-store).
    def unchanged(request: Request, tag: str) -> bool:
        offered = {t.strip().removeprefix("W/") for t in request.headers.get("if-none-match", "").split(",")}
        return "*" in offered or tag in offered

    def checked(request: Request, payload: bytes, recheck, tag: str, kind=CheckedResponse) -> Response:
        if unchanged(request, tag):
            if not recheck():
                raise SourceUnavailable("No longer public")
            return Response(status_code=304, headers={"ETag": tag})

        def refusal(status: int, detail: str):
            changed = "The record changed while this answer was being sent. Request it again."
            return problem("temporarily_unavailable", request_id(request), detail=changed)

        response = kind(payload, recheck, refusal=refusal)
        response.headers["ETag"] = tag
        return response

    def still_public(version_id: str, content_hash: str):
        def recheck() -> bool:
            try:
                with session() as fresh:
                    version, _, _ = fresh.version(version_id)
            except SourceUnavailable:
                return False
            return version.content_hash == content_hash

        return recheck

    @app.get("/versions/{version_id}/text")
    def version_text(request: Request, version_id: str, service: LegacyCatalogue = Depends(catalogue)):
        version, source, _ = service.version(version_id)
        reach(request).admit(source.jurisdiction_id)
        payload = version.canonical_text.encode()
        if len(payload) > MAX_SINGLE_RESPONSE_BYTES:
            raise ResultTooLarge("This text is larger than one response may be")
        recheck = still_public(version.id, version.content_hash)
        return checked(request, payload, recheck, f'"{version.content_hash}"', kind=CheckedText)

    @app.get("/versions/{version_id}/structure")
    def version_structure(
        request: Request,
        version_id: str,
        limit: int = Query(100, ge=1, le=500),
        cursor: str | None = Query(None, max_length=200),
        service: LegacyCatalogue = Depends(catalogue),
    ) -> StructurePage:
        page = service.structure(version_id, limit=limit, cursor=cursor)
        _, source, _ = service.version(version_id)
        reach(request).admit(source.jurisdiction_id)
        payload = page.model_dump_json().encode()
        tag = f'"{hashlib.sha256(payload).hexdigest()}"'
        return checked(request, payload, still_public(page.version_id, page.content_hash), tag)

    @app.get("/anchors/{anchor_id}")
    def anchor(request: Request, anchor_id: str, service: LegacyCatalogue = Depends(catalogue)) -> AnchorView:
        view = service.anchor(anchor_id)
        _, source, _ = service.version(view.version_id)
        reach(request).admit(source.jurisdiction_id)
        payload = view.model_dump_json().encode()
        tag = f'"{hashlib.sha256(payload).hexdigest()}"'
        return checked(request, payload, still_public(view.version_id, view.content_hash), tag)

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
        # Files download from /downloads on either domain (as the website's listing says), keyless.
        listing = LegacyReleases(store, releases).listing()
        for manifest in listing:
            manifest["download_base"] = "/downloads" + manifest["download_base"].removeprefix("/releases")
        return listing

    def openapi():
        if app.openapi_schema is None:
            generated = get_openapi(
                title=app.title, version=app.version, description=app.description, routes=app.routes
            )
            app.openapi_schema = describe(generated)
        return app.openapi_schema

    app.openapi = openapi
    return app
