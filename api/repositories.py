"""Read-only pilot repositories. Connections/transactions belong to the caller.

Legacy JSON remains authoritative. SQL limits row windows and never selects unapproved
canonical bodies; the shared policy runs on gate fields before an individual body fetch.
"""

import unicodedata
from collections.abc import Iterator

from sqlalchemy import (
    LargeBinary,
    Text,
    and_,
    case,
    cast,
    false,
    func,
    literal,
    literal_column,
    or_,
    select,
    text,
    true,
    union_all,
)
from sqlalchemy.engine import Connection

from api.search import literal_pattern
from api.store import RELATIONS, tables
from schema.models import Amendment, Source
from schema.public import RightsViewRecord, VersionGate, VersionViewRecord

BATCH_SIZE = 100
# The website's catalogue reads at most this many public sources' compact rows in one request.
BROWSE_BOUND = 25000
# Letters whose case folding is not their lower case (ß → ss, final ς → σ): the database lowers them
# differently from normalize_citation, so a source holding one is matched in Python.
_FOLDED_APART = "".join(
    c
    for c in map(chr, range(0x80, 0x10000))
    if unicodedata.normalize("NFKC", c).casefold() != unicodedata.normalize("NFKC", c).lower()
)
# A stored link (newer → older) as each side sees it, and the order a reader asks about them in.
EDGE_KINDS = {
    ("in", "repeals"): "repealed_by",
    ("in", "supersedes"): "superseded_by",
    ("in", "amends"): "amended_by",
    ("out", "made_under"): "made_under",
    ("out", "repeals"): "repeals",
    ("out", "supersedes"): "supersedes",
    ("out", "amends"): "amends",
    ("out", "cross_reference"): "cross_reference",
    ("in", "cross_reference"): "cross_reference",
    ("in", "made_under"): "enables",
}
EDGE_ORDER = [
    "repealed_by",
    "superseded_by",
    "amended_by",
    "made_under",
    "repeals",
    "supersedes",
    "amends",
    "cross_reference",
    "enables",
]
MAX_UNPAGED_ROWS = 1000
MAX_BUFFER_BYTES = 10 * 1024 * 1024
# A version's structure: an Act of a few hundred pages has a few thousand nodes (the Electricity Act
# 2023: 2,144), each a few hundred bytes; the response byte caps still apply.
MAX_STRUCTURE_ROWS = 20_000


class PublicUnavailable(ValueError):
    pass


class ResultTooLarge(ValueError):
    pass


class LegacyRepository:
    def __init__(self, connection: Connection):
        self.connection = connection
        self._sources = tables["sources"]
        self._rights = tables["rights"]
        self._indexed_metadata = None

    @staticmethod
    def _identity_matches(table):
        return and_(
            table.c.payload["id"].as_string() == table.c.id,
            *(table.c.payload[key].as_string() == table.c[key] for key in RELATIONS[table.name]),
        )

    def _visible(self):
        r = self._rights.c.payload
        return and_(
            self._identity_matches(self._sources),
            self._identity_matches(self._rights),
            r["metadata_public"].as_boolean().is_(True),
            r["metadata_basis"].as_string() != "",
            r["status"].as_string() != "RESTRICTED",
        )

    def _select_sources(self, jurisdiction=None):
        statement = (
            select(self._sources.c.payload.label("source"), self._rights.c.payload.label("rights"))
            .select_from(self._sources.join(self._rights, self._sources.c.id == self._rights.c.source_id))
            .where(self._visible())
        )
        if jurisdiction is None:
            return statement
        if isinstance(jurisdiction, str):
            return statement.where(self._sources.c.jurisdiction_id == jurisdiction)
        return statement.where(self._sources.c.jurisdiction_id.in_(sorted(jurisdiction)))  # a plan's reach

    @staticmethod
    def _pair(row):
        source = Source.model_validate(row.source)
        rights = RightsViewRecord.model_validate(row.rights)
        if rights.source_id != source.id:
            raise PublicUnavailable("Policy/source identity mismatch")
        return source, rights

    def source(self, source_id: str):
        row = self.connection.execute(
            self._select_sources().where(self._sources.c.id == source_id)
        ).one_or_none()
        return self._pair(row) if row is not None else None

    def source_page(self, *, limit: int, offset: int, jurisdiction: str | None = None, where=None):
        if not 1 <= limit <= BATCH_SIZE or offset < 0:
            raise ValueError("Invalid page bounds")
        statement = self._select_sources(jurisdiction)
        if where is not None:
            statement = statement.where(where)
        total = self.connection.scalar(select(func.count()).select_from(statement.subquery()))
        rows = self.connection.execute(statement.order_by(self._sources.c.id).limit(limit).offset(offset))
        return total, tuple(self._pair(row) for row in rows)

    def sources(self, jurisdiction=None, *, where=None) -> Iterator:
        cursor = None
        while True:
            statement = self._select_sources(jurisdiction).order_by(self._sources.c.id).limit(BATCH_SIZE)
            if where is not None:
                statement = statement.where(where)
            if cursor is not None:
                statement = statement.where(self._sources.c.id > cursor)
            rows = self.connection.execute(statement).all()
            for row in rows:
                yield self._pair(row)
            if len(rows) < BATCH_SIZE:
                return
            cursor = rows[-1].source["id"]

    def potential_text(self):
        hashes = self._rights.c.payload["approved_content_hashes"].as_string()
        return and_(hashes.is_not(None), hashes.not_in(("[]", "", "null")))

    def access_predicate(self, open_ids, access):
        if access == "all":
            return None
        return self._sources.c.id.in_(open_ids) if access == "open" else self._sources.c.id.not_in(open_ids)

    def search_predicates(self, term):
        if self._indexed_metadata is None:
            self._indexed_metadata = self.connection.dialect.name != "postgresql" or bool(
                self.connection.scalar(
                    text("SELECT to_regprocedure('legacy_metadata_normalize(text)') IS NOT NULL")
                )
            )
        if not self._indexed_metadata:
            # Preserve legacy read compatibility while an older schema is upgrading.
            return false(), true()
        payload = self._sources.c.payload
        if self.connection.dialect.name == "postgresql":
            title = payload.op("->>", return_type=Text)("title")
            citation = payload.op("->>", return_type=Text)("citation")
        else:
            title, citation = payload["title"].as_string(), payload["citation"].as_string()
        value = func.coalesce(title, "") + literal(" ") + func.coalesce(citation, "")
        metadata = func.legacy_metadata_normalize(value).like(literal_pattern(term), escape="\\")
        if self.connection.dialect.name == "postgresql":
            non_ascii = func.octet_length(value) != func.length(value)
            confirmed = and_(~non_ascii, metadata)
            fallback = and_(~confirmed, or_(non_ascii, self.potential_text()))
        else:
            confirmed = metadata
            fallback = and_(~confirmed, self.potential_text())
        return confirmed, fallback

    def _unicode_lower(self) -> bool:
        """Whether this PostgreSQL lowers letters beyond ASCII (a UTF-8 ctype), as Python does."""
        if getattr(self, "_lowers_unicode", None) is None:
            self._lowers_unicode = bool(self.connection.scalar(text("SELECT lower('ÉΣ') = 'éσ'")))
        return self._lowers_unicode

    def browse_rows(self, term: str | None):
        """Every public source as a compact row for the website's catalogue: its id, title, year,
        filter values and, given a search term (normalize_citation's), its rank (3 title, 2
        citation, 1 summary) where the database matches as normalize_citation does; `python` marks
        a source to match in Python (one with text to read, or a letter the database folds apart).
        Without a term every source comes back. Ordered by id; at most BROWSE_BOUND."""
        payload = self._sources.c.payload
        if self.connection.dialect.name == "postgresql":
            field = lambda name: payload.op("->>", return_type=Text)(name)  # noqa: E731
        else:
            field = lambda name: payload[name].as_string()  # noqa: E731
        title, citation, summary = field("title"), field("citation"), field("summary")
        exact = self.connection.dialect.name != "postgresql"  # SQLite runs normalize_citation itself
        if exact or not self._unicode_lower():
            normal = lambda value: func.legacy_metadata_normalize(func.coalesce(value, ""))  # noqa: E731
        else:
            normal = lambda value: func.btrim(  # noqa: E731
                func.regexp_replace(
                    func.lower(func.normalize(func.coalesce(value, ""), literal_column("NFKC"))),
                    r"\s+",
                    " ",
                    "g",
                )
            )
        python = self.potential_text()
        if not exact:
            everything = (
                func.coalesce(title, "")
                + " "
                + func.coalesce(citation, "")
                + " "
                + func.coalesce(summary, "")
            )
            if self._unicode_lower():
                python = or_(python, everything.op("~")(f"[{_FOLDED_APART}]"))
            else:
                python = or_(python, func.octet_length(everything) != func.length(everything))
        if term is None:
            rank = literal(0)
        else:
            pattern = literal_pattern(term)
            rank = case(
                (normal(title).like(pattern, escape="\\"), 3),
                (normal(citation).like(pattern, escape="\\"), 2),
                (normal(summary).like(pattern, escape="\\"), 1),
                else_=0,
            )
        statement = (
            select(
                self._sources.c.id,
                title.label("title"),
                payload["year"].as_integer().label("year"),
                self._sources.c.jurisdiction_id,
                field("document_type").label("type"),
                field("language").label("language"),
                field("legal_status").label("status"),
                rank.label("rank"),
                python.label("python"),
            )
            .select_from(self._sources.join(self._rights, self._sources.c.id == self._rights.c.source_id))
            .where(self._visible())
            .order_by(self._sources.c.id)
            .limit(BROWSE_BOUND + 1)
        )
        if term is not None:
            statement = statement.where(or_(rank > 0, python))
        rows = self.connection.execute(statement).all()
        if len(rows) > BROWSE_BOUND:
            raise ResultTooLarge("Pilot catalogue browsing exceeds declared bound")
        return rows

    def public_counts(self) -> dict[str, int]:
        """How many public sources each jurisdiction has, in order of each one's first source id."""
        statement = (
            select(self._sources.c.jurisdiction_id, func.count())
            .select_from(self._sources.join(self._rights, self._sources.c.id == self._rights.c.source_id))
            .where(self._visible())
            .group_by(self._sources.c.jurisdiction_id)
            .order_by(func.min(self._sources.c.id))
        )
        return dict(self.connection.execute(statement).all())

    def search_page(self, term, matched_ids, *, jurisdiction, limit, offset, access_where=None):
        confirmed, _ = self.search_predicates(term)
        predicate = or_(confirmed, self._sources.c.id.in_(matched_ids))
        if access_where is not None:
            predicate = and_(predicate, access_where)
        return self.source_page(limit=limit, offset=offset, jurisdiction=jurisdiction, where=predicate)

    def version_gates(self, source_id: str, hashes: tuple[str, ...]) -> Iterator[VersionGate]:
        if not hashes:
            return
        table = tables["versions"]
        fields = ("content_hash", "verification_status", "content_verifier", "content_verified_at")
        statement = (
            select(
                table.c.id,
                table.c.source_id,
                *(table.c.payload[key].as_string().label(key) for key in fields),
            )
            .where(
                table.c.source_id == source_id,
                self._identity_matches(table),
                table.c.payload["content_hash"].as_string().in_(hashes),
            )
            .order_by(table.c.id)
        )
        cursor = None
        while True:
            query = statement.limit(BATCH_SIZE)
            if cursor is not None:
                query = query.where(table.c.id > cursor)
            rows = self.connection.execute(query).mappings().all()
            for row in rows:
                yield VersionGate.model_validate(dict(row))
            if len(rows) < BATCH_SIZE:
                return
            cursor = rows[-1]["id"]

    def version(self, version_id: str, source_id: str) -> VersionViewRecord:
        table = tables["versions"]
        body = table.c.payload["canonical_text"].as_string()
        length = (
            func.octet_length(body)
            if self.connection.dialect.name == "postgresql"
            else func.length(cast(body, LargeBinary))
        )
        where = (table.c.id == version_id, table.c.source_id == source_id, self._identity_matches(table))
        size = self.connection.scalar(select(length).where(*where))
        if size is None:
            raise PublicUnavailable("Version unavailable")
        if size > MAX_BUFFER_BYTES:
            raise ResultTooLarge("Pilot response exceeds byte bound")
        payload = self.connection.scalar(select(table.c.payload).where(*where))
        return VersionViewRecord.model_validate(payload)

    def rows(self, name: str, *, ids=None, source_id=None, version_ids=None, bound: int = MAX_UNPAGED_ROWS):
        table = tables[name]
        statement = select(table.c.payload).where(self._identity_matches(table)).order_by(table.c.id)
        if ids is not None:
            statement = statement.where(table.c.id.in_(ids))
        if source_id is not None:
            statement = statement.where(table.c.source_id == source_id)
        if version_ids is not None:
            statement = statement.where(table.c.version_id.in_(version_ids))
        rows = list(self.connection.scalars(statement.limit(bound + 1)))
        if len(rows) > bound:
            raise ResultTooLarge("Pilot unpaged response exceeds row bound")
        return rows

    def version_owner(self, version_id: str) -> str | None:
        """The source a version belongs to, or None; nothing about whether it is public."""
        table = tables["versions"]
        return self.connection.scalar(
            select(table.c.source_id).where(table.c.id == version_id, self._identity_matches(table))
        )

    def versions_at(self, addresses: list[str], *, limit: int = 20) -> list[tuple[str, str]]:
        """(version, source) pairs whose official address is one of ``addresses``; nothing about
        whether they are public."""
        table = tables["versions"]
        return [
            tuple(row)
            for row in self.connection.execute(
                select(table.c.id, table.c.source_id)
                .where(
                    table.c.payload["canonical_url"].as_string().in_(addresses), self._identity_matches(table)
                )
                .order_by(table.c.id)
                .limit(limit)
            )
        ]

    def node(self, node_id: str) -> dict | None:
        table = tables["structure"]
        return self.connection.scalar(
            select(table.c.payload).where(table.c.id == node_id, self._identity_matches(table))
        )

    def edges(self, source_id: str, kinds, *, limit: int, offset: int):
        """One page of a source's checked links to public sources, each as seen from its side
        (EDGE_KINDS), in EDGE_ORDER then by the other source; and how many there are. The database
        filters, counts and pages, so a law with hundreds of orders made under it costs one page."""
        table, sources, rights = tables["amendments"], self._sources, self._rights
        stored = table.c.payload["kind"].as_string()
        parts = []
        for direction, mine, other in (
            ("out", table.c.source_id, table.c.target_source_id),
            ("in", table.c.target_source_id, table.c.source_id),
        ):
            wanted = [(k, seen) for (d, k), seen in EDGE_KINDS.items() if d == direction and seen in kinds]
            if not wanted:
                continue
            parts.append(
                select(
                    table.c.payload.label("payload"),
                    case(*[(stored == k, EDGE_ORDER.index(seen)) for k, seen in wanted]).label("rank"),
                    other.label("other"),
                    table.c.id.label("edge"),
                )
                .select_from(
                    table.join(sources, sources.c.id == other).join(rights, rights.c.source_id == other)
                )
                .where(
                    self._identity_matches(table),
                    # Only links a reviewer has checked are public, as in the releases (rights.policy).
                    table.c.payload["verified"].as_boolean().is_(True),
                    mine == source_id,
                    stored.in_([k for k, _ in wanted]),
                    self._visible(),
                )
            )
        if not parts:
            return 0, []
        edges = union_all(*parts).subquery()
        total = self.connection.scalar(select(func.count()).select_from(edges))
        rows = self.connection.execute(
            select(edges).order_by(edges.c.rank, edges.c.other, edges.c.edge).limit(limit).offset(offset)
        ).all()
        return total, [
            (EDGE_ORDER[row.rank], row.other, Amendment.model_validate(row.payload)) for row in rows
        ]

    def replaced(self, source_id: str):
        """What repealed or replaced a source (checked links only; at most a page of them)."""
        table = tables["amendments"]
        kind = table.c.payload["kind"].as_string()
        statement = select(table.c.payload).where(
            self._identity_matches(table),
            table.c.payload["verified"].as_boolean().is_(True),
            table.c.target_source_id == source_id,
            kind.in_(("repeals", "supersedes")),
        )
        return [Amendment.model_validate(row) for row in self.connection.scalars(statement.limit(BATCH_SIZE))]

    def visible(self, ids) -> dict:
        """The sources among these that the public may see, with their rights records."""
        if not ids:
            return {}
        rows = self.connection.execute(self._select_sources().where(self._sources.c.id.in_(sorted(ids))))
        return {pair[0].id: pair for pair in map(self._pair, rows)}

    def aliases(self, source_id: str):
        return self.rows("citations", source_id=source_id)

    def _field(self, table, name):
        if self.connection.dialect.name == "postgresql":
            return table.c.payload.op("->>", return_type=Text)(name)
        return table.c.payload[name].as_string()

    def citation_candidates(self, term: str) -> list[tuple[str, dict | None]]:
        """Every catalogued value that may read as ``term`` (normalize_citation's), as (source id, the
        alias, or None for the source's own citation or title). One query each for aliases and sources;
        the caller confirms each value, so a value the database folds differently is never matched."""
        self.search_predicates(term)  # learns whether the indexed normaliser exists
        citations, sources = tables["citations"], self._sources

        def may_be(value):
            if not self._indexed_metadata:
                return true()
            normal = func.legacy_metadata_normalize(func.coalesce(value, "")) == term
            if self.connection.dialect.name != "postgresql":
                return normal
            return or_(normal, func.octet_length(value) != func.length(value))

        aliases = self.connection.execute(
            select(citations.c.source_id, citations.c.payload)
            .where(self._identity_matches(citations), may_be(self._field(citations, "value")))
            .limit(MAX_UNPAGED_ROWS + 1)
        ).all()
        own = self.connection.execute(
            select(sources.c.id)
            .where(
                self._identity_matches(sources),
                or_(may_be(self._field(sources, "citation")), may_be(self._field(sources, "title"))),
            )
            .limit(MAX_UNPAGED_ROWS + 1)
        ).all()
        if len(aliases) > MAX_UNPAGED_ROWS or len(own) > MAX_UNPAGED_ROWS:
            raise ResultTooLarge("Citation candidates exceed pilot bound")
        return [(row.source_id, row.payload) for row in aliases] + [(row.id, None) for row in own]

    def count_public(self, *, jurisdictions=None, types=(), statuses=(), language=None, years=(None, None)):
        """How many public sources match a text search's record filters."""
        statement = (
            select(func.count())
            .select_from(self._sources.join(self._rights, self._sources.c.id == self._rights.c.source_id))
            .where(self._visible())
        )
        if jurisdictions is not None:
            statement = statement.where(self._sources.c.jurisdiction_id.in_(sorted(jurisdictions)))
        if types:
            statement = statement.where(self._field(self._sources, "document_type").in_(sorted(types)))
        if statuses:
            statement = statement.where(self._field(self._sources, "legal_status").in_(sorted(statuses)))
        if language:
            lang = func.lower(self._field(self._sources, "language"))
            statement = statement.where(
                or_(lang == language.casefold(), lang.like(language.casefold() + "-%"))
            )
        year = self._sources.c.payload["year"].as_integer()
        if years[0] is not None:
            statement = statement.where(year >= years[0])
        if years[1] is not None:
            statement = statement.where(year <= years[1])
        return self.connection.scalar(statement)
