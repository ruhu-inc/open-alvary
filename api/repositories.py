"""Read-only pilot repositories. Connections/transactions belong to the caller.

Legacy JSON remains authoritative. SQL limits row windows and never selects unapproved
canonical bodies; the shared policy runs on gate fields before an individual body fetch.
"""

from collections.abc import Iterator

from sqlalchemy import LargeBinary, Text, and_, cast, false, func, literal, or_, select, text, true
from sqlalchemy.engine import Connection

from api.search import literal_pattern
from api.store import RELATIONS, tables
from schema.models import Source
from schema.public import RightsViewRecord, VersionGate, VersionViewRecord

BATCH_SIZE = 100
MAX_UNPAGED_ROWS = 1000
MAX_BUFFER_BYTES = 10 * 1024 * 1024


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
        return (
            statement.where(self._sources.c.jurisdiction_id == jurisdiction)
            if jurisdiction is not None
            else statement
        )

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

    def rows(self, name: str, *, ids=None, source_id=None, version_ids=None):
        table = tables[name]
        statement = select(table.c.payload).where(self._identity_matches(table)).order_by(table.c.id)
        if ids is not None:
            statement = statement.where(table.c.id.in_(ids))
        if source_id is not None:
            statement = statement.where(table.c.source_id == source_id)
        if version_ids is not None:
            statement = statement.where(table.c.version_id.in_(version_ids))
        rows = list(self.connection.scalars(statement.limit(MAX_UNPAGED_ROWS + 1)))
        if len(rows) > MAX_UNPAGED_ROWS:
            raise ResultTooLarge("Pilot unpaged response exceeds row bound")
        return rows

    def aliases(self, source_id: str):
        return self.rows("citations", source_id=source_id)
