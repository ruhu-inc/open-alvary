"""The public service (MAC-02, activation A): one process for everything the website's origin serves.

    /site/v1/...    the website's own data path (api.site), admitted in the "site" class
    /downloads/...  public release files, rechecked on every response
    /api/...        the legacy anonymous API, unchanged until activation B; its release-file route
                    now redirects to /downloads
    /healthz        liveness of the catalogue authority
    /               the built website, where a frontend directory is given (Vercel, Cloud Run with
                    ALVARY_FRONTEND, local)

Every response carries the website's security headers, which Vercel and nginx used to add, so the
service can stand alone behind the load balancer (MAC-12).

Admission is all or nothing: when the API's allowance is configured, the website's own class must be
configured too (``ALVARY_SITE_ADMISSION_CONFIG``), or the service refuses to start.
"""

import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from api.admission import AdmissionConfig
from api.main import create_app
from api.site import create_downloads_app, create_site_app
from api.store import ROOT, Store, engine_for

SECURITY_HEADERS = {
    "Strict-Transport-Security": "max-age=31536000",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; font-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    ),
}


def create_service(
    store: Store | None = None, releases: Path | None = None, frontend: Path | None = None
) -> FastAPI:
    admission = AdmissionConfig.from_environment()
    site_admission = AdmissionConfig.from_environment("ALVARY_SITE_ADMISSION_CONFIG")
    if admission is not None and site_admission is None:
        raise ValueError(
            "The website's data path needs its own admission class (ALVARY_SITE_ADMISSION_CONFIG)"
        )
    if store is None:
        pool = max(c.instance_pool_size for c in (admission, site_admission) if c) if site_admission else None
        store = Store(engine_for(pool_size=pool))
    release_root = releases or ROOT / "releases"
    service = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @service.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        return response

    @service.get("/healthz", include_in_schema=False)
    def health():
        try:
            with store.engine.connect() as connection:
                connection.execute(text("SELECT id FROM sources LIMIT 1"))
        except SQLAlchemyError:
            return JSONResponse(status_code=503, content={"status": "unavailable"})
        return JSONResponse({"status": "ok"}, headers={"Cache-Control": "no-store"})

    service.mount("/site/v1", create_site_app(store, release_root, site_admission))
    service.mount("/downloads", create_downloads_app(store, release_root, site_admission))
    service.mount("/api", create_app(store, release_root, downloads_path="/downloads"))
    if frontend is not None:
        service.mount("/", StaticFiles(directory=frontend, html=True, check_dir=False), name="website")
    return service


app = create_service(frontend=Path(os.environ["ALVARY_FRONTEND"]) if os.getenv("ALVARY_FRONTEND") else None)
