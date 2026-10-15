"""The public service (MAC-02, activation A): one process for everything the website's origin serves.

    /site/v1/...    the website's own data path (api.site), admitted in the "site" class
    /downloads/...  public release files, rechecked on every response
    /api/...        the legacy anonymous API, unchanged until activation B; its release-file route
                    now redirects to /downloads
    /console/...    the developer console and its API (api.console), PostgreSQL only
    /healthz        liveness of the catalogue authority
    /               the built website, where a frontend directory is given (Vercel, Cloud Run with
                    ALVARY_FRONTEND, local)

Every response carries the website's security headers, which Vercel and nginx used to add, so the
service can stand alone behind the load balancer (MAC-12).

Admission is all or nothing: when the API's allowance is configured, the website's own class must be
configured too (``ALVARY_SITE_ADMISSION_CONFIG``), or the service refuses to start.
"""

import os
from html import escape
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException

from accounts.store import Accounts
from api.admission import AdmissionConfig
from api.catalogue import LegacyCatalogue
from api.console import ConsoleConfig, Provider, create_console_app
from api.main import create_app
from api.repositories import LegacyRepository
from api.site import create_downloads_app, create_site_app
from api.store import ROOT, Store, check_legacy_authority, engine_for
from rights.database import PolicyNamespace
from rights.policy import decide

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
    if store.engine.dialect.name == "postgresql":
        # The developer console needs the accounts schema, which only PostgreSQL carries.
        with store.engine.connect() as connection:
            namespace = PolicyNamespace(connection.scalar(text("SELECT current_schema()")))
        config = ConsoleConfig.from_environment()
        service.add_api_route(
            "/console", lambda: RedirectResponse("/console/", status_code=308), include_in_schema=False
        )
        service.mount(
            "/console",
            create_console_app(
                Accounts(store.engine, namespace),
                Provider(config) if config else None,
                frontend=frontend,
                trusted_proxies=site_admission.trusted_proxies if site_admission else 0,
                dev_login=os.getenv("ALVARY_CONSOLE_DEV_LOGIN") == "1",
            ),
        )

    @service.get("/robots.txt", include_in_schema=False)
    def robots(request: Request):
        return PlainTextResponse(
            "User-agent: *\nDisallow: /site/\nDisallow: /api/\nAllow: /\n"
            f"Sitemap: {_origin(request)}/sitemap.xml\n"
        )

    @service.get("/sitemap.xml", include_in_schema=False)
    def sitemap(request: Request):
        # Every public page a reader can open, so search engines find each source's record.
        origin = _origin(request)
        paths = list(STATIC_PAGES)
        with store.engine.begin() as connection:
            check_legacy_authority(connection, write=False)
            catalogue = LegacyCatalogue(LegacyRepository(connection))
            for source, rights in catalogue.repository.sources():
                if len(paths) >= 50000:
                    break
                if decide(rights, now=catalogue.now).metadata:
                    paths.append(f"/sources/{quote(source.id)}")
        urls = "".join(f"<url><loc>{escape(origin + path)}</loc></url>" for path in paths)
        body = f'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>'
        return Response(body, media_type="application/xml", headers={"Cache-Control": "no-store"})

    if frontend is not None:
        service.mount("/", WebsiteFiles(directory=frontend, html=True, check_dir=False), name="website")
    return service


STATIC_PAGES = ("/", "/browse", "/coverage", "/governance", "/releases", "/developers", "/about", "/privacy")


def _origin(request: Request) -> str:
    domain = os.getenv("PUBLIC_DOMAIN")
    return f"https://{domain}" if domain else str(request.base_url).rstrip("/")


class WebsiteFiles(StaticFiles):
    """The built website with real page addresses: any path that is not a file (/browse,
    /sources/<id>) answers with the website's page, which reads the address itself. Fingerprinted
    files under /assets are cached for a year; the page itself is revalidated on every visit."""

    async def get_response(self, path: str, scope):
        page = "." not in path.rsplit("/", 1)[-1]
        try:
            response = await super().get_response(path, scope)
        except StarletteHTTPException as error:
            if error.status_code != 404 or not page:
                raise
            response = await super().get_response("index.html", scope)
        if response.status_code == 404 and page:
            response = await super().get_response("index.html", scope)
        response.headers["Cache-Control"] = (
            "public, max-age=31536000, immutable" if path.startswith("assets/") else "no-cache"
        )
        return response


app = create_service(frontend=Path(os.environ["ALVARY_FRONTEND"]) if os.getenv("ALVARY_FRONTEND") else None)
