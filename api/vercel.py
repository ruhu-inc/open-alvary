"""Vercel entrypoint for the immutable, metadata-only pilot.

Each function instance rebuilds a small in-memory catalogue from the committed
snapshot. Updates require redeployment; this is never a mutable corpus database.
"""

from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from api.main import create_app
from api.store import ROOT, Store, metadata
from schema.models import Corpus


def create_vercel_app(catalogue: Corpus | None = None) -> FastAPI:
    catalogue = catalogue or Corpus.model_validate_json((ROOT / "ingestion/catalogue/ng.json").read_text())
    if catalogue.versions or catalogue.structure:
        raise ValueError("Vercel snapshot mode supports metadata only; use the database deployment for text")
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    metadata.create_all(engine)
    store = Store(engine)
    store.save(catalogue)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/api", create_app(store))
    return app


app = create_vercel_app()
