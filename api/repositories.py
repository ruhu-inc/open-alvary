"""Read-only pilot repositories. Connections/transactions belong to the caller.

Legacy JSON remains authoritative. SQL limits row windows and never selects unapproved
canonical bodies; the shared policy runs on gate fields before an individual body fetch.
"""

from collections.abc import Iterator

from sqlalchemy import (
    Boolean,
    LargeBinary,
    Text,
    and_,
    case,
    cast,
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

from api.search import FIELDS, fields_vector
from api.store import RELATIONS, tables
from schema.models import Amendment, Source
from schema.public import RightsViewRecord, VersionGate, VersionViewRecord

BATCH_SIZE = 100
# The website's catalogue reads at most this many public sources' compact rows in one request.
BROWSE_BOUND = 25000
# A stored link (newer → older) as each side sees it, and the order a reader asks about them in.
EDGE_KINDS = {
    ("in", "repeals"): "repealed_by",
    ("in", "supersedes"): "superseded_by",
    ("in", "amends"): "amended_by",
    ("in", "commences"): "commenced_by",
    ("out", "made_under"): "made_under",
    ("out", "transposes"): "transposes",
    ("out", "domesticates"): "domesticates",
    ("out", "implements"): "implements",
    ("out", "ratifies"): "ratifies",
    ("out", "repeals"): "repeals",
    ("out", "supersedes"): "supersedes",
    ("out", "amends"): "amends",
    ("out", "commences"): "commences",
    ("in", "transposes"): "transposed_by",
    ("in", "domesticates"): "domesticated_by",
    ("in", "implements"): "implemented_by",
    ("in", "ratifies"): "ratified_by",
    ("out", "cross_reference"): "cross_reference",
    ("in", "cross_reference"): "cross_reference",
    ("out", "cites"): "cites",
    ("in", "cites"): "cited_by",
    ("in", "made_under"): "enables",
}
# What changed this law, what it rests on or gives effect to, what it changed, what gives effect to it,
# what it refers to and is referred to by, and last the instruments made under it, which may be many.
EDGE_ORDER = [
    "repealed_by",
    "superseded_by",
    "amended_by",
    "commenced_by",
    "made_under",
    "transposes",
    "domesticates",
    "implements",
    "ratifies",
    "repeals",
    "supersedes",
    "amends",
    "commences",
    "transposed_by",
    "domesticated_by",
    "implemented_by",
    "ratified_by",
    "cross_reference",
    "cites",
    "cited_by",
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

    def treaties(self) -> Iterator:
        """The public records that carry a treaty's status (schema.models.TreatyStatus), in ID order."""
        return self.sources(where=self._field(self._sources, "treaty").is_not(None))

    def potential_text(self):
        hashes = self._rights.c.payload["approved_content_hashes"].as_string()
        return and_(hashes.is_not(None), hashes.not_in(("[]", "", "null")))

    def any_potential_text(self) -> bool:
        """Whether any record has an approved text, which may be published."""
        statement = select(literal(1)).select_from(self._rights).where(self.potential_text()).limit(1)
        return self.connection.scalar(statement) is not None

    def access_predicate(self, open_ids, access):
        if access == "all":
            return None
        return self._sources.c.id.in_(open_ids) if access == "open" else self._sources.c.id.not_in(open_ids)

    def matching(self, query: str):
        """The catalogue search's word tests (api.search) as SQL: every word of ``query`` among a
        source's title, citation and summary together, in its title alone, in its citation alone. On
        PostgreSQL the first is the expression migration 0038 indexes, so it is read from the index."""
        payload = self._sources.c.payload
        if self.connection.dialect.name == "postgresql":
            wanted = func.plainto_tsquery(
                literal_column("'simple'"),
                func.regexp_replace(
                    func.normalize(cast(literal(query), Text), literal_column("NFKC")),
                    "[[:punct:]]+",
                    " ",
                    "g",
                ),
            )

            def has(*fields):
                vector = literal_column(fields_vector(self._sources.name, fields))
                return vector.op("@@", return_type=Boolean)(wanted)
        else:

            def has(*fields):
                value = func.coalesce(payload[fields[0]].as_string(), "")
                for name in fields[1:]:
                    value = value + " " + func.coalesce(payload[name].as_string(), "")
                return func.catalogue_words_match(value, literal(query)) == 1

        return has(*FIELDS), has("title"), has("citation")

    def browse_rows(self, query: str | None, by_text=()):
        """Every public source as a compact row for the website's catalogue: its id, title, year,
        filter values, whether it may have a cleared text and, given a search, its rank: 3 when its
        title has every word (api.search), 2 its citation, else 1 (its summary, the three together, or
        ``by_text``, the sources whose published text matched). Without a search every source comes
        back; with one, only those that match. Ordered by id; at most BROWSE_BOUND."""
        payload = self._sources.c.payload
        if self.connection.dialect.name == "postgresql":
            field = lambda name: payload.op("->>", return_type=Text)(name)  # noqa: E731
        else:
            field = lambda name: payload[name].as_string()  # noqa: E731
        found = rank = None
        if query is not None:
            every, in_title, in_citation = self.matching(query)
            found = or_(every, self._sources.c.id.in_(sorted(by_text))) if by_text else every
            rank = case((in_title, 3), (in_citation, 2), else_=1)
        statement = (
            select(
                self._sources.c.id,
                field("title").label("title"),
                payload["year"].as_integer().label("year"),
                self._sources.c.jurisdiction_id,
                field("document_type").label("type"),
                field("language").label("language"),
                field("legal_status").label("status"),
                (literal(0) if rank is None else rank).label("rank"),
                self.potential_text().label("may_have_text"),
            )
            .select_from(self._sources.join(self._rights, self._sources.c.id == self._rights.c.source_id))
            .where(self._visible())
            .order_by(self._sources.c.id)
            .limit(BROWSE_BOUND + 1)
        )
        if found is not None:
            statement = statement.where(found)
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

    def search_page(self, query, by_text, *, jurisdiction, limit, offset, access_where=None):
        """A page of the public sources whose title, citation and summary have every word of
        ``query``, or that are among ``by_text`` (their published text matched)."""
        every, _, _ = self.matching(query)
        predicate = or_(every, self._sources.c.id.in_(sorted(by_text))) if by_text else every
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
        if self._indexed_metadata is None:
            self._indexed_metadata = self.connection.dialect.name != "postgresql" or bool(
                self.connection.scalar(
                    text("SELECT to_regprocedure('legacy_metadata_normalize(text)') IS NOT NULL")
                )
            )
        citations, sources = tables["citations"], self._sources

        def may_be(value):
            if not self._indexed_metadata:
                # Preserve read compatibility while an older schema is upgrading.
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

    def count_public(
        self, *, jurisdictions=None, types=(), statuses=(), language=None, years=(None, None), works=()
    ):
        """How many public sources match a text search's record filters; ``works`` are records of other
        jurisdictions an applicable-law scope admits besides ``jurisdictions``."""
        statement = (
            select(func.count())
            .select_from(self._sources.join(self._rights, self._sources.c.id == self._rights.c.source_id))
            .where(self._visible())
        )
        if jurisdictions is not None:
            within = self._sources.c.jurisdiction_id.in_(sorted(jurisdictions))
            statement = statement.where(
                or_(within, self._sources.c.id.in_(sorted(works))) if works else within
            )
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
