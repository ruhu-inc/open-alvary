"""Vercel entrypoint for the immutable, metadata-only pilot.

Each function instance rebuilds a small in-memory catalogue from the committed
snapshot. Updates require redeployment; this is never a mutable corpus database.
It serves the same public service as every other deployment (api.service): the
website's data path, public downloads, the legacy API and the built website.
"""

from pathlib import Path

from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from api.service import create_service
from api.store import ROOT, Store, metadata
from schema.models import Corpus


def create_vercel_app(catalogue: Corpus | None = None, frontend: Path | None = None) -> FastAPI:
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
    return create_service(store, frontend=frontend or ROOT / "web/dist")


app = create_vercel_app()
