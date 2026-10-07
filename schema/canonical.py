"""Private OA-text-1 projections; no public versions, anchors or approval."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from schema.identity import CollectionID
from schema.models import Hash
from schema.operator import Reason

LocalKey = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9:_-]+$")]


class PrivateModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TextBlock(PrivateModel):
    kind: Literal["text"]
    candidate_id: UUID


class CellSelection(PrivateModel):
    candidates: tuple[UUID, ...] = Field(min_length=1, max_length=50)


class UnavailablePosition(PrivateModel):
    row: int = Field(ge=0, le=99, strict=True)
    column: int = Field(ge=0, le=99, strict=True)
    content_state: Literal["illegible", "unsupported"]
    reason: Reason


class TableBlock(PrivateModel):
    kind: Literal["table"]
    table_local_id: LocalKey
    rows: int = Field(ge=1, le=100, strict=True)
    columns: int = Field(ge=1, le=100, strict=True)
    cells: tuple[CellSelection, ...] = Field(min_length=1, max_length=50)
    headers: tuple[UUID, ...] = Field(default=(), max_length=50)
    unavailable: tuple[UnavailablePosition, ...] = Field(default=(), max_length=10000)


BodyBlock = Annotated[TextBlock | TableBlock, Field(discriminator="kind")]


class FootnoteDefinition(PrivateModel):
    local_id: LocalKey
    candidates: tuple[UUID, ...] = Field(min_length=1, max_length=50)


class FootnoteMarker(PrivateModel):
    candidate_id: UUID
    source_start: int = Field(ge=0, le=32768, strict=True)
    source_end: int = Field(gt=0, le=32768, strict=True)
    footnote_id: LocalKey


class NoticeBlock(PrivateModel):
    kind: Literal["notice"]
    local_id: LocalKey
    blocks: tuple[BodyBlock, ...] = Field(min_length=1, max_length=50)
    footnotes: tuple[FootnoteDefinition, ...] = Field(default=(), max_length=50)
    markers: tuple[FootnoteMarker, ...] = Field(default=(), max_length=100)


DocumentBlock = Annotated[TextBlock | TableBlock | NoticeBlock, Field(discriminator="kind")]


class Exclusion(PrivateModel):
    candidate_id: UUID
    kind: Literal["furniture", "repeated_header"]
    reason: Reason
    target_candidate_id: UUID | None = None


class CanonicalPlan(PrivateModel):
    blocks: tuple[DocumentBlock, ...] = Field(default=(), max_length=50)
    footnotes: tuple[FootnoteDefinition, ...] = Field(default=(), max_length=50)
    markers: tuple[FootnoteMarker, ...] = Field(default=(), max_length=100)
    exclusions: tuple[Exclusion, ...] = Field(default=(), max_length=50)


class CanonicalCommand(PrivateModel):
    command_id: UUID
    collection_id: CollectionID
    reason: Reason
    action: Literal["record_canonical_proposal"]
    proposal_id: UUID
    snapshot_id: UUID
    snapshot_revision: int = Field(ge=1, le=32, strict=True)
    snapshot_hash: Hash
    purpose: Reason
    serialization_profile_id: Literal["OA-text-1"] = "OA-text-1"
    plan: CanonicalPlan


class CandidateSpan(PrivateModel):
    candidate_id: UUID
    start: int
    end: int
    span_hash: Hash


class Normalization(PrivateModel):
    candidate_id: UUID
    source_hash: Hash
    source_bytes: int
    normalized_hash: Hash
    normalized_bytes: int
    operations: tuple[Literal["lf", "nfc"], ...]


class SeparatorOffsets(PrivateModel):
    block: tuple[int, ...] = ()
    cell: tuple[int, ...] = ()
    column: tuple[int, ...] = ()
    row: tuple[int, ...] = ()
    terminator: tuple[int, ...] = ()


class MarkerProjection(PrivateModel):
    candidate_id: UUID
    source_start: int
    source_end: int
    start: int
    end: int
    marker_hash: Hash
    footnote_id: LocalKey
    scope: LocalKey | None


class NoticeRange(PrivateModel):
    local_id: LocalKey
    start: int
    end: int
    content_hash: Hash


class ProjectionDetails(PrivateModel):
    spans: tuple[CandidateSpan, ...]
    normalizations: tuple[Normalization, ...]
    separators: SeparatorOffsets
    markers: tuple[MarkerProjection, ...]
    notice_ranges: tuple[NoticeRange, ...]
    incomplete_tables: tuple[LocalKey, ...]
    publication_eligible: Literal[False] = False


class CanonicalPayload(PrivateModel):
    plan: CanonicalPlan
    projection: ProjectionDetails


class CanonicalView(PrivateModel):
    proposal_id: UUID
    snapshot_id: UUID
    snapshot_revision: int
    snapshot_hash: Hash
    serialization_profile_id: Literal["OA-text-1"]
    profile_hash: Hash
    content_hash: Hash
    canonical_text: str
    payload: CanonicalPayload
