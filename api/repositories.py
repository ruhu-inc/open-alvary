"""Read-only pilot repositories. Connections/transactions belong to the caller.

Legacy JSON remains authoritative. SQL limits row windows and never selects unapproved
canonical bodies; the shared policy runs on gate fields before an individual body fetch.
"""

from collections.abc import Iterator

from sqlalchemy import (
    JSON,
    Boolean,
    LargeBinary,
    Text,
    and_,
    case,
    cast,
    column,
    func,
    literal,
    literal_column,
    or_,
    select,
    text,
    true,
    union_all,
)
from sqlalchemy import table as sql_table
from sqlalchemy.engine import Connection

from api.search import FIELDS, fields_vector
from api.store import RELATIONS, tables
from rights.database import PolicyNamespace
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
# How a judgment's paragraph treats the judgment it cites (corpus.judicial_treatment, EDT-01), and the
# same treatment as the treated judgment sees it.
TREATED_BY = {
    "follows": "followed_by",
    "applies": "applied_by",
    "distinguishes": "distinguished_by",
    "doubts": "doubted_by",
    "overrules": "overruled_by",
}
# How later judgments treated this one; what changed this law, what it rests on or gives effect to, what
# it changed, what gives effect to it; how this judgment treats others; what it refers to and is referred
# to by, and last the instruments made under it, which may be many.
EDGE_ORDER = [
    "overruled_by",
    "doubted_by",
    "distinguished_by",
    "applied_by",
    "followed_by",
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
    "overrules",
    "doubts",
    "distinguishes",
    "applies",
    "follows",
    "cross_reference",
    "cites",
    "cited_by",
    "enables",
]
# The columns of the case-law tables (migration 0042) that the relationship pages read.
CITATION_COLUMNS = (
    "citation_id", "citing_work", "citing_locator", "citing_pinpoint", "citing_order", "cited_work",
    "cited_locator", "cited_pinpoint", "cited_order", "printed", "quote", "evidence_url", "review_state",
    "reviewed_by", "reviewer_role", "reviewed_at",
)  # fmt: skip
TREATMENT_COLUMNS = (
    "treatment_id", "citation_id", "treatment", "opinion", "quote", "review_state", "reviewed_by",
    "reviewer_role", "reviewer_qualification", "reviewed_at",
)  # fmt: skip
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
        self._case_law_tables = None

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

    def edges(self, source_id: str, kinds, *, limit: int, offset: int, locator: str | None = None):
        """One page of a source's checked links to public sources, each as seen from its side
        (EDGE_KINDS), and of a judgment's reviewed citations and treatments (_assertions), in EDGE_ORDER
        then by the other source; and how many there are. The database filters, counts and pages, so a
        law with hundreds of orders made under it costs one page. With ``locator``, only citations and
        treatments whose part on this side is that one or within it. A link is an Amendment; a citation
        or treatment is its row's fields (a dict)."""
        table, sources, rights = tables["amendments"], self._sources, self._rights
        stored = table.c.payload["kind"].as_string()
        parts = []
        for direction, mine, other in (
            ("out", table.c.source_id, table.c.target_source_id),
            ("in", table.c.target_source_id, table.c.source_id),
        ):
            wanted = [(k, seen) for (d, k), seen in EDGE_KINDS.items() if d == direction and seen in kinds]
            if not wanted or locator is not None:
                continue
            parts.append(
                select(
                    table.c.payload.label("payload"),
                    case(*[(stored == k, EDGE_ORDER.index(seen)) for k, seen in wanted]).label("rank"),
                    other.label("other"),
                    literal(0).label("first"),
                    literal(0).label("second"),
                    table.c.id.label("edge"),
                    literal(False).label("assertion"),
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
        parts += self._assertions(source_id, kinds, locator)
        if not parts:
            return 0, []
        edges = union_all(*parts).subquery()
        total = self.connection.scalar(select(func.count()).select_from(edges))
        rows = self.connection.execute(
            select(edges)
            .order_by(edges.c.rank, edges.c.other, edges.c.first, edges.c.second, edges.c.edge)
            .limit(limit)
            .offset(offset)
        ).all()
        return total, [
            (
                EDGE_ORDER[row.rank],
                row.other,
                row.payload if row.assertion else Amendment.model_validate(row.payload),
            )
            for row in rows
        ]

    def _case_law(self):
        """corpus.citation and corpus.judicial_treatment (migration 0042), or None: SQLite, or before it."""
        if self._case_law_tables is None:
            found = ()
            if self.connection.dialect.name == "postgresql":
                ns = PolicyNamespace(self.connection.scalar(text("SELECT current_schema()")))
                treatments = ns.qualified(self.connection, "corpus", "judicial_treatment")
                if self.connection.scalar(text("SELECT to_regclass(:t)"), {"t": treatments}) is not None:
                    found = (
                        sql_table("citation", *map(column, CITATION_COLUMNS), schema=ns.schema("corpus")),
                        sql_table(
                            "judicial_treatment", *map(column, TREATMENT_COLUMNS), schema=ns.schema("corpus")
                        ),
                    )
            self._case_law_tables = found
        return self._case_law_tables or None

    def _assertions(self, source_id: str, kinds, locator: str | None) -> list:
        """The union parts of a judgment's reviewed citations (`cites`, `cited_by`) and treatments (each
        kind and its TREATED_BY form) with public records, from this source's side: the citing paragraph
        and the part cited, the paragraph's words, and the review. Within one other record, in the order of
        the citing paragraphs."""
        found = self._case_law()
        if found is None:
            return []
        c, t = found
        sources, rights, parts = self._sources, self._rights, []

        def within(mine):
            return (
                true()
                if locator is None
                else or_(mine == locator, func.left(mine, len(locator) + 2) == locator + "__")
            )

        def citation_fields():
            return [
                "citing_work", c.c.citing_work, "citing_locator", c.c.citing_locator,
                "citing_pinpoint", c.c.citing_pinpoint, "cited_locator", c.c.cited_locator,
                "cited_pinpoint", c.c.cited_pinpoint, "printed", c.c.printed, "evidence_url", c.c.evidence_url,
            ]  # fmt: skip

        for direction, mine, other, here in (
            ("out", c.c.citing_work, c.c.cited_work, c.c.citing_locator),
            ("in", c.c.cited_work, c.c.citing_work, c.c.cited_locator),
        ):
            seen = "cites" if direction == "out" else "cited_by"
            joined = c.join(sources, sources.c.id == other).join(rights, rights.c.source_id == other)
            if seen in kinds:
                payload = func.json_build_object(
                    "assertion_id", c.c.citation_id, *citation_fields(), "quote", c.c.quote,
                    "level", "structural", "reviewed_by", c.c.reviewed_by, "reviewer_role", c.c.reviewer_role,
                    "reviewed_at", c.c.reviewed_at, type_=JSON,
                )  # fmt: skip
                parts.append(
                    select(
                        payload.label("payload"), literal(EDGE_ORDER.index(seen)).label("rank"),
                        other.label("other"), c.c.citing_order.label("first"),
                        func.coalesce(c.c.cited_order, -1).label("second"), c.c.citation_id.label("edge"),
                        literal(True).label("assertion"),
                    )
                    .select_from(joined)
                    .where(mine == source_id, c.c.review_state == "reviewed", within(here), self._visible())
                )  # fmt: skip
            wanted = [(k, k if direction == "out" else TREATED_BY[k]) for k in TREATED_BY]
            wanted = [(k, shown) for k, shown in wanted if shown in kinds]
            if not wanted:
                continue
            payload = func.json_build_object(
                "assertion_id", t.c.treatment_id, *citation_fields(), "quote", t.c.quote, "opinion", t.c.opinion,
                "level", "doctrinal", "reviewed_by", t.c.reviewed_by, "reviewer_role", t.c.reviewer_role,
                "reviewer_qualification", t.c.reviewer_qualification, "reviewed_at", t.c.reviewed_at, type_=JSON,
            )  # fmt: skip
            parts.append(
                select(
                    payload.label("payload"),
                    case(*[(t.c.treatment == k, EDGE_ORDER.index(shown)) for k, shown in wanted]).label("rank"),
                    other.label("other"), c.c.citing_order.label("first"),
                    func.coalesce(c.c.cited_order, -1).label("second"), t.c.treatment_id.label("edge"),
                    literal(True).label("assertion"),
                )
                .select_from(joined.join(t, t.c.citation_id == c.c.citation_id))
                .where(
                    mine == source_id, c.c.review_state == "reviewed", t.c.review_state == "reviewed",
                    t.c.treatment.in_([k for k, _ in wanted]), within(here), self._visible(),
                )
            )  # fmt: skip
        return parts

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
