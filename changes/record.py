"""Record public catalogue changes as events, inside the transaction that makes them (SUB C1).

changes-contracts.md: every write to the public catalogue calls ``publish`` before it commits. It
computes each affected work's public projection with the read path's own rules (the legacy
repository's visibility and ``rights.policy.decide``), compares it with the last recorded one and
appends events for the differences, all under one LIF-02 publication revision. The first call
records a baseline without events, so existing records are never announced as new. ``sweep`` runs
the same comparison over every work, for changes nobody writes, such as a rights record's expiry.

Two times an event may carry besides committed_at (G15). observed_at is when Open Alvary saw at the
official source what the event announces: written for text_version_published, as the retrieved_at of
the official file its text was read from; null for every other type, whose change is Open Alvary's own
(cataloguing, a review, a correction, a rights decision, a withdrawal) and is seen nowhere else.
publisher_published_at is when the publisher published it: the catalogue the log is computed from keeps
no publication date for a record or a text, so it is null on every event; the column is kept for
source_publication_observed, which is not emitted yet.
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
from schema.dates import UncertainDate
from schema.public import RightsViewRecord

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
# A rights record's fields, as GET /rights/{id} serves them: what a rights_changed event names as changed.
RIGHTS_FIELDS = tuple(RightsViewRecord.model_fields)
# The events about a record that carry the revision of its rights record in force after the change they
# announce (OA-R1), as rights_revision. Recorded on catalogue_added, metadata_corrected and rights_changed
# from G15 on; events recorded before then carry it only on text_version_published and withdrawal.
WITH_RIGHTS_REVISION = (
    "catalogue_added", "metadata_corrected", "rights_changed", "text_version_published", "withdrawal",
)  # fmt: skip
# A correction may show a field's previous value only where it can never have been withheld (SUB C1).
SAFE_PREVIOUS = ("legal_status", "rights_status", "document_type", "year")
# Checked links between works that change what law applies (legal_effect_recorded): the effect, and
# how the affected work's summary says it.
EFFECTS = {
    "repeals": ("repeal", "repealed"),
    "amends": ("amendment", "amended"),
    "supersedes": ("supersession", "superseded"),
    "commences": ("commencement", "commenced"),
    "transposes": ("transposition", "transposed"),
    "domesticates": ("domestication", "domesticated"),
    "ratifies": ("ratification", "ratified"),
}
# A law that transposes, domesticates or ratifies an instrument carries it into national law: the law
# itself is affected too, so each work has its own event, the law's naming the instrument in `of`.
BOTH_SIDES = frozenset({"transposes", "domesticates", "ratifies"})
OF = ":of"  # after a link's ID, the state (and event) key of its event on the asserting work
# A version diff names provisions, not the containers around a text (TXP-04); each list is capped.
DIFF_SKIPPED = frozenset({"document", "contents", "contents_entry", "notice"})
DIFF_CAP = 200
# What a reviewed membership asserts (JUR-04): a change to any of these is announced; its note, and the
# words its basis quotes, are not. Dates are EDTF strings (schema.dates).
MEMBERSHIP_FIELDS = ("member_id", "body_id", "valid_from", "valid_to", "basis", "evidence_url")
# What a reviewed suspension from participation asserts, each article of its legal basis with its role;
# its note, and the words its legal basis quotes, are not announced. It never ends its membership.
SUSPENSION_FIELDS = ("membership_id", "member_id", "body_id", "valid_from", "valid_to", "legal_basis",
                     "evidence_url", "lifted_evidence_url")  # fmt: skip
# Reviewed judicial treatment (EDT-01, migration 0042): announced on the treating judgment, and an
# overruling, which changes the treated judgment's authority, on that judgment too. Its passage is not
# announced; the treating paragraph and the part it treats are.
TREATED = ":treated"  # after a treatment's ID, the state (and event) key of its event on the treated judgment
ON_TREATED = frozenset({"overrules"})
TREATED_AS = {
    "follows": "followed",
    "applies": "applied",
    "distinguishes": "distinguished",
    "doubts": "doubted",
    "overrules": "overruled",
}


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
                "event_state",
                "membership_state",
                "suspension_state",
                "treatment_state",
            )
        }
        for name in ("legal_event", "membership", "membership_suspension", "citation", "judicial_treatment"):
            self.t[name] = ns.qualified(conn, "corpus", name)
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
        observed_at=None,
    ):
        """Plan one event; ``corrects`` (event_id, event_revision) makes it the next revision of that
        event (SUB C1); ``observed_at`` is when Open Alvary saw at the official source what it announces,
        where that is recorded (the module's notes). Returns the planned event."""
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
        if event_type in WITH_RIGHTS_REVISION and work_id in self.rights_revision:
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
            "observed_at": observed_at,
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
    keeps_events = conn.scalar(text("SELECT to_regclass(:t)"), {"t": rec.t["event_state"]}) is not None
    # Memberships as migration 0041 stores them (dates with their bounds, a basis), and their suspensions.
    keeps_memberships = (
        conn.scalar(text("SELECT to_regclass(:t)"), {"t": rec.t["suspension_state"]}) is not None
    )
    keeps_treatments = (
        conn.scalar(text("SELECT to_regclass(:t)"), {"t": rec.t["treatment_state"]}) is not None
    )
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
        for item in revisions:
            _rights_changed(rec, item, now.get(item["work_id"]))
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
    provision_events = (
        _provision_events(rec, scope if state["baselined"] else None, announce=bool(state["baselined"]))
        if keeps_events
        else []
    )
    # Memberships and suspensions are few and change only by publishing the register: compared whole.
    memberships = _memberships(rec, announce=bool(state["baselined"])) if keeps_memberships else []
    suspensions = _suspensions(rec, announce=bool(state["baselined"])) if keeps_memberships else []
    treatments = (
        _treatments(rec, scope if state["baselined"] else None, announce=bool(state["baselined"]))
        if keeps_treatments
        else []
    )
    if not state["baselined"]:
        _record_rights(rec, revisions, 0)
        _record_links(rec, links, baseline=True)
        _record_provision_events(rec, provision_events)
        _record_memberships(rec, memberships)
        _record_suspensions(rec, suspensions)
        _record_treatments(rec, treatments)
        conn.execute(
            text(f"UPDATE {rec.t['feed_state']} SET baselined = true, baseline_at = statement_timestamp()")
        )
        return 0
    if keeps_links:
        _record_links(rec, links, baseline=not state["links_baselined"])
    _record_provision_events(rec, provision_events)
    _record_memberships(rec, memberships)
    _record_suspensions(rec, suspensions)
    _record_treatments(rec, treatments)
    if rec.events or revisions:
        revision = conn.scalar(text(f"SELECT {rec.allocate}"))
        _record_rights(rec, revisions, revision)
        for event in rec.events:
            conn.execute(
                text(
                    f"INSERT INTO {rec.t['change_event']} (event_id, event_revision, event_type, logical_event_key,"
                    " reviewed_revision, publication_revision, feed_epoch, legal_effective, jurisdiction_id,"
                    " work_id, version_id, document_class, language, change_reason, supersedes_event_id,"
                    " supersedes_revision, observed_at, payload, payload_hash) VALUES (:event_id, :event_revision,"
                    " :event_type, :logical_event_key, :reviewed_revision, :revision, :epoch,"
                    " CAST(:legal_effective AS jsonb), :jurisdiction_id, :work_id, :version_id, :document_class,"
                    " :language, :change_reason, :supersedes_event_id, :supersedes_revision,"
                    " CAST(:observed_at AS timestamptz), CAST(:payload AS jsonb), :hash)"
                ),
                event
                | {
                    "observed_at": event.get("observed_at"),
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
        row.work_id: (row.revision, row.record_hash, row.record)
        for row in rec.conn.execute(
            text(
                f"SELECT DISTINCT ON (work_id) work_id, revision, record_hash, record FROM {table}"
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
        record_hash = digest(row.payload)
        number, held, before = latest.get(row.source_id, (0, None, None))
        if held == record_hash:
            rec.rights_revision[row.source_id] = number
            continue
        rec.rights_revision[row.source_id] = number + 1
        item = {
            "work_id": row.source_id,
            "revision": number + 1,
            "record": row.payload,
            "hash": record_hash,
            "previous": before,
        }
        changed.append(item)
    return changed


def _record_rights(rec: _Recorder, revisions: list[dict], publication_revision: int) -> None:
    for item in revisions:
        rec.conn.execute(
            text(
                f"INSERT INTO {rec.t['rights_revision']} (work_id, revision, publication_revision, record,"
                " record_hash) VALUES (:work_id, :revision, :at, CAST(:record AS jsonb), :hash)"
            ),
            {
                "work_id": item["work_id"],
                "revision": item["revision"],
                "at": publication_revision,
                "record": json.dumps(item["record"], ensure_ascii=False),
                "hash": item["hash"],
            },
        )


def _rights_changed(rec: _Recorder, item: dict, now: dict | None) -> None:
    """Announce a new revision of a work's rights record (G15) when the work is public after it: the
    revision, the rights record's fields that changed (every field, for its first revision) and the
    decision taken from it. A work that is not public is never named; one that stopped being public by
    this change is announced by its withdrawal instead."""
    if now is None:
        return
    work_id, record, before = item["work_id"], item["record"], item["previous"]
    changed = [f for f in RIGHTS_FIELDS if before is None or record.get(f) != before.get(f)]
    title = now["projection"].get("title")
    rec.add(
        "rights_changed",
        f"{work_id}:rights:{item['revision']}",
        work_id,
        now,
        changed=changed,
        summary=f"The rights record for {title} changed ({', '.join(f.replace('_', ' ') for f in changed)})."
        if before is not None
        else f"The rights record for {title} was recorded.",
        extra={"rights": {"revision": item["revision"], "decision": _decision(rec.conn, work_id)}},
    )


def _decision(conn: Connection, work_id: str) -> dict:
    """The decision taken from a public work's rights record, as GET /rights/{id} answers it: for its
    first readable text, or for none when it has none."""
    repository, now = LegacyRepository(conn), datetime.now(UTC)
    source, rights = repository.source(work_id)
    gates = repository.version_gates(source.id, rights.approved_content_hashes)
    readable = next((gate for gate in gates if decide(rights, gate, now=now).full_text), None)
    return decide(rights, readable, now=now).model_dump(mode="json")


def _as_public(rec: _Recorder, known: dict, work_id: str) -> dict:
    """A work's public facts now or, when it is no longer public, as it last was (published_state): what
    the announcement of a withdrawn link or event names. The feed shows it only while they are public."""
    if work_id not in known:
        known[work_id] = current(rec.conn, [work_id]).get(work_id) or {
            "projection": rec.conn.scalar(
                text(f"SELECT projection FROM {rec.t['published_state']} WHERE work_id = :w"), {"w": work_id}
            )
            or {}
        }
    return known[work_id]


def _links(rec: _Recorder, scope, *, announce: bool) -> list[tuple]:
    """The checked links that change what law applies (EFFECTS) between two public works that concern
    these works (all when None), compared with the last recorded state of each affected work's event
    (both works' for BOTH_SIDES): a new one is a legal_effect_recorded event on the work, a changed one a
    correction of its event, and one no longer public (deleted, unchecked, of another kind, or a work no
    longer public) a correction marked withdrawn that names the link (SUB C1; OA-R2). Returns the state
    to record. Without ``announce`` (the first comparison) nothing is announced."""
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
    public = {}  # state key -> (the work the event is on, the link)
    for r in rows:
        if r.source_id not in visible or r.target_source_id not in visible:
            continue
        link = {
            "kind": r.payload["kind"],
            "affected": r.target_source_id,
            "by": r.source_id,
            "effective_date": r.payload.get("effective_date"),
            "evidence_url": r.payload.get("evidence_url"),
        }
        public[r.id] = (r.target_source_id, link)
        if link["kind"] in BOTH_SIDES:
            public[r.id + OF] = (r.source_id, link)
    table = rec.t["link_state"]
    held_query = (
        f"SELECT link_id, work_id, projection, projection_hash, event_id, event_revision FROM {table}"
    )
    params = {}
    if scope is not None:
        held_query += (
            " WHERE link_id = ANY(:links) OR work_id = ANY(:ids) OR projection->>'by' = ANY(:ids)"
            " OR projection->>'affected' = ANY(:ids)"
        )
        params = {"links": sorted(public), "ids": sorted(scope)}
    held = {row.link_id: row for row in rec.conn.execute(text(held_query), params)}
    states, known = [], dict(visible)
    for key, (work, link) in sorted(public.items()):
        link_hash, was = digest(link), held.get(key)
        if was is not None and was.projection_hash == link_hash:
            continue
        event = _link_event(rec, known, key, work, link, was) if announce else None
        states.append((key, work, link, link_hash, event))
    for key in sorted(held.keys() - public.keys()):
        was = held[key]
        if was.projection.get("review_state") == "withdrawn":
            continue  # announced as withdrawn already
        link = was.projection | {"review_state": "withdrawn"}
        event = _link_event(rec, known, key, was.work_id, link, was) if announce else None
        states.append((key, was.work_id, link, digest(link), event))
    return states


def _link_event(rec: _Recorder, known: dict, key: str, work: str, link: dict, was) -> dict:
    """Plan the event on ``work`` (the affected work, or the asserting one of a BOTH_SIDES link) for a
    link that is new, changed, or withdrawn (``review_state``): the next revision of the event announced
    before, when there is one."""
    noun, verb = EFFECTS[link["kind"]]
    affected = _as_public(rec, known, link["affected"])["projection"]
    by = _as_public(rec, known, link["by"])["projection"]
    title = affected.get("title")
    corrects = (was.event_id, was.event_revision) if was is not None and was.event_id else None
    withdrawn = link.get("review_state") == "withdrawn"
    on_affected = work == link["affected"]
    if withdrawn:
        summary = f"The recorded {noun} of {title} by {by.get('title')} was withdrawn."
    elif corrects:
        summary = f"The record of how {by.get('title')} {verb} {title} was corrected."
    elif on_affected:
        summary = f"{title} was {verb} by {by.get('title')}."
    else:
        summary = f"{by.get('title')} {link['kind']} {title}."
    other, named = (link["by"], by) if on_affected else (link["affected"], affected)
    return rec.add(
        "legal_effect_recorded",
        key,
        work,
        _as_public(rec, known, work),
        summary=summary,
        extra={
            "legal_effect": {
                "kind": noun,
                ("by" if on_affected else "of"): {
                    "id": other,
                    "title": named.get("title"),
                    "citation": named.get("citation"),
                },
                "effective_date": link["effective_date"],
                "evidence_url": link["evidence_url"],
                **({"review_state": "withdrawn"} if withdrawn else {}),
            }
        },
        corrects=corrects,
        legal_effective={"from": link["effective_date"], "precision": "day"}
        if link["effective_date"]
        else None,
    )


def _record_links(rec: _Recorder, states: list[tuple], *, baseline: bool) -> None:
    table = rec.t["link_state"]
    for key, work, link, link_hash, event in states:
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
                "l": key,
                "w": work,
                "p": json.dumps(link, ensure_ascii=False),
                "h": link_hash,
                "e": event and event["event_id"],
                "r": event and event["event_revision"],
            },
        )
    if baseline:
        rec.conn.execute(text(f"UPDATE {rec.t['feed_state']} SET links_baselined = true"))


# Reviewed provision events (CPR-02, migration 0035), as the change log announces them.
EVENT_FIELDS = ("event_type", "scope", "locators", "date_from", "date_to", "precision", "review_state",
                "supersedes_event_id", "asserting_work")  # fmt: skip


def _provision_events(rec: _Recorder, scope, *, announce: bool) -> list[tuple]:
    """Reviewed events on public works that concern these works (all when None), compared with their
    last announced form: a new one is a legal_effect_recorded event on the affected work, a changed one
    (corrected, superseded or withdrawn) its correction, and one no longer public (its row deleted, or
    its work no longer public) a correction marked withdrawn that names it (SUB C1). Returns the state
    to record."""
    query = f"SELECT event_id, affected_work, {', '.join(EVENT_FIELDS)} FROM {rec.t['legal_event']}"
    params = {}
    if scope is not None:
        query += " WHERE affected_work = ANY(:ids)"
        params = {"ids": sorted(scope)}
    rows = rec.conn.execute(text(query + " ORDER BY event_id"), params).mappings().all()
    works = sorted({r["affected_work"] for r in rows})
    visible = current(rec.conn, works) if works else {}
    public = {
        r["event_id"]: {
            "work": r["affected_work"],
            **{
                k: (v.isoformat() if hasattr(v, "isoformat") else list(v) if isinstance(v, list) else v)
                for k, v in r.items()
                if k in EVENT_FIELDS
            },
        }  # fmt: skip
        for r in rows
        if r["affected_work"] in visible
    }
    held_query = (
        "SELECT event_id, projection, projection_hash, change_event_id, change_event_revision"
        f" FROM {rec.t['event_state']}"
    )
    if scope is not None:
        held_query += " WHERE event_id = ANY(:events) OR work_id = ANY(:ids)"
        params = {"events": sorted(public), "ids": sorted(scope)}
    held = {row.event_id: row for row in rec.conn.execute(text(held_query), params)}
    states, known = [], dict(visible)
    for event_id, event in sorted(public.items()):
        event_hash, was = digest(event), held.get(event_id)
        if was is not None and was.projection_hash == event_hash:
            continue
        if was is None and event["review_state"] != "reviewed":
            states.append((event_id, event, event_hash, None))  # withdrawn before it was announced
            continue
        planned = _provision_event(rec, known, event_id, event, was) if announce else None
        states.append((event_id, event, event_hash, planned))
    for event_id in sorted(held.keys() - public.keys()):
        was = held[event_id]
        if was.projection.get("review_state") == "withdrawn":
            continue  # announced as withdrawn already, or never announced
        event = was.projection | {"review_state": "withdrawn"}
        planned = _provision_event(rec, known, event_id, event, was) if announce else None
        states.append((event_id, event, digest(event), planned))
    return states


def _provision_event(rec: _Recorder, known: dict, event_id: str, event: dict, was) -> dict:
    """Plan the event for a provision event that is new, changed or withdrawn: the next revision of the
    one announced before, when there is one."""
    affected = _as_public(rec, known, event["work"])
    title = affected["projection"].get("title")
    corrects = (
        (was.change_event_id, was.change_event_revision) if was is not None and was.change_event_id else None
    )
    what = "the whole work" if event["scope"] == "whole" else ", ".join(event["locators"])
    if event["review_state"] == "withdrawn":
        summary = f"The recorded {event['event_type']} of {title} ({what}) was withdrawn."
    elif corrects:
        summary = f"The record of when {title} ({what}) was {event['event_type']} was corrected."
    else:
        summary = f"{title} ({what}): {event['event_type']}."
    return rec.add(
        "legal_effect_recorded",
        event_id,
        event["work"],
        affected,
        summary=summary,
        extra={
            "legal_effect": {
                "kind": event["event_type"],
                "scope": event["scope"],
                "provisions": event["locators"],
                "effective_date": event["date_from"],
                "precision": event["precision"],
                "review_state": event["review_state"],
                **({"by": {"id": event["asserting_work"]}} if event["asserting_work"] else {}),
            }
        },
        corrects=corrects,
        legal_effective={"from": event["date_from"], "precision": event["precision"]}
        if event["date_from"]
        else None,
    )


def _record_provision_events(rec: _Recorder, states: list[tuple]) -> None:
    table = rec.t["event_state"]
    for event_id, event, event_hash, planned in states:
        rec.conn.execute(
            text(
                f"INSERT INTO {table} (event_id, work_id, projection, projection_hash, change_event_id,"
                " change_event_revision) VALUES (:e, :w, CAST(:p AS jsonb), :h, :c, :r) ON CONFLICT (event_id)"
                " DO UPDATE SET work_id = EXCLUDED.work_id, projection = EXCLUDED.projection,"
                " projection_hash = EXCLUDED.projection_hash,"
                f" change_event_id = coalesce(EXCLUDED.change_event_id, {table}.change_event_id),"
                f" change_event_revision = coalesce(EXCLUDED.change_event_revision, {table}.change_event_revision),"
                " updated_at = statement_timestamp()"
            ),
            {
                "e": event_id,
                "w": event["work"],
                "p": json.dumps(event, ensure_ascii=False),
                "h": event_hash,
                "c": planned and planned["event_id"],
                "r": planned and planned["event_revision"],
            },
        )


def _treatments(rec: _Recorder, scope, *, announce: bool) -> list[tuple]:
    """Reviewed treatments of reviewed citations between two public judgments that concern these works
    (all when None), compared with the last recorded state of each event: a new one is a
    treatment_recorded event on the treating judgment (and, for ON_TREATED kinds, on the treated one), a
    changed one a correction of its event, and one no longer public (withdrawn, its citation withdrawn,
    or a judgment no longer public) a correction marked withdrawn that names it (SUB C1, as _links).
    Returns the state to record. Without ``announce`` (before the feed's baseline) nothing is announced."""
    query = (
        "SELECT t.treatment_id, t.treatment, t.opinion, c.citing_work, c.citing_locator, c.citing_pinpoint,"
        " c.cited_work, c.cited_locator, c.cited_pinpoint, c.evidence_url"
        f" FROM {rec.t['judicial_treatment']} t JOIN {rec.t['citation']} c ON c.citation_id = t.citation_id"
        " WHERE t.review_state = 'reviewed' AND c.review_state = 'reviewed'"
    )
    params = {}
    if scope is not None:
        query += " AND (c.citing_work = ANY(:ids) OR c.cited_work = ANY(:ids))"
        params = {"ids": sorted(scope)}
    rows = rec.conn.execute(text(query + " ORDER BY t.treatment_id"), params).mappings().all()
    works = sorted({w for r in rows for w in (r["citing_work"], r["cited_work"])})
    visible = current(rec.conn, works) if works else {}
    public = {}  # state key -> (the judgment the event is on, the treatment)
    for r in rows:
        if r["citing_work"] not in visible or r["cited_work"] not in visible:
            continue
        treatment = {
            "id": r["treatment_id"],
            "kind": r["treatment"],
            "treating": r["citing_work"],
            "treated": r["cited_work"],
            "citing_locator": r["citing_locator"],
            "citing_pinpoint": r["citing_pinpoint"],
            "cited_locator": r["cited_locator"],
            "cited_pinpoint": r["cited_pinpoint"],
            "opinion": r["opinion"],
            "evidence_url": r["evidence_url"],
        }
        public[r["treatment_id"]] = (r["citing_work"], treatment)
        if r["treatment"] in ON_TREATED:
            public[r["treatment_id"] + TREATED] = (r["cited_work"], treatment)
    table = rec.t["treatment_state"]
    held_query = (
        f"SELECT state_key, work_id, projection, projection_hash, event_id, event_revision FROM {table}"
    )
    if scope is not None:
        held_query += (
            " WHERE state_key = ANY(:keys) OR work_id = ANY(:ids) OR projection->>'treating' = ANY(:ids)"
            " OR projection->>'treated' = ANY(:ids)"
        )
        params = {"keys": sorted(public), "ids": sorted(scope)}
    held = {row.state_key: row for row in rec.conn.execute(text(held_query), params)}
    states, known = [], dict(visible)
    for key, (work, treatment) in sorted(public.items()):
        treatment_hash, was = digest(treatment), held.get(key)
        if was is not None and was.projection_hash == treatment_hash:
            continue
        event = _treatment_event(rec, known, key, work, treatment, was) if announce else None
        states.append((key, work, treatment, treatment_hash, event))
    for key in sorted(held.keys() - public.keys()):
        was = held[key]
        if was.projection.get("review_state") == "withdrawn":
            continue  # announced as withdrawn already
        treatment = was.projection | {"review_state": "withdrawn"}
        event = _treatment_event(rec, known, key, was.work_id, treatment, was) if announce else None
        states.append((key, was.work_id, treatment, digest(treatment), event))
    return states


def _treatment_event(rec: _Recorder, known: dict, key: str, work: str, treatment: dict, was) -> dict:
    """Plan the event on ``work`` (the treating judgment, or the treated one for ON_TREATED kinds) for a
    treatment that is new, changed or withdrawn: the next revision of the one announced before, if any."""
    treating = _as_public(rec, known, treatment["treating"])["projection"]
    treated = _as_public(rec, known, treatment["treated"])["projection"]
    corrects = (was.event_id, was.event_revision) if was is not None and was.event_id else None
    withdrawn = treatment.get("review_state") == "withdrawn"
    on_treated = key.endswith(TREATED)
    kind, at = treatment["kind"], treatment["citing_pinpoint"]
    if withdrawn:
        summary = f"The recorded treatment of {treated.get('title')} by {treating.get('title')} ({kind}) was withdrawn."
    elif corrects:
        summary = f"The record of how {treating.get('title')} treats {treated.get('title')} was corrected."
    elif on_treated:
        summary = f"{treated.get('title')} was {TREATED_AS[kind]} by {treating.get('title')}, at {at}."
    else:
        summary = f"{treating.get('title')} {kind} {treated.get('title')}, at {at}."
    other, named = (treatment["treating"], treating) if on_treated else (treatment["treated"], treated)
    return rec.add(
        "treatment_recorded",
        key,
        work,
        _as_public(rec, known, work),
        summary=summary,
        extra={
            "treatment": {
                "id": treatment["id"],
                "kind": kind,
                # A direct treatment a reviewer recorded, never an inferred risk (EDT-02).
                "assertion": "direct_treatment",
                ("by" if on_treated else "of"): {
                    "id": other,
                    "title": named.get("title"),
                    "citation": named.get("citation"),
                },
                "citing_locator": treatment["citing_locator"],
                "citing_pinpoint": at,
                "cited_locator": treatment["cited_locator"],
                "cited_pinpoint": treatment["cited_pinpoint"],
                "opinion": treatment["opinion"],
                "review_level": "doctrinal",
                "evidence_url": treatment["evidence_url"],
                **({"review_state": "withdrawn"} if withdrawn else {}),
            }
        },
        corrects=corrects,
    )


def _record_treatments(rec: _Recorder, states: list[tuple]) -> None:
    table = rec.t["treatment_state"]
    for key, work, treatment, treatment_hash, event in states:
        rec.conn.execute(
            text(
                f"INSERT INTO {table} (state_key, work_id, projection, projection_hash, event_id, event_revision)"
                " VALUES (:k, :w, CAST(:p AS jsonb), :h, :e, :r) ON CONFLICT (state_key) DO UPDATE SET"
                " work_id = EXCLUDED.work_id, projection = EXCLUDED.projection,"
                " projection_hash = EXCLUDED.projection_hash,"
                f" event_id = coalesce(EXCLUDED.event_id, {table}.event_id),"
                f" event_revision = coalesce(EXCLUDED.event_revision, {table}.event_revision),"
                " updated_at = statement_timestamp()"
            ),
            {
                "k": key,
                "w": work,
                "p": json.dumps(treatment, ensure_ascii=False),
                "h": treatment_hash,
                "e": event and event["event_id"],
                "r": event and event["event_revision"],
            },
        )


def _spoken(value: str) -> str:
    """A date of schema.dates in a summary: a day as it is, otherwise the window the day lies in."""
    found = UncertainDate(value)
    if found.precision == "day":
        return value
    if found.precision == "range":
        return f"a day between {found.earliest} and {found.latest}"
    return f"a day in {value}"


def _bounded(projection: dict) -> dict:
    """A dated projection's dates for an event: each EDTF string with its bounds (schema.dates)."""
    found = {}
    for name in ("valid_from", "valid_to"):
        value = projection[name]
        found[name] = value
        found[f"{name}_bounds"] = value and UncertainDate(value).bounds().model_dump(mode="json")
    return found


def _occasion(key: str, now: dict, was, retracted: bool, end: str) -> tuple:
    """Which event a change of a dated assertion is: (key, reason, corrects, case). Its first
    announcement; its end (``valid_to`` set: reason ``end``, a new event keyed ``{key}:{end}``); or a
    correction of the latest, or its retraction, which keeps that event's ID."""
    before = was.projection if was is not None and was.event_id else None
    if before is None:
        return key, None, None, "new"
    if not retracted and before["valid_to"] is None and now["valid_to"] is not None:
        return f"{key}:{end}", end, None, end
    return was.event_key, None, (was.event_id, was.event_revision), "retracted" if retracted else "corrected"


def _compare_dated(public: dict, held: dict, announce, *, announced: bool) -> list[tuple]:
    """Reviewed dated assertions (memberships, suspensions) compared with their last announced form: the
    state to record, each change planned as an event by ``announce`` (key, now, was, retracted=)."""
    states = []
    for key, now in sorted(public.items()):
        now_hash, was = digest(now), held.get(key)
        if was is not None and was.projection_hash == now_hash:
            continue
        planned = announce(key, now, was) if announced else None
        states.append(("put", key, now, now_hash, planned))
    for key in sorted(held.keys() - public.keys()):
        was = held[key]
        if announced and was.event_id:
            announce(key, was.projection, was, retracted=True)
        states.append(("drop", key, None, None, None))
    return states


def _held(rec: _Recorder, state: str, key: str) -> dict:
    return {
        row[0]: row
        for row in rec.conn.execute(
            text(
                f"SELECT {key}, projection, projection_hash, event_id, event_revision, event_key"
                f" FROM {rec.t[state]}"
            )
        )
    }


def _memberships(rec: _Recorder, *, announce: bool) -> list[tuple]:
    """The reviewed memberships published to the catalogue (corpus.membership), compared with their last
    announced form: a new one is a membership_recorded event; its end (valid_to set) is another, with the
    reason ``ended``; any other change, or its retraction, corrects the latest of them (SUB C1, JUR-04).
    Returns the state to record. Without ``announce`` (before the feed's baseline) nothing is announced."""
    rows = rec.conn.execute(
        text(
            "SELECT membership_id, member_id, body_id, valid_from, valid_to, basis - 'quote' AS basis,"
            f" evidence_url FROM {rec.t['membership']}"
        )
    ).mappings()
    public = {r["membership_id"]: {k: r[k] for k in MEMBERSHIP_FIELDS} for r in rows}
    held = _held(rec, "membership_state", "membership_id")
    places = {m[k] for m in public.values() for k in ("member_id", "body_id")}
    places |= {row.projection[k] for row in held.values() for k in ("member_id", "body_id")}
    names = _names(rec.conn, sorted(places)) if announce else {}

    def announce_one(membership_id, now, was, *, retracted=False):
        member, body = (names.get(now[k], now[k]) for k in ("member_id", "body_id"))
        key, reason, corrects, case = _occasion(membership_id, now, was, retracted, "ended")
        start = _spoken(now["valid_from"])
        if case == "new":
            summary = f"{member} is a member of {body}, from {start}."
            if now["valid_to"]:
                summary = f"{member} was a member of {body} from {start} until {_spoken(now['valid_to'])}."
        elif case == "ended":
            summary = f"{member}'s membership of {body} ended; its last day was {_spoken(now['valid_to'])}."
        elif case == "corrected":
            summary = f"The record of {member}'s membership of {body} was corrected."
        else:
            summary = f"The record of {member}'s membership of {body} was withdrawn; it is not a fact now."
        before = None if case == "new" else was.projection
        changed = [] if before is None else [k for k in MEMBERSHIP_FIELDS if now.get(k) != before.get(k)]
        return rec.add(
            "membership_recorded",
            key,
            membership_id,
            {
                "projection": {
                    "title": f"{member}: membership of {body}",
                    "canonical_url": now["evidence_url"],
                    "jurisdiction_id": now["member_id"],
                }
            },
            summary=summary,
            reason=reason,
            changed=changed,
            extra={
                "membership": {
                    "id": membership_id,
                    "member": {"id": now["member_id"], "name": member},
                    "body": {"id": now["body_id"], "name": body},
                    **_bounded(now),
                    "basis": now.get("basis"),
                    "evidence_url": now["evidence_url"],
                    "retracted": retracted,
                }
            },
            corrects=corrects,
            legal_effective={
                "from": now["valid_from"],
                "until": now["valid_to"],
                "precision": UncertainDate(now["valid_from"]).precision,
            },
        )

    return _compare_dated(public, held, announce_one, announced=announce)


def _suspensions(rec: _Recorder, *, announce: bool) -> list[tuple]:
    """The reviewed suspensions from participation published to the catalogue
    (corpus.membership_suspension), compared with their last announced form: a new one is a
    suspension_recorded event; its lifting (valid_to set) is another, with the reason ``lifted``; any
    other change, or its retraction, corrects the latest of them. A suspension never ends its membership:
    it is announced apart from it, under its own type. Returns the state to record."""
    rows = rec.conn.execute(
        text(
            "SELECT s.suspension_id, s.membership_id, m.member_id, m.body_id, s.valid_from, s.valid_to,"
            " (SELECT jsonb_agg(p - 'quote') FROM jsonb_array_elements(s.legal_basis) p) AS legal_basis,"
            f" s.evidence_url, s.lifted_evidence_url FROM {rec.t['membership_suspension']} s"
            f" JOIN {rec.t['membership']} m ON m.membership_id = s.membership_id"
        )
    ).mappings()
    public = {r["suspension_id"]: {k: r[k] for k in SUSPENSION_FIELDS} for r in rows}
    held = _held(rec, "suspension_state", "suspension_id")
    places = {m[k] for m in public.values() for k in ("member_id", "body_id")}
    places |= {row.projection[k] for row in held.values() for k in ("member_id", "body_id")}
    names = _names(rec.conn, sorted(places)) if announce else {}

    def announce_one(suspension_id, now, was, *, retracted=False):
        member, body = (names.get(now[k], now[k]) for k in ("member_id", "body_id"))
        key, reason, corrects, case = _occasion(suspension_id, now, was, retracted, "lifted")
        what = f"{member}'s suspension from participation in {body}"
        start = _spoken(now["valid_from"])
        if case == "new":
            summary = f"{member} is suspended from participation in {body} from {start}; it remains a member."
            if now["valid_to"]:
                summary = (
                    f"{member} was suspended from participation in {body} from {start} until "
                    f"{_spoken(now['valid_to'])}; it remained a member."
                )
        elif case == "lifted":
            summary = f"{what} was lifted; its last day was {_spoken(now['valid_to'])}."
        elif case == "corrected":
            summary = f"The record of {what} was corrected."
        else:
            summary = f"The record of {what} was withdrawn; it is not a fact now."
        before = None if case == "new" else was.projection
        changed = [] if before is None else [k for k in SUSPENSION_FIELDS if now.get(k) != before.get(k)]
        return rec.add(
            "suspension_recorded",
            key,
            suspension_id,
            {
                "projection": {
                    "title": f"{member}: suspension from participation in {body}",
                    "canonical_url": now["evidence_url"],
                    "jurisdiction_id": now["member_id"],
                }
            },
            summary=summary,
            reason=reason,
            changed=changed,
            extra={
                "suspension": {
                    "id": suspension_id,
                    "kind": "suspended_from_participation",
                    "membership_id": now["membership_id"],
                    "member": {"id": now["member_id"], "name": member},
                    "body": {"id": now["body_id"], "name": body},
                    **_bounded(now),
                    "legal_basis": now["legal_basis"],
                    "evidence_url": now["evidence_url"],
                    "lifted_evidence_url": now["lifted_evidence_url"],
                    "retracted": retracted,
                }
            },
            corrects=corrects,
            legal_effective={
                "from": now["valid_from"],
                "until": now["valid_to"],
                "precision": UncertainDate(now["valid_from"]).precision,
            },
        )

    return _compare_dated(public, held, announce_one, announced=announce)


def _record_dated(
    rec: _Recorder, state: str, key: str, columns: tuple[str, ...], states: list[tuple]
) -> None:
    """Record the announced form of each changed membership or suspension in its state table, with the
    columns copied from it, and drop those no longer published."""
    table = rec.t[state]
    for action, record_id, projection, projection_hash, planned in states:
        if action == "drop":
            rec.conn.execute(text(f"DELETE FROM {table} WHERE {key} = :m"), {"m": record_id})
            continue
        rec.conn.execute(
            text(
                f"INSERT INTO {table} ({key}, {', '.join(columns)}, projection, projection_hash, event_id,"
                f" event_revision, event_key) VALUES (:m, {', '.join(':' + c for c in columns)},"
                f" CAST(:p AS jsonb), :h, :e, :r, :k) ON CONFLICT ({key}) DO UPDATE SET"
                + "".join(f" {c} = EXCLUDED.{c}," for c in columns)
                + " projection = EXCLUDED.projection, projection_hash = EXCLUDED.projection_hash,"
                f" event_id = coalesce(EXCLUDED.event_id, {table}.event_id),"
                f" event_revision = coalesce(EXCLUDED.event_revision, {table}.event_revision),"
                f" event_key = coalesce(EXCLUDED.event_key, {table}.event_key),"
                " updated_at = statement_timestamp()"
            ),
            {
                "m": record_id,
                **{c: projection[c] for c in columns},
                "p": json.dumps(projection, ensure_ascii=False),
                "h": projection_hash,
                "e": planned and planned["event_id"],
                "r": planned and planned["event_revision"],
                "k": planned and planned["logical_event_key"],
            },
        )


def _record_memberships(rec: _Recorder, states: list[tuple]) -> None:
    _record_dated(rec, "membership_state", "membership_id", ("member_id", "body_id"), states)


def _record_suspensions(rec: _Recorder, states: list[tuple]) -> None:
    _record_dated(rec, "suspension_state", "suspension_id", ("membership_id",), states)


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
        # Each correction of a record is a new event, keyed by the projection it leaves, never the next
        # revision of an earlier one (G15): an earlier correction stays true, and a revision with
        # `supersedes` means the earlier one is replaced. As revisions, GET /changes/{event_id} (the latest
        # revision only) would lose an earlier correction's fields and previous values, Atom and RSS
        # (entry ID and guid: the event_id) would show a later correction as an update of the earlier
        # entry, and consumers that keep only the highest revision would drop one.
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
    versions = tables["versions"]
    retrieved = (rec.conn.scalar(select(versions.c.payload).where(versions.c.id == version_id)) or {}).get(
        "retrieved_at"
    )
    rec.add(
        "text_version_published",
        version_id,
        work_id,
        now,
        version={"id": version_id, **facts},
        reason="first_text" if first else _reason(rec, version_id),
        extra={"version_diff": diff} if (diff := _version_diff(rec.conn, version_id)) else None,
        observed_at=retrieved,  # when the official file this text was read from was retrieved
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
