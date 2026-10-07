"""Catalogue domain services; injected repositories own no commits or HTTP behaviour."""

from collections import Counter
from datetime import UTC, datetime
from typing import Protocol

from api.repositories import (
    MAX_BUFFER_BYTES,
    MAX_UNPAGED_ROWS,
    LegacyRepository,
    PublicUnavailable,
    ResultTooLarge,
)
from api.search import normalize_citation as normalize_citation
from rights.policy import decide
from schema.models import Citation, Corpus
from schema.policy import MetadataPage, PublicMetadata
from schema.public import (
    CitationView,
    CoverageView,
    DecisionView,
    JurisdictionView,
    NodeViewRecord,
    RightsView,
    SearchPage,
    SourcePage,
    SourceView,
    VersionPage,
)


class SourceUnavailable(LookupError):
    pass


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
            for row in self.repository.rows("structure", version_ids=[v.id for v in versions])
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

    def rights(self, source_id):
        source, rights = self._require(source_id)
        version = next(self._approved(source, rights), None)
        decision = decide(rights, version, now=self.now)
        return RightsView(
            record=rights, decision=DecisionView.model_validate(decision.model_dump(mode="json"))
        )

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

    def coverage(self):
        total, open_sources, published = 0, 0, 0
        scopes = Counter()
        for source, rights in self.repository.sources():
            total += 1
            scopes[source.jurisdiction_id] += 1
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
