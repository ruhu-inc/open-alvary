import json
import logging
import re
import unicodedata
from pathlib import Path
from time import perf_counter
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from api.project import project_info
from api.store import ROOT, Store, engine_for
from exports.release import coverage
from exports.verify import release_available
from rights.policy import decide, public_corpus
from schema.models import Corpus


def normalize_citation(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


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

    def public() -> Corpus:
        return public_corpus(app.state.store.load())

    def source_by_id(corpus, source_id):
        source = next((s for s in corpus.sources if s.id == source_id), None)
        if not source:
            raise HTTPException(404, "Source not available")
        return source

    def source_view(corpus, source):
        has_text = any(v.source_id == source.id for v in corpus.versions)
        rights = next(r for r in corpus.rights if r.source_id == source.id)
        return {
            **source.model_dump(mode="json"),
            "access_mode": "open_full_text" if has_text else "metadata_link_only",
            "rights_status": rights.status,
            "has_full_text": has_text,
        }

    @app.get("/jurisdictions")
    def jurisdictions(corpus: Corpus = Depends(public)):
        return corpus.jurisdictions

    @app.get("/sources")
    def sources(
        jurisdiction: str | None = None,
        limit: int = Query(50, ge=1, le=100),
        offset: int = Query(0, ge=0),
        corpus: Corpus = Depends(public),
    ):
        rows = [s for s in corpus.sources if jurisdiction is None or s.jurisdiction_id == jurisdiction]
        return {"total": len(rows), "items": [source_view(corpus, s) for s in rows[offset : offset + limit]]}

    @app.get("/sources/{source_id}")
    def source(source_id: str, corpus: Corpus = Depends(public)):
        return source_view(corpus, source_by_id(corpus, source_id))

    @app.get("/sources/{source_id}/versions")
    def versions(source_id: str, corpus: Corpus = Depends(public)):
        source_by_id(corpus, source_id)
        rows = [v for v in corpus.versions if v.source_id == source_id]
        return {
            "items": rows,
            "rights": next(r for r in corpus.rights if r.source_id == source_id),
            "structure": [n for n in corpus.structure if n.source_id == source_id],
            "notice": None if rows else "No version has passed both rights and content review.",
        }

    @app.get("/search")
    def search(
        q: str = Query(min_length=1, max_length=200),
        jurisdiction: str | None = None,
        limit: int = Query(25, ge=1, le=100),
        offset: int = Query(0, ge=0),
        corpus: Corpus = Depends(public),
    ):
        term = normalize_citation(q)
        if not term:
            raise HTTPException(422, "Search term must not be blank")
        text_by_source = {}
        for v in corpus.versions:
            text_by_source.setdefault(v.source_id, []).append(v.canonical_text.casefold())
        rows = [
            s
            for s in corpus.sources
            if (jurisdiction is None or s.jurisdiction_id == jurisdiction)
            and (
                term in normalize_citation(s.title + " " + s.citation)
                or any(term in t for t in text_by_source.get(s.id, []))
            )
        ]
        return {
            "total": len(rows),
            "items": [source_view(corpus, s) for s in rows[offset : offset + limit]],
            "scope": "Catalogue metadata and cleared full text only",
        }

    @app.get("/citations/{citation:path}")
    def citations(citation: str, corpus: Corpus = Depends(public)):
        ids = {
            c.source_id
            for c in corpus.citations
            if normalize_citation(c.value) == normalize_citation(citation)
        }
        rows = [source_view(corpus, s) for s in corpus.sources if s.id in ids]
        return {
            "status": "resolved" if len(rows) == 1 else "ambiguous" if rows else "unresolved",
            "matches": rows,
        }

    @app.get("/coverage")
    def get_coverage(corpus: Corpus = Depends(public)):
        return coverage(corpus)

    @app.get("/rights/{source_id}")
    def rights(source_id: str, corpus: Corpus = Depends(public)):
        source_by_id(corpus, source_id)
        record = next(r for r in corpus.rights if r.source_id == source_id)
        versions = [v for v in corpus.versions if v.source_id == source_id]
        return {"record": record, "decision": decide(record, versions[0] if versions else None)}

    def manifests():
        return sorted(release_root.glob("*/*/*/manifest.json"), reverse=True)

    @app.get("/releases")
    def get_releases():
        result = []
        current = app.state.store.load()
        for path in manifests():
            if not release_available(path.parent, current):
                continue
            manifest = json.loads(path.read_text())
            manifest["download_base"] = "/releases/" + path.parent.relative_to(release_root).as_posix()
            result.append(manifest)
        return result

    @app.get("/releases/{release_id}/{jurisdiction}/{mode}/{filename}")
    def release_file(release_id: str, jurisdiction: str, mode: str, filename: str):
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", release_id) or not re.fullmatch(
            r"[a-z0-9-]{2,30}", jurisdiction
        ):
            raise HTTPException(404)
        if mode not in {"corpus", "metadata"}:
            raise HTTPException(404)
        directory = release_root / release_id / jurisdiction / mode
        manifest_path = directory / "manifest.json"
        if not manifest_path.is_file():
            raise HTTPException(404)
        if not release_available(directory, app.state.store.load()):
            raise HTTPException(410, "Release withdrawn or invalid; consult the latest release")
        allowed = set(json.loads(manifest_path.read_text())["files"]) | {"manifest.json"}
        if filename not in allowed or Path(filename).name != filename:
            raise HTTPException(404)
        path = directory / filename
        if not path.is_file() or not path.resolve().is_relative_to(release_root.resolve()):
            raise HTTPException(404)
        return FileResponse(path, filename=filename)

    return app


app = create_app()
