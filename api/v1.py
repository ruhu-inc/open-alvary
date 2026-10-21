"""REST v1 (MAC-06): the read routes for entitled organisations, at https://api.<domain>/api/v1.

The same response shapes as today's read routes (API-01–06), behind the credential gate
(api.credentials). A plan's countries are read at its own limits and every other one at the Starter
rate (api.reach); a search or listing without a jurisdiction covers the plan's. Public without a
credential, and open to any origin without credentials: the OpenAPI document, the quota policy and
the tier catalogue. Data routes send no CORS headers: keys never belong in a browser (N18).

Releases with full text download only from /bulk, in the gate's bulk class: the organisation's access
must include bulk and the release's country must be within its plan (MAC-06; owner direction,
2026-10-19: bulk text for the sovereign tier only). Catalogue metadata releases stay keyless at
/downloads.
"""

import hashlib
import re
from collections.abc import Callable
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from api import courts, text_index
from api.admission import AdmissionUnavailable
from api.catalogue import InvalidSearch, LegacyCatalogue, SourceUnavailable
from api.credentials import NOT_INCLUDED, problem
from api.emission import MAX_SINGLE_RESPONSE_BYTES, CheckedFileResponse, CheckedResponse, CheckedText
from api.openapi_docs import describe
from api.reach import EVERYWHERE, OutsidePlanLimited, Reach
from api.release_adapter import LegacyReleases, ReleaseMissing, ReleaseWithdrawn, holds_text
from api.repositories import LegacyRepository, PublicUnavailable, ResultTooLarge
from api.store import Store, check_legacy_authority
from changes.routes import create_router as create_changes_router
from retrieval.check import Checker
from retrieval.pack import INCLUDE, Retriever
from retrieval.pack import Request as EvidenceRequest
from schema.public import (
    LEGAL_STATUS,
    AnchorView,
    CheckPage,
    CheckRequest,
    CitationView,
    CourtRegisters,
    CourtRegisterView,
    CoverageView,
    EvidencePack,
    ExpressionPage,
    JurisdictionDetail,
    JurisdictionView,
    RelationKind,
    RightsHistory,
    RightsView,
    SearchPage,
    SourcePage,
    SourceRelationships,
    SourceView,
    StructurePage,
    TextSearchPage,
    VersionPage,
)

PUBLIC_PATHS = frozenset({"/openapi.json", "/quota-policy", "/tiers"})
# Paths in another class of the credential gate: bulk downloads need a plan that includes bulk.
CLASSES = (("/bulk/", "bulk"),)
DocumentType = Literal["constitution", "act", "regulation", "judgment", "gazette", "guidance", "other"]
# A BCP 47 language tag, such as en, fr or en-NG.
LANGUAGE = Query(None, min_length=2, max_length=35, pattern=r"^[A-Za-z]{2,8}(-[A-Za-z0-9]{1,8})*$")
PLACE = re.compile(r"[a-z]{2}(?:-[a-z0-9]{1,12})?")  # a court register's jurisdiction (schema.record)
ONE_JURISDICTION = (
    "scope=applicable needs exactly one jurisdiction: the country, or the place within one, whose applicable "
    "law you ask for."
)


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

    def applicable(request: Request, service: LegacyCatalogue, jurisdiction, as_of, source_id, version_id):
        """An applicable-law scope (api.applicable), asked for with scope=applicable: the records of other
        jurisdictions it admits are read like any other jurisdiction's under the plan."""
        named = source_id or (service.repository.version_owner(version_id) if version_id else None)
        scope = service.applicable_scope(jurisdiction, as_of, named=named)
        reach(request).admit(*sorted(scope.jurisdictions))
        return scope

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
            "bytes": {
                "allowance": "bytes_per_day",
                "algorithm": "token_bucket",
                "capacity": "one day's bytes_per_day",
                "refill": "bytes_per_day spread evenly over 24 hours",
                "counted": "response body bytes as sent, before compression",
            },
            "note": (
                "Each organisation's access tier sets its requests a minute, burst, concurrent requests and "
                "data a day (bytes_per_day; see /tiers), shared by every key and app of the organisation "
                "across REST, MCP and bulk downloads. One key or app may use at most half the organisation's "
                "concurrent requests. Data a day refills continuously: a response that would go over it is "
                "refused. A refused request answers 429 with Retry-After."
            ),
        }

    @app.get("/tiers")
    def tier_catalogue():
        return {"tiers": tiers()}

    # Jurisdictions and their memberships are reference data: plan countries do not narrow them.
    @app.get("/jurisdictions")
    def jurisdictions(
        request: Request, as_of: date | None = None, service: LegacyCatalogue = Depends(catalogue)
    ) -> tuple[JurisdictionView, ...]:
        return service.jurisdictions(as_of=as_of or datetime.now(UTC).date())

    @app.get("/jurisdictions/{jurisdiction_id}")
    def jurisdiction(
        request: Request,
        jurisdiction_id: str,
        as_of: date | None = None,
        service: LegacyCatalogue = Depends(catalogue),
    ) -> JurisdictionDetail:
        return service.jurisdiction(jurisdiction_id, as_of or datetime.now(UTC).date())

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

    @app.get("/sources/{source_id}/expressions")
    def expressions(
        request: Request, source_id: str, service: LegacyCatalogue = Depends(catalogue)
    ) -> ExpressionPage:
        check_source(request, service, source_id)
        return service.expressions(source_id)

    @app.get("/sources/{source_id}/versions")
    def versions(
        request: Request,
        source_id: str,
        as_of: date | None = None,
        language: str | None = LANGUAGE,
        expression_id: str | None = Query(None, min_length=3, max_length=200),
        service: LegacyCatalogue = Depends(catalogue),
    ) -> VersionPage:
        check_source(request, service, source_id)
        page = service.versions(
            source_id, as_of=as_of, language=language, expression=expression_id, editions=True
        )
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

    @app.get("/search/text")
    def search_text(
        request: Request,
        q: str = Query(min_length=1, max_length=2000),
        jurisdiction: str | None = None,
        language: str | None = LANGUAGE,
        kinds: list[DocumentType] = Query([], alias="type"),
        statuses: list[LEGAL_STATUS] = Query([], alias="status"),
        as_of: date | None = None,
        year_from: int | None = Query(None, ge=1800, le=2200),
        year_to: int | None = Query(None, ge=1800, le=2200),
        source_id: str | None = Query(None, min_length=1, max_length=160),
        version_id: str | None = Query(None, min_length=1, max_length=160),
        law: Literal["applicable"] | None = Query(None, alias="scope"),
        limit: int = Query(25, ge=1, le=100),
        cursor: str | None = Query(None, max_length=200),
        service: LegacyCatalogue = Depends(catalogue),
    ) -> TextSearchPage:
        if law is not None and jurisdiction is None:
            raise InvalidSearch(ONE_JURISDICTION)
        covered = within(request, service, jurisdiction)
        widened = applicable(request, service, jurisdiction, as_of, source_id, version_id) if law else None
        filters = text_index.Filters(
            jurisdictions=None
            if covered is None
            else (covered,)
            if isinstance(covered, str)
            else tuple(sorted(covered)),
            language=language,
            types=tuple(kinds),
            statuses=tuple(statuses),
            as_of=as_of,
            year_from=year_from,
            year_to=year_to,
            source_id=source_id,
            version_id=version_id,
        )
        page = service.search_text(q, filters, limit=limit, cursor=cursor, scope=widened)
        payload = page.model_dump_json().encode()
        served = {(hit.version_id, hit.content_hash) for hit in page.items}

        def recheck() -> bool:
            """Immediately before the first byte: every text quoted is still public."""
            with session() as fresh:
                return all(fresh.readable(version_id) == content_hash for version_id, content_hash in served)

        return checked(request, payload, recheck, f'"{hashlib.sha256(payload).hexdigest()}"')

    @app.get("/retrieve")
    def retrieve(
        request: Request,
        q: str = Query(min_length=1, max_length=2000),
        jurisdictions: list[str] = Query([], alias="jurisdiction"),
        language: str | None = LANGUAGE,
        kinds: list[DocumentType] = Query([], alias="type"),
        statuses: list[LEGAL_STATUS] = Query([], alias="status"),
        as_of: date | None = None,
        year_from: int | None = Query(None, ge=1800, le=2200),
        year_to: int | None = Query(None, ge=1800, le=2200),
        source_id: str | None = Query(None, min_length=1, max_length=160),
        version_id: str | None = Query(None, min_length=1, max_length=160),
        law: Literal["applicable"] | None = Query(None, alias="scope"),
        max_units: int = Query(10, ge=1, le=25),
        max_evidence_bytes: int = Query(49_152, ge=4_096, le=262_144),
        include: list[Literal["context", "expansion", "ranking"]] = Query([]),
        service: LegacyCatalogue = Depends(catalogue),
    ) -> EvidencePack:
        if len(jurisdictions) > 10 or any(not 2 <= len(j) <= 40 for j in jurisdictions):
            raise InvalidSearch("jurisdiction: at most 10, each 2 to 40 characters")
        if law is not None and len(set(jurisdictions)) != 1:
            raise InvalidSearch(ONE_JURISDICTION)
        if jurisdictions:
            plan = reach(request)
            for jurisdiction in jurisdictions:
                plan.admit(jurisdiction)
            scope = tuple(sorted(set(jurisdictions)))
        else:
            within_plan = within(request, service, None)
            scope = (
                None
                if within_plan is None
                else (within_plan,)
                if isinstance(within_plan, str)
                else tuple(sorted(within_plan))
            )
        filters = text_index.Filters(
            jurisdictions=scope,
            language=language,
            types=tuple(kinds),
            statuses=tuple(statuses),
            as_of=as_of,
            year_from=year_from,
            year_to=year_to,
            source_id=source_id,
            version_id=version_id,
        )
        widened = (
            applicable(request, service, jurisdictions[0], as_of, source_id, version_id) if law else None
        )
        pack = Retriever(service).retrieve(
            EvidenceRequest(
                query=q,
                filters=filters,
                max_units=max_units,
                max_evidence_bytes=max_evidence_bytes,
                include=frozenset(include) or INCLUDE,
                scope=widened,
            )
        )
        payload = pack.model_dump_json().encode()
        served = {(unit.version_id, unit.content_hash) for unit in pack.evidence}

        def recheck() -> bool:
            """Immediately before the first byte: every text quoted is still public."""
            with session() as fresh:
                return all(fresh.readable(version_id) == content_hash for version_id, content_hash in served)

        return checked(request, payload, recheck, f'W/"{hashlib.sha256(payload).hexdigest()}"')

    @app.post("/currentness")
    def currentness(
        request: Request, body: CheckRequest, service: LegacyCatalogue = Depends(catalogue)
    ) -> CheckPage:
        checker = Checker(service)
        page = checker.check(body.items)
        reach(request).admit(*sorted(checker.jurisdictions))
        payload = page.model_dump_json().encode()
        served = checker.served

        def recheck() -> bool:
            with session() as fresh:
                return all(fresh.readable(version_id) == content_hash for version_id, content_hash in served)

        return checked(request, payload, recheck, f'W/"{hashlib.sha256(payload).hexdigest()}"')

    @app.get("/citations/{citation:path}")
    def citations(
        request: Request,
        citation: str,
        as_of: date | None = None,
        language: str | None = LANGUAGE,
        service: LegacyCatalogue = Depends(catalogue),
    ) -> CitationView:
        view = service.citations(citation, as_of=as_of, language=language)
        reach(request).admit(*(m.jurisdiction_id for m in view.matches))
        return view

    @app.get("/coverage")
    def coverage(service: LegacyCatalogue = Depends(catalogue)) -> CoverageView:
        return service.coverage()

    # Court registers (CPR-04) are reference data written in the repository, not corpus records: no plan
    # narrows them. A register that fails to load is unavailable, never stale; an entry changed after its
    # review loads, and is served as unreviewed.
    @app.get("/courts")
    def court_registers() -> CourtRegisters:
        try:
            return CourtRegisters(registers=[courts.summary(r) for r in courts.written()])
        except ValueError as error:
            raise PublicUnavailable("A court register is unavailable") from error

    @app.get("/courts/{jurisdiction}")
    def court_register(jurisdiction: str) -> CourtRegisterView:
        if not PLACE.fullmatch(jurisdiction):
            raise InvalidSearch("jurisdiction: a place code, such as `ke`, `ng-la` or `aa-ohada`")
        try:
            return courts.public(courts.load(jurisdiction))
        except ValueError as error:
            raise PublicUnavailable("This court register is unavailable") from error

    @app.get("/rights/{source_id}")
    def rights(request: Request, source_id: str, service: LegacyCatalogue = Depends(catalogue)) -> RightsView:
        check_source(request, service, source_id)
        return service.rights(source_id)

    @app.get("/rights/{source_id}/history")
    def rights_history(
        request: Request, source_id: str, service: LegacyCatalogue = Depends(catalogue)
    ) -> RightsHistory:
        check_source(request, service, source_id)
        return service.rights_history(source_id)

    def bulk(request: Request) -> bool:
        """Whether the organisation's access includes bulk; never without the gate's word for it."""
        tier = getattr(request.state, "access", {}).get("tier") or {}
        return "bulk" in (tier.get("includes") or ())

    @app.get("/releases")
    def release_listing(request: Request):
        # Metadata releases download from /downloads on either domain (as the website's listing says),
        # keyless. Releases with full text are listed only to organisations with bulk access, for the
        # countries of their plan, and download from /bulk.
        plan = reach(request)
        listing = []
        for manifest in LegacyReleases(store, releases, bulk_text=bulk(request)).listing():
            text = holds_text(manifest)
            if text and not plan.covers(manifest["jurisdiction"]):
                continue
            where = "/api/v1/bulk" if text else "/downloads"
            manifest["download_base"] = where + manifest["download_base"].removeprefix("/releases")
            listing.append(manifest)
        return listing

    @app.get("/bulk/{release_id}/{jurisdiction}/{mode}/{filename}")
    def bulk_file(request: Request, release_id: str, jurisdiction: str, mode: str, filename: str):
        if not bulk(request):
            return problem("entitlement_required", request_id(request), detail=NOT_INCLUDED["bulk"])
        if not reach(request).covers(jurisdiction):
            return problem("jurisdiction_not_included", request_id(request))
        files = LegacyReleases(store, releases, bulk_text=True)
        try:
            path = files.file(release_id, jurisdiction, mode, filename)
        except ReleaseMissing:
            return problem("unavailable", request_id(request))
        except ReleaseWithdrawn:
            return problem("withdrawn", request_id(request))

        def recheck() -> bool:
            """Immediately before the first byte: the release is still current (REL-01)."""
            try:
                files.file(release_id, jurisdiction, mode, filename)
            except (ReleaseMissing, ReleaseWithdrawn):
                return False
            return True

        def refusal(status: int, detail: str):
            return problem("withdrawn" if status == 410 else "temporarily_unavailable", request_id(request))

        return CheckedFileResponse(path, recheck, refusal, filename=filename)

    def openapi():
        if app.openapi_schema is None:
            generated = get_openapi(
                title=app.title, version=app.version, description=app.description, routes=app.routes
            )
            app.openapi_schema = describe(generated)
        return app.openapi_schema

    app.openapi = openapi
    return app
