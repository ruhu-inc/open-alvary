"""Relational identities and foreign keys, with validated JSON entity payloads."""

import os
from pathlib import Path

from sqlalchemy import JSON, Column, ForeignKey, MetaData, String, Table, create_engine, event, select
from sqlalchemy.engine import Engine

from schema.models import Corpus

ROOT = Path(__file__).resolve().parents[1]
metadata = MetaData()
RELATIONS = {
    "languages": {},
    "jurisdictions": {},
    "authorities": {"jurisdiction_id": "jurisdictions"},
    "sources": {"jurisdiction_id": "jurisdictions", "authority_id": "authorities", "language": "languages"},
    "rights": {"source_id": "sources"},
    "versions": {"source_id": "sources", "language": "languages"},
    "structure": {"source_id": "sources", "version_id": "versions"},
    "citations": {"source_id": "sources"},
    "amendments": {"source_id": "sources", "target_source_id": "sources"},
    "translations": {
        "source_version_id": "versions",
        "translated_version_id": "versions",
        "language": "languages",
    },
}
tables = {
    name: Table(
        name,
        metadata,
        Column("id", String(160), primary_key=True),
        *[
            Column(
                key,
                String(160),
                ForeignKey(f"{target}.id"),
                nullable=False,
                index=True,
                unique=(name == "rights" and key == "source_id"),
            )
            for key, target in refs.items()
        ],
        Column("payload", JSON, nullable=False),
    )
    for name, refs in RELATIONS.items()
}


def engine_for(url: str | None = None) -> Engine:
    url = url or os.getenv("DATABASE_URL", f"sqlite:///{ROOT / 'open_alvary.db'}")
    engine = create_engine(url, connect_args={"check_same_thread": False} if url.startswith("sqlite") else {})
    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def enable_foreign_keys(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")

    return engine


class Store:
    def __init__(self, engine: Engine):
        self.engine = engine

    def load(self) -> Corpus:
        with self.engine.connect() as conn:
            return Corpus.model_validate(
                {
                    name: list(conn.scalars(select(table.c.payload).order_by(table.c.id)))
                    for name, table in tables.items()
                }
            )

    def save(self, corpus: Corpus) -> None:
        corpus = Corpus.model_validate(corpus.model_dump(mode="json"))
        with self.engine.begin() as conn:
            for name, table in tables.items():
                for model in getattr(corpus, name):
                    payload = model.model_dump(mode="json")
                    old = conn.scalar(select(table.c.payload).where(table.c.id == model.id))
                    review_fields = (
                        {"verification_status", "content_verifier", "content_verified_at"}
                        if name == "versions"
                        else set()
                    )
                    if (
                        name in {"versions", "structure"}
                        and old is not None
                        and (
                            {k: v for k, v in old.items() if k not in review_fields}
                            != {k: v for k, v in payload.items() if k not in review_fields}
                        )
                    ):
                        raise ValueError(f"{name} are immutable; create a new version")
                    row = {
                        "id": model.id,
                        "payload": payload,
                        **{key: payload[key] for key in RELATIONS[name]},
                    }
                    if old is None:
                        conn.execute(table.insert().values(**row))
                    else:
                        conn.execute(table.update().where(table.c.id == model.id).values(**row))


def seed(store: Store) -> None:
    catalogue = Corpus.model_validate_json((ROOT / "ingestion/catalogue/ng.json").read_text())
    # Do not overwrite a maintainer's later review when rerunning bootstrap.
    current = store.load()
    for name in Corpus.model_fields:
        existing = {m.id for m in getattr(current, name)}
        getattr(current, name).extend(m for m in getattr(catalogue, name) if m.id not in existing)
    store.save(current)
