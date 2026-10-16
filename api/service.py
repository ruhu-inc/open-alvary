"""The public service (MAC-02, activation A): one process for everything the website's origin serves.

    /site/v1/...    the website's own data path (api.site), admitted in the "site" class
    /downloads/...  public release files, rechecked on every response
    /api/v1/...     REST v1 for organisations with access (api.v1), behind the credential gate
    /mcp            MCP for organisations with access, behind the same gate
                    (these three only with ALVARY_ACCOUNTS=1: activation B, PostgreSQL)
    /api/...        the legacy anonymous API, unchanged until activation B; its release-file route
                    now redirects to /downloads
    /console/...    the developer console and its API (api.console)
    /healthz        liveness of the catalogue authority
    /               the built website, where a frontend directory is given (Vercel, Cloud Run with
                    ALVARY_FRONTEND, local)

Every response carries the website's security headers, which Vercel and nginx used to add, so the
service can stand alone behind the load balancer (MAC-12).

Admission is all or nothing: when the API's allowance is configured, the website's own class must be
configured too (``ALVARY_SITE_ADMISSION_CONFIG``), or the service refuses to start.
"""

import json
import os
from contextlib import contextmanager
from html import escape
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.routing import Route

from accounts.store import TIERS, Accounts
from api.admission import AdmissionConfig, AdmissionStore, AdmissionUnavailable
from api.catalogue import LegacyCatalogue
from api.console import ConsoleConfig, Provider, create_console_app
from api.credentials import CredentialGate, GateConfig, TokenVerifier
from api.main import create_app
from api.mcp import create_router as create_mcp_router
from api.repositories import LegacyRepository, PublicUnavailable
from api.security_txt import security_txt
from api.site import create_downloads_app, create_site_app
from api.store import ROOT, Store, check_legacy_authority, engine_for
from api.v1 import PUBLIC_PATHS as V1_PUBLIC
from api.v1 import create_v1_app
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
    accounts_on = store.engine.dialect.name == "postgresql" and os.getenv("ALVARY_ACCOUNTS") == "1"
    api_origin = os.getenv("ALVARY_API_ORIGIN", "https://api.open.alvary.ai").rstrip("/")
    if accounts_on:
        # Activation B (MAC-15): accounts, the console, REST v1 and MCP go live together, only when
        # switched on, and need the accounts schema (PostgreSQL only).
        with store.engine.connect() as connection:
            namespace = PolicyNamespace(connection.scalar(text("SELECT current_schema()")))
        accounts = Accounts(store.engine, namespace)
        config = ConsoleConfig.from_environment()
        resource = api_origin + "/api/v1"
        metadata = api_origin + "/.well-known/oauth-protected-resource/api/v1"
        v1 = create_v1_app(store, release_root, lambda: accounts.tiers(published_only=True))
        # Outside a plan's countries, requests also run at the Starter rate (api.reach).
        starter = next(t for t in json.loads(TIERS.read_text()) if t["id"] == "starter")["allowance"]
        outside = {
            "outside_per_minute": starter["requests_per_minute"],
            "outside_burst": starter.get("burst", starter["requests_per_minute"]),
        }
        service.mount(
            "/api/v1",
            CredentialGate(
                v1,
                config=GateConfig(resource, "rest", "api", metadata, public_paths=V1_PUBLIC, **outside),
                check=accounts.check_credential,
                record_usage=accounts.record_usage,
                admission=AdmissionStore(store.engine, namespace),
                verifier=TokenVerifier(config.issuer) if config else None,
            ),
        )

        mcp_resource = api_origin + "/mcp"
        mcp_metadata = api_origin + "/.well-known/oauth-protected-resource/mcp"
        mcp = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

        @contextmanager
        def catalogue_session():
            with store.engine.begin() as connection:
                check_legacy_authority(connection, write=False)
                yield LegacyCatalogue(LegacyRepository(connection))

        def reach(request: Request):
            return request.state.access["reach"]

        mcp.include_router(
            create_mcp_router(
                catalogue_session,
                (SQLAlchemyError, ValidationError, PublicUnavailable, AdmissionUnavailable),
                reach,
            )
        )
        service.router.routes.append(
            Route(
                "/mcp",
                CredentialGate(
                    mcp,
                    config=GateConfig(mcp_resource, "mcp", "mcp", mcp_metadata, jsonrpc=True, **outside),
                    check=accounts.check_credential,
                    record_usage=accounts.record_usage,
                    admission=AdmissionStore(store.engine, namespace),
                    verifier=TokenVerifier(config.issuer) if config else None,
                ),
                methods=["GET", "POST", "DELETE"],
            )
        )

        def published_countries() -> list[dict]:
            with catalogue_session() as service:
                return service.countries()

        def resource_metadata(resource_url: str) -> JSONResponse:
            # RFC 9728: which authorisation server issues tokens for this resource, and for what.
            body = {
                "resource": resource_url,
                "authorization_servers": [config.issuer.rstrip("/")] if config else [],
                "scopes_supported": ["corpus:read"],
                "bearer_methods_supported": ["header"],
                "resource_documentation": "https://open.alvary.ai/developers",
            }
            return JSONResponse(
                body, headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "max-age=300"}
            )

        service.add_api_route(
            "/.well-known/oauth-protected-resource/api/v1",
            lambda: resource_metadata(resource),
            include_in_schema=False,
        )
        for path in ("/.well-known/oauth-protected-resource/mcp", "/.well-known/oauth-protected-resource"):
            # The bare path too: some MCP hosts look there first.
            service.add_api_route(path, lambda: resource_metadata(mcp_resource), include_in_schema=False)

        service.add_api_route(
            "/console", lambda: RedirectResponse("/console/", status_code=308), include_in_schema=False
        )
        service.mount(
            "/console",
            create_console_app(
                accounts,
                Provider(config) if config else None,
                frontend=frontend,
                trusted_proxies=site_admission.trusted_proxies if site_admission else 0,
                dev_login=os.getenv("ALVARY_CONSOLE_DEV_LOGIN") == "1",
                consent_secret=os.environ["ALVARY_CONSENT_SECRET"].encode()
                if os.getenv("ALVARY_CONSENT_SECRET")
                else None,
                countries=published_countries,
            ),
        )
    anonymous = create_app(store, release_root, downloads_path="/downloads")
    if accounts_on:
        close_anonymous(anonymous, api_origin)
    service.mount("/api", anonymous)

    @service.get("/robots.txt", include_in_schema=False)
    def robots(request: Request):
        return PlainTextResponse(
            "User-agent: *\nDisallow: /site/\nDisallow: /api/\nAllow: /\n"
            f"Sitemap: {_origin(request)}/sitemap.xml\n"
        )

    @service.get("/.well-known/security.txt", include_in_schema=False)
    def security_contact(request: Request):
        # The API's domain reaches this same service only once activation B is live.
        return PlainTextResponse(
            security_txt(_origin(request), api_origin if accounts_on else None),
            headers={"Cache-Control": "max-age=3600"},
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


STATIC_PAGES = (
    "/",
    "/browse",
    "/coverage",
    "/changes",
    "/governance",
    "/releases",
    "/developers",
    "/about",
    "/privacy",
    "/security",
    "/docs/errors",
)


def close_anonymous(app, api_origin: str) -> None:
    """Activation B closes the anonymous API (MAC-02): every old route answers 410 Gone with where to
    go instead. The change feed stays anonymous (SUB C1), with the app's own admission and headers."""
    keep = ("/changes", "/change-coverage")

    def kept(route) -> bool:
        # Included routers can sit here as nested objects without a path of their own.
        inner = getattr(getattr(route, "original_router", route), "routes", [])
        paths = [getattr(route, "path", None)] + [getattr(r, "path", None) for r in inner]
        return any(isinstance(p, str) and p.startswith(keep) for p in paths)

    app.router.routes = [r for r in app.router.routes if kept(r)]

    @app.api_route(
        "/{path:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"], include_in_schema=False
    )
    def gone(path: str):
        target = (
            f"{api_origin}/mcp"
            if path.rstrip("/") == "mcp"
            else f"{api_origin}/api/v1/openapi.json"
            if path == "openapi.json"
            else f"{api_origin}/api/v1"
        )
        return JSONResponse(
            {
                "type": "https://open.alvary.ai/docs/errors#gone",
                "title": "Gone",
                "status": 410,
                "detail": "The anonymous API has closed. Use the API with a key or a connected app, see the developer"
                " pages, or follow the change feed, which stays open at /api/changes.",
                "code": "gone",
                "successor": target,
                "developers": "https://open.alvary.ai/developers",
            },
            status_code=410,
            media_type="application/problem+json",
            headers={"Link": f'<{target}>; rel="successor-version"'},
        )


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
