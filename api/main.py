import json
import logging
from pathlib import Path
from time import perf_counter
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from api.catalogue import InvalidSearch, LegacyCatalogue, SourceUnavailable
from api.catalogue import normalize_citation as normalize_citation
from api.project import project_info
from api.release_adapter import LegacyReleases, ReleaseMissing, ReleaseWithdrawn
from api.repositories import LegacyRepository, PublicUnavailable, ResultTooLarge
from api.store import ROOT, Store, check_legacy_authority, engine_for
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


def create_app(store: Store | None = None, releases: Path | None = None) -> FastAPI:
    app = FastAPI(
        title="Open Alvary",
        version="0.1.0",
        description="Read-only, rights-filtered legal corpus. Metadata is not permission to reuse linked text.",
    )

    @app.middleware("http")
    async def no_stale_publication(request, call_next):
        # Generate our own ID; never trust caller headers as log content or identity.
        request_id = str(uuid4())
        request.state.request_id = request_id
        started = perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            response.headers["Cache-Control"] = "no-store"
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["X-Request-ID"] = request_id
            return response
        finally:
            method = (
                request.method
                if request.method in {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}
                else "OTHER"
            )
            logging.getLogger("open_alvary.requests").info(
                json.dumps(
                    {
                        "event": "http_request",
                        "request_id": request_id,
                        "method": method,
                        "status": status,
                        "response_headers_ms": round((perf_counter() - started) * 1000, 2),
                    }
                )
            )

    app.state.store = store or Store(engine_for())
    release_root = releases or ROOT / "releases"

    @app.get("/healthz", include_in_schema=False)
    def health():
        try:
            with app.state.store.engine.connect() as connection:
                connection.execute(text("SELECT id FROM sources LIMIT 1"))
        except SQLAlchemyError:
            return JSONResponse(status_code=503, content={"status": "unavailable"})
        return {"status": "ok"}

    @app.get("/project")
    def project():
        return project_info()

    def catalogue():
        # The HTTP dependency owns transaction lifetime; repositories/services never commit.
        with app.state.store.engine.begin() as connection:
            check_legacy_authority(connection, write=False)
            yield LegacyCatalogue(LegacyRepository(connection))

    @app.exception_handler(SourceUnavailable)
    async def source_missing(request, exception):
        return JSONResponse(status_code=404, content={"detail": "Source not available"})

    @app.exception_handler(InvalidSearch)
    async def invalid_search(request, exception):
        return JSONResponse(status_code=422, content={"detail": str(exception)})

    @app.exception_handler(ResultTooLarge)
    async def result_too_large(request, exception):
        return JSONResponse(status_code=413, content={"detail": "Result exceeds the bounded pilot interface"})

    @app.exception_handler(SQLAlchemyError)
    @app.exception_handler(ValidationError)
    @app.exception_handler(PublicUnavailable)
    async def unavailable(request, exception):
        return JSONResponse(status_code=503, content={"detail": "Catalogue authority unavailable"})

    @app.get("/jurisdictions")
    def jurisdictions(service: LegacyCatalogue = Depends(catalogue)) -> tuple[JurisdictionView, ...]:
        return service.jurisdictions()

    @app.get("/sources")
    def sources(
        jurisdiction: str | None = None,
        limit: int = Query(50, ge=1, le=100),
        offset: int = Query(0, ge=0),
        service: LegacyCatalogue = Depends(catalogue),
    ) -> SourcePage:
        return service.sources(jurisdiction=jurisdiction, limit=limit, offset=offset)

    @app.get("/sources/{source_id}")
    def source(source_id: str, service: LegacyCatalogue = Depends(catalogue)) -> SourceView:
        return service.source(source_id)

    @app.get("/sources/{source_id}/versions")
    def versions(source_id: str, service: LegacyCatalogue = Depends(catalogue)) -> VersionPage:
        return service.versions(source_id)

    @app.get("/search")
    def search(
        q: str = Query(min_length=1, max_length=200),
        jurisdiction: str | None = None,
        limit: int = Query(25, ge=1, le=100),
        offset: int = Query(0, ge=0),
        service: LegacyCatalogue = Depends(catalogue),
    ) -> SearchPage:
        return service.search(q, jurisdiction=jurisdiction, limit=limit, offset=offset)

    @app.get("/citations/{citation:path}")
    def citations(citation: str, service: LegacyCatalogue = Depends(catalogue)) -> CitationView:
        return service.citations(citation)

    @app.get("/coverage")
    def get_coverage(service: LegacyCatalogue = Depends(catalogue)) -> CoverageView:
        return service.coverage()

    @app.get("/rights/{source_id}")
    def rights(source_id: str, service: LegacyCatalogue = Depends(catalogue)) -> RightsView:
        return service.rights(source_id)

    @app.get("/releases")
    def get_releases():
        return LegacyReleases(app.state.store, release_root).listing()

    @app.get("/releases/{release_id}/{jurisdiction}/{mode}/{filename}")
    def release_file(release_id: str, jurisdiction: str, mode: str, filename: str):
        try:
            path = LegacyReleases(app.state.store, release_root).file(
                release_id, jurisdiction, mode, filename
            )
        except ReleaseMissing:
            raise HTTPException(404) from None
        except ReleaseWithdrawn as exception:
            raise HTTPException(410, str(exception)) from None
        return FileResponse(path, filename=filename)

    return app


app = create_app()
