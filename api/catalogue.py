"""Catalogue domain services; injected repositories own no commits or HTTP behaviour."""

import base64
import hashlib
from collections import Counter
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import text

from api.reach import country
from api.repositories import (
    EDGE_ORDER,
    MAX_BUFFER_BYTES,
    MAX_STRUCTURE_ROWS,
    MAX_UNPAGED_ROWS,
    LegacyRepository,
    PublicUnavailable,
    ResultTooLarge,
)
from api.search import normalize_citation as normalize_citation
from changes.record import namespace
from rights.policy import decide
from schema.models import Citation, Corpus
from schema.policy import MetadataPage, PublicMetadata
from schema.public import (
    AnchorView,
    CitationView,
    CoverageView,
    CurrentSource,
    DecisionView,
    JurisdictionView,
    NodeViewRecord,
    RelatedSource,
    RightsHistory,
    RightsRevision,
    RightsView,
    RightsViewRecord,
    SearchPage,
    SourcePage,
    SourceRelationships,
    SourceView,
    StructurePage,
    VersionPage,
    VersionViewRecord,
)


class SourceUnavailable(LookupError):
    pass


def _cursor(offset: int, kind: str = "edges") -> str:
    return base64.urlsafe_b64encode(f"{kind}:{offset}".encode()).decode().rstrip("=")


def _offset(cursor: str | None, expected: str = "edges") -> int:
    """Where a page starts: an opaque cursor from the page before (links or nodes)."""
    if cursor is None:
        return 0
    try:
        kind, _, offset = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode().partition(":")
        if kind == expected and offset.isdigit() and int(offset) <= 100_000:
            return int(offset)
    except (ValueError, UnicodeDecodeError):
        pass
    raise InvalidSearch("Invalid cursor")


# Graph bounds (API-04, BUD-01): links one step away, a page of 25 by default and 100 at most, and
# at most 300 links examined in a request (the walk to the source in force now, three steps at most).
EDGES_PER_PAGE, MAX_EDGES_PER_PAGE, EDGE_BUDGET, MAX_SUCCESSION = 25, 100, 300, 3
# An anchor answers with its whole span, never silently shortened (BUD-01), inside the 1 MiB JSON cap.
MAX_ANCHOR_BYTES = 900 * 1024


# The website's filters (MAC-01): each is a source field, except access, which follows the rights
# decision. Values are counted over at most the pilot's bound of examined sources.
FACETS = ("jurisdiction", "type", "language", "status", "year", "access")
SORTS = ("relevance", "newest", "oldest", "title")


class InvalidSearch(ValueError):
    pass


class MetadataReadPort(Protocol):
    def metadata(self, work_id: str) -> PublicMetadata | None: ...
    def metadata_page(self, *, limit: int = 50, after: str | None = None) -> MetadataPage: ...


class TargetCatalogue:
    """Target service kept separate from pilot HTTP and authority cutover."""

    def __init__(self, repository: MetadataReadPort):
        self.repository = repository

    def metadata(self, work_id: str) -> PublicMetadata | None:
        return self.repository.metadata(work_id)

    def page(self, *, limit: int = 50, after: str | None = None) -> MetadataPage:
        return self.repository.metadata_page(limit=limit, after=after)


class LegacyCatalogue:
    """Compatibility domain adapter over legacy authority, never over policy shadows.

    These pilot routes keep their wire fields. Unpaged results have explicit development
    bounds until versioned pagination/delivery is accepted; no partial text is returned.
    """

    def __init__(self, repository: LegacyRepository):
        self.repository = repository
        self.now = datetime.now(UTC)

    def _require(self, source_id):
        pair = self.repository.source(source_id)
        if pair is None or not decide(pair[1], now=self.now).metadata:
            raise SourceUnavailable("Source not available")
        return pair

    def _approved(self, source, rights):
        for gate in self.repository.version_gates(source.id, rights.approved_content_hashes):
            if decide(rights, gate, now=self.now).full_text:
                version = self.repository.version(gate.id, source.id)
                if not decide(rights, version, now=self.now).full_text:
                    raise PublicUnavailable("Version policy changed during lookup")
                yield version

    def _view(self, pair):
        source, rights = pair
        has_text = next(self._approved(source, rights), None) is not None
        return SourceView(
            **source.model_dump(mode="json"),
            has_full_text=has_text,
            access_mode="open_full_text" if has_text else "metadata_link_only",
            rights_status=rights.status,
        )

    def source(self, source_id):
        return self._view(self._require(source_id))

    def _access(self, access, jurisdiction):
        if access not in {"all", "open", "metadata"}:
            raise InvalidSearch("Invalid access filter")
        if access == "all":
            return None
        open_ids = []
        for source, rights in self.repository.sources(jurisdiction, where=self.repository.potential_text()):
            if any(
                decide(rights, gate, now=self.now).full_text
                for gate in self.repository.version_gates(source.id, rights.approved_content_hashes)
            ):
                open_ids.append(source.id)
                if len(open_ids) > 10000:
                    raise ResultTooLarge("Pilot access filtering exceeds declared bound")
        return self.repository.access_predicate(open_ids, access)

    def sources(self, *, jurisdiction=None, limit=50, offset=0, access="all"):
        total, pairs = self.repository.source_page(
            limit=limit, offset=offset, jurisdiction=jurisdiction, where=self._access(access, jurisdiction)
        )
        return SourcePage(total=total, items=tuple(self._view(pair) for pair in pairs))

    def _public(self, ids) -> dict:
        return {
            source_id: pair
            for source_id, pair in self.repository.visible(ids).items()
            if decide(pair[1], now=self.now).metadata
        }

    def _current(self, source):
        """The source in force at the end of this one's line of repeals and replacements, if any
        within three steps and the request's budget of examined links."""
        seen, at, found, examined = {source.id}, source.id, None, 0
        for _ in range(MAX_SUCCESSION):
            replacing = self.repository.replaced(at)
            examined += len(replacing)
            later = self._public({a.source_id for a in replacing})
            if not later or examined > EDGE_BUDGET:
                break
            successor = max((pair[0] for pair in later.values()), key=lambda s: (s.year or 0, s.id))
            if successor.id in seen:
                break
            seen.add(successor.id)
            at, found = successor.id, successor
        return (
            CurrentSource(source_id=found.id, title=found.title, citation=found.citation) if found else None
        )

    def relationships(self, source_id, *, kinds=None, limit=EDGES_PER_PAGE, cursor=None, current=True):
        """A page of a source's checked links to other public sources, as seen from its side: what it
        repeals or amends and is made under, what repealed or amended it, the instruments made under
        it; and, when it has been repealed or replaced, the source in force in its place now."""
        source, _ = self._require(source_id)
        kinds = set(EDGE_ORDER) if kinds is None else set(kinds)
        if not kinds <= set(EDGE_ORDER) or not 1 <= limit <= MAX_EDGES_PER_PAGE:
            raise InvalidSearch("Invalid relationship filter or page size")
        offset = _offset(cursor)
        total, edges = self.repository.edges(source_id, kinds, limit=limit, offset=offset)
        public = self._public({other for _, other, _ in edges})
        items = tuple(
            RelatedSource(
                kind=seen,
                source_id=other,
                title=public[other][0].title,
                citation=public[other][0].citation,
                legal_status=public[other][0].legal_status,
                year=public[other][0].year,
                effective_date=link.effective_date,
                evidence_url=link.evidence_url,
                verified=link.verified,
            )
            for seen, other, link in edges
            if other in public
        )
        return SourceRelationships(
            source_id=source.id,
            legal_status=source.legal_status,
            current=self._current(source) if current else None,
            total=total,
            items=items,
            next_cursor=_cursor(offset + limit) if offset + limit < total else None,
        )

    def versions(self, source_id):
        source, rights = self._require(source_id)
        versions = []
        size = 0
        for version in self._approved(source, rights):
            size += len(version.canonical_text.encode())
            if len(versions) == MAX_UNPAGED_ROWS or size > MAX_BUFFER_BYTES:
                raise ResultTooLarge("Pilot unpaged text result exceeds bound")
            versions.append(version)
        nodes = tuple(
            NodeViewRecord.model_validate(row)
            for row in self.repository.rows(
                "structure", version_ids=[v.id for v in versions], bound=MAX_STRUCTURE_ROWS
            )
        )
        # Reuse the canonical multibyte/hash/hierarchy validator on one approved source.
        # Reference context is scoped and bounded; unrelated private corpus is never read.
        if versions:
            jurisdictions = self.repository.rows("jurisdictions", ids=[source.jurisdiction_id])
            visited = {source.jurisdiction_id}
            current = jurisdictions
            while current:
                parents = {j.get("parent_id") for j in current if j.get("parent_id")}
                if parents & visited:
                    raise PublicUnavailable("Invalid jurisdiction hierarchy")
                if len(visited | parents) > MAX_UNPAGED_ROWS:
                    raise ResultTooLarge("Reference context exceeds bound")
                current = self.repository.rows("jurisdictions", ids=parents)
                jurisdictions.extend(current)
                visited.update(parents)
            languages = {
                source.language,
                *(v.language for v in versions),
                *(language for j in jurisdictions for language in j.get("languages", [])),
            }
            Corpus.model_validate(
                dict(
                    sources=[source.model_dump(mode="json")],
                    rights=[rights.model_dump(mode="json")],
                    versions=[v.model_dump(mode="json") for v in versions],
                    structure=[n.model_dump(mode="json") for n in nodes],
                    jurisdictions=jurisdictions,
                    authorities=self.repository.rows("authorities", ids=[source.authority_id]),
                    languages=self.repository.rows("languages", ids=languages),
                )
            )
        return VersionPage(
            items=tuple(versions),
            rights=rights,
            structure=nodes,
            notice=None if versions else "No version has passed both rights and content review.",
        )

    # --- one version, its structure and its anchors (API-02), each rechecked for eligibility ---------

    def version(self, version_id: str) -> tuple[VersionViewRecord, object, object]:
        """A version that is public now, with its source and rights record. A hidden version and one
        that does not exist are the same answer: unavailable."""
        source_id = self.repository.version_owner(version_id)
        if source_id is None:
            raise SourceUnavailable("Version not available")
        source, rights = self._require(source_id)
        for version in self._approved(source, rights):
            if version.id == version_id:
                return version, source, rights
        raise SourceUnavailable("Version not available")

    def _ordered_nodes(self, version: VersionViewRecord) -> list[NodeViewRecord]:
        """A version's nodes in document order: by where they start, the enclosing node first."""
        nodes = [
            NodeViewRecord.model_validate(row)
            for row in self.repository.rows("structure", version_ids=[version.id], bound=MAX_STRUCTURE_ROWS)
        ]
        parents = {n.id: n.parent_id for n in nodes}

        def depth(node):
            steps, at = 0, node.parent_id
            while at is not None and steps < 64:
                steps, at = steps + 1, parents.get(at)
            return steps

        return sorted(nodes, key=lambda n: (n.start_byte, -n.end_byte, depth(n), n.order))

    def structure(self, version_id: str, *, limit: int = 100, cursor: str | None = None) -> StructurePage:
        version, _, _ = self.version(version_id)
        nodes = self._ordered_nodes(version)
        offset = _offset(cursor, "nodes")
        return StructurePage(
            version_id=version.id,
            content_hash=version.content_hash,
            total=len(nodes),
            items=tuple(nodes[offset : offset + limit]),
            next_cursor=_cursor(offset + limit, "nodes") if offset + limit < len(nodes) else None,
        )

    def _pinpoint(self, node: NodeViewRecord) -> str:
        """How a part is cited within its text, from its printed numbers: "s. 23(1)(a)", "art. 12",
        "First Schedule, para. 3", a definition with its section; else its heading or label."""
        chain, at = [node], node
        while at.parent_id and len(chain) < 16:
            row = self.repository.node(at.parent_id)
            if row is None:
                break
            at = NodeViewRecord.model_validate(row)
            chain.append(at)
        chain.reverse()  # outermost first

        def number(label):
            return (label or "").strip().rstrip(".")

        lead = {"section": "s. ", "article": "art. "}
        top = next((i for i, n in enumerate(chain) if n.kind in lead and n.label), None)
        if top is not None:
            cited = lead[chain[top].kind] + number(chain[top].label)
            for part in chain[top + 1 :]:
                if part.kind == "definition":
                    cited += f", “{part.label}”"
                elif part.label:
                    cited += number(part.label) if part.label.startswith("(") else f"({number(part.label)})"
            return cited
        schedule = next((n for n in chain if n.kind == "schedule"), None)
        if schedule is not None:
            numbered = [n for n in chain if n.kind in ("paragraph", "subparagraph") and n.label]
            if not numbered:
                return schedule.label or schedule.heading or schedule.locator
            head, *rest = numbered
            return f"{schedule.label or schedule.locator}, para. {number(head.label)}" + "".join(
                number(n.label) if n.label.startswith("(") else f"({number(n.label)})" for n in rest
            )
        if node.kind == "document":
            return ""
        return node.heading or node.label or node.locator

    def anchor(self, anchor_id: str) -> AnchorView:
        """One part of a public text exactly. Each node of a published version is an anchor (DAT-06)."""
        row = self.repository.node(anchor_id)
        if row is None:
            raise SourceUnavailable("Anchor not available")
        node = NodeViewRecord.model_validate(row)
        version, source, rights = self.version(node.version_id)
        if node.source_id != version.source_id or node.source_hash != version.content_hash:
            raise PublicUnavailable("Invalid anchor provenance")
        span = version.canonical_text.encode()[node.start_byte : node.end_byte]
        if len(span) > MAX_ANCHOR_BYTES:
            raise ResultTooLarge("This part is larger than one response may be; read it from the text")
        span_hash = hashlib.sha256(span).hexdigest()
        if node.span_hash and node.span_hash != span_hash:
            raise PublicUnavailable("Anchor span hash mismatch")
        pinpoint = self._pinpoint(node)
        return AnchorView(
            anchor_id=node.id,
            source_id=node.source_id,
            version_id=node.version_id,
            kind=node.kind,
            locator=node.locator,
            label=node.label,
            heading=node.heading,
            start_byte=node.start_byte,
            end_byte=node.end_byte,
            span_hash=span_hash,
            content_hash=version.content_hash,
            serialization_profile=version.serialization_profile,
            page=node.page,
            page_end=node.page_end,
            citation=f"{source.citation}, {pinpoint}" if pinpoint else source.citation,
            official_url=str(version.canonical_url),
            licence=rights.licence,
            attribution_required=rights.attribution_required,
            attribution_text=rights.attribution_text,
            text=span.decode("utf-8"),
        )

    def rights(self, source_id):
        source, rights = self._require(source_id)
        version = next(self._approved(source, rights), None)
        decision = decide(rights, version, now=self.now)
        latest = self._rights_revisions(source_id, limit=1)
        return RightsView(
            record=rights,
            decision=DecisionView.model_validate(decision.model_dump(mode="json")),
            revision=latest[0]["revision"] if latest else None,
        )

    def rights_history(self, source_id) -> RightsHistory:
        """Every revision of a public record's rights record, newest first (OA-R1). A hidden record
        and one that does not exist are the same answer: unavailable."""
        self._require(source_id)
        rows = self._rights_revisions(source_id, limit=MAX_UNPAGED_ROWS)
        return RightsHistory(
            source_id=source_id,
            revisions=tuple(
                RightsRevision(
                    revision=row["revision"],
                    recorded_at=row["committed_at"],
                    publication_revision=row["publication_revision"],
                    record=RightsViewRecord.model_validate(row["record"]),
                )
                for row in rows
            ),
        )

    def _rights_revisions(self, source_id: str, *, limit: int) -> list[dict]:
        """The recorded revisions, newest first; none where no history is kept (SQLite, or a database
        before migration 0029)."""
        conn = self.repository.connection
        if conn.dialect.name != "postgresql":
            return []
        table = namespace(conn).qualified(conn, "changes", "rights_revision")
        if conn.scalar(text("SELECT to_regclass(:t)"), {"t": table}) is None:
            return []
        return [
            dict(row)
            for row in conn.execute(
                text(
                    f"SELECT revision, committed_at, publication_revision, record FROM {table}"
                    " WHERE work_id = :w ORDER BY revision DESC LIMIT :n"
                ),
                {"w": source_id, "n": limit},
            ).mappings()
        ]

    def search(self, query, *, jurisdiction=None, limit=25, offset=0, access="all"):
        term = normalize_citation(query)
        if not term:
            raise InvalidSearch("Search term must not be blank")
        if not 1 <= limit <= 100 or offset < 0:
            raise InvalidSearch("Invalid page bounds")
        access_where = self._access(access, jurisdiction)
        _, fallback = self.repository.search_predicates(term)
        matched_ids = []
        examined = 0
        for source, rights in self.repository.sources(jurisdiction, where=fallback):
            examined += 1
            if examined > 10000:
                raise ResultTooLarge("Pilot Unicode/full-text fallback exceeds declared bound")
            matches = term in normalize_citation(source.title + " " + source.citation)
            if not matches:
                matches = any(
                    term in version.canonical_text.casefold() for version in self._approved(source, rights)
                )
            if matches:
                matched_ids.append(source.id)
        total, pairs = self.repository.search_page(
            term,
            matched_ids,
            jurisdiction=jurisdiction,
            limit=limit,
            offset=offset,
            access_where=access_where,
        )
        return SearchPage(total=total, items=tuple(self._view(pair) for pair in pairs))

    def _open(self, pairs) -> set[str]:
        """Which of these sources have a text cleared for reuse now."""
        return {
            source.id for source, rights in pairs if next(self._approved(source, rights), None) is not None
        }

    def browse(self, *, query=None, filters=None, sort="relevance", limit=20, offset=0):
        """The website's catalogue (MAC-01): an optional search, filters and sorting, with a count per
        filter value. Each count applies every other selected filter, so choosing a value never hides
        the alternatives. The database matches and ranks each public source (title 3, citation 2,
        summary 1, a cleared text 1) and returns a compact row for each; only the page's sources are
        read in full. Bounded: at most BROWSE_BOUND public sources."""
        filters = {k: v for k, v in (filters or {}).items() if v is not None}
        unknown = set(filters) - set(FACETS)
        if unknown or sort not in SORTS:
            raise InvalidSearch("Invalid filter or sort")
        term = None
        if query is not None:
            term = normalize_citation(query)
            if not term:
                raise InvalidSearch("Search term must not be blank")
        rows = self.repository.browse_rows(term)
        # Sources the database leaves to Python: a text to search or read for access, or a letter it
        # folds apart. Read in full (few), matched as before, and checked for a cleared text.
        apart = self.repository.visible([row.id for row in rows if row.python])
        opened = self._open(apart.values())
        ranks = {}
        if term is not None:
            for source_id, (source, rights) in apart.items():
                if term in normalize_citation(source.title):
                    ranks[source_id] = 3
                elif term in normalize_citation(source.citation):
                    ranks[source_id] = 2
                elif term in normalize_citation(source.summary or ""):
                    ranks[source_id] = 1
                elif any(term in v.canonical_text.casefold() for v in self._approved(source, rights)):
                    ranks[source_id] = 1
                else:
                    ranks[source_id] = 0
        candidates = []
        for row in rows:
            rank = ranks.get(row.id, row.rank) if term is not None else 0
            if term is not None and not rank:
                continue
            values = {
                "jurisdiction": row.jurisdiction_id,
                "type": row.type,
                "language": row.language,
                "status": row.status,
                "year": str(row.year) if row.year else None,
                "access": "open" if row.id in opened else "metadata",
            }
            candidates.append((rank, row, values))

        def selected(values, skip=None):
            return all(values[k] == v for k, v in filters.items() if k != skip)

        results = [c for c in candidates if selected(c[2])]
        facets = {
            name: Counter(c[2][name] for c in candidates if selected(c[2], skip=name) and c[2][name])
            for name in FACETS
        }
        by_title = lambda c: c[1].title.casefold()  # noqa: E731
        if sort == "relevance":
            results.sort(key=lambda c: (-c[0], by_title(c)))
        elif sort == "title":
            results.sort(key=by_title)
        else:
            dated = [c for c in results if c[1].year]
            results = sorted(
                dated, key=lambda c: (c[1].year, by_title(c)), reverse=sort == "newest"
            ) + sorted((c for c in results if not c[1].year), key=by_title)
        page = [c[1].id for c in results[offset : offset + limit]]
        pairs = self.repository.visible(page)
        return (
            # A source withdrawn since its row was read is left out of the page.
            SearchPage(total=len(results), items=tuple(self._view(pairs[i]) for i in page if i in pairs)),
            {name: dict(counts.most_common()) for name, counts in facets.items()},
        )

    def citations(self, value):
        term = normalize_citation(value)
        matches = []
        for source, rights in self.repository.sources():
            for row in self.repository.aliases(source.id):
                alias = Citation.model_validate(row)
                if normalize_citation(alias.value) != term:
                    continue
                if alias.node_id and not alias.version_id:
                    raise PublicUnavailable("Invalid citation provenance")
                if alias.version_id:
                    approved = {v.id for v in self._approved(source, rights)}
                    if alias.version_id not in approved:
                        continue
                    if alias.node_id:
                        nodes = {n.id: n for n in self.versions(source.id).structure}
                        if alias.node_id not in nodes:
                            continue
                        if nodes[alias.node_id].version_id != alias.version_id:
                            raise PublicUnavailable("Invalid citation provenance")
                if len(matches) == MAX_UNPAGED_ROWS:
                    raise ResultTooLarge("Citation alternatives exceed pilot bound")
                matches.append(self._view((source, rights)))
                break
        return CitationView(
            status="resolved" if len(matches) == 1 else "ambiguous" if matches else "unresolved",
            matches=tuple(matches),
        )

    def jurisdictions(self):
        return tuple(JurisdictionView.model_validate(row) for row in self.repository.rows("jurisdictions"))

    def countries(self):
        """Each country with catalogued material, for choosing a plan's countries: its sources and how
        many have open full text. Sub-national material counts under its country (api.reach)."""
        names = {j.id: j.name for j in self.jurisdictions()}
        found: dict[str, dict] = {}
        for source, rights in self.repository.sources():
            code = country(source.jurisdiction_id)
            if code is None:
                continue
            entry = found.setdefault(
                code, {"id": code, "name": names.get(code, code.upper()), "sources": 0, "full_text": 0}
            )
            entry["sources"] += 1
            entry["full_text"] += any(True for _ in self._approved(source, rights))
        return sorted(found.values(), key=lambda c: c["name"])

    def coverage(self):
        """Counts of public sources, by jurisdiction from the database, and of those with a cleared
        text and its versions, from the few sources that have any approved."""
        scopes = Counter(self.repository.public_counts())
        total = sum(scopes.values())
        open_sources, published = 0, 0
        for source, rights in self.repository.sources(where=self.repository.potential_text()):
            versions = sum(1 for _ in self._approved(source, rights))
            published += versions
            open_sources += bool(versions)
        return CoverageView(
            catalogued_sources=total,
            open_full_text_sources=open_sources,
            metadata_only_sources=total - open_sources,
            published_versions=published,
            jurisdictions=dict(scopes),
            notice="Catalogue coverage only; not a claim of complete or current law.",
        )
