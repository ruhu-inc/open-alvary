"""Immutable pilot-compatible wire DTOs; no target authority or private policy fields."""

from datetime import date
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, HttpUrl, field_serializer, model_validator

from schema.models import (
    ID,
    Hash,
    Jurisdiction,
    Nonempty,
    RightsRecord,
    RightsStatus,
    Source,
    SourceVersion,
    StructureNode,
)


class WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SourceView(Source):
    model_config = ConfigDict(extra="forbid", frozen=True)
    access_mode: Literal["open_full_text", "metadata_link_only"]
    rights_status: RightsStatus
    has_full_text: bool


class RightsViewRecord(RightsRecord):
    model_config = ConfigDict(extra="forbid", frozen=True)
    evidence: tuple[HttpUrl, ...] = ()
    conditions: tuple[Nonempty, ...] = ()
    approved_content_hashes: tuple[Hash, ...] = ()


class VersionViewRecord(SourceVersion):
    model_config = ConfigDict(extra="forbid", frozen=True)
    # The language edition the text belongs to (API-02), `{source_id}:{language}`; the keyed API
    # answers it, and it is absent elsewhere, so the pilot's answer is unchanged.
    expression_id: ID | None = Field(default=None, exclude_if=lambda value: value is None)


class NodeViewRecord(StructureNode):
    model_config = ConfigDict(extra="forbid", frozen=True)


class JurisdictionView(Jurisdiction):
    model_config = ConfigDict(extra="forbid", frozen=True)
    legal_systems: tuple[str, ...] = ()
    languages: tuple[str, ...] = ()


class DecisionView(WireModel):
    metadata: bool
    full_text: bool
    reasons: tuple[str, ...]


class SourcePage(WireModel):
    total: int = Field(ge=0)
    items: tuple[SourceView, ...]


class SearchPage(SourcePage):
    scope: Literal["Catalogue metadata and cleared full text only"] = (
        "Catalogue metadata and cleared full text only"
    )


class ExpressionView(WireModel):
    """One language edition of a record (API-02 expressions): its public texts and the one to read when
    no date is given. A preferred text is a reading default, not a claim that the edition is
    authoritative; the catalogue does not yet record which editions are."""

    expression_id: ID
    source_id: ID
    language: ID
    authority: Literal["unknown"] = "unknown"
    version_ids: tuple[ID, ...]
    preferred_version_id: ID | None = None
    preferred_reason: Literal["only_text", "in_force_today", "several_texts", "dates_unknown"]


class ExpressionPage(WireModel):
    source_id: ID
    items: tuple[ExpressionView, ...]


class VersionSelection(WireModel):
    """Which text a date and a language select (API-02, OA-T3): one, alternatives with the reason, or
    none. Never an arbitrary preference: a text with no effective date is never taken to be in force."""

    status: Literal["selected", "ambiguous", "none"]
    as_of: date | None = None
    language: str | None = None
    version_id: ID | None = None
    alternatives: tuple[ID, ...] = ()
    reason: (
        Literal[
            "several_languages",
            "several_texts",
            "effective_dates_unknown",
            "none_in_language",
            "none_in_force",
        ]
        | None
    ) = None


class VersionPage(WireModel):
    items: tuple[VersionViewRecord, ...]
    rights: RightsViewRecord
    structure: tuple[NodeViewRecord, ...]
    notice: str | None
    # Present only when a date or language was asked for, so the pilot's answer is unchanged.
    selection: VersionSelection | None = Field(default=None, exclude_if=lambda value: value is None)


class RightsView(WireModel):
    record: RightsViewRecord
    decision: DecisionView
    # The record's revision in the rights history, where one is kept; absent otherwise, so the pilot's
    # answer is unchanged.
    revision: int | None = Field(default=None, exclude_if=lambda value: value is None)


class RightsRevision(WireModel):
    revision: int = Field(ge=1)
    recorded_at: AwareDatetime
    publication_revision: int = Field(ge=0)
    record: RightsViewRecord


class RightsHistory(WireModel):
    """Every revision of a record's rights record, newest first, never edited (OA-R1)."""

    source_id: ID
    revisions: tuple[RightsRevision, ...]


class ProvisionMatch(WireModel):
    """One part of a public text that a pinpoint names (API-04 provision lookup): its anchor, locator and
    exact span. /anchors/{anchor_id} gives its words."""

    anchor_id: ID
    source_id: ID
    version_id: ID
    language: ID
    kind: str
    locator: str
    label: str | None = None
    heading: str | None = None
    pinpoint: str
    citation: str
    start_byte: int = Field(ge=0)
    end_byte: int = Field(ge=0)
    offset_unit: Literal["utf-8-bytes"] = "utf-8-bytes"
    span_hash: Hash
    content_hash: Hash
    effective_from: date | None = None
    effective_until: date | None = None


class ProvisionLookup(WireModel):
    """What a citation's pinpoint names in the works it resolves to: one part, alternatives (never a
    guess) or none, with the reason, and when none, the nearest enclosing part that exists."""

    pinpoint: str
    status: Literal["resolved", "ambiguous", "unresolved"]
    reason: (
        Literal[
            "several_works",
            "several_texts",
            "repeated_number",
            "not_found",
            "no_public_text",
            "no_text_in_scope",
        ]
        | None
    ) = None
    candidates: tuple[ProvisionMatch, ...] = ()
    nearest: ProvisionMatch | None = None


class CitationView(WireModel):
    status: Literal["resolved", "ambiguous", "unresolved"]
    matches: tuple[SourceView, ...]
    # Present only when the citation names a part ("NDPA s. 23(1)(a)"), so the pilot's answer is unchanged.
    provision: ProvisionLookup | None = Field(default=None, exclude_if=lambda value: value is None)


class ScopeCount(WireModel):
    jurisdiction: str
    count: int = Field(ge=0)


class CoverageView(WireModel):
    catalogued_sources: int = Field(ge=0)
    open_full_text_sources: int = Field(ge=0)
    metadata_only_sources: int = Field(ge=0)
    published_versions: int = Field(ge=0)
    jurisdictions: tuple[ScopeCount, ...]
    notice: str

    @model_validator(mode="before")
    @classmethod
    def scope_counts(cls, value):
        if isinstance(value, dict) and isinstance(value.get("jurisdictions"), dict):
            value = {
                **value,
                "jurisdictions": tuple(
                    ScopeCount(jurisdiction=k, count=v) for k, v in value["jurisdictions"].items()
                ),
            }
        return value

    @field_serializer("jurisdictions", return_type=dict[str, int])
    def scopes_as_object(self, scopes):
        return {item.jurisdiction: item.count for item in scopes}


class VersionGate(WireModel):
    """Policy inputs without canonical bytes, fetched before any body lookup."""

    id: ID
    source_id: ID
    content_hash: Hash
    verification_status: Literal["pending", "verified", "rejected"]
    content_verifier: str | None
    content_verified_at: AwareDatetime | None


LEGAL_STATUS = Literal["unknown", "current", "historical", "repealed", "superseded"]
# How another source relates to this one, from this one's side: it "repeals" an earlier law, it is
# "repealed_by" a later one, it is "made_under" its parent law, which "enables" it.
RelationKind = Literal[
    "repeals",
    "repealed_by",
    "amends",
    "amended_by",
    "supersedes",
    "superseded_by",
    "made_under",
    "enables",
    "cross_reference",
]


class RelatedSource(WireModel):
    kind: RelationKind
    source_id: ID
    title: Nonempty
    citation: Nonempty
    legal_status: LEGAL_STATUS
    year: int | None = None
    effective_date: date | None = None
    evidence_url: HttpUrl
    verified: bool


class CurrentSource(WireModel):
    source_id: ID
    title: Nonempty
    citation: Nonempty


class SourceRelationships(WireModel):
    source_id: ID
    legal_status: LEGAL_STATUS
    current: CurrentSource | None = None
    total: int = Field(ge=0)
    items: tuple[RelatedSource, ...]
    next_cursor: str | None = None


class StructurePage(WireModel):
    """A page of one text version's structure in document order (API-02 /versions/{id}/structure)."""

    version_id: ID
    content_hash: Hash
    total: int = Field(ge=0)
    items: tuple[NodeViewRecord, ...]
    next_cursor: str | None = None


class AnchorView(WireModel):
    """One part of a text exactly (API-02 /anchors/{id}, DAT-06): its span, locator, hashes, offset unit,
    citation and attribution, and the words of the span themselves."""

    anchor_id: ID
    source_id: ID
    version_id: ID
    kind: str
    locator: str
    label: str | None = None
    heading: str | None = None
    start_byte: int = Field(ge=0)
    end_byte: int = Field(ge=0)
    offset_unit: Literal["utf-8-bytes"] = "utf-8-bytes"
    span_hash: Hash
    content_hash: Hash
    serialization_profile: str | None = None
    page: int | None = None
    page_end: int | None = None
    citation: str
    official_url: str
    licence: str | None = None
    attribution_required: bool | None = None
    attribution_text: str | None = None
    text: str


class TextHit(WireModel):
    """One passage of a published text that matches a search (OA-S1, RET-03): the part it is in, a
    snippet of exact canonical bytes with its terms marked, and what quoting it asks for."""

    anchor_id: ID
    source_id: ID
    version_id: ID
    title: str
    citation: str
    pinpoint: str | None = None
    kind: str
    locator: str
    heading: str | None = None
    language: ID
    jurisdiction_id: ID
    document_type: str
    legal_status: LEGAL_STATUS
    year: int | None = None
    effective_from: date | None = None
    effective_until: date | None = None
    start_byte: int = Field(ge=0)
    end_byte: int = Field(ge=0)
    snippet: str
    snippet_start_byte: int = Field(ge=0)
    snippet_end_byte: int = Field(ge=0)
    highlights: tuple[tuple[int, int], ...] = ()
    offset_unit: Literal["utf-8-bytes"] = "utf-8-bytes"
    content_hash: Hash
    matched_by: Literal["citation", "text"]
    score: float | None = None
    official_url: str
    licence: str | None = None
    attribution_required: bool | None = None
    attribution_text: str | None = None


class SearchCoverage(WireModel):
    """What a text search covered: only published texts are searched, never a record's metadata alone."""

    records_in_scope: int = Field(ge=0)
    records_with_text: int = Field(ge=0)
    texts_searched: int = Field(ge=0)
    notice: str


class TextSearchPage(WireModel):
    """A page of a search over published texts' provisions (OA-S1). ``status`` tells no result apart from
    a scope with no published text and from a search that could not run."""

    status: Literal["results", "no_result", "unsupported_scope", "degraded"]
    query: str
    total: int = Field(ge=0)
    total_exact: bool
    items: tuple[TextHit, ...]
    next_cursor: str | None = None
    citation: CitationView | None = None
    coverage: SearchCoverage
    analysis: dict[str, str] = Field(default_factory=dict)
    analyzer: str
    ranking: str
    index_built_at: AwareDatetime | None = None
    notices: tuple[str, ...] = ()


CURRENTNESS = Literal["in_force", "not_in_force", "partly_in_force", "unknown", "contested"]
OMIT = {"default": None, "exclude_if": lambda value: value is None}


class Currentness(WireModel):
    """Whether a part was in force at a date (RSV-08, IDT-03). ``basis`` says what the state rests on:
    reviewed events covering this provision (``provision``), only the record's status for its whole text
    (``record``), or nothing (``none``). A record-level state is never a claim about one provision."""

    state: CURRENTNESS
    basis: Literal["provision", "record", "none"]
    assessed_at: date
    algorithm: str
    profile: str | None = Field(**OMIT)
    evidence: tuple[ID, ...] = ()
    reasons: tuple[str, ...] = ()


class RankingSignal(WireModel):
    """One documented signal that moved a unit (RSV-05), with its value and effect."""

    name: Literal[
        "legal_status",
        "replaced",
        "route_match",
        "instrument_match",
        "instrument_authority",
        "court_authority",
    ]
    value: str
    effect: str


class RankingExplanation(WireModel):
    """Why a unit is where it is (RSV-05): the route, each arm's rank, the fused score, the band, the
    signals applied and any diversification. Mechanical, never a legal justification."""

    position: int = Field(ge=1)
    router: str
    routes: tuple[str, ...]
    profile: str
    arms: dict[str, int]
    fused_score: float
    rerank_score: float | None = Field(**OMIT)
    band: int = Field(ge=0)
    signals: tuple[RankingSignal, ...] = ()
    diversification: tuple[str, ...] = ()


class EvidenceDefinition(WireModel):
    """A definition in force for a term the unit uses (RSV-06, derived): its exact words and span."""

    term: str
    anchor_id: ID
    locator: str
    text: str
    start_byte: int = Field(ge=0)
    end_byte: int = Field(ge=0)
    span_hash: Hash


class EvidenceReference(WireModel):
    """A provision of the same text that the unit refers to by number (RSV-06, derived)."""

    pinpoint: str
    anchor_id: ID
    locator: str


class EvidenceQualification(WireModel):
    """A reviewed link that qualifies the unit's work: it was amended, repealed or superseded."""

    kind: Literal["amended_by", "repealed_by", "superseded_by"]
    source_id: ID
    title: str
    citation: str
    effective_date: date | None = Field(**OMIT)
    scope: Literal["work"] = "work"


class EvidenceContext(WireModel):
    """What travels with a unit and is never part of its quotable words."""

    heading_path: tuple[str, ...] = ()
    definitions: tuple[EvidenceDefinition, ...] = ()
    references: tuple[EvidenceReference, ...] = ()
    qualifications: tuple[EvidenceQualification, ...] = ()
    also_in: tuple[ID, ...] = ()


class EvidenceAuthority(WireModel):
    """The inputs to the authority signals (RSV-05): the instrument's place in its jurisdiction's
    hierarchy (0 is highest), and for decisions the court's relation to the queried jurisdiction."""

    document_type: str
    instrument_rank: int | None = Field(**OMIT)
    court_relation: Literal["binding", "persuasive", "not_applicable", "unknown"] = "not_applicable"


class EvidenceUnit(WireModel):
    """One unit of legal evidence (RSV-07): which part of which published text, its exact words (or an
    excerpt within the byte budget), the hashes to check them, what quoting asks for, whether it was in
    force, and why it ranks where it does."""

    anchor_id: ID
    source_id: ID
    version_id: ID
    expression_id: str
    unit: Literal["provision", "container_run", "document"]
    kind: str
    locator: str
    pinpoint: str | None = Field(**OMIT)
    heading: str | None = Field(**OMIT)
    citation: str
    title: str
    language: ID
    jurisdiction_id: ID
    document_type: str
    official_url: str
    text: str
    text_start_byte: int = Field(ge=0)
    text_end_byte: int = Field(ge=0)
    complete: bool
    start_byte: int = Field(ge=0)
    end_byte: int = Field(ge=0)
    offset_unit: Literal["utf-8-bytes"] = "utf-8-bytes"
    span_hash: Hash
    content_hash: Hash
    licence: str | None = Field(**OMIT)
    attribution_required: bool | None = Field(**OMIT)
    attribution_text: str | None = Field(**OMIT)
    currentness: Currentness
    authority: EvidenceAuthority
    ranking: RankingExplanation | None = Field(**OMIT)
    context: EvidenceContext | None = Field(**OMIT)
    warnings: tuple[str, ...] = ()


class EvidenceQuery(WireModel):
    text: str
    routes: tuple[str, ...]
    filters: dict[str, object]
    as_of: date | None = Field(**OMIT)


class EvidenceRevisions(WireModel):
    """Everything the pack depends on: the same request and revisions give the same pack."""

    dataset: str
    router: str
    ranking: str
    analyzer: str
    vector: str | None = Field(**OMIT)
    reranker: str | None = Field(**OMIT)


class EvidenceCoverage(WireModel):
    records_in_scope: int = Field(ge=0)
    records_with_text: int = Field(ge=0)
    texts_searched: int = Field(ge=0)
    dense_coverage: float | None = Field(**OMIT)
    degraded: tuple[str, ...] = ()
    unsupported: tuple[str, ...] = ()
    incomplete_evidence: bool = False
    omitted_units: int = Field(0, ge=0)
    notices: tuple[str, ...] = ()


class EvidenceBytes(WireModel):
    unique: int = Field(ge=0)
    transmitted: int = Field(ge=0)
    budget: int = Field(ge=0)


class NextStep(WireModel):
    kind: Literal["ambiguous_citation", "metadata_only", "narrow_query"]
    detail: str
    candidates: tuple[ID, ...] = ()


class CatalogueMatch(WireModel):
    """A record that matches but has no published text: identity only, never words."""

    source_id: ID
    title: str
    citation: str
    jurisdiction_id: ID
    document_type: str
    official_url: str


class EvidencePack(WireModel):
    """The retrieval service's answer (RSV-07): ordered evidence a generator can cite, with everything
    needed to check it and the revisions it depends on. Never a generated answer."""

    schema_version: Literal["oa-evidence-1"] = "oa-evidence-1"
    status: Literal["results", "no_result", "unsupported_scope", "degraded"]
    query: EvidenceQuery
    revisions: EvidenceRevisions
    coverage: EvidenceCoverage
    evidence: tuple[EvidenceUnit, ...]
    catalogue: tuple[CatalogueMatch, ...] = ()
    next_steps: tuple[NextStep, ...] = ()
    bytes: EvidenceBytes


class CheckItem(WireModel):
    """A cited part to check (RSV-08): its anchor, or its text version and locator, with the span hash
    the client holds and the date to assess it at."""

    anchor_id: ID | None = None
    version_id: ID | None = None
    locator: str | None = Field(None, min_length=1, max_length=1000)
    span_hash: Hash | None = None
    as_of: date | None = None

    @model_validator(mode="after")
    def one_reference(self):
        if (self.anchor_id is None) == (self.version_id is None and self.locator is None):
            raise ValueError("give anchor_id, or version_id with locator")
        if self.anchor_id is None and (self.version_id is None or self.locator is None):
            raise ValueError("give version_id with locator")
        return self


class CheckRequest(WireModel):
    items: tuple[CheckItem, ...] = Field(min_length=1, max_length=100)


class CheckResult(WireModel):
    """What became of a cited part: ``unchanged``; ``newer_version`` (a later text of the same expression,
    with the same locator's anchor there and whether its words are identical); ``span_mismatch`` (the
    hash sent is not this part's); or ``unavailable`` (not public now, or never: not told apart)."""

    index: int = Field(ge=0)
    result: Literal["unchanged", "newer_version", "span_mismatch", "unavailable"]
    anchor_id: ID | None = Field(**OMIT)
    version_id: ID | None = Field(**OMIT)
    locator: str | None = Field(**OMIT)
    newer_version_id: ID | None = Field(**OMIT)
    newer_anchor_id: ID | None = Field(**OMIT)
    same_words: bool | None = Field(**OMIT)
    currentness: Currentness | None = Field(**OMIT)


class CheckPage(WireModel):
    schema_version: Literal["oa-check-1"] = "oa-check-1"
    dataset: str
    results: tuple[CheckResult, ...]


class PublicContracts(WireModel):
    schema_version: Literal["pilot-public-1"] = "pilot-public-1"
    sources: tuple[SourcePage, ...] = ()
    searches: tuple[SearchPage, ...] = ()
    versions: tuple[VersionPage, ...] = ()
    rights: tuple[RightsView, ...] = ()
    citations: tuple[CitationView, ...] = ()
    jurisdictions: tuple[JurisdictionView, ...] = ()
    coverage: tuple[CoverageView, ...] = ()
    relationships: tuple[SourceRelationships, ...] = ()
    structures: tuple[StructurePage, ...] = ()
    anchors: tuple[AnchorView, ...] = ()
    rights_histories: tuple[RightsHistory, ...] = ()
    text_searches: tuple[TextSearchPage, ...] = ()
