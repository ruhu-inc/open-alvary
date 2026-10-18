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


class VersionPage(WireModel):
    items: tuple[VersionViewRecord, ...]
    rights: RightsViewRecord
    structure: tuple[NodeViewRecord, ...]
    notice: str | None


class RightsView(WireModel):
    record: RightsViewRecord
    decision: DecisionView


class CitationView(WireModel):
    status: Literal["resolved", "ambiguous", "unresolved"]
    matches: tuple[SourceView, ...]


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
