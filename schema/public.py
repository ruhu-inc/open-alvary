"""Immutable pilot-compatible wire DTOs; no target authority or private policy fields."""

from datetime import date, datetime
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, HttpUrl, field_serializer, model_validator

from schema.dates import DateBounds, UncertainDate
from schema.memberships import BASES
from schema.models import (
    ID,
    Edition,
    Hash,
    Jurisdiction,
    LanguageRule,
    Nonempty,
    RightsRecord,
    RightsStatus,
    Source,
    SourceVersion,
    StructureNode,
)
from schema.record import EXPRESSION_AUTHORITY, LEGAL_EFFECTS

OMIT = {"default": None, "exclude_if": lambda value: value is None}


class WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# What a language edition is worth as law, as its record states it (API-02, DAT-07); ``unknown`` when
# the record states nothing. Never inferred from a preferred or selected text.
ExpressionAuthority = Literal[*EXPRESSION_AUTHORITY, "unknown"]


class AuthorityEvidence(WireModel):
    """Where an edition's authority or a language rule is stated, or a treaty's party list is shown
    complete: a page and the words of the document (``document``), the address of another instrument
    (``external``), or the reviewer who recorded it."""

    source: Literal["document", "reviewer", "registry", "manifest", "external"]
    page: int | None = Field(ge=1, **OMIT)
    quote: str | None = Field(**OMIT)
    url: str | None = Field(**OMIT)


class LanguageRuleView(WireModel):
    """The instrument's own rule for divergence between its language versions (LNG-03, DAT-07), as its
    record states it: ``stated``, with the rule, the language whose version prevails when it names one,
    where it is stated and the evidence; ``not_stated``, the reviewed copy states none;
    ``not_applicable``; or ``unknown``, nothing is recorded."""

    status: Literal["stated", "not_stated", "not_applicable", "unknown"]
    rule: str | None = Field(**OMIT)
    prevails: ID | None = Field(**OMIT)
    provision: str | None = Field(**OMIT)
    evidence: AuthorityEvidence | None = Field(**OMIT)


class StatedDateView(WireModel):
    """A reviewed date as its record gives it: the date, how precise it is, and as printed when the record
    keeps that. A date with no normalised value is never compared with another."""

    value: date | None = Field(**OMIT)
    precision: Literal["day", "month", "year"] = "day"
    printed: str | None = Field(**OMIT)


class RegionalView(WireModel):
    """A regional instrument's legal effect, as its reviewed record states it (JUR-04, DAT-03)."""

    organ: str
    legal_effect: Literal[*LEGAL_EFFECTS]
    member_states: tuple[ID, ...] = ()  # none listed: every member
    transposition_deadline: date | None = Field(**OMIT)


class LinkedAct(WireModel):
    """A public record a checked link names (EDT-01), such as a national act that domesticates a treaty or
    transposes a directive."""

    source_id: ID
    jurisdiction_id: ID
    title: str
    citation: str
    effective_date: date | None = Field(**OMIT)


class TreatyPartyView(WireModel):
    """One party to a treaty as its reviewed record lists it, with the acts of that state that a checked
    link records as domesticating the treaty. Signature alone never binds (COR-01)."""

    party: str
    place: ID | None = Field(**OMIT)
    signed: StatedDateView | None = Field(**OMIT)
    ratified: StatedDateView | None = Field(**OMIT)
    acceded: StatedDateView | None = Field(**OMIT)
    denounced: StatedDateView | None = Field(**OMIT)
    reservations: str | None = Field(**OMIT)
    domesticated_by: tuple[LinkedAct, ...] = ()


class TreatyView(WireModel):
    """A treaty's status, as its reviewed record states it (JUR-04, DAT-03). ``parties`` is on the record
    and its anchors, never on an evidence unit; an empty list means none is recorded, never none exists.
    Beside it, the date the list is complete as of and where that is shown, when recorded: without it, a
    state missing from the list is unknown, never not a party."""

    adopted_by: str | None = Field(**OMIT)
    depositary: str | None = Field(**OMIT)
    entered_into_force: StatedDateView | None = Field(**OMIT)
    authentic_languages: tuple[ID, ...] = ()
    parties: tuple[TreatyPartyView, ...] | None = Field(**OMIT)
    parties_complete_as_of: date | None = Field(**OMIT)
    parties_complete_evidence: AuthorityEvidence | None = Field(**OMIT)


class SourceView(Source):
    model_config = ConfigDict(extra="forbid", frozen=True)
    access_mode: Literal["open_full_text", "metadata_link_only"]
    rights_status: RightsStatus
    has_full_text: bool
    # Answered on the record's editions and texts instead (API-02), so the record's answer is unchanged.
    editions: list[Edition] = Field(default_factory=list, exclude=True)
    language_rule: LanguageRule | None = Field(default=None, exclude=True)
    # Absent from every record that is neither a regional instrument nor a treaty.
    regional: RegionalView | None = Field(**OMIT)
    treaty: TreatyView | None = Field(**OMIT)


class RightsViewRecord(RightsRecord):
    model_config = ConfigDict(extra="forbid", frozen=True)
    evidence: tuple[HttpUrl, ...] = ()
    conditions: tuple[Nonempty, ...] = ()
    approved_content_hashes: tuple[Hash, ...] = ()


class VersionViewRecord(SourceVersion):
    model_config = ConfigDict(extra="forbid", frozen=True)
    # The language edition the text belongs to (API-02), `{source_id}:{language}`, what the edition is
    # worth as law as its record states it and where; the keyed API answers them, and they are absent
    # elsewhere, so the pilot's answer is unchanged.
    expression_id: ID | None = Field(default=None, exclude_if=lambda value: value is None)
    expression_authority: ExpressionAuthority | None = Field(**OMIT)
    expression_authority_evidence: AuthorityEvidence | None = Field(**OMIT)


class NodeViewRecord(StructureNode):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _with_bounds(data, names=("valid_from", "valid_to")):
    """An answer's dates of schema.dates with their bounds, ``{name}_bounds``, always from the date."""
    if isinstance(data, dict):
        data = data | {
            f"{name}_bounds": data.get(name) and UncertainDate.parse(data[name]).bounds() for name in names
        }
    return data


class ProvisionView(WireModel):
    """An article of an instrument, as cited: the instrument, the article, where it was read and its words
    when recorded."""

    instrument: Nonempty
    pinpoint: Nonempty
    evidence_url: Nonempty
    quote: str | None = Field(**OMIT)


class BasisView(ProvisionView):
    """Why a membership starts when it does, and the article that makes that its start."""

    kind: Literal[*BASES]


class SuspensionView(WireModel):
    """A reviewed suspension of a member from participation in a body's activities: context for its
    membership, which it never ends. Its first and last day (inclusive; none: not lifted), each an EDTF
    date with its bounds, whether it was in effect on the date asked (``null``: the date falls inside an
    uncertain start or end), its legal basis, the decision that suspended it, the one that lifted it, and
    who reviewed it when."""

    id: ID
    kind: Literal["suspended_from_participation"] = "suspended_from_participation"
    valid_from: UncertainDate
    valid_from_bounds: DateBounds
    valid_to: UncertainDate | None = None
    valid_to_bounds: DateBounds | None = None
    in_effect: bool | None = None
    legal_basis: tuple[ProvisionView, ...]
    evidence_url: Nonempty
    lifted_evidence_url: str | None = None
    note: Nonempty
    reviewed_by: Nonempty
    reviewed_at: AwareDatetime

    @model_validator(mode="before")
    @classmethod
    def bounded(cls, data):
        return _with_bounds(data)


class MembershipView(WireModel):
    """One reviewed membership of the African Union or a regional body (JUR-04): the member, the body, the
    first and last day it held (inclusive; no last day means it has not ended), each an EDTF date (a day,
    a month, a year or a window) with its bounds, why it starts then (``basis``), the evidence, who
    reviewed it when, and its suspensions from participation, as context. Membership alone never applies
    the body's treaties to the member; a suspension never ends it."""

    id: ID
    member_id: ID
    member_name: Nonempty
    body_id: ID
    body_name: Nonempty
    valid_from: UncertainDate
    valid_from_bounds: DateBounds
    valid_to: UncertainDate | None = None
    valid_to_bounds: DateBounds | None = None
    basis: BasisView
    evidence_url: Nonempty
    note: Nonempty
    reviewed_by: Nonempty
    reviewed_at: AwareDatetime
    suspensions: tuple[SuspensionView, ...] = ()

    @model_validator(mode="before")
    @classmethod
    def bounded(cls, data):
        return _with_bounds(data)


class MembershipList(WireModel):
    """Memberships in one direction on one date: the bodies a jurisdiction belongs to, or a body's members.
    ``items`` held on the date; ``uncertain`` may or may not have held (the date falls inside an uncertain
    start or end); ``ended`` had ended by then. ``unknown`` when no membership in this direction is
    reviewed: never an empty list read as "none"."""

    status: Literal["reviewed", "unknown"]
    items: tuple[MembershipView, ...] = ()
    uncertain: tuple[MembershipView, ...] = ()
    ended: tuple[MembershipView, ...] = ()
    notice: str


class JurisdictionView(Jurisdiction):
    model_config = ConfigDict(extra="forbid", frozen=True)
    legal_systems: tuple[str, ...] = ()
    languages: tuple[str, ...] = ()
    # The bodies it belongs to, where the keyed API answers them; absent elsewhere, so the pilot's answer
    # is unchanged.
    member_of: MembershipList | None = Field(default=None, exclude_if=lambda value: value is None)


class JurisdictionDetail(JurisdictionView):
    """One jurisdiction on a date (API /jurisdictions/{id}): a country's bodies, or a body's members."""

    level: Literal["continental", "regional", "national", "subnational", "local"] | None = None
    as_of: date
    members: MembershipList | None = Field(default=None, exclude_if=lambda value: value is None)


class MembershipBasis(WireModel):
    """A reviewed membership an applicable-law answer rests on: its ID, the body, and whether the
    jurisdiction was a member of that body on the date asked about: ``held``, ``not_held``, or ``unknown``
    when the date falls in an uncertain window of the membership's dates. /jurisdictions/{id} gives its
    dates, evidence and review."""

    id: ID
    body_id: ID
    held: Literal["held", "not_held", "unknown"]


# A record's relation to the law of the jurisdiction asked about, on a date (api.applicable).
APPLICABILITY = Literal[
    "national",
    "regional_direct",
    "regional_requires_transposition",
    "regional_binding_on_addressees",
    "treaty_in_force_for_state",
    "treaty_not_in_force_for_state",
    "non_binding",
    "comparative",
    "unknown",
]


class Applicability(WireModel):
    """How a record relates to the law of a jurisdiction on a date (JUR-03, JUR-04), and what that rests on:
    the reviewed memberships, the instrument's legal effect and the state's entry in the treaty's party
    list. ``binds`` is ``bound``, ``not_bound`` or ``unknown``; whether the text is in force is
    ``currentness``, a separate question. Never a decision of anyone's governing law."""

    jurisdiction_id: ID
    as_of: date
    relation: APPLICABILITY
    binds: Literal["bound", "not_bound", "unknown"]
    memberships: tuple[MembershipBasis, ...] = ()
    regional: RegionalView | None = Field(**OMIT)
    party: TreatyPartyView | None = Field(**OMIT)
    entered_into_force: StatedDateView | None = Field(**OMIT)
    transposed_by: tuple[LinkedAct, ...] = ()
    reasons: tuple[str, ...] = ()


class ApplicableScope(WireModel):
    """The applicable-law scope a request asked for (``scope=applicable``) and the widening it applied, so
    that nothing is widened silently (RSV-03): each body the jurisdiction has a reviewed membership of, with
    whether it held on the date (only the law of bodies ``held`` was added), how many records of other
    jurisdictions were admitted, and how many were left out, by relation."""

    jurisdiction_id: ID
    as_of: date
    date_basis: Literal["as_of", "today"]
    membership: Literal["reviewed", "unknown"]
    bodies: tuple[MembershipBasis, ...] = ()
    admitted: int = Field(ge=0)
    left_out: dict[str, int] = Field(default_factory=dict)
    notices: tuple[str, ...] = ()


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
    # Why published texts were not searched, or why nothing could be matched (API-04); absent when
    # there is nothing to say, so such an answer is unchanged.
    notices: tuple[str, ...] = Field(default=(), exclude_if=lambda value: not value)


class ExpressionView(WireModel):
    """One language edition of a record (API-02 expressions): its public texts, the one to read when no
    date is given, and what the edition is worth as law as its record states it, with where; ``unknown``
    when the record states nothing. A preferred text is a reading default, never a claim of authority."""

    expression_id: ID
    source_id: ID
    language: ID
    authority: ExpressionAuthority = "unknown"
    authority_evidence: AuthorityEvidence | None = Field(**OMIT)
    version_ids: tuple[ID, ...]
    preferred_version_id: ID | None = None
    preferred_reason: Literal["only_text", "in_force_today", "several_texts", "dates_unknown"]


class ExpressionPage(WireModel):
    source_id: ID
    language_rule: LanguageRuleView = LanguageRuleView(status="unknown")
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
# "repealed_by" a later one, it is "made_under" its parent law, which "enables" it; a national law
# "transposes" a directive, which is "transposed_by" it. Judicial treatment is not served yet.
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
    "commences",
    "commenced_by",
    "transposes",
    "transposed_by",
    "domesticates",
    "domesticated_by",
    "implements",
    "implemented_by",
    "ratifies",
    "ratified_by",
    "cites",
    "cited_by",
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
    # The record's legal effect (a regional instrument) or status and parties (a treaty), from its record.
    regional: RegionalView | None = Field(**OMIT)
    treaty: TreatyView | None = Field(**OMIT)
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
    # In an applicable-law search (scope=applicable): the record's relation to the jurisdiction asked about.
    applicability: Applicability | None = Field(**OMIT)


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
    applicability: ApplicableScope | None = Field(**OMIT)


CURRENTNESS = Literal["in_force", "not_in_force", "partly_in_force", "unknown", "contested"]


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
    features: dict[str, dict[str, float]] | None = Field(**OMIT)
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
    expanded_from: ID | None = Field(**OMIT)


# How one court's decisions bear on another (CPR-04), in its source's own terms: precedent binds; a rule of
# precedence on a subject matter, not stare decisis; the ruling binds the court that referred a question, in
# that case only; persuasive; not applicable. ``unknown`` is an answer, never a recorded relation.
COURT_BEARINGS = ("binding", "prevails_on_matter", "binding_in_referred_case", "persuasive", "not_applicable")
COURT_ANSWER = Literal[*COURT_BEARINGS, "unknown"]


class CourtQuote(WireModel):
    """The words a court relation rests on, verbatim from its source, with the provision they are in."""

    quote: str
    provision: str
    title: str
    url: str


class EvidenceBearing(WireModel):
    """How the decision's court bears on another court, as a reviewed relation of its court register records
    it: on ``court``, on the jurisdiction's courts generally (null), or with ``members`` on the courts of
    each member state of a regional body; only on ``matter`` where one is given."""

    id: str
    court: str | None
    members: bool | None = Field(**OMIT)
    matter: str | None = Field(**OMIT)
    relation: Literal[*COURT_BEARINGS]
    valid_from: date | None = Field(**OMIT)
    valid_until: date | None = Field(**OMIT)
    evidence: tuple[str, ...]
    rests_on: tuple[CourtQuote, ...] = ()


class EvidenceCourt(WireModel):
    """For a decision: its court in the court register that answers for the jurisdiction asked about
    (CPR-04), with the court's own review, and how its decisions bear on other courts on the date assessed.
    ``bears_on`` lists only reviewed relations: nothing is guessed."""

    id: str
    name: str
    jurisdiction: str  # the register's
    review_state: Literal["unreviewed", "reviewed"]
    reviewed_on: date | None = Field(**OMIT)
    bears_on: tuple[EvidenceBearing, ...] = ()


class EvidenceAuthority(WireModel):
    """The inputs to the authority signals (RSV-05): the instrument's place in its jurisdiction's
    hierarchy (0 is highest), and for decisions the court's relation to the queried jurisdiction's
    courts generally (on the question's matter, where it is known), with the court and its bearing on
    other courts as its register records them."""

    document_type: str
    instrument_rank: int | None = Field(**OMIT)
    court_relation: COURT_ANSWER = "not_applicable"
    court: EvidenceCourt | None = Field(**OMIT)


class EvidenceLinks(WireModel):
    """Where to read a unit: ``web``, the record on the website, at the unit when the website shows this
    text; ``api``, this exact part of this exact text; ``text``, the whole text it is in."""

    web: str
    api: str
    text: str


class EvidenceUnit(WireModel):
    """One unit of legal evidence (RSV-07): which part of which published text, its exact words (or an
    excerpt within the byte budget), the hashes to check them, where it is in the original file and
    where to read it, what quoting asks for, whether it was in force, and why it ranks where it does."""

    anchor_id: ID
    source_id: ID
    version_id: ID
    expression_id: str
    # What the edition is worth as law, as its record states it (API-02, DAT-07), and where.
    expression_authority: ExpressionAuthority = "unknown"
    expression_authority_evidence: AuthorityEvidence | None = Field(**OMIT)
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
    retrieved_at: datetime  # when the original file was fetched from the official address
    original_sha256: Hash  # the original file's SHA-256, to check a copy of it
    # The pages of the original file the unit is on (1 is the file's first page, not a printed number);
    # absent when the text was not read from a file with pages.
    page_start: int | None = Field(**OMIT)
    page_end: int | None = Field(**OMIT)
    links: EvidenceLinks
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
    # A regional instrument's legal effect, or a treaty's status (its parties are on the record), from its
    # record (JUR-04); and in an applicable-law answer, the unit's relation to the jurisdiction asked about.
    regional: RegionalView | None = Field(**OMIT)
    treaty: TreatyView | None = Field(**OMIT)
    applicability: Applicability | None = Field(**OMIT)
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
    engine: str | None = Field(**OMIT)
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
    applicability: ApplicableScope | None = Field(**OMIT)


class CheckItem(WireModel):
    """A cited part to check (RSV-08): its anchor, or its text version and locator, with the span hash
    the client holds, the date to assess it at and, to ask whether it binds there, a jurisdiction."""

    anchor_id: ID | None = None
    version_id: ID | None = None
    locator: str | None = Field(None, min_length=1, max_length=1000)
    span_hash: Hash | None = None
    as_of: date | None = None
    jurisdiction: str | None = Field(
        None, min_length=2, max_length=40, pattern=r"^[a-z]{2}(?:-[a-z0-9]{1,12})?$"
    )

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
    hash sent is not this part's); or ``unavailable`` (not public now, or never: not told apart). Asked with
    a jurisdiction, ``applicability`` says whether the record binds there on the date (api.applicable)."""

    index: int = Field(ge=0)
    result: Literal["unchanged", "newer_version", "span_mismatch", "unavailable"]
    anchor_id: ID | None = Field(**OMIT)
    version_id: ID | None = Field(**OMIT)
    locator: str | None = Field(**OMIT)
    newer_version_id: ID | None = Field(**OMIT)
    newer_anchor_id: ID | None = Field(**OMIT)
    same_words: bool | None = Field(**OMIT)
    currentness: Currentness | None = Field(**OMIT)
    applicability: Applicability | None = Field(**OMIT)


class CheckPage(WireModel):
    schema_version: Literal["oa-check-1"] = "oa-check-1"
    dataset: str
    results: tuple[CheckResult, ...]


REVIEW_STATE = Literal["unreviewed", "reviewed"]


class CourtEvidence(WireModel):
    """A source a court register entry cites: a constitution, an Act, a treaty, a court's own record."""

    title: str
    url: str
    pinpoint: str | None = Field(**OMIT)


class CourtEntry(WireModel):
    """What every entry of a court register carries: its own review, at the level its kind needs
    (``structural`` for courts, matters, members, routes and declarations; ``doctrinal`` for relations
    and the precedent rule), the sources it rests on, and a note on what they say."""

    id: str
    review_state: REVIEW_STATE
    review_level: Literal["structural", "doctrinal"]
    reviewed_by: str | None = Field(**OMIT)
    reviewer_role: str | None = Field(**OMIT)
    reviewer_qualification: str | None = Field(**OMIT)
    reviewed_on: date | None = Field(**OMIT)
    evidence: tuple[CourtEvidence, ...]
    note: str | None = Field(**OMIT)


class CourtPrecedentView(CourtEntry):
    id: Literal["precedent"]
    binds: bool


class CourtLevelView(CourtEntry):
    name: str
    kind: Literal["court", "tribunal", "quasi_judicial"]
    level: str
    rank: int | None = Field(**OMIT)
    jurisdiction: str | None = Field(**OMIT)
    valid_from: date | None = Field(**OMIT)
    valid_until: date | None = Field(**OMIT)
    appeals_to: tuple[str, ...] = ()


class CourtRelationView(CourtEntry):
    court: str
    bound: str | None
    members: bool
    matter: str | None = Field(**OMIT)
    relation: COURT_ANSWER
    recorded: Literal[*COURT_BEARINGS]
    rests_on: tuple[CourtQuote, ...]
    valid_from: date | None = Field(**OMIT)
    valid_until: date | None = Field(**OMIT)


class CourtMatterView(CourtEntry):
    name: str


class CourtMemberView(CourtEntry):
    state: str
    court: str | None = Field(**OMIT)
    valid_from: date | None = Field(**OMIT)
    valid_until: date | None = Field(**OMIT)
    withdrawal_notified: date | None = Field(**OMIT)


class CourtRouteView(CourtEntry):
    court: str
    kind: Literal["cassation", "appeal", "reference"]
    matter: str | None = Field(**OMIT)
    valid_from: date | None = Field(**OMIT)
    valid_until: date | None = Field(**OMIT)


class CourtDeclarationView(CourtEntry):
    state: str
    court: str
    valid_from: date | None = Field(**OMIT)
    valid_until: date | None = Field(**OMIT)
    withdrawal_deposited: date | None = Field(**OMIT)


REGISTER_REVIEW_STATE = Literal["unreviewed", "partly_reviewed", "reviewed"]


class CourtRegisterView(WireModel):
    """A jurisdiction's court register (CPR-04, JUR-03, JUR-04): its courts with their levels and appeal
    routes, how each court's decisions bear on the others, whether precedent binds there at all, and for
    a regional or continental body the states its courts reach. Every entry with its own review and its
    sources. A relation answers ``unknown`` until it has its own doctrinal review; ``recorded`` is what
    awaits review, and ``rests_on`` the words it rests on."""

    jurisdiction: str
    review_state: REGISTER_REVIEW_STATE
    precedent: CourtPrecedentView | None
    courts: tuple[CourtLevelView, ...] = ()
    relations: tuple[CourtRelationView, ...] = ()
    matters: tuple[CourtMatterView, ...] = ()
    members: tuple[CourtMemberView, ...] = ()
    member_routes: tuple[CourtRouteView, ...] = ()
    declarations: tuple[CourtDeclarationView, ...] = ()


class CourtRegisterSummary(WireModel):
    jurisdiction: str
    review_state: REGISTER_REVIEW_STATE
    courts: int = Field(ge=0)
    relations: int = Field(ge=0)
    entries: int = Field(ge=0)
    reviewed_entries: int = Field(ge=0)


class CourtRegisters(WireModel):
    registers: tuple[CourtRegisterSummary, ...]


class PublicContracts(WireModel):
    schema_version: Literal["pilot-public-1"] = "pilot-public-1"
    sources: tuple[SourcePage, ...] = ()
    searches: tuple[SearchPage, ...] = ()
    versions: tuple[VersionPage, ...] = ()
    rights: tuple[RightsView, ...] = ()
    citations: tuple[CitationView, ...] = ()
    jurisdictions: tuple[JurisdictionView, ...] = ()
    jurisdiction_details: tuple[JurisdictionDetail, ...] = ()
    coverage: tuple[CoverageView, ...] = ()
    relationships: tuple[SourceRelationships, ...] = ()
    structures: tuple[StructurePage, ...] = ()
    anchors: tuple[AnchorView, ...] = ()
    rights_histories: tuple[RightsHistory, ...] = ()
    text_searches: tuple[TextSearchPage, ...] = ()
