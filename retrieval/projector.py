"""The search engine, rebuilt and kept current from the system of record (RSV-04, RSV-10).

PostgreSQL decides what is public: the section index (changes.text_unit) holds exactly the units of
texts anyone may read now, and changes.document_unit their document units. The projector makes the
engine hold the same units, never others:

- ``sync`` reads the change log after the engine's checkpoint (changes.engine_state), and for each
  work an event names, reconciles that work: units of texts no longer indexed are deleted, units of new
  texts are fed. It then records the position reached. A feed reset (a new epoch), a new application
  or ``rebuild`` reconciles every work.
- Units are recomputed from the published text with the section index's own function
  (api.text_index.units), so the engine's units are the section index's units, field for field.

Withdrawal is therefore applied at the next sync (run every minute by the worker), and is enforced
before that by the rights recheck of every hit against PostgreSQL before any words are served.

    python -m retrieval.projector deploy | sync | rebuild | status
"""

import hashlib
import json
import sys
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from retrieval.engine import Application, document
from retrieval.engine import Engine as SearchEngine


def _t(conn: Connection, name: str) -> str:
    from changes.record import namespace

    return namespace(conn).qualified(conn, "changes", name)


def application(conn: Connection) -> Application:
    """The application for the generations whose vectors live in the engine."""
    profiles = {}
    if conn.scalar(text("SELECT to_regclass(:t)"), {"t": _t(conn, "index_generation")}) is not None:
        for row in conn.execute(
            text(
                f"SELECT vectors_table, profile FROM {_t(conn, 'index_generation')}"
                " WHERE backend = 'vespa' AND state <> 'retired'"
            )
        ):
            profiles[row.vectors_table] = row.profile["dims"]
    return Application(profiles=profiles)


def application_hash(app: Application) -> str:
    return hashlib.sha256(app.schema().encode() + app.services().encode()).hexdigest()


def state(conn: Connection) -> dict:
    row = conn.execute(text(f"SELECT * FROM {_t(conn, 'engine_state')}")).mappings().first()
    return dict(row) if row else {}


def _save(conn: Connection, **values) -> None:
    table = _t(conn, "engine_state")
    if conn.scalar(text(f"SELECT count(*) FROM {table}")) == 0:
        conn.execute(text(f"INSERT INTO {table} (engine) VALUES ('vespa')"))
    sets = ", ".join(f"{k} = :{k}" for k in values)
    conn.execute(text(f"UPDATE {table} SET {sets}"), values)


def deploy(db: Engine, engine: SearchEngine) -> str:
    """Deploy the application for the current generations; a changed application is rebuilt next."""
    with db.begin() as conn:
        app = application(conn)
    engine.deploy(app)
    digest = application_hash(app)
    with db.begin() as conn:
        if state(conn).get("application_hash") != digest:
            _save(conn, application_hash=digest, publication_revision=0, feed_epoch=None)
    return digest


def _documents(conn: Connection, work_id: str) -> dict[str, list[dict]]:
    """The engine documents a work should have now: each indexed version's units and document unit."""
    from api import text_index

    units_table, _ = text_index._tables(conn)
    versions = (
        conn.execute(
            text(
                f"SELECT DISTINCT ON (version_id) version_id, work_id, jurisdiction_id, document_type, legal_status,"
                f" year, replaced FROM {units_table} WHERE work_id = :w ORDER BY version_id"
            ),
            {"w": work_id},
        )
        .mappings()
        .all()
    )
    if not versions:
        return {}
    from sqlalchemy import select

    from api.store import tables

    sources = tables["sources"]
    source = conn.scalar(select(sources.c.payload).where(sources.c.id == work_id)) or {}
    title, citation = source.get("title") or "", source.get("citation") or ""
    found: dict[str, list[dict]] = {}
    for facts in versions:
        loaded = text_index._load(conn, facts["version_id"])
        if loaded is None:
            continue
        version, nodes = loaded
        raw = version.canonical_text.encode()
        config = text_index.config_for(version.language)
        base = {
            "version_id": version.id, "work_id": work_id, "language": version.language, "config": config,
            "jurisdiction_id": facts["jurisdiction_id"], "document_type": facts["document_type"],
            "legal_status": facts["legal_status"], "year": facts["year"], "effective_from": version.effective_from,
            "effective_until": version.effective_until, "content_hash": version.content_hash,
            "replaced": facts["replaced"],
        }  # fmt: skip
        docs = []
        for unit in text_index.units(version, nodes, source.get("document_type")):
            row = base | {
                "start_byte": unit.start, "end_byte": unit.end, "node_id": unit.node.id, "kind": unit.node.kind,
                "locator": unit.node.locator, "pinpoint": unit.pinpoint, "heading": unit.node.heading,
                "unit_kind": "unit",
            }  # fmt: skip
            docs.append(
                document(
                    row,
                    raw[unit.start : unit.end].decode(),
                    unit.headings,
                    title,
                    text_index.identifiers(citation, unit.pinpoint),
                )  # fmt: skip
            )
        whole = text_index.document_unit(version, nodes)
        names = text_index.name_forms([title, citation, *text_index._aliases(conn, work_id)])
        row = base | {"start_byte": -1, "end_byte": -1, "node_id": whole.node_id, "kind": "document",
                      "locator": "document", "pinpoint": None, "heading": None, "unit_kind": "document"}  # fmt: skip
        doc = document(row, raw[whole.start : whole.end].decode(), whole.outline, title,
                       text_index.identifiers(citation, None) + " " + " ".join(names))  # fmt: skip
        doc["start_byte"], doc["end_byte"] = whole.start, whole.end
        doc["unit_id"] = f"{version.id}:document"
        docs.append(doc)
        found[version.id] = docs
    return found


def reconcile(db: Engine, engine: SearchEngine, work_ids: list[str]) -> dict:
    """Make the engine hold exactly each work's public units."""
    fed = removed = 0
    for work_id in work_ids:
        with db.begin() as conn:
            wanted = _documents(conn, work_id)
        held = {d["version_id"] for d in engine.visit(f'unit.work_id=="{work_id}"', fields="version_id")}
        for version_id in sorted(held - set(wanted)):
            removed += engine.delete_version(version_id)
        for version_id, docs in sorted(wanted.items()):
            if version_id in held:
                continue
            for doc in docs:
                engine.put(doc)
                fed += 1
    return {"works": len(work_ids), "fed": fed, "removed": removed}


def _all_works(db: Engine) -> list[str]:
    from api import text_index

    with db.begin() as conn:
        units_table, _ = text_index._tables(conn)
        return sorted(conn.scalars(text(f"SELECT DISTINCT work_id FROM {units_table}")).all())


def sync(db: Engine, engine: SearchEngine) -> dict:
    """Catch the engine up with the change log since its checkpoint (or rebuild after a reset)."""
    from changes.feed import Feed

    with db.begin() as conn:
        feed = Feed(conn, None)
        epoch, high = str(feed.state()["epoch"]), feed.high()
        held = state(conn)
        if not held or str(held.get("feed_epoch")) != epoch or not held.get("publication_revision"):
            rebuild_needed = True
            works: list[str] = []
        else:
            rebuild_needed = False
            works = sorted(
                conn.scalars(
                    text(
                        f"SELECT DISTINCT work_id FROM {_t(conn, 'change_event')}"
                        " WHERE publication_revision > :r AND publication_revision <= :h"
                    ),
                    {"r": held["publication_revision"], "h": high},
                ).all()
            )
    if rebuild_needed:
        engine_works = sorted({d["work_id"] for d in engine.visit(fields="work_id")})
        works = sorted(set(_all_works(db)) | set(engine_works))
    report = reconcile(db, engine, works)
    with db.begin() as conn:
        now = datetime.now(UTC)
        _save(conn, feed_epoch=epoch, publication_revision=high, synced_at=now,
              **({"rebuilt_at": now} if rebuild_needed else {}))  # fmt: skip
    return report | {"rebuilt": rebuild_needed, "publication_revision": high}


def rebuild(db: Engine, engine: SearchEngine) -> dict:
    with db.begin() as conn:
        _save(conn, publication_revision=0, feed_epoch=None)
    return sync(db, engine)


def main(argv=None) -> int:
    from api.store import engine_for
    from retrieval.engine import config_from_env

    command = (argv or sys.argv[1:] or ["status"])[0]
    config = config_from_env()
    if config is None:
        print("Set OPEN_ALVARY_ENGINE_URL (and OPEN_ALVARY_ENGINE_CONFIG_URL to deploy).", file=sys.stderr)
        return 2
    db, engine = engine_for(), SearchEngine(*config)
    if command == "deploy":
        print(deploy(db, engine))
    elif command == "sync":
        print(json.dumps(sync(db, engine)))
    elif command == "rebuild":
        print(json.dumps(rebuild(db, engine)))
    else:
        with db.begin() as conn:
            print(json.dumps(state(conn), default=str, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
