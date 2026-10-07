"""PostgreSQL shadow migration operations with caller-owned transactions.

Only maintenance credentials may use these writes. There is no live cutover command,
target policy writer, or public route. Rehearsal deliberately closes persistent reads.
"""

import json
from datetime import UTC, datetime
from hashlib import sha256

from sqlalchemy import select, text
from sqlalchemy.engine import Connection

from api.store import RELATIONS, tables
from rights.policy import decide, public_corpus
from schema.identity import PublicWorkIdentity, Work, mint
from schema.models import Corpus, RightsRecord, Source

SOURCE_FIELDS = {
    "jurisdiction_id": "jurisdiction_id",
    "authority_id": "authority_id",
    "document_type": "document_class",
    "title": "title",
    "citation": "citation",
    "canonical_url": "reference_url",
    "url_kind": "reference_kind",
    "language": "catalogue_language_id",
    "legal_status": "legacy_legal_status",
    "status_evidence": "status_evidence_url",
    "catalogue_note": "catalogue_note",
}
COLLECTION_UNKNOWN = "Legacy catalogue does not establish a provider collection"
EXPRESSION_DEFERRED = "Legacy language does not establish a legal-content edition"
VERSION_DEFERRED = "Legacy bytes retained; edition, raw artifact metadata and OA-text-1 qualification absent"


class IdentityConflict(ValueError):
    """Safe maintenance conflict; never include raw records in its message."""


def digest(value) -> str:
    return sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _state(conn: Connection, expected_epoch: int):
    row = (
        conn.execute(text("SELECT * FROM identity_migration_state WHERE singleton FOR UPDATE"))
        .mappings()
        .one()
    )
    if row["epoch"] != expected_epoch:
        raise IdentityConflict("Stale migration epoch")
    return row


def _capture(conn: Connection):
    """Full capture is maintenance-only; every caller holds the writer-excluding state lock."""
    records = {}
    for name, table in tables.items():
        for row in conn.execute(select(table).order_by(table.c.id)).mappings():
            payload = row["payload"]
            if row["id"] != payload.get("id") or any(row[k] != payload.get(k) for k in RELATIONS[name]):
                raise IdentityConflict("Legacy relational and payload identities differ")
            records[(name, row["id"])] = payload
    corpus = Corpus.model_validate(
        {name: [p for (n, _), p in records.items() if n == name] for name in tables}
    )
    return records, corpus


def _checkpoint(records):
    return digest([[name, key, value] for (name, key), value in sorted(records.items())])


def _source_values(payload):
    source = Source.model_validate(payload).model_dump(mode="json")
    return {target: source[field] for field, target in SOURCE_FIELDS.items()}


def backfill(conn: Connection, *, expected_epoch: int) -> str:
    """Idempotent atomic shadow copy. Legacy writes wait, then remain authoritative.

    This bounded pilot copy takes a state-row exclusive lock until caller commit. Failure
    rolls back all rows/IDs/checkpoints. Never treat its snapshots as serving authority.
    """
    state = _state(conn, expected_epoch)
    if state["phase"] not in {"legacy", "frozen"}:
        raise IdentityConflict("Backfill requires legacy authority")
    records, _ = _capture(conn)
    for (name, key), payload in records.items():
        if name != "sources":
            continue
        work_id = conn.scalar(
            text("SELECT work_id FROM identity_legacy_sources WHERE source_id=:id"), {"id": key}
        )
        values = _source_values(payload)
        values.update(reason=COLLECTION_UNKNOWN)
        columns = ",".join(SOURCE_FIELDS.values())
        parameters = ",".join(":" + field for field in SOURCE_FIELDS.values())
        updates = ",".join(f"{field}=EXCLUDED.{field}" for field in SOURCE_FIELDS.values())
        if work_id is None:
            for _ in range(5):
                values["id"] = mint("wrk")
                work_id = conn.scalar(
                    text(f"""
                    INSERT INTO identity_works(id,{columns},collection_unknown_reason)
                    VALUES (:id,{parameters},:reason) ON CONFLICT (id) DO NOTHING RETURNING id
                """),
                    values,
                )
                if work_id is not None:
                    break
            else:
                raise IdentityConflict("Identity mint collision retries exhausted")
        else:
            values["id"] = work_id
            conn.execute(
                text(f"""
            INSERT INTO identity_works(id,{columns},collection_unknown_reason)
            VALUES (:id,{parameters},:reason)
            ON CONFLICT (id) DO UPDATE SET {updates},updated_at=CURRENT_TIMESTAMP
            """),
                values,
            )
        conn.execute(
            text("""
            INSERT INTO identity_legacy_sources(source_id,work_id,expression_deferred_reason,source_digest)
            VALUES (:id,:work,:reason,:digest)
            ON CONFLICT (source_id) DO UPDATE SET source_digest=EXCLUDED.source_digest
        """),
            {"id": key, "work": work_id, "reason": EXPRESSION_DEFERRED, "digest": digest(payload)},
        )
    for (name, key), payload in records.items():
        if name == "versions":
            conn.execute(
                text("""
                INSERT INTO identity_legacy_versions(version_id,source_id,raw_hash,content_hash,
                    deferred_reason,source_digest)
                VALUES (:id,:source,:raw,:content,:reason,:digest)
                ON CONFLICT (version_id) DO UPDATE SET raw_hash=EXCLUDED.raw_hash,
                    content_hash=EXCLUDED.content_hash,source_digest=EXCLUDED.source_digest
            """),
                {
                    "id": key,
                    "source": payload["source_id"],
                    "raw": payload["raw_hash"],
                    "content": payload["content_hash"],
                    "reason": VERSION_DEFERRED,
                    "digest": digest(payload),
                },
            )
    conn.execute(text("DELETE FROM identity_legacy_snapshots"))
    for (name, key), payload in records.items():
        conn.execute(
            text("""
            INSERT INTO identity_legacy_snapshots(entity_type,legacy_id,payload,digest)
            VALUES (:name,:id,CAST(:payload AS jsonb),:digest)
        """),
            {"name": name, "id": key, "payload": json.dumps(payload), "digest": digest(payload)},
        )
    checkpoint = _checkpoint(records)
    conn.execute(
        text("""
        UPDATE identity_migration_state SET source_checkpoint=:hash,reconciled_hash=NULL,
            operator_evidence=NULL,updated_at=CURRENT_TIMESTAMP WHERE singleton
    """),
        {"hash": checkpoint},
    )
    return checkpoint


def freeze(conn: Connection, *, expected_epoch: int) -> int:
    state = _state(conn, expected_epoch)
    if state["phase"] != "legacy":
        raise IdentityConflict("Freeze requires legacy phase")
    conn.execute(
        text("""
        UPDATE identity_migration_state SET phase='frozen',epoch=epoch+1,
            reconciled_hash=NULL,operator_evidence=NULL,updated_at=CURRENT_TIMESTAMP WHERE singleton
    """)
    )
    return expected_epoch + 1


def reconcile(conn: Connection, *, expected_epoch: int) -> str:
    state = _state(conn, expected_epoch)
    if state["phase"] != "frozen":
        raise IdentityConflict("Reconciliation requires a committed maintenance fence")
    records, corpus = _capture(conn)
    shadow = {}
    for row in conn.execute(text("SELECT * FROM identity_legacy_snapshots")).mappings():
        if digest(row["payload"]) != row["digest"]:
            raise IdentityConflict("Shadow snapshot digest differs")
        shadow[(row["entity_type"], row["legacy_id"])] = row["payload"]
    # Exact payload equality includes policy vectors, actors/evidence/expiry, canonical
    # bytes/hashes, aliases and all other legacy fields; cardinality alone is insufficient.
    if records != shadow or state["source_checkpoint"] != _checkpoint(records):
        raise IdentityConflict("Legacy snapshot changed; final backfill required")
    mappings = {
        r["source_id"]: r for r in conn.execute(text("SELECT * FROM identity_legacy_sources")).mappings()
    }
    works = {r["id"]: r for r in conn.execute(text("SELECT * FROM identity_works")).mappings()}
    sources = {key: p for (name, key), p in records.items() if name == "sources"}
    if set(mappings) != set(sources) or set(works) != {r["work_id"] for r in mappings.values()}:
        raise IdentityConflict("Shadow source mappings differ")
    for key, payload in sources.items():
        mapping = mappings[key]
        work = works[mapping["work_id"]]
        if (
            mapping["source_digest"] != digest(payload)
            or mapping["expression_deferred_reason"] != EXPRESSION_DEFERRED
            or any(work[field] != value for field, value in _source_values(payload).items())
            or work["identity_status"] != "legacy_unreviewed"
            or work["collection_id"] is not None
            or work["collection_unknown_reason"] != COLLECTION_UNKNOWN
        ):
            raise IdentityConflict("Shadow typed identity differs")
    version_maps = {
        r["version_id"]: r for r in conn.execute(text("SELECT * FROM identity_legacy_versions")).mappings()
    }
    versions = {key: p for (name, key), p in records.items() if name == "versions"}
    if set(version_maps) != set(versions):
        raise IdentityConflict("Shadow version mappings differ")
    for key, payload in versions.items():
        row = version_maps[key]
        if (
            any(row[field] != payload[field] for field in ("source_id", "raw_hash", "content_hash"))
            or row["source_digest"] != digest(payload)
            or row["deferred_reason"] != VERSION_DEFERRED
            or row["normalization_status"] != "legacy_unqualified"
        ):
            raise IdentityConflict("Shadow version evidence differs")
    # This slice cannot infer reviewed editions or observed artifacts from catalogue links.
    for table in (
        "identity_expressions",
        "identity_manifestations",
        "identity_observations",
        "identity_work_manifestations",
        "identity_collections",
    ):
        if conn.scalar(text(f"SELECT EXISTS(SELECT 1 FROM {table})")):
            raise IdentityConflict("Additional target evidence requires a later reconciliation contract")
    shadow_corpus = Corpus.model_validate(
        {name: [p for (n, _), p in shadow.items() if n == name] for name in tables}
    )
    if public_corpus(corpus).model_dump(mode="json") != public_corpus(shadow_corpus).model_dump(mode="json"):
        raise IdentityConflict("Public projections differ")
    checkpoint = _checkpoint(records)
    conn.execute(
        text("UPDATE identity_migration_state SET reconciled_hash=:hash WHERE singleton"),
        {"hash": checkpoint},
    )
    return checkpoint


def rehearse_cutover(conn: Connection, *, expected_epoch: int, evidence: str) -> int:
    """Synthetic rehearsal only; no target writer exists and persistent reads deny."""
    if not evidence.strip():
        raise IdentityConflict("Rehearsal requires operator evidence")
    reconcile(conn, expected_epoch=expected_epoch)
    conn.execute(
        text("""
        UPDATE identity_migration_state SET phase='rehearsal',epoch=epoch+1,
            operator_evidence=:evidence,updated_at=CURRENT_TIMESTAMP WHERE singleton
    """),
        {"evidence": evidence},
    )
    return expected_epoch + 1


def end_rehearsal(conn: Connection, *, expected_epoch: int) -> int:
    state = _state(conn, expected_epoch)
    if state["phase"] != "rehearsal":
        raise IdentityConflict("No rehearsal to end")
    conn.execute(text("UPDATE identity_migration_state SET phase='frozen',epoch=epoch+1 WHERE singleton"))
    return expected_epoch + 1


def resume_legacy(conn: Connection, *, expected_epoch: int) -> int:
    """Pre-cutover maintenance rollback only. Target writers are never enabled here."""
    state = _state(conn, expected_epoch)
    if state["phase"] != "frozen":
        raise IdentityConflict("Resume requires frozen phase")
    conn.execute(
        text("""
        UPDATE identity_migration_state SET phase='legacy',epoch=epoch+1,
            reconciled_hash=NULL,operator_evidence=NULL WHERE singleton
    """)
    )
    return expected_epoch + 1


class IdentityRepository:
    """Bounded shadow lookups. Caller owns transaction and maintenance/read credentials."""

    def __init__(self, connection: Connection):
        self.connection = connection

    def work_for_legacy_id(self, source_id: str) -> Work | None:
        row = (
            self.connection.execute(
                text("""
            SELECT w.* FROM identity_works w JOIN identity_legacy_sources m ON m.work_id=w.id
            WHERE m.source_id=:id
        """),
                {"id": source_id},
            )
            .mappings()
            .one_or_none()
        )
        return Work.model_validate(dict(row)) if row is not None else None

    def public_identity(self, source_id: str) -> PublicWorkIdentity | None:
        from api.store import check_legacy_authority

        check_legacy_authority(self.connection, write=False)
        row = (
            self.connection.execute(
                text("""
            SELECT s.payload AS source,r.payload AS rights,m.source_digest FROM sources s
            JOIN rights r ON r.source_id=s.id JOIN identity_legacy_sources m ON m.source_id=s.id
            WHERE s.id=:id
        """),
                {"id": source_id},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        rights = RightsRecord.model_validate(row["rights"])
        if not decide(rights).metadata or (rights.expires_at and rights.expires_at <= datetime.now(UTC)):
            return None
        if digest(row["source"]) != row["source_digest"]:
            raise IdentityConflict("Identity shadow requires refresh")
        work = self.work_for_legacy_id(source_id)
        if work is None or any(
            work.model_dump(mode="json")[field] != value
            for field, value in _source_values(row["source"]).items()
        ):
            raise IdentityConflict("Identity shadow differs")
        source = Source.model_validate(row["source"])
        return PublicWorkIdentity(
            legacy_source_id=source_id,
            work_id=work.id,
            title=source.title,
            citation=source.citation,
            reference_url=source.canonical_url,
        )
