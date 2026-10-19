"""The website's own data path (MAC-01): what the public pages show, and nothing more.

Served at /site/v1 on the website's origin, and the public release files at /downloads. Anonymous by
design: a request carrying credentials is refused, so the path cannot become an API by accident.
Lists come in fixed pages of 20, pages 1-25 for each query or filter set; there is no page size,
cursor, bulk export or whole-catalogue enumeration. Every response is ``no-store``, so a withdrawal
or permission change takes effect on the next request, and no CORS headers are sent. Errors are RFC
9457 problem details carrying the MAC-06 codes. Admission is the ACS-05 machinery in its own
``site`` class (``ALVARY_SITE_ADMISSION_CONFIG``), separate from any API allowance.

This path makes bulk copying slow and visible; it cannot make it impossible, since whatever a page
shows can be read by a program behaving like a browser.
"""

import math
import os
import re
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from api.admission import AdmissionConfig, AdmissionMiddleware, AdmissionStore
from api.catalogue import FACETS, SORTS, InvalidSearch, LegacyCatalogue, SourceUnavailable
from api.emission import CheckedFileResponse, CheckedResponse
from api.project import project_info
from api.release_adapter import BULK_ELSEWHERE, BulkRequired, LegacyReleases, ReleaseMissing, ReleaseWithdrawn
from api.repositories import EDGE_ORDER, LegacyRepository, PublicUnavailable, ResultTooLarge
from api.store import Store, check_legacy_authority
from changes.routes import create_router as create_changes_router
from rights.bytes import ByteLedger, Scope
from rights.database import PolicyNamespace

PAGE_SIZE = 20
MAX_PAGES = 25
MAX_VERSIONS_BYTES = 2 * 1024 * 1024  # larger texts follow the public-reader specification
ACCESS = {"all", "open", "metadata"}
FILTER_VALUE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._:-]{0,63}")
TITLES = {
    "invalid_input": "The request is not valid",
    "beyond_bounds": "Past the end of what this page shows",
    "credential_not_accepted": "The website's data path takes no credentials",
    "unavailable": "Not available",
    "withdrawn": "Withdrawn",
    "result_too_large": "Too large for one page",
    "temporarily_unavailable": "Temporarily unavailable",
}


class SiteProblem(Exception):
    def __init__(self, status: int, code: str, detail: str):
        super().__init__(detail)
        self.status, self.code, self.detail = status, code, detail


def problem(status: int, code: str, detail: str, request_id: str | None = None) -> JSONResponse:
    body = {
        "type": f"https://open.alvary.ai/docs/errors#{code}",
        "title": TITLES.get(code, "Request refused"),
        "status": status,
        "detail": detail,
        "code": code,
        "retryable": status in (429, 503),
        "request_id": request_id,
    }
    return JSONResponse(body, status_code=status, media_type="application/problem+json")


def _only(request: Request, *allowed: str) -> None:
    unknown = sorted(set(request.query_params) - set(allowed))
    if unknown:
        raise SiteProblem(400, "invalid_input", f"Unknown parameter: {', '.join(unknown)}")


def _page(value: str | None) -> int:
    if value is None:
        return 1
    if not value.isdigit() or int(value) < 1:
        raise SiteProblem(400, "invalid_input", "page must be a whole number from 1")
    if int(value) > MAX_PAGES:
        raise SiteProblem(
            400,
            "beyond_bounds",
            f"Only the first {MAX_PAGES} pages ({MAX_PAGES * PAGE_SIZE} results) are shown: refine the search.",
        )
    return int(value)


def _access(value: str | None) -> str:
    if value is None:
        return "all"
    if value not in ACCESS:
        raise SiteProblem(400, "invalid_input", "access must be all, open or metadata")
    return value


def _paged(result, page: int) -> dict:
    pages = min(MAX_PAGES, math.ceil(result.total / PAGE_SIZE)) if result.total else 0
    return {**result.model_dump(mode="json"), "page": page, "page_size": PAGE_SIZE, "last_page": pages}


def _names(service: LegacyCatalogue, facets: dict) -> dict:
    """Display names for the jurisdictions and languages offered as filters."""
    names = {}
    for facet, relation in (("jurisdiction", "jurisdictions"), ("language", "languages")):
        ids = list(facets.get(facet, {}))
        for row in service.repository.rows(relation, ids=ids) if ids else []:
            names[(facet, row["id"])] = row.get("name")
    return names


def _base(admission: AdmissionConfig | None, store: Store) -> FastAPI:
    """An app with the site's headers, credential refusal, problem errors and admission."""
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    if admission is not None:
        if store.engine.dialect.name != "postgresql":
            raise ValueError("Admission enforcement requires PostgreSQL")
        namespace = PolicyNamespace(_policy_schema())
        app.add_middleware(
            AdmissionMiddleware,
            config=admission,
            store=AdmissionStore(store.engine, namespace),
            route_class="site",
        )

    @app.middleware("http")
    async def site_rules(request: Request, call_next):
        request_id = str(uuid4())
        request.state.request_id = request_id
        if "authorization" in request.headers:
            # Before admission: a refused credential is never charged and never looked up.
            response = problem(
                400,
                "credential_not_accepted",
                "The website's data path is anonymous. Machine access uses the API with a key.",
                request_id,
            )
        else:
            response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Request-ID"] = request_id
        return response

    @app.exception_handler(SiteProblem)
    async def site_problem(request, exception):
        return problem(exception.status, exception.code, exception.detail, request.state.request_id)

    @app.exception_handler(RequestValidationError)
    async def invalid(request, exception):
        return problem(400, "invalid_input", "The request is not valid", request.state.request_id)

    @app.exception_handler(SourceUnavailable)
    async def missing(request, exception):
        return problem(404, "unavailable", "Source not available", request.state.request_id)

    @app.exception_handler(InvalidSearch)
    async def invalid_search(request, exception):
        return problem(400, "invalid_input", str(exception), request.state.request_id)

    @app.exception_handler(ResultTooLarge)
    async def too_large(request, exception):
        return problem(413, "result_too_large", "Too large for one page", request.state.request_id)

    @app.exception_handler(SQLAlchemyError)
    @app.exception_handler(ValidationError)
    @app.exception_handler(PublicUnavailable)
    async def unavailable(request, exception):
        return problem(
            503, "temporarily_unavailable", "Catalogue authority unavailable", request.state.request_id
        )

    return app


def _policy_schema() -> str:
    return os.getenv("ALVARY_POLICY_SCHEMA", "public")


def create_site_app(store: Store, release_root: Path, admission: AdmissionConfig | None = None) -> FastAPI:
    app = _base(admission, store)
    ledger = (
        ByteLedger(store.engine, PolicyNamespace(_policy_schema()))
        if admission is not None and admission.byte_limits is not None
        else None
    )

    @contextmanager
    def catalogue():
        with store.engine.begin() as connection:
            check_legacy_authority(connection, write=False)
            yield LegacyCatalogue(LegacyRepository(connection))

    @contextmanager
    def change_session():
        with store.engine.begin() as connection:
            check_legacy_authority(connection, write=False)
            yield connection

    if store.engine.dialect.name == "postgresql":  # the change log lives in PostgreSQL
        app.include_router(create_changes_router(change_session))

    @app.get("/project")
    def project(request: Request):
        _only(request)
        return project_info()

    @app.get("/coverage")
    def coverage(request: Request):
        _only(request)
        with catalogue() as service:
            return service.coverage()

    def _browse(request: Request, query: str | None):
        page = _page(request.query_params.get("page"))
        sort = request.query_params.get("sort", "relevance" if query else "title")
        if sort not in SORTS:
            raise SiteProblem(400, "invalid_input", "sort must be relevance, newest, oldest or title")
        filters = {}
        for name in FACETS:
            value = request.query_params.get(name)
            if value is not None and not FILTER_VALUE.fullmatch(value):
                raise SiteProblem(400, "invalid_input", f"{name} is not a valid filter value")
            filters[name] = value
        if filters["access"] is not None:
            _access(filters["access"])
        if filters["access"] == "all":
            filters["access"] = None
        with catalogue() as service:
            result, facets = service.browse(
                query=query, filters=filters, sort=sort, limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE
            )
            names = _names(service, facets)
        return {
            **_paged(result, page),
            "sort": sort,
            "facets": {
                name: [{"value": v, "count": n, "label": names.get((name, v))} for v, n in counts.items()]
                for name, counts in facets.items()
            },
        }

    @app.get("/search")
    def search(request: Request):
        _only(request, "q", "page", "sort", *FACETS)
        q = request.query_params.get("q", "")
        if not 1 <= len(q) <= 200:
            raise SiteProblem(400, "invalid_input", "q must be 1 to 200 characters")
        return _browse(request, q)

    @app.get("/sources")
    def sources(request: Request):
        _only(request, "page", "sort", *FACETS)
        return _browse(request, None)

    @app.get("/sources/{source_id}")
    def source(source_id: str, request: Request):
        _only(request)
        with catalogue() as service:
            view = service.source(source_id)
            repository = service.repository
            authority = repository.rows("authorities", ids=[view.authority_id])
            jurisdiction = repository.rows("jurisdictions", ids=[view.jurisdiction_id])
            language = repository.rows("languages", ids=[view.language])
        # Plain names beside the identifiers, for the record page.
        return {
            **view.model_dump(mode="json"),
            "authority_name": authority[0].get("name") if authority else None,
            "jurisdiction_name": jurisdiction[0].get("name") if jurisdiction else None,
            "language_name": language[0].get("name") if language else None,
        }

    @app.get("/sources/{source_id}/versions")
    def versions(source_id: str, request: Request):
        _only(request)
        with catalogue() as service:
            page = service.versions(source_id)
        payload = page.model_dump_json().encode()
        if len(payload) > MAX_VERSIONS_BYTES:
            raise SiteProblem(413, "result_too_large", "This source's text is too large for one page")
        served = {(v.id, v.content_hash) for v in page.items}

        def recheck():
            try:
                with catalogue() as fresh:
                    current = fresh.versions(source_id)
            except SourceUnavailable:
                return False
            return served <= {(v.id, v.content_hash) for v in current.items}

        limits = admission.byte_limits if admission is not None else None
        identity = getattr(request.state, "admission_identity", None)
        scopes = (
            [
                Scope(identity, limits.capacity, limits.refill_per_minute),
                Scope("global:bytes", limits.global_capacity, limits.global_refill_per_minute),
            ]
            if limits is not None and identity is not None
            else None
        )
        return CheckedResponse(
            payload,
            recheck,
            ledger=ledger,
            scopes=scopes,
            lease_seconds=limits.emission_lease_seconds if limits is not None else None,
            charged=admission is not None and bool(page.items),
        )

    @app.get("/sources/{source_id}/relationships")
    def relationships(source_id: str, request: Request):
        """The record page's history: its links (a page), and apart from them the first page of the
        instruments made under it, which may be many."""
        _only(request)
        with catalogue() as service:
            links = service.relationships(source_id, kinds=set(EDGE_ORDER) - {"enables"}, limit=PAGE_SIZE)
            enabled = service.relationships(source_id, kinds={"enables"}, limit=PAGE_SIZE, current=False)
        return {
            **links.model_dump(mode="json", exclude={"next_cursor"}),
            "enables": [item.model_dump(mode="json") for item in enabled.items],
            "enables_total": enabled.total,
        }

    @app.get("/rights/{source_id}")
    def rights(source_id: str, request: Request):
        _only(request)
        with catalogue() as service:
            return service.rights(source_id)

    @app.get("/releases")
    def releases(request: Request):
        _only(request)
        listing = LegacyReleases(store, release_root).listing()
        for manifest in listing:
            manifest["download_base"] = "/downloads" + manifest["download_base"].removeprefix("/releases")
        return listing

    return app


def create_downloads_app(
    store: Store, release_root: Path, admission: AdmissionConfig | None = None
) -> FastAPI:
    """Public release files (metadata releases stay public after activation B, D25)."""
    app = _base(admission, store)

    @app.get("/{release_id}/{jurisdiction}/{mode}/{filename}")
    def release_file(release_id: str, jurisdiction: str, mode: str, filename: str, request: Request):
        _only(request)
        releases = LegacyReleases(store, release_root)
        try:
            path = releases.file(release_id, jurisdiction, mode, filename)
        except BulkRequired:
            # Never served here, keyless: a full-text release is for organisations with bulk access.
            raise SiteProblem(404, "unavailable", BULK_ELSEWHERE) from None
        except ReleaseMissing:
            raise SiteProblem(404, "unavailable", "No such release file") from None
        except ReleaseWithdrawn as exception:
            raise SiteProblem(410, "withdrawn", str(exception)) from None

        def recheck():
            try:
                releases.file(release_id, jurisdiction, mode, filename)
            except (ReleaseMissing, ReleaseWithdrawn):
                return False
            return True

        return CheckedFileResponse(path, recheck, filename=filename)

    return app
