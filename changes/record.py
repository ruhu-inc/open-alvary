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

from sqlalchemy import or_, select, text
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
# Public events say which version of their shape they are; new event types and fields may be added
# within a version, and consumers ignore what they do not know (SUB-02).
SCHEMA_VERSION = "change-event-1"
# A correction may show a field's previous value only where it can never have been withheld (SUB C1).
SAFE_PREVIOUS = ("legal_status", "rights_status", "document_type", "year")
# Checked links between works that change the law of the affected work (legal_effect_recorded).
EFFECTS = {
    "repeals": ("repeal", "repealed"),
    "amends": ("amendment", "amended"),
    "supersedes": ("supersession", "superseded"),
}
# A version diff names provisions, not the containers around a text (TXP-04); each list is capped.
DIFF_SKIPPED = frozenset({"document", "contents", "contents_entry", "notice"})
DIFF_CAP = 200


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
            for name in (
                "change_event",
                "feed_state",
                "published_state",
                "coverage_state",
                "rights_revision",
                "link_state",
            )
        }
        self.allocate = ns.qualified(conn, "policy", "allocate_publication_revision") + "()"
        self.events: list[dict] = []
        self.rights_revision: dict[str, int] = {}  # work -> the rights revision in force after this write

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
        extra=None,
        corrects=None,
        legal_effective=None,
    ):
        """Plan one event; ``corrects`` (event_id, event_revision) makes it the next revision of that
        event (SUB C1). Returns the planned event."""
        projection = facts["projection"]
        payload = {
            "schema_version": SCHEMA_VERSION,
            "title": projection.get("title"),
            "citation": projection.get("citation"),
            "summary": summary,
            "changed_fields": list(changed),
            "sources": [projection["canonical_url"]] if projection.get("canonical_url") else [],
            "version": version,
            **({"withdrawal": withdrawal} if withdrawal else {}),
            **(extra or {}),
        }
        if event_type in ("text_version_published", "withdrawal") and work_id in self.rights_revision:
            payload["rights_revision"] = self.rights_revision[work_id]
        event = {
            "event_id": corrects[0] if corrects else uuid.uuid4(),
            "event_revision": corrects[1] + 1 if corrects else 1,
            "supersedes_event_id": corrects[0] if corrects else None,
            "supersedes_revision": corrects[1] if corrects else None,
            "legal_effective": json.dumps(legal_effective) if legal_effective else None,
            "event_type": event_type,
            "logical_event_key": key[:300],
            "reviewed_revision": self.next_revision(event_type, key[:300]),
            "jurisdiction_id": projection.get("jurisdiction_id") or "unknown",
            "work_id": work_id,
            "version_id": version and version["id"],
            "document_class": projection.get("document_type"),
            "language": (version or {}).get("language") or projection.get("language"),
            "change_reason": "corrected" if corrects else reason,
            "payload": payload,
        }
        self.events.append(event)
        return event


def publish(conn: Connection, work_ids: Iterable[str] | None = None) -> int:
    """Append the events for what changed among these works (all when None); returns how many.
    Call inside the transaction that wrote the catalogue, after the writes."""
    if conn.dialect.name != "postgresql":
        return 0
    rec = _Recorder(conn)
    if conn.scalar(text("SELECT to_regclass(:t)"), {"t": rec.t["feed_state"]}) is None:
        return 0  # a database before migration 0023 has no change log
    keeps_links = conn.scalar(text("SELECT to_regclass(:t)"), {"t": rec.t["link_state"]}) is not None
    state = (
        conn.execute(
            text(
                "SELECT epoch, baselined"
                + (", links_baselined" if keeps_links else "")
                + f" FROM {rec.t['feed_state']} FOR UPDATE"
            )
        )
        .mappings()
        .one()
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
    revisions = _rights_revisions(rec, scope)
    if state["baselined"]:
        for work_id in sorted(scope):
            _compare(rec, work_id, now.get(work_id), previous.get(work_id))
    for work_id in sorted(scope):
        _remember(rec, work_id, now.get(work_id), previous.get(work_id))
    # The section index follows the same public state, in the same transaction (migration 0031).
    from api import text_index

    text_index.sync(conn, now, None if work_ids is None else set(work_ids))
    _coverage(rec, record_events=state["baselined"])
    links = []
    if keeps_links:
        announce = bool(state["baselined"] and state["links_baselined"])
        links = _links(rec, scope if announce else None, announce=announce)
    if not state["baselined"]:
        _record_rights(rec, revisions, 0)
        _record_links(rec, links, baseline=True)
        conn.execute(
            text(f"UPDATE {rec.t['feed_state']} SET baselined = true, baseline_at = statement_timestamp()")
        )
        return 0
    if keeps_links:
        _record_links(rec, links, baseline=not state["links_baselined"])
    if rec.events or revisions:
        revision = conn.scalar(text(f"SELECT {rec.allocate}"))
        _record_rights(rec, revisions, revision)
        for event in rec.events:
            conn.execute(
                text(
                    f"INSERT INTO {rec.t['change_event']} (event_id, event_revision, event_type, logical_event_key,"
                    " reviewed_revision, publication_revision, feed_epoch, legal_effective, jurisdiction_id,"
                    " work_id, version_id, document_class, language, change_reason, supersedes_event_id,"
                    " supersedes_revision, payload, payload_hash) VALUES (:event_id, :event_revision, :event_type,"
                    " :logical_event_key, :reviewed_revision, :revision, :epoch, CAST(:legal_effective AS jsonb),"
                    " :jurisdiction_id, :work_id, :version_id, :document_class, :language, :change_reason,"
                    " :supersedes_event_id, :supersedes_revision, CAST(:payload AS jsonb), :hash)"
                ),
                event
                | {
                    "revision": revision,
                    "epoch": state["epoch"],
                    "payload": json.dumps(event["payload"], ensure_ascii=False),
                    "hash": digest(event["payload"]),
                },
            )
    return len(rec.events)


def _rights_revisions(rec: _Recorder, scope) -> list[dict]:
    """The works whose rights record differs from its last recorded revision, each with its next
    revision; and, in rec.rights_revision, the revision in force for every work in scope (OA-R1)."""
    table = rec.t["rights_revision"]
    if not scope or rec.conn.scalar(text("SELECT to_regclass(:t)"), {"t": table}) is None:
        return []  # a database before migration 0029 keeps no rights history
    ids = sorted(scope)
    latest = {
        row.work_id: (row.revision, row.record_hash)
        for row in rec.conn.execute(
            text(
                f"SELECT DISTINCT ON (work_id) work_id, revision, record_hash FROM {table}"
                " WHERE work_id = ANY(:ids) ORDER BY work_id, revision DESC"
            ),
            {"ids": ids},
        )
    }
    rights = tables["rights"]
    changed = []
    for row in rec.conn.execute(
        select(rights.c.source_id, rights.c.payload).where(rights.c.source_id.in_(ids))
    ):
        record_hash, (number, held) = digest(row.payload), latest.get(row.source_id, (0, None))
        if held == record_hash:
            rec.rights_revision[row.source_id] = number
            continue
        rec.rights_revision[row.source_id] = number + 1
        changed.append(
            {"work_id": row.source_id, "revision": number + 1, "record": row.payload, "hash": record_hash}
        )
    return changed


def _record_rights(rec: _Recorder, revisions: list[dict], publication_revision: int) -> None:
    for item in revisions:
        rec.conn.execute(
            text(
                f"INSERT INTO {rec.t['rights_revision']} (work_id, revision, publication_revision, record,"
                " record_hash) VALUES (:work_id, :revision, :at, CAST(:record AS jsonb), :hash)"
            ),
            item | {"at": publication_revision, "record": json.dumps(item["record"], ensure_ascii=False)},
        )


def _links(rec: _Recorder, scope, *, announce: bool) -> list[tuple]:
    """The checked repeals, amendments and supersessions between two public works that concern these
    works (all when None), compared with the last recorded state: a new one is a legal_effect_recorded
    event on the affected work, a changed one a correction of its event (SUB C1; OA-R2). Returns the
    state to record. Without ``announce`` (the first comparison) nothing is announced."""
    amendments = tables["amendments"]
    query = select(
        amendments.c.id, amendments.c.source_id, amendments.c.target_source_id, amendments.c.payload
    )
    if scope is not None:
        ids = sorted(scope)
        query = query.where(or_(amendments.c.source_id.in_(ids), amendments.c.target_source_id.in_(ids)))
    rows = [
        r for r in rec.conn.execute(query) if r.payload.get("kind") in EFFECTS and r.payload.get("verified")
    ]
    works = sorted({w for r in rows for w in (r.source_id, r.target_source_id)})
    visible = current(rec.conn, works) if works else {}
    public = {
        r.id: {
            "kind": r.payload["kind"],
            "affected": r.target_source_id,
            "by": r.source_id,
            "effective_date": r.payload.get("effective_date"),
            "evidence_url": r.payload.get("evidence_url"),
        }
        for r in rows
        if r.source_id in visible and r.target_source_id in visible
    }
    table = rec.t["link_state"]
    held_query = f"SELECT link_id, projection_hash, event_id, event_revision FROM {table}"
    params = {}
    if scope is not None:
        held_query += " WHERE link_id = ANY(:links) OR work_id = ANY(:ids) OR projection->>'by' = ANY(:ids)"
        params = {"links": sorted(public), "ids": sorted(scope)}
    held = {row.link_id: row for row in rec.conn.execute(text(held_query), params)}
    states = []
    for link_id, link in sorted(public.items()):
        link_hash, was = digest(link), held.get(link_id)
        if was is not None and was.projection_hash == link_hash:
            continue
        event = None
        if announce:
            affected, by = visible[link["affected"]], visible[link["by"]]["projection"]
            noun, verb = EFFECTS[link["kind"]]
            corrects = (was.event_id, was.event_revision) if was is not None and was.event_id else None
            title = affected["projection"].get("title")
            event = rec.add(
                "legal_effect_recorded",
                link_id,
                link["affected"],
                affected,
                summary=f"The record of how {by.get('title')} {verb} {title} was corrected."
                if corrects
                else f"{title} was {verb} by {by.get('title')}.",
                extra={
                    "legal_effect": {
                        "kind": noun,
                        "by": {"id": link["by"], "title": by.get("title"), "citation": by.get("citation")},
                        "effective_date": link["effective_date"],
                        "evidence_url": link["evidence_url"],
                    }
                },
                corrects=corrects,
                legal_effective={"from": link["effective_date"], "precision": "day"}
                if link["effective_date"]
                else None,
            )
        states.append(("put", link_id, link, link_hash, event))
    for link_id in sorted(held.keys() - public.keys()):
        states.append(("drop", link_id, None, None, None))
    return states


def _record_links(rec: _Recorder, states: list[tuple], *, baseline: bool) -> None:
    table = rec.t["link_state"]
    for action, link_id, link, link_hash, event in states:
        if action == "drop":
            rec.conn.execute(text(f"DELETE FROM {table} WHERE link_id = :l"), {"l": link_id})
            continue
        rec.conn.execute(
            text(
                f"INSERT INTO {table} (link_id, work_id, projection, projection_hash, event_id, event_revision)"
                " VALUES (:l, :w, CAST(:p AS jsonb), :h, :e, :r) ON CONFLICT (link_id) DO UPDATE SET"
                " work_id = EXCLUDED.work_id, projection = EXCLUDED.projection,"
                " projection_hash = EXCLUDED.projection_hash,"
                f" event_id = coalesce(EXCLUDED.event_id, {table}.event_id),"
                f" event_revision = coalesce(EXCLUDED.event_revision, {table}.event_revision),"
                " updated_at = statement_timestamp()"
            ),
            {
                "l": link_id,
                "w": link["affected"],
                "p": json.dumps(link, ensure_ascii=False),
                "h": link_hash,
                "e": event and event["event_id"],
                "r": event and event["event_revision"],
            },
        )
    if baseline:
        rec.conn.execute(text(f"UPDATE {rec.t['feed_state']} SET links_baselined = true"))


def _version_diff(conn: Connection, version_id: str) -> dict | None:
    """The provisions a version adds, removes or changes against the version it names as prior, by
    locator, in document order; changed means the provision's bytes differ (TXP-04)."""
    versions, structure = tables["versions"], tables["structure"]
    new = conn.scalar(select(versions.c.payload).where(versions.c.id == version_id)) or {}
    prior_id = new.get("prior_version_id")
    prior = conn.scalar(select(versions.c.payload).where(versions.c.id == prior_id)) if prior_id else None
    if not prior:
        return None

    def provisions(version):
        raw = version["canonical_text"].encode()
        found = {}
        for node in conn.scalars(select(structure.c.payload).where(structure.c.version_id == version["id"])):
            if node.get("kind") in DIFF_SKIPPED:
                continue
            span = (
                node.get("span_hash")
                or hashlib.sha256(raw[node["start_byte"] : node["end_byte"]]).hexdigest()
            )
            found[node["locator"]] = (node["start_byte"], span)
        return found

    now, before = provisions(new), provisions(prior)

    def ordered(keys, where):
        return [k for k in sorted(keys, key=lambda k: (where[k][0], k))]

    added = ordered(now.keys() - before.keys(), now)
    removed = ordered(before.keys() - now.keys(), before)
    changed = ordered({k for k in now.keys() & before.keys() if now[k][1] != before[k][1]}, now)
    return {
        "prior_version_id": prior_id,
        "added": added[:DIFF_CAP],
        "removed": removed[:DIFF_CAP],
        "changed": changed[:DIFF_CAP],
        "truncated": any(len(found) > DIFF_CAP for found in (added, removed, changed)),
    }


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
            extra={"previous": {f: before["projection"].get(f) for f in changed if f in SAFE_PREVIOUS}},
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
        extra={"version_diff": diff} if (diff := _version_diff(rec.conn, version_id)) else None,
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
                    "event_id": uuid.uuid4(),
                    "event_revision": 1,
                    "supersedes_event_id": None,
                    "supersedes_revision": None,
                    "legal_effective": None,
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
                        "schema_version": SCHEMA_VERSION,
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
