"""Record public catalogue changes as events, inside the transaction that makes them (SUB C1).

changes-contracts.md: every write to the public catalogue calls ``publish`` before it commits. It
computes each affected work's public projection with the read path's own rules (the legacy
repository's visibility and ``rights.policy.decide``), compares it with the last recorded one and
appends events for the differences, all under one LIF-02 publication revision. The first call
records a baseline without events, so existing records are never announced as new. ``sweep`` runs
the same comparison over every work, for changes nobody writes, such as a rights record's expiry.
"""

import hashlib
import json
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime

from sqlalchemy import select, text
from sqlalchemy.engine import Connection

from api.repositories import LegacyRepository
from api.store import tables
from rights.database import PolicyNamespace
from rights.policy import decide

# The public fields a correction compares; text availability is reported by text events instead.
PUBLIC_FIELDS = (
    "title",
    "citation",
    "document_type",
    "jurisdiction_id",
    "authority_id",
    "language",
    "canonical_url",
    "url_kind",
    "legal_status",
    "status_evidence",
    "catalogue_note",
    "summary",
    "year",
    "rights_status",
)


def digest(value) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def namespace(conn: Connection) -> PolicyNamespace:
    return PolicyNamespace(conn.scalar(text("SELECT current_schema()")))


def current(conn: Connection, work_ids: Iterable[str] | None = None) -> dict[str, dict]:
    """The public projection of each visible work (all of them, or the given ones): its public
    fields and the text versions anyone may read now."""
    repository = LegacyRepository(conn)
    now = datetime.now(UTC)
    pairs = repository.sources() if work_ids is None else (repository.source(i) for i in work_ids)
    found = {}
    for pair in pairs:
        if pair is None:
            continue
        source, rights = pair
        if not decide(rights, now=now).metadata:
            continue
        readable = [
            gate.id
            for gate in repository.version_gates(source.id, rights.approved_content_hashes)
            if decide(rights, gate, now=now).full_text
        ]
        public = source.model_dump(mode="json") | {"rights_status": rights.status}
        found[source.id] = {
            "projection": {field: public.get(field) for field in PUBLIC_FIELDS},
            "versions": _version_facts(conn, readable),
        }
    return found


def _version_facts(conn, ids):
    if not ids:
        return {}
    versions = tables["versions"]
    rows = conn.execute(select(versions.c.id, versions.c.payload).where(versions.c.id.in_(ids))).all()
    return {
        row.id: {"content_hash": row.payload["content_hash"], "language": row.payload.get("language")}
        for row in rows
    }


def _names(conn, ids):
    jurisdictions = tables["jurisdictions"]
    rows = conn.execute(
        select(jurisdictions.c.id, jurisdictions.c.payload).where(jurisdictions.c.id.in_(ids))
    )
    return {row.id: row.payload.get("name", row.id) for row in rows}


class _Recorder:
    def __init__(self, conn: Connection):
        self.conn = conn
        ns = namespace(conn)
        self.t = {
            name: ns.qualified(conn, "changes", name)
            for name in ("change_event", "feed_state", "published_state", "coverage_state")
        }
        self.allocate = ns.qualified(conn, "policy", "allocate_publication_revision") + "()"
        self.events: list[dict] = []

    def next_revision(self, event_type, key):
        planned = sum(
            1 for e in self.events if e["event_type"] == event_type and e["logical_event_key"] == key
        )
        stored = self.conn.scalar(
            text(
                f"SELECT coalesce(max(reviewed_revision), 0) FROM {self.t['change_event']}"
                " WHERE event_type = :t AND logical_event_key = :k"
            ),
            {"t": event_type, "k": key},
        )
        return stored + planned + 1

    def add(
        self,
        event_type,
        key,
        work_id,
        facts,
        *,
        summary,
        version=None,
        reason=None,
        changed=(),
        withdrawal=None,
    ):
        projection = facts["projection"]
        payload = {
            "title": projection.get("title"),
            "citation": projection.get("citation"),
            "summary": summary,
            "changed_fields": list(changed),
            "sources": [projection["canonical_url"]] if projection.get("canonical_url") else [],
            "version": version,
            **({"withdrawal": withdrawal} if withdrawal else {}),
        }
        self.events.append(
            {
                "event_type": event_type,
                "logical_event_key": key[:300],
                "reviewed_revision": self.next_revision(event_type, key[:300]),
                "jurisdiction_id": projection.get("jurisdiction_id") or "unknown",
                "work_id": work_id,
                "version_id": version and version["id"],
                "document_class": projection.get("document_type"),
                "language": (version or {}).get("language") or projection.get("language"),
                "change_reason": reason,
                "payload": payload,
            }
        )


def publish(conn: Connection, work_ids: Iterable[str] | None = None) -> int:
    """Append the events for what changed among these works (all when None); returns how many.
    Call inside the transaction that wrote the catalogue, after the writes."""
    if conn.dialect.name != "postgresql":
        return 0
    rec = _Recorder(conn)
    if conn.scalar(text("SELECT to_regclass(:t)"), {"t": rec.t["feed_state"]}) is None:
        return 0  # a database before migration 0023 has no change log
    state = (
        conn.execute(text(f"SELECT epoch, baselined FROM {rec.t['feed_state']} FOR UPDATE")).mappings().one()
    )
    if not state["baselined"]:
        work_ids = None  # the baseline covers everything
    now = current(conn, work_ids)
    previous = {
        row["work_id"]: row
        for row in conn.execute(
            text(
                f"SELECT work_id, jurisdiction_id, visible, projection, projection_hash, readable_versions"
                f" FROM {rec.t['published_state']}"
                + ("" if work_ids is None else " WHERE work_id = ANY(:ids)")
            ),
            {} if work_ids is None else {"ids": list(work_ids)},
        ).mappings()
    }
    scope = set(now) | set(previous) if work_ids is None else set(work_ids)
    if state["baselined"]:
        for work_id in sorted(scope):
            _compare(rec, work_id, now.get(work_id), previous.get(work_id))
    for work_id in sorted(scope):
        _remember(rec, work_id, now.get(work_id), previous.get(work_id))
    _coverage(rec, record_events=state["baselined"])
    if not state["baselined"]:
        conn.execute(
            text(f"UPDATE {rec.t['feed_state']} SET baselined = true, baseline_at = statement_timestamp()")
        )
        return 0
    if rec.events:
        revision = conn.scalar(text(f"SELECT {rec.allocate}"))
        for event in rec.events:
            conn.execute(
                text(
                    f"INSERT INTO {rec.t['change_event']} (event_id, event_revision, event_type, logical_event_key,"
                    " reviewed_revision, publication_revision, feed_epoch, jurisdiction_id, work_id, version_id,"
                    " document_class, language, change_reason, payload, payload_hash) VALUES (:id, 1, :event_type,"
                    " :logical_event_key, :reviewed_revision, :revision, :epoch, :jurisdiction_id, :work_id,"
                    " :version_id, :document_class, :language, :change_reason, CAST(:payload AS jsonb), :hash)"
                ),
                event
                | {
                    "id": uuid.uuid4(),
                    "revision": revision,
                    "epoch": state["epoch"],
                    "payload": json.dumps(event["payload"], ensure_ascii=False),
                    "hash": digest(event["payload"]),
                },
            )
    return len(rec.events)


def sweep(conn: Connection) -> int:
    """Compare every work: catches changes no write made, such as an expired rights record."""
    return publish(conn, None)


def expired(conn: Connection, now: datetime | None = None) -> int:
    """Compare the works whose rights review has expired, so what an expiry took out of public view
    is in the change log within one worker run (every five minutes), not at the next daily sweep.
    Works already compared since they expired add nothing."""
    if conn.dialect.name != "postgresql":
        return 0
    rights, now = tables["rights"], now or datetime.now(UTC)
    rows = conn.execute(select(rights.c.source_id, rights.c.payload["expires_at"].as_string().label("until")))
    ids = [row.source_id for row in rows if row.until and datetime.fromisoformat(row.until) <= now]
    return publish(conn, ids) if ids else 0


def _withdrawn_because(conn: Connection, work_id: str, version_id: str) -> str:
    """The withdrawal notice's reason (access-contracts.md): the rights record lapsed or changed, or the
    text's own approval was withdrawn (a correction) while the rights record still stands."""
    rights, versions = tables["rights"], tables["versions"]
    record = conn.scalar(select(rights.c.payload).where(rights.c.source_id == work_id)) or {}
    version = conn.scalar(select(versions.c.payload).where(versions.c.id == version_id)) or {}
    until = record.get("expires_at")
    if until and datetime.fromisoformat(until) <= datetime.now(UTC):
        return "rights_changed"
    if record.get("status") in ("OPEN", "OPEN_WITH_CONDITIONS") and version.get("content_hash") not in (
        record.get("approved_content_hashes") or []
    ):
        return "correction"
    return "rights_changed"


def _compare(rec: _Recorder, work_id: str, now: dict | None, before) -> None:
    was_visible = before is not None and before["visible"]
    if now and not was_visible:
        restored = before is not None
        title = now["projection"].get("title")
        rec.add(
            "catalogue_added",
            work_id,
            work_id,
            now,
            reason="restored" if restored else None,
            summary=f"{title} is available again in the catalogue."
            if restored
            else f"{title} was added to the Open Alvary catalogue.",
        )
        first = True
        for version_id, facts in sorted(now["versions"].items()):
            _text(rec, work_id, now, version_id, facts, first=first)
            first = False
        return
    if before is None or not was_visible:
        return
    title = (now or {}).get("projection", {}).get("title") or before["projection"].get("title")
    if now is None:
        rec.add(
            "withdrawal",
            f"{work_id}:w",
            work_id,
            {"projection": before["projection"]},
            summary="A previously public record was withdrawn. Revalidate any copies you keep.",
        )
        return
    if digest(now["projection"]) != before["projection_hash"]:
        changed = sorted(f for f in PUBLIC_FIELDS if now["projection"].get(f) != before["projection"].get(f))
        rec.add(
            "metadata_corrected",
            f"{work_id}:{digest(now['projection'])[:16]}",
            work_id,
            now,
            changed=changed,
            summary=f"The catalogue record for {title} was corrected ({', '.join(f.replace('_', ' ') for f in changed)}).",
        )
    had = set(before["readable_versions"])
    for version_id, facts in sorted(now["versions"].items()):
        if version_id not in had:
            _text(rec, work_id, now, version_id, facts, first=not had)
    gone = sorted(had - set(now["versions"]))
    facts = _version_facts(rec.conn, gone)
    for version_id in gone:
        # The record stays public: name the version and say why, so copies of it can be taken down.
        rec.add(
            "withdrawal",
            f"{version_id}:w",
            work_id,
            now,
            version={"id": version_id, **facts.get(version_id, {})},
            summary=f"A text version of {title} was withdrawn. Stop serving copies of it.",
            withdrawal={
                "action": "stop_serving",
                "reason_code": _withdrawn_because(rec.conn, work_id, version_id),
            },
        )


def _text(rec, work_id, now, version_id, facts, *, first):
    title = now["projection"].get("title")
    rec.add(
        "text_version_published",
        version_id,
        work_id,
        now,
        version={"id": version_id, **facts},
        reason="first_text" if first else _reason(rec, version_id),
        summary=f"The text of {title} is now available."
        if first
        else f"A new text version of {title} was published.",
    )


def _reason(rec: _Recorder, version_id: str) -> str:
    """Why a later text version exists (TXP-04): a correction read from the same original file, or a
    new original file; unknown when the version names no earlier one."""
    versions = tables["versions"]
    row = rec.conn.execute(select(versions.c.payload).where(versions.c.id == version_id)).first()
    prior_id = row.payload.get("prior_version_id") if row else None
    prior = (
        rec.conn.execute(select(versions.c.payload).where(versions.c.id == prior_id)).first()
        if prior_id
        else None
    )
    return change_reason(row.payload if row else None, prior.payload if prior else None)


def change_reason(version: dict | None, prior: dict | None) -> str:
    if not version or not prior:
        return "unknown"
    return "extraction_correction" if prior.get("raw_hash") == version.get("raw_hash") else "source_revision"


def _remember(rec: _Recorder, work_id: str, now: dict | None, before) -> None:
    if now is None and before is None:
        return
    projection = now["projection"] if now else before["projection"]
    rec.conn.execute(
        text(
            f"INSERT INTO {rec.t['published_state']} (work_id, jurisdiction_id, visible, projection, projection_hash,"
            " readable_versions) VALUES (:w, :j, :v, CAST(:p AS jsonb), :h, :r) ON CONFLICT (work_id) DO UPDATE"
            " SET jurisdiction_id = EXCLUDED.jurisdiction_id, visible = EXCLUDED.visible,"
            " projection = EXCLUDED.projection, projection_hash = EXCLUDED.projection_hash,"
            " readable_versions = EXCLUDED.readable_versions, updated_at = statement_timestamp()"
        ),
        {
            "w": work_id,
            "j": projection.get("jurisdiction_id") or "unknown",
            "v": now is not None,
            "p": json.dumps(projection, ensure_ascii=False),
            "h": digest(projection),
            "r": sorted(now["versions"]) if now else [],
        },
    )


def _coverage(rec: _Recorder, *, record_events: bool) -> None:
    """A jurisdiction gaining its first public record, or losing its last, is a coverage change."""
    counts = dict(
        rec.conn.execute(
            text(
                f"SELECT jurisdiction_id, count(*) FILTER (WHERE visible) FROM {rec.t['published_state']}"
                " GROUP BY jurisdiction_id"
            )
        ).all()
    )
    before = {
        row.jurisdiction_id: row.snapshot
        for row in rec.conn.execute(text(f"SELECT jurisdiction_id, snapshot FROM {rec.t['coverage_state']}"))
    }
    names = _names(rec.conn, list(set(counts) | set(before)))
    for jurisdiction, count in sorted(counts.items()):
        snapshot = {"has_public_records": count > 0}
        if before.get(jurisdiction) == snapshot:
            continue
        if record_events and (jurisdiction in before or count > 0):
            name = names.get(jurisdiction, jurisdiction)
            rec.events.append(
                {
                    "event_type": "coverage_changed",
                    "logical_event_key": f"{jurisdiction}:{digest(snapshot)[:16]}",
                    "reviewed_revision": rec.next_revision(
                        "coverage_changed", f"{jurisdiction}:{digest(snapshot)[:16]}"
                    ),
                    "jurisdiction_id": jurisdiction,
                    "work_id": jurisdiction,
                    "version_id": None,
                    "document_class": None,
                    "language": None,
                    "change_reason": None,
                    "payload": {
                        "title": name,
                        "citation": None,
                        "summary": f"{name} now has records in the catalogue."
                        if count
                        else f"{name} no longer has public records in the catalogue.",
                        "changed_fields": [],
                        "sources": [],
                        "version": None,
                        "coverage": snapshot,
                    },
                }
            )
        rec.conn.execute(
            text(
                f"INSERT INTO {rec.t['coverage_state']} (jurisdiction_id, snapshot, snapshot_hash) VALUES"
                " (:j, CAST(:s AS jsonb), :h) ON CONFLICT (jurisdiction_id) DO UPDATE SET snapshot = EXCLUDED.snapshot,"
                " snapshot_hash = EXCLUDED.snapshot_hash, updated_at = statement_timestamp()"
            ),
            {"j": jurisdiction, "s": json.dumps(snapshot), "h": digest(snapshot)},
        )
