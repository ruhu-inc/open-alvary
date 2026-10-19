"""The section index (RET-01, MIN-02, D15): each published text's provisions as search units, kept in
the transaction that changes the catalogue, and the bounded lexical search over them.

A unit is a provision (a section, an article, a schedule's paragraph), never a whole text. A provision
larger than MAX_UNIT_BYTES is searched by its parts, and text outside every provision (a long title, a
preamble, a part's heading) by its own runs, so units cover a text without overlapping and one passage
is one hit. Only texts anyone may read now are indexed (changes.record.current, migration 0031); a hit is
rechecked against the rights policy before any of its words are served.

Ranking (oa-rank-1) is PostgreSQL's cover density, ts_rank_cd with normalisation 32, over a unit's
headings (weight 1.0), its words (0.4), its work's title (0.2) and identifiers (0.1): its citation and
pinpoint. A text no longer in force (its record is repealed, superseded or historical, or a later
published text of the same record replaces it) scores 0.8 of that. Ties go to the version, then the
earlier unit. English texts are analysed in English and French texts in French (stemming and common
words; tests/test_postgres_search.py); every other language word for word (simple) until it passes
the language tests.

Beside the provisions, each published text has one document unit (RSV-02, migration 0034) for questions
about a whole law: its work's title (weight 1.0), its long title or preamble (0.4, the unit's quotable
span) and the outline of its part, chapter and section headings (0.2), with its identifiers (0.1). Its
names (the work's title, citation and aliases, folded to letters and digits) let a query that names the
instrument be recognised (RSV-03). A document unit never stands for a provision.
"""

import hashlib
import json
import math
import re
import unicodedata
from dataclasses import dataclass, field, replace
from datetime import date

from sqlalchemy import select, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import OperationalError

from api import pinpoint
from api.store import tables
from schema.public import NodeViewRecord, VersionViewRecord

ANALYZER = "oa-search-3"  # a new value rebuilds every unit at the next publication
RANKING = "oa-rank-1"
CONFIGS = {"en": "english", "fr": "french"}  # every other language: simple (MIN-02)
ANALYSES = ("english", "french", "simple")  # the configs a unit may have (migration 0033)
WEIGHTS = "{0.1, 0.2, 0.4, 1.0}"  # D identifiers, C the work's title, B words, A headings
NOT_IN_FORCE = 0.8
MAX_UNIT_BYTES = 64 * 1024
MAX_INDEXED_NODES = 200_000
COUNT_BOUND = 10_000
TIMEOUT_MS = 10_000  # RET-02: a retrieval answers within ten seconds or says it could not
START, STOP = "⟦", "⟧"  # highlight marks; never part of a snippet
HEADLINE = f"StartSel={START}, StopSel={STOP}, MaxWords=40, MinWords=18, ShortWord=2, MaxFragments=1"
# A provision of its own; a schedule's paragraph is one when it is not inside another paragraph.
PROVISIONS = {"section", "article"}
SKIPPED = {"contents", "contents_entry"}  # a contents list repeats headings: layout, not law
OUTLINE_KINDS = {"part", "chapter", "title", "division", "section", "article", "schedule"}
OUTLINE_BYTES = 8 * 1024
MIN_NAME = 4  # shorter names (and single letters) are never matched inside a query


def config_for(language: str) -> str:
    return CONFIGS.get(language.split("-")[0].casefold(), "simple")


@dataclass(frozen=True)
class Unit:
    node: NodeViewRecord
    start: int
    end: int
    headings: str
    pinpoint: str | None


def _segments(raw: bytes, start: int, end: int) -> list[tuple[int, int]]:
    """[start, end) without its outer whitespace, cut at whitespace into pieces of MAX_UNIT_BYTES at most."""
    while start < end and raw[start : start + 1].isspace():
        start += 1
    while end > start and raw[end - 1 : end].isspace():
        end -= 1
    pieces = []
    while end - start > MAX_UNIT_BYTES:
        window = raw[start : start + MAX_UNIT_BYTES]
        cut = max(window.rfind(b"\n\n"), window.rfind(b"\n"), window.rfind(b" "))
        if cut <= 0:
            cut = MAX_UNIT_BYTES
            while cut > 0 and raw[start + cut] & 0xC0 == 0x80:  # never inside a character
                cut -= 1
        pieces.append((start, start + cut))
        start += cut
        while start < end and raw[start : start + 1].isspace():
            start += 1
    if end > start:
        pieces.append((start, end))
    return pieces


def units(version: VersionViewRecord, nodes: list[NodeViewRecord], document_type: str | None) -> list[Unit]:
    """A text's search units in document order (see the module docstring)."""
    raw = version.canonical_text.encode()
    by_id = {n.id: n for n in nodes}
    children: dict[str | None, list[NodeViewRecord]] = {}
    for node in sorted(nodes, key=lambda n: (n.start_byte, -n.end_byte, n.order)):
        children.setdefault(node.parent_id if node.parent_id in by_id else None, []).append(node)
    found: list[Unit] = []

    def chain(node):
        at, out = node, [node]
        while at.parent_id in by_id and len(out) < 64:
            at = by_id[at.parent_id]
            out.append(at)
        return out[::-1]

    def provision(node, path):
        if node.kind in PROVISIONS:
            return True
        if node.kind == "paragraph":
            enclosing = next((p for p in reversed(path) if p.kind not in ("part", "division")), None)
            return enclosing is not None and enclosing.kind in ("schedule", "annex")
        return False

    def emit(node, start, end, path):
        context = " ".join(" ".join(filter(None, (p.label, p.heading))) for p in path if p.kind != "document")
        cited = pinpoint.cite(chain(node), document_type)
        for a, b in _segments(raw, start, end):
            found.append(Unit(node, a, b, context, cited[1] if cited else None))

    def visit(node, path, split):
        if node.kind in SKIPPED:
            return
        here = [*path, node]
        whole = provision(node, path) or split
        if whole and node.end_byte - node.start_byte <= MAX_UNIT_BYTES:
            emit(node, node.start_byte, node.end_byte, here)
            return
        at = node.start_byte  # the node's own words between its children, in document order
        for child in children.get(node.id, []):
            if child.start_byte > at:
                emit(node, at, child.start_byte, here)
            visit(child, here, whole)
            at = max(at, child.end_byte)
        if node.end_byte > at:
            emit(node, at, node.end_byte, here)

    for root in children.get(None, []):
        visit(root, [], False)
    return found


@dataclass(frozen=True)
class DocumentUnit:
    start: int
    end: int
    node_id: str | None
    outline: str
    truncated: bool


def fold(value: str) -> str:
    """Letters and digits only, in lower case, one space apart: how names and queries are compared."""
    folded = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(re.sub(r"[\W_]+", " ", folded).split())


def name_forms(values) -> list[str]:
    """The forms a query may name a work by: each value folded, and again without a closing year
    ("nigeria data protection act 2023" and "nigeria data protection act"). Short forms are left out."""
    forms: list[str] = []
    for value in values:
        folded = fold(value or "")
        for form in (folded, re.sub(r"(?<!\bof)\s+\d{4}$", "", folded)):
            if len(form) >= MIN_NAME and not form.isdigit() and form not in forms:
                forms.append(form)
    return forms


def document_unit(version: VersionViewRecord, nodes: list[NodeViewRecord]) -> DocumentUnit:
    """A text's document unit: its quotable span (the preamble or long title; else the words the
    document node holds before its first part) and the outline of its headings, truncated."""
    raw = version.canonical_text.encode()
    ordered = sorted(nodes, key=lambda n: (n.start_byte, -n.end_byte, n.order))
    in_contents = set()
    for node in ordered:
        if node.kind in SKIPPED or node.parent_id in in_contents:
            in_contents.add(node.id)
    preamble = next((n for n in ordered if n.kind == "preamble"), None)
    root = next((n for n in ordered if n.kind == "document"), None)
    if preamble is not None:
        start, end, node_id = preamble.start_byte, preamble.end_byte, preamble.id
    elif root is not None:
        first = next((n.start_byte for n in ordered if n.parent_id == root.id), root.end_byte)
        start, end, node_id = root.start_byte, first, root.id
    else:
        start, end, node_id = 0, 0, None
    pieces = _segments(raw, start, end)
    start, end = pieces[0] if pieces else (start, start)
    lines, size, truncated = [], 0, False
    for node in ordered:
        if node.kind not in OUTLINE_KINDS or node.id in in_contents:
            continue
        line = " ".join(filter(None, (node.label, node.heading)))
        if not line:
            continue
        cost = len(line.encode()) + 2
        if size + cost > OUTLINE_BYTES:
            truncated = True
            break
        lines.append(line)
        size += cost
    return DocumentUnit(start, end, node_id, "; ".join(lines), truncated)


# --- keeping the index in step with what is public --------------------------------------------------


def _tables(conn: Connection) -> tuple[str, str]:
    from changes.record import namespace

    ns = namespace(conn)
    return ns.qualified(conn, "changes", "text_unit"), ns.qualified(conn, "changes", "text_index_state")


def _documents(conn: Connection) -> str | None:
    """The document unit table, or None before migration 0034."""
    from changes.record import namespace

    table = namespace(conn).qualified(conn, "changes", "document_unit")
    return table if conn.scalar(text("SELECT to_regclass(:t)"), {"t": table}) is not None else None


def available(conn: Connection) -> bool:
    if conn.dialect.name != "postgresql":
        return False
    return conn.scalar(text("SELECT to_regclass(:t)"), {"t": _tables(conn)[0]}) is not None


WORK_FIELDS = ("title", "citation", "jurisdiction_id", "document_type", "legal_status", "year")


_SPELLED = {"s.": "section", "reg.": "regulation", "art.": "article", "para.": "paragraph"}


def identifiers(citation: str, cited: str | None) -> str:
    """A unit's identifiers, matched word for word: its work's citation and its pinpoint, also spelled
    out ("s. 23(1)" as well as "section 23 1"), so "section 23" finds it."""
    spelled = " ".join(_SPELLED.get(word.casefold(), word) for word in (cited or "").split())
    return f"{citation} {cited or ''} {spelled}"


def _work_hash(projection: dict) -> str:
    facts = {k: projection.get(k) for k in WORK_FIELDS}
    return hashlib.sha256(json.dumps(facts, sort_keys=True).encode()).hexdigest()


INSERT = """
INSERT INTO {t} (version_id, start_byte, end_byte, work_id, node_id, kind, locator, pinpoint, heading,
    language, config, jurisdiction_id, document_type, legal_status, year, effective_from, effective_until,
    content_hash, prior_version_id, work_hash, document, context)
VALUES (:version_id, :start_byte, :end_byte, :work_id, :node_id, :kind, :locator, :pinpoint, :heading,
    :language, :config, :jurisdiction_id, :document_type, :legal_status, :year, :effective_from,
    :effective_until, :content_hash, :prior_version_id, :work_hash,
    setweight(to_tsvector(CAST(:analysis AS regconfig), :headings), 'A')
        || setweight(to_tsvector(CAST(:analysis AS regconfig), :words), 'B'),
    setweight(to_tsvector(CAST(:analysis AS regconfig), :title), 'C')
        || setweight(to_tsvector('simple', :identifiers), 'D'))
"""
REFRESH = """
UPDATE {t} SET jurisdiction_id = :jurisdiction_id, document_type = :document_type,
    legal_status = :legal_status, year = :year, work_hash = :work_hash,
    context = setweight(to_tsvector(CAST(config AS regconfig), :title), 'C')
        || setweight(to_tsvector('simple', :identifiers), 'D')
WHERE work_id = :work_id AND pinpoint IS NOT DISTINCT FROM :pinpoint AND work_hash <> :work_hash
"""


def _load(conn: Connection, version_id: str) -> tuple[VersionViewRecord, list[NodeViewRecord]] | None:
    from api.repositories import LegacyRepository

    versions = tables["versions"]
    payload = conn.scalar(select(versions.c.payload).where(versions.c.id == version_id))
    if payload is None:
        return None
    version = VersionViewRecord.model_validate(payload)
    rows = LegacyRepository(conn).rows("structure", version_ids=[version_id], bound=MAX_INDEXED_NODES)
    return version, [NodeViewRecord.model_validate(row) for row in rows]


def _index_version(conn: Connection, table: str, work_id: str, version_id: str, projection: dict) -> None:
    loaded = _load(conn, version_id)
    if loaded is None:
        return
    version, nodes = loaded
    raw = version.canonical_text.encode()
    config = config_for(version.language)
    citation = projection.get("citation") or ""
    rows = [
        {
            "version_id": version_id,
            "start_byte": unit.start,
            "end_byte": unit.end,
            "work_id": work_id,
            "node_id": unit.node.id,
            "kind": unit.node.kind,
            "locator": unit.node.locator,
            "pinpoint": unit.pinpoint,
            "heading": unit.node.heading,
            "language": version.language,
            "config": config,
            "analysis": config,
            "jurisdiction_id": projection.get("jurisdiction_id") or "unknown",
            "document_type": projection.get("document_type") or "other",
            "legal_status": projection.get("legal_status") or "unknown",
            "year": projection.get("year"),
            "effective_from": version.effective_from,
            "effective_until": version.effective_until,
            "content_hash": version.content_hash,
            "prior_version_id": version.prior_version_id,
            "work_hash": _work_hash(projection),
            "headings": unit.headings,
            "words": raw[unit.start : unit.end].decode("utf-8"),
            "title": projection.get("title") or "",
            "identifiers": identifiers(citation, unit.pinpoint),
        }
        for unit in units(version, nodes, projection.get("document_type"))
    ]
    if rows:
        conn.execute(text(INSERT.format(t=table)), rows)


def sync(conn: Connection, now: dict[str, dict] | None, scope: set[str] | None) -> None:
    """Bring the index to the public state. ``now`` is changes.record.current for ``scope`` (every work
    when None), or None to read it here. The first call after migration 0031, or after ANALYZER
    changes, indexes every work."""
    if not available(conn):
        return
    from changes.record import current

    table, state_table = _tables(conn)
    state = conn.execute(text(f"SELECT built, analyzer FROM {state_table} FOR UPDATE")).one()
    rebuild = not state.built or state.analyzer != ANALYZER
    if rebuild:
        conn.execute(text(f"DELETE FROM {table}"))
        if now is None or scope is not None:
            now = current(conn)
        scope = None
    elif now is None:
        now = current(conn, scope)
    held: dict[str, dict[str, str]] = {}
    for row in conn.execute(
        text(
            f"SELECT work_id, version_id, min(work_hash) AS work_hash FROM {table}"
            + ("" if scope is None else " WHERE work_id = ANY(:ids)")
            + " GROUP BY work_id, version_id"
        ),
        {} if scope is None else {"ids": sorted(scope)},
    ):
        held.setdefault(row.work_id, {})[row.version_id] = row.work_hash
    touched = set()
    for work_id, versions in held.items():
        readable = set((now.get(work_id) or {}).get("versions") or ())
        for version_id in set(versions) - readable:
            conn.execute(text(f"DELETE FROM {table} WHERE version_id = :v"), {"v": version_id})
            touched.add(work_id)
    for work_id, facts in now.items():
        projection = facts["projection"]
        kept = set(held.get(work_id, {})) & set(facts.get("versions") or ())
        for version_id in sorted(set(facts.get("versions") or ()) - kept):
            _index_version(conn, table, work_id, version_id, projection)
            touched.add(work_id)
        if kept and any(held[work_id][v] != _work_hash(projection) for v in kept):
            # The work's record was corrected: its filter values, title and citation, for every unit.
            facts = {
                "work_id": work_id,
                "work_hash": _work_hash(projection),
                "jurisdiction_id": projection.get("jurisdiction_id") or "unknown",
                "document_type": projection.get("document_type") or "other",
                "legal_status": projection.get("legal_status") or "unknown",
                "year": projection.get("year"),
                "title": projection.get("title") or "",
            }
            cited = conn.scalars(
                text(f"SELECT DISTINCT pinpoint FROM {table} WHERE work_id = :w"), {"w": work_id}
            ).all()
            conn.execute(
                text(REFRESH.format(t=table)),
                [
                    facts | {"pinpoint": p, "identifiers": identifiers(projection.get("citation") or "", p)}
                    for p in cited
                ],
            )
    if touched:
        conn.execute(
            text(
                f"UPDATE {table} u SET replaced = EXISTS (SELECT 1 FROM {table} v WHERE v.work_id = u.work_id"
                " AND v.prior_version_id = u.version_id AND v.version_id <> u.version_id)"
                " WHERE u.work_id = ANY(:ids)"
            ),
            {"ids": sorted(touched)},
        )
    documents = _documents(conn)
    if documents is not None:
        _sync_documents(conn, documents, now, scope, rebuild)
    if rebuild:
        conn.execute(
            text(f"UPDATE {state_table} SET built = true, analyzer = :a, built_at = statement_timestamp()"),
            {"a": ANALYZER},
        )


DOCUMENT_INSERT = """
INSERT INTO {t} (version_id, work_id, start_byte, end_byte, node_id, language, config, jurisdiction_id,
    document_type, legal_status, year, effective_from, effective_until, content_hash, prior_version_id,
    work_hash, names, outline_truncated, document, context)
VALUES (:version_id, :work_id, :start_byte, :end_byte, :node_id, :language, :config, :jurisdiction_id,
    :document_type, :legal_status, :year, :effective_from, :effective_until, :content_hash,
    :prior_version_id, :work_hash, :names, :truncated,
    setweight(to_tsvector(CAST(:analysis AS regconfig), :title), 'A')
        || setweight(to_tsvector(CAST(:analysis AS regconfig), :words), 'B')
        || setweight(to_tsvector(CAST(:analysis AS regconfig), :outline), 'C'),
    setweight(to_tsvector('simple', :identifiers), 'D'))
"""


def _aliases(conn: Connection, work_id: str) -> list[str]:
    """The values a work is cited by as a whole (aliases naming a part are left out)."""
    citations = tables["citations"]
    rows = conn.execute(select(citations.c.payload).where(citations.c.source_id == work_id)).all()
    return sorted({row.payload["value"] for row in rows if not row.payload.get("node_id")})


def _sync_documents(conn: Connection, table: str, now: dict, scope: set[str] | None, rebuild: bool) -> None:
    """Document units for exactly the texts ``now`` says anyone may read, rebuilt when the work's
    public fields or names change (their hash is kept in work_hash)."""
    if rebuild:
        conn.execute(text(f"DELETE FROM {table}"))
    held = {
        row.version_id: row.work_hash
        for row in conn.execute(
            text(
                f"SELECT version_id, work_hash FROM {table}"
                + ("" if scope is None else " WHERE work_id = ANY(:ids)")
            ),
            {} if scope is None else {"ids": sorted(scope)},
        )
    }
    wanted = {}
    for work_id, facts in now.items():
        projection = facts["projection"]
        names = name_forms([projection.get("title"), projection.get("citation"), *_aliases(conn, work_id)])
        digest = hashlib.sha256(
            json.dumps(
                [ANALYZER, {k: projection.get(k) for k in WORK_FIELDS}, names], sort_keys=True
            ).encode()
        ).hexdigest()
        for version_id in facts.get("versions") or ():
            wanted[version_id] = (work_id, projection, names, digest)
    stale = [v for v, digest in held.items() if v not in wanted or wanted[v][3] != digest]
    if stale:
        conn.execute(text(f"DELETE FROM {table} WHERE version_id = ANY(:ids)"), {"ids": stale})
    touched = set()
    for version_id, (work_id, projection, names, digest) in sorted(wanted.items()):
        if held.get(version_id) == digest:
            continue
        loaded = _load(conn, version_id)
        if loaded is None:
            continue
        version, nodes = loaded
        unit = document_unit(version, nodes)
        config = config_for(version.language)
        raw = version.canonical_text.encode()
        conn.execute(
            text(DOCUMENT_INSERT.format(t=table)),
            {
                "version_id": version_id,
                "work_id": work_id,
                "start_byte": unit.start,
                "end_byte": unit.end,
                "node_id": unit.node_id,
                "language": version.language,
                "config": config,
                "analysis": config,
                "jurisdiction_id": projection.get("jurisdiction_id") or "unknown",
                "document_type": projection.get("document_type") or "other",
                "legal_status": projection.get("legal_status") or "unknown",
                "year": projection.get("year"),
                "effective_from": version.effective_from,
                "effective_until": version.effective_until,
                "content_hash": version.content_hash,
                "prior_version_id": version.prior_version_id,
                "work_hash": digest,
                "names": names,
                "truncated": unit.truncated,
                "title": projection.get("title") or "",
                "words": raw[unit.start : unit.end].decode("utf-8"),
                "outline": unit.outline,
                "identifiers": identifiers(projection.get("citation") or "", None) + " " + " ".join(names),
            },
        )
        touched.add(work_id)
    if touched or stale:
        conn.execute(
            text(
                f"UPDATE {table} u SET replaced = EXISTS (SELECT 1 FROM {table} v WHERE v.work_id = u.work_id"
                " AND v.prior_version_id = u.version_id AND v.version_id <> u.version_id)"
                + ("" if scope is None else " WHERE u.work_id = ANY(:ids)")
            ),
            {} if scope is None else {"ids": sorted(scope)},
        )


def ensure(conn: Connection) -> bool:
    """Build the index if it is not built (the worker's run; a deployment before its first publication).
    True when this call built it."""
    if not available(conn):
        return False
    state = conn.execute(text(f"SELECT built, analyzer FROM {_tables(conn)[1]}")).one()
    if state.built and state.analyzer == ANALYZER:
        return False
    sync(conn, None, None)
    return True


# --- searching ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Filters:
    jurisdictions: tuple[str, ...] | None = None  # None: every jurisdiction
    language: str | None = None
    types: tuple[str, ...] = ()
    statuses: tuple[str, ...] = ()
    as_of: date | None = None
    year_from: int | None = None
    year_to: int | None = None
    source_id: str | None = None
    version_id: str | None = None
    kinds: tuple[str, ...] = ()  # provision kinds (the definition route); never for document units

    def where(self) -> tuple[str, dict]:
        clauses, params = ["true"], {}
        if self.jurisdictions is not None:
            clauses.append("u.jurisdiction_id = ANY(:f_jurisdictions)")
            params["f_jurisdictions"] = list(self.jurisdictions)
        if self.language:
            # RFC 4647 basic filtering: "en" also matches "en-NG".
            clauses.append("(lower(u.language) = :f_language OR lower(u.language) LIKE :f_language_range)")
            params |= {
                "f_language": self.language.casefold(),
                "f_language_range": self.language.casefold() + "-%",
            }
        if self.types:
            clauses.append("u.document_type = ANY(:f_types)")
            params["f_types"] = list(self.types)
        if self.statuses:
            clauses.append("u.legal_status = ANY(:f_statuses)")
            params["f_statuses"] = list(self.statuses)
        if self.as_of:
            clauses.append(
                "u.effective_from <= :f_as_of AND (u.effective_until IS NULL OR :f_as_of < u.effective_until)"
            )
            params["f_as_of"] = self.as_of
        if self.year_from is not None:
            clauses.append("u.year >= :f_year_from")
            params["f_year_from"] = self.year_from
        if self.year_to is not None:
            clauses.append("u.year <= :f_year_to")
            params["f_year_to"] = self.year_to
        if self.source_id:
            clauses.append("u.work_id = :f_source")
            params["f_source"] = self.source_id
        if self.version_id:
            clauses.append("u.version_id = :f_version")
            params["f_version"] = self.version_id
        if self.kinds:
            clauses.append("u.kind = ANY(:f_kinds)")
            params["f_kinds"] = list(self.kinds)
        return " AND ".join(clauses), params


@dataclass
class Found:
    state: str  # ok, no_terms, timeout, building, unavailable
    rows: list = field(default_factory=list)
    total: int = 0
    exact: bool = True
    common_only: tuple[str, ...] = ()  # analyses in which the query has only common words


@dataclass(frozen=True)
class Scope:
    texts: int
    records: int
    languages: tuple[str, ...]
    built_at: object = None


# What a hit needs from its unit; never the search vectors.
_COLUMNS = ", ".join(
    f"u.{c}"
    for c in ("version_id", "start_byte", "end_byte", "work_id", "node_id", "kind", "locator", "pinpoint",
              "heading", "language", "config", "content_hash", "legal_status", "replaced", "jurisdiction_id",
              "document_type")
)  # fmt: skip


_MATCH = " OR ".join(
    f"(u.config = '{c}' AND (u.document || u.context) @@ websearch_to_tsquery('{c}', :q))" for c in ANALYSES
)
_SCORE = (
    f"ts_rank_cd(CAST('{WEIGHTS}' AS float4[]), u.document || u.context,"
    " websearch_to_tsquery(CAST(u.config AS regconfig), :q), 32)"
    f" * CASE WHEN u.replaced OR u.legal_status IN ('repealed', 'superseded', 'historical') THEN {NOT_IN_FORCE}"
    " ELSE 1.0 END"
)

# The any-term arm (oa-any-1): each of the query's words, per analysis, weighted by how rare it is in
# the index (an inverse document frequency from capped counts), so a provision with the rare words of
# a question outranks a long one with its common words. An approximation of BM25's term weighting
# over PostgreSQL's ranking, not BM25 (EMB-03 keeps a pinned BM25 engine as a separate comparison).
ANY_RANKING = "oa-any-1"
MAX_ANY_TERMS = 12
DF_BOUND = 50_000


def _weighted(conn: Connection, table: str, query: str) -> tuple[str, str, dict] | None:
    """The any-term arm's match and score expressions and their parameters; None when the query has no
    indexable word in any analysis."""
    total = conn.scalar(
        text("SELECT greatest(reltuples, 0)::bigint FROM pg_class WHERE oid = to_regclass(:t)"), {"t": table}
    )
    if not total:
        total = conn.scalar(text(f"SELECT count(*) FROM {table}")) or 0
    matches, scores, params = [], [], {}
    for config in ANALYSES:
        present = conn.scalar(
            text(f"SELECT EXISTS (SELECT 1 FROM {table} u WHERE u.config = :c)"), {"c": config}
        )
        if not present:
            continue
        lexemes = conn.scalars(
            text("SELECT unnest(tsvector_to_array(to_tsvector(CAST(:c AS regconfig), :q)))"),
            {"c": config, "q": query},
        ).all()[:MAX_ANY_TERMS]
        if not lexemes:
            continue
        parts, keys = [], []
        for n, lexeme in enumerate(sorted(lexemes)):
            key = f"lx_{config}_{n}"
            params[key] = "'" + lexeme.replace("'", "''") + "'"
            keys.append(key)
            df = conn.scalar(
                text(
                    f"SELECT count(*) FROM (SELECT 1 FROM {table} u WHERE u.config = :c"
                    f" AND (u.document || u.context) @@ CAST(:lx AS tsquery) LIMIT {DF_BOUND}) AS d"
                ),
                {"c": config, "lx": params[key]},
            )
            idf = math.log(1 + (max(total, df) - df + 0.5) / (df + 0.5))
            parts.append(
                f"{idf:.6f} * ts_rank_cd(CAST('{WEIGHTS}' AS float4[]), u.document || u.context,"
                f" CAST(:{key} AS tsquery), 1)"
            )
        params[f"any_{config}"] = " | ".join(params[k] for k in keys)
        matches.append(
            f"(u.config = '{config}' AND (u.document || u.context) @@ CAST(:any_{config} AS tsquery))"
        )
        scores.append(f"WHEN '{config}' THEN {' + '.join(parts)}")
    if not matches:
        return None
    score = (
        f"(CASE u.config {' '.join(scores)} ELSE 0 END)"
        f" * CASE WHEN u.replaced OR u.legal_status IN ('repealed', 'superseded', 'historical')"
        f" THEN {NOT_IN_FORCE} ELSE 1.0 END"
    )
    return " OR ".join(matches), score, params


def built(conn: Connection):
    """When the index was built, or None while it is not (or not this ANALYZER's)."""
    if not available(conn):
        return None
    state = conn.execute(text(f"SELECT built, analyzer, built_at FROM {_tables(conn)[1]}")).one()
    return state.built_at if state.built and state.analyzer == ANALYZER else None


def scope(conn: Connection, filters: Filters) -> Scope:
    """How many published texts and records the filters cover, and their languages."""
    table, _ = _tables(conn)
    where, params = filters.where()
    row = conn.execute(
        text(
            f"SELECT count(DISTINCT u.version_id) AS texts, count(DISTINCT u.work_id) AS records,"
            f" coalesce(array_agg(DISTINCT u.language), '{{}}') AS languages FROM {table} u WHERE {where}"
        ),
        params,
    ).one()
    return Scope(row.texts, row.records, tuple(sorted(row.languages)), built(conn))


def search(
    conn: Connection,
    query: str,
    filters: Filters,
    *,
    limit: int,
    offset: int,
    exclude: tuple | None = None,
    any_terms: bool = False,
) -> Found:
    """One page of units matching ``query`` within ``filters``, best first, and how many match; with
    ``any_terms``, units matching any of its terms rather than all."""
    if not available(conn):
        return Found("unavailable")
    if built(conn) is None:
        return Found("building")
    table, _ = _tables(conn)
    terms = dict(
        conn.execute(
            text(
                "SELECT c, numnode(websearch_to_tsquery(CAST(c AS regconfig), :q)) > 0"
                " FROM unnest(CAST(:configs AS text[])) AS c"
            ),
            {"q": query, "configs": list(ANALYSES)},
        ).all()
    )
    common_only = tuple(c for c, found in sorted(terms.items()) if not found)
    if not any(terms.values()):
        return Found("no_terms", common_only=common_only)
    where, params = filters.where()
    params |= {"q": query}
    if exclude:
        where += " AND NOT (u.version_id = :x_version AND u.start_byte = :x_start AND u.end_byte = :x_end)"
        params |= dict(zip(("x_version", "x_start", "x_end"), exclude, strict=True))
    match, score = _MATCH, _SCORE
    if any_terms:
        weighted = _weighted(conn, table, query)
        if weighted is None:
            return Found("no_terms", common_only=common_only)
        match, score, extra = weighted
        params |= extra
    old = conn.scalar(text("SELECT current_setting('statement_timeout')"))
    try:
        with conn.begin_nested():
            conn.execute(text("SELECT set_config('statement_timeout', :ms, true)"), {"ms": str(TIMEOUT_MS)})
            total = conn.scalar(
                text(
                    f"SELECT count(*) FROM (SELECT 1 FROM {table} u WHERE ({match}) AND {where}"
                    f" LIMIT {COUNT_BOUND + 1}) AS matched"
                ),
                params,
            )
            rows = conn.execute(
                text(
                    f"SELECT {_COLUMNS}, {score} AS score FROM {table} u WHERE ({match}) AND {where}"
                    " ORDER BY score DESC, u.version_id, u.start_byte LIMIT :limit OFFSET :offset"
                ),
                params | {"limit": limit, "offset": offset},
            ).all()
            conn.execute(text("SELECT set_config('statement_timeout', :ms, true)"), {"ms": old})
    except OperationalError as error:
        if getattr(error.orig, "sqlstate", None) == "57014":  # query_canceled: the deadline passed
            return Found("timeout")
        raise
    return Found("ok", list(rows), min(total, COUNT_BOUND), total <= COUNT_BOUND, common_only)


def headlines(conn: Connection, query: str, items: list[tuple[str, str]]) -> list[str]:
    """For each (config, words), PostgreSQL's best fragment of the words for the query, terms marked."""
    if not items:
        return []
    rows = conn.execute(
        text(
            "SELECT t.n, ts_headline(CAST(t.config AS regconfig), t.words,"
            " websearch_to_tsquery(CAST(t.config AS regconfig), :q), :options)"
            " FROM unnest(CAST(:ns AS integer[]), CAST(:configs AS text[]), CAST(:words AS text[]))"
            " AS t(n, config, words)"
        ),
        {
            "q": query,
            "options": HEADLINE,
            "ns": list(range(len(items))),
            "configs": [c for c, _ in items],
            "words": [w for _, w in items],
        },
    ).all()
    found = dict(rows)
    return [found.get(n, "") for n in range(len(items))]


def place(words: str, headline: str, base: int) -> tuple[str, int, int, tuple[tuple[int, int], ...]]:
    """A snippet as exact canonical bytes: the headline's words found in the unit's ``words`` (which
    start at byte ``base`` of the text), their byte span, and the byte spans of the marked terms. When
    the headline cannot be found as it is, the unit's opening words, unmarked."""
    plain = headline.replace(START, "").replace(STOP, "")
    at = words.find(plain) if plain.strip() else -1
    if at < 0:
        opening = words[:300]
        cut = opening.rfind(" ") if len(words) > 300 else len(opening)
        snippet = opening[: cut if cut > 0 else len(opening)]
        start = base
        return snippet, start, start + len(snippet.encode()), ()
    start = base + len(words[:at].encode())
    marks, offset, open_at = [], 0, None
    for piece in re.split(f"([{START}{STOP}])", headline):
        if piece == START:
            open_at = offset
        elif piece == STOP and open_at is not None:
            marks.append((start + open_at, start + offset))
            open_at = None
        else:
            offset += len(piece.encode())
    return plain, start, start + len(plain.encode()), tuple(marks)


# --- document units (RSV-02, RSV-03) -----------------------------------------------------------------

_DOCUMENT_COLUMNS = ", ".join(
    f"u.{c}"
    for c in ("version_id", "work_id", "start_byte", "end_byte", "node_id", "language", "config",
              "content_hash", "legal_status", "replaced")
)  # fmt: skip
MAX_NAMED = 5  # a query naming more works than this names none in particular


def search_documents(conn: Connection, query: str, filters: Filters, *, limit: int) -> Found:
    """Document units matching ``query`` within ``filters``, best first (the same ranking as units)."""
    table = _documents(conn) if available(conn) else None
    if table is None:
        return Found("unavailable")
    if built(conn) is None:
        return Found("building")
    where, params = replace(filters, kinds=()).where()
    params |= {"q": query, "limit": limit}
    try:
        with conn.begin_nested():
            conn.execute(text("SELECT set_config('statement_timeout', :ms, true)"), {"ms": str(TIMEOUT_MS)})
            rows = conn.execute(
                text(
                    f"SELECT {_DOCUMENT_COLUMNS}, {_SCORE} AS score FROM {table} u WHERE ({_MATCH}) AND {where}"
                    " ORDER BY score DESC, u.version_id LIMIT :limit"
                ),
                params,
            ).all()
    except OperationalError as error:
        if getattr(error.orig, "sqlstate", None) == "57014":
            return Found("timeout")
        raise
    return Found("ok", list(rows), len(rows))


def named_works(conn: Connection, query: str, filters: Filters) -> dict[str, str]:
    """The works the query names by a whole name (title, citation or alias, folded), each with the
    longest name found; empty when none, or when more than MAX_NAMED works match."""
    table = _documents(conn) if available(conn) else None
    folded = fold(query)
    if table is None or len(folded) < MIN_NAME:
        return {}
    where, params = replace(filters, kinds=(), version_id=None).where()
    rows = conn.execute(
        text(
            f"SELECT DISTINCT u.work_id, n.name FROM {table} u CROSS JOIN LATERAL unnest(u.names) AS n(name)"
            f" WHERE position(' ' || n.name || ' ' IN :q) > 0 AND {where}"
        ),
        params | {"q": f" {folded} "},
    ).all()
    found: dict[str, str] = {}
    for work_id, name in rows:
        if len(name) > len(found.get(work_id, "")):
            found[work_id] = name
    return found if len(found) <= MAX_NAMED else {}
