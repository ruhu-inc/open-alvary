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
# Where it reads them (LegacyCatalogue._walk), the website's catalogue reads at most this many public sources'
# compact rows in one request.
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
# What rights.policy.decide lets anyone read, from the gates migration 0045 keeps, reading no payload
# (tests/test_postgres_coverage.py holds it to decide's answers). The tests on the time of asking and the
# approved hash stay outside the joins (OFFSET 0): the planner cannot tell they nearly always pass, and would
# look up each record's source one at a time if it took them for rare. Telling IDs apart needs no locale, and
# sorting them by one took two thirds of the time.
READ_NOW = """
        gate_verified_at::timestamptz <= :now
        AND gate_reviewed_at::timestamptz <= :now
        AND (gate_expires_at IS NULL OR gate_expires_at::timestamptz > :now)
        AND gate_approved::jsonb ? gate_content_hash
"""
PUBLISHED_COUNTS = text(
    f"""
    SELECT jurisdiction_id, count(DISTINCT source_id COLLATE "C") AS sources, count(*) AS versions FROM (
        SELECT s.jurisdiction_id, v.source_id, v.gate_content_hash, v.gate_verified_at,
               r.gate_reviewed_at, r.gate_expires_at, r.gate_approved
        FROM versions v
        JOIN rights r ON r.source_id = v.source_id
        JOIN sources s ON s.id = v.source_id
        WHERE v.gate_text AND r.gate_text AND s.gate_identity
        OFFSET 0
    ) candidates
    WHERE {READ_NOW}
    GROUP BY jurisdiction_id
    """
)
# The same, for a query that already holds each record to its gates: the records with a text to read.
READABLE = f"""
    SELECT DISTINCT source_id COLLATE "C" AS source_id FROM (
        SELECT v.source_id, v.gate_content_hash, v.gate_verified_at,
               r.gate_reviewed_at, r.gate_expires_at, r.gate_approved
        FROM versions v
        JOIN rights r ON r.source_id = v.source_id
        WHERE v.gate_text AND r.gate_text
        OFFSET 0
    ) candidates
    WHERE {READ_NOW}
"""
# The website's catalogue from the columns migration 0046 keeps. The search's words are read as in matching.
WANTED = "plainto_tsquery('simple', regexp_replace(normalize(CAST(:query AS text), NFKC), '[[:punct:]]+', ' ', 'g'))"
RANK = f"CASE WHEN s.words_title @@ {WANTED} THEN 3 WHEN s.words_citation @@ {WANTED} THEN 2 ELSE 1 END"
# Exactly str.casefold in the order of code points, with the index's prefix first so the index can serve it;
# the sort is stable over IDs, in the database's order, which the walk reads them in.
BY_TITLE = 'left(s.browse_title, 200) COLLATE "C"{0}, s.browse_title COLLATE "C"{0}'
ORDERS = {
    "title": f"{BY_TITLE.format('')}, s.id",
    "relevance": f"{BY_TITLE.format('')}, s.id",
    "oldest": f"s.browse_year IS NULL, s.browse_year, {BY_TITLE.format('')}, s.id",
    "newest": (
        "s.browse_year IS NULL, s.browse_year DESC,"
        ' CASE WHEN s.browse_year IS NULL THEN s.browse_title END COLLATE "C",'
        f" {BY_TITLE.format(' DESC')}, s.id"
    ),
}
# The records a catalogue request looks among and the ones among them with a text to read. Where a search or a
# filter narrows them, those are held and only their texts looked at, so finding few costs little and the
# planner goes by how many it expects (the tests on the time of asking stay out of the joins, as in
# PUBLISHED_COUNTS, or it expects few and looks each up in turn); else every text is looked at once.
MATCHED = f"""
    WITH matched AS {{held}} (
        SELECT {{columns}} FROM sources s JOIN rights r ON r.source_id = s.id WHERE {{scope}}
    ), readable AS (
        SELECT DISTINCT source_id COLLATE "C" AS source_id FROM (
            SELECT v.source_id, v.gate_content_hash, v.gate_verified_at,
                   r.gate_reviewed_at, r.gate_expires_at, r.gate_approved
            FROM {{texts}}
            WHERE v.gate_text AND r.gate_text
            OFFSET 0
        ) candidates
        WHERE {READ_NOW}
    )
"""
EVERY_TEXT = {"held": "NOT MATERIALIZED", "texts": "versions v JOIN rights r ON r.source_id = v.source_id"}
TEXTS_MATCHED = {
    "held": "MATERIALIZED",
    "texts": "matched m JOIN versions v ON v.source_id = m.id JOIN rights r ON r.source_id = m.id",
}
ACCESS = "CASE WHEN readable.source_id IS NULL THEN 'metadata' ELSE 'open' END"
# Each of the catalogue's filters (api.catalogue.FACETS) but access, as a value of a record, as a filter names it.
BROWSE_VALUES = {
    "jurisdiction": "s.jurisdiction_id",
    "type": "s.browse_type",
    "language": "s.language",
    "status": "s.browse_status",
    "year": "s.browse_year::text",
}
BROWSE_FACETS = (*BROWSE_VALUES, "access")
# What GROUPING says of each filter's row: a bit for each value not grouped by, the first filter's the highest.
BROWSE_SETS = {
    (1 << len(BROWSE_FACETS)) - 1 - (1 << (len(BROWSE_FACETS) - 1 - n)): name
    for n, name in enumerate(BROWSE_FACETS)
}
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
        self._named_by_key = None
        self._case_law_tables = None
        self._has_gates = None
        self._has_browse = None

    def _gated(self) -> bool:
        """Whether the database keeps the catalogue's gates in columns (migration 0045); not on SQLite,
        nor before that migration."""
        if self._has_gates is None:
            self._has_gates = self.connection.dialect.name == "postgresql" and bool(
                self.connection.scalar(
                    text(
                        "SELECT 1 FROM pg_attribute WHERE attrelid = to_regclass('rights')"
                        " AND attname = 'gate_text' AND NOT attisdropped"
                    )
                )
            )
        return self._has_gates

    def browsable(self) -> bool:
        """Whether the database also keeps what the website's catalogue filters and sorts by in columns
        (migration 0046); else the catalogue reads the records."""
        if not self._gated():
            return False
        if self._has_browse is None:
            self._has_browse = bool(
                self.connection.scalar(
                    text(
                        "SELECT 1 FROM pg_attribute WHERE attrelid = to_regclass('sources')"
                        " AND attname = 'browse_title' AND NOT attisdropped"
                    )
                )
            )
        return self._has_browse

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
            .where(self._public_gate())
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

    def _public_gate(self):
        """_visible, from its columns where the database keeps them (_gated), so no payload is parsed."""
        return text("sources.gate_identity AND rights.gate_public") if self._gated() else self._visible()

    def public_source_ids(self, limit: int, offset: int = 0) -> list[str]:
        """The public records' IDs, in ID order: the sitemap's walk. The database applies the gate
        _select_sources does and sends back the IDs alone, so no record's body is read into memory."""
        statement = (
            select(self._sources.c.id)
            .select_from(self._sources.join(self._rights, self._sources.c.id == self._rights.c.source_id))
            .where(self._public_gate())
            .order_by(self._sources.c.id)
            .limit(limit)
            .offset(offset)
        )
        return list(self.connection.scalars(statement))

    def public_among(self, source_ids) -> set[str]:
        """Which of these records are public now, by the gate every catalogue read applies: IDs alone."""
        statement = (
            select(self._sources.c.id)
            .select_from(self._sources.join(self._rights, self._sources.c.id == self._rights.c.source_id))
            .where(self._public_gate(), self._sources.c.id.in_(source_ids))
        )
        return set(self.connection.scalars(statement))

    def public_rights(self, source_ids) -> dict[str, dict]:
        """The whole rights record, as stored, of each of these that is public now, by source."""
        statement = (
            select(self._rights.c.source_id, self._rights.c.payload)
            .select_from(self._sources.join(self._rights, self._sources.c.id == self._rights.c.source_id))
            .where(self._public_gate(), self._sources.c.id.in_(source_ids))
        )
        return {row.source_id: row.payload for row in self.connection.execute(statement)}

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

    def readable_predicate(self, access, now):
        """The records with a text that may be read at ``now`` (or, for ``metadata``, those without), from the
        gates: the same test as access_predicate over the IDs of a walk of the records. None where the database
        keeps no gates (_gated)."""
        if not self._gated():
            return None
        found = "" if access == "open" else "NOT "
        return text(
            f"{found}EXISTS (SELECT 1 FROM ({READABLE}) readable WHERE readable.source_id = sources.id)"
        ).bindparams(now=now)

    def readable_among(self, ids, now) -> set[str] | None:
        """Which of these records have a text that may be read at ``now``; None where the database keeps no
        gates (_gated). A page's worth costs one lookup, and no text is read."""
        if not self._gated():
            return None
        return set(
            self.connection.scalars(
                text(
                    f"""
                    SELECT DISTINCT v.source_id FROM versions v
                    JOIN rights r ON r.source_id = v.source_id
                    JOIN sources s ON s.id = v.source_id
                    WHERE v.source_id = ANY(CAST(:ids AS text[]))
                      AND v.gate_text AND r.gate_text AND s.gate_identity AND {READ_NOW}
                    """
                ),
                {"ids": list(ids), "now": now},
            )
        )

    def matching(self, query: str):
        """The catalogue search's word tests (api.search) as SQL: every word of ``query`` among a
        source's title, citation and summary together, in its title alone, in its citation alone. On
        PostgreSQL the first is read from an index: of the column migration 0046 keeps, or before it of
        the expression migration 0038 indexes."""
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
                vector = (
                    f"{self._sources.name}.words_all"
                    if fields == FIELDS and self.browsable()
                    else fields_vector(self._sources.name, fields)
                )
                return literal_column(vector).op("@@", return_type=Boolean)(wanted)
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

    def plan_for_each_answer(self) -> None:
        """Until the transaction ends, plan each statement for its values, as the catalogue's depend on them:
        how many records a word finds, and a list of IDs it can hash only if it is known. A statement run ten
        times on a connection otherwise gets a plan blind to them, which took five times as long."""
        self.connection.execute(text("SELECT set_config('plan_cache_mode', 'force_custom_plan', true)"))

    @staticmethod
    def _browse_scope(query, by_text) -> tuple[str, dict]:
        """The public records a catalogue request looks among: those a search matches, or every one. The IDs a
        text search found go as one array, however many there are."""
        scope = "s.gate_identity AND r.gate_public"
        if query is None:
            return scope, {}
        if by_text:
            found = f"(s.words_all @@ {WANTED} OR s.id = ANY(CAST(:works AS text[])))"
            return f"{scope} AND {found}", {"query": query, "works": list(by_text)}
        return f"{scope} AND s.words_all @@ {WANTED}", {"query": query}

    def browse_counts(self, query, by_text, filters, now) -> tuple[int, dict[str, dict[str, int]]]:
        """How many public records a catalogue request finds, and for each filter how many have each of its
        values, applying every other selected filter, so choosing a value never hides the alternatives. One
        pass over the records' columns, whatever the filters."""
        scope, params = self._browse_scope(query, by_text)
        params |= {f"f_{name}": value for name, value in filters.items()} | {"now": now}
        columns = ", ".join(("s.id", *(f"{value} AS {name}" for name, value in BROWSE_VALUES.items())))
        names = ", ".join(BROWSE_FACETS)

        def counted(skip=None):
            tests = [f"{name} = :f_{name}" for name in filters if name != skip]
            return f"count(*) FILTER (WHERE {' AND '.join(tests)})" if tests else "count(*)"

        rows = self.connection.execute(
            text(
                f"""
                {MATCHED.format(columns=columns, scope=scope, **(EVERY_TEXT if query is None else TEXTS_MATCHED))}
                SELECT GROUPING({names}) AS grouped, {names}, {counted()} AS counted
                    {"".join(f", {counted(name)} AS {name}_counted" for name in filters)}
                FROM (
                    SELECT {", ".join(BROWSE_VALUES)}, {ACCESS} AS access
                    FROM matched LEFT JOIN readable ON readable.source_id = matched.id
                ) catalogue
                GROUP BY GROUPING SETS ({", ".join(f"({name})" for name in BROWSE_FACETS)}, ())
                """
            ),
            params,
        ).mappings()
        total, counts = 0, {name: {} for name in BROWSE_FACETS}
        for row in rows:
            name = BROWSE_SETS.get(row["grouped"])
            if name is None:
                total = row["counted"]
                continue
            count = row[f"{name}_counted" if name in filters else "counted"]
            if row[name] and count:
                counts[name][row[name]] = count
        return total, counts

    def browse_page(self, query, by_text, filters, sort, *, limit, offset, now) -> list[str]:
        """The IDs of one page of a catalogue request, in the order sorting the records in Python gives: best
        match first (RANK), by title (BY_TITLE), or by year with the undated last by title, and what ties
        by ID."""
        scope, params = self._browse_scope(query, by_text)
        tests = [f"{BROWSE_VALUES[name]} = :f_{name}" for name in filters if name in BROWSE_VALUES]
        scope = " AND ".join((scope, *tests))
        params |= {f"f_{name}": value for name, value in filters.items()} | {"limit": limit, "offset": offset}
        searching = sort == "relevance" and query is not None
        order = f"{RANK} DESC, {ORDERS[sort]}" if searching else ORDERS[sort]
        page = f"ORDER BY {order} LIMIT :limit OFFSET :offset"
        if "access" not in filters:
            statement = f"SELECT s.id FROM sources s JOIN rights r ON r.source_id = s.id WHERE {scope} {page}"
        else:
            columns = "s.id, s.browse_title, s.browse_year" + (
                ", s.words_title, s.words_citation" if searching else ""
            )
            params["now"] = now
            statement = f"""
                {MATCHED.format(columns=columns, scope=scope, **(TEXTS_MATCHED if query or tests else EVERY_TEXT))}
                SELECT s.id FROM matched s LEFT JOIN readable ON readable.source_id = s.id
                WHERE {ACCESS} = :f_access {page}
            """
        return list(self.connection.scalars(text(statement), params))

    def public_counts(self) -> dict[str, int]:
        """How many public sources each jurisdiction has, in order of each one's first source id."""
        statement = (
            select(self._sources.c.jurisdiction_id, func.count())
            .select_from(self._sources.join(self._rights, self._sources.c.id == self._rights.c.source_id))
            .where(self._public_gate())
            .group_by(self._sources.c.jurisdiction_id)
            .order_by(func.min(self._sources.c.id))
        )
        return dict(self.connection.execute(statement).all())

    def published_counts(self, now) -> dict[str, tuple[int, int]] | None:
        """For each jurisdiction that has any, how many of its public sources have a text that may be read
        at ``now``, and how many such texts there are; None where the database keeps no gates (_gated), so
        the caller reads the records instead."""
        if not self._gated():
            return None
        rows = self.connection.execute(PUBLISHED_COUNTS, {"now": now})
        return {row.jurisdiction_id: (row.sources, row.versions) for row in rows}

    def search_page(self, query, by_text, *, jurisdiction, limit, offset, access_where=None):
        """A page of the public sources whose title, citation and summary have every word of
        ``query``, or that are among ``by_text`` (their published text matched)."""
        every, _, _ = self.matching(query)
        predicate = every
        if by_text and self.browsable():
            self.plan_for_each_answer()
            found = text("sources.id = ANY(CAST(:works AS text[]))").bindparams(works=list(by_text))
            predicate = or_(every, found)
        elif by_text:
            predicate = or_(every, self._sources.c.id.in_(sorted(by_text)))
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

    def gates_of_versions(self, version_ids) -> dict[str, VersionGate]:
        """These versions' policy inputs, by ID, reading no text. Where the database keeps the gates
        (_gated) they come from its columns, since a field read from a payload parses all its text; gate_text
        has then asked for what is stated here, and a version it refuses is left out, as decide refuses it."""
        if self._gated():
            statement = text(
                "SELECT id, source_id, gate_content_hash AS content_hash,"
                " gate_verified_at AS content_verified_at FROM versions"
                " WHERE gate_text AND id = ANY(CAST(:ids AS text[]))"
            ).bindparams(ids=list(version_ids))
            asked = {"verification_status": "verified", "content_verifier": "recorded"}
        else:
            table = tables["versions"]
            fields = ("content_hash", "verification_status", "content_verifier", "content_verified_at")
            statement = select(
                table.c.id,
                table.c.source_id,
                *(table.c.payload[key].as_string().label(key) for key in fields),
            ).where(table.c.id.in_(version_ids), self._identity_matches(table))
            asked = {}
        rows = self.connection.execute(statement).mappings()
        return {row["id"]: VersionGate.model_validate({**row, **asked}) for row in rows}

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
        the caller confirms each value, so a value the database folds differently is never matched. On
        PostgreSQL the values are found by an index (migration 0048). Before it, each is compared, and
        any that is not ASCII is handed over whatever it says, as the comparison only folds ASCII."""
        if self._indexed_metadata is None:
            self._indexed_metadata = self.connection.dialect.name != "postgresql" or bool(
                self.connection.scalar(
                    text("SELECT to_regprocedure('legacy_metadata_normalize(text)') IS NOT NULL")
                )
            )
        if self._named_by_key is None:
            self._named_by_key = self.connection.dialect.name == "postgresql" and bool(
                self.connection.scalar(text("SELECT to_regprocedure('catalogue_name_key(text)') IS NOT NULL"))
            )
        citations, sources = tables["citations"], self._sources

        def may_be(table, name):
            if self._named_by_key:
                # Spelled as the index spells it: a bound name would be another expression to the planner.
                return func.catalogue_name_key(literal_column(f"payload ->> '{name}'"), type_=Text) == term
            if not self._indexed_metadata:
                # Preserve read compatibility while an older schema is upgrading.
                return true()
            value = self._field(table, name)
            normal = func.legacy_metadata_normalize(func.coalesce(value, "")) == term
            if self.connection.dialect.name != "postgresql":
                return normal
            return or_(normal, func.octet_length(value) != func.length(value))

        named = (
            select(citations.c.source_id, citations.c.payload)
            .where(self._identity_matches(citations), may_be(citations, "value"))
            .limit(MAX_UNPAGED_ROWS + 1)
        )
        if self._named_by_key:
            # The table's order, as a scan reads it: of two aliases of a source naming different parts, the
            # caller takes the first, and an index walks a hash's entries in an order of its own.
            named = named.order_by(literal_column("ctid"))
        aliases = self.connection.execute(named).all()
        own = self.connection.execute(
            select(sources.c.id)
            .where(
                self._identity_matches(sources),
                or_(may_be(sources, "citation"), may_be(sources, "title")),
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
            .where(self._public_gate())
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
