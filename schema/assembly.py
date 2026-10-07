"""Private synthetic proposals; these records never establish extraction or approval."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from schema.canonical import CanonicalCommand, CanonicalView
from schema.identity import CollectionID
from schema.models import Hash
from schema.operator import Reason

LocalKey = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9:_-]+$")]


class PrivateModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CellProposal(PrivateModel):
    table_local_id: LocalKey
    row: int = Field(ge=0, le=99, strict=True)
    column: int = Field(ge=0, le=99, strict=True)
    row_span: int = Field(default=1, ge=1, le=100, strict=True)
    column_span: int = Field(default=1, ge=1, le=100, strict=True)

    @model_validator(mode="after")
    def bounds(self):
        if self.row + self.row_span > 100 or self.column + self.column_span > 100:
            raise ValueError("Cell grid bound exceeded")
        return self


class CandidateInput(PrivateModel):
    id: UUID
    adapter_local_id: LocalKey
    region_key: LocalKey
    parent_local_id: LocalKey | None = None
    artifact_class: Literal["native_text", "ocr_transcription"]
    block_type: Literal["heading", "paragraph", "list_item", "table_cell", "footnote"]
    text: str = Field(max_length=8192)
    content_state: Literal["present", "empty", "illegible", "unsupported"] = "present"
    content_reason: Reason | None = None
    unavailable_page_reason: Reason
    unavailable_geometry_reason: Reason
    language: LocalKey | None = None
    direction: Literal["ltr", "rtl", "unknown"] = "unknown"
    quality_state: Literal["unassessed"] = "unassessed"
    warnings: tuple[Annotated[str, Field(min_length=1, max_length=256)], ...] = Field(
        default=(), max_length=10
    )
    cell: CellProposal | None = None

    @model_validator(mode="after")
    def content(self):
        if (self.content_state == "present") != bool(self.text):
            raise ValueError("Literal text and content state disagree")
        if self.content_state in {"illegible", "unsupported"} and not self.content_reason:
            raise ValueError("Unavailable content reason required")
        if (self.block_type == "table_cell") != (self.cell is not None):
            raise ValueError("Explicit local cell proposal required")
        return self


class AssemblyEnvelope(PrivateModel):
    command_id: UUID
    collection_id: CollectionID
    reason: Reason


class SyntheticRunCommand(AssemblyEnvelope):
    action: Literal["record_synthetic_run"]
    run_id: UUID
    artifact_id: UUID
    assessment_id: UUID
    assessment_revision: int = Field(gt=0, strict=True)
    artifact_hash: Hash
    purpose: Reason
    profile_key: LocalKey
    candidates: tuple[CandidateInput, ...] = Field(min_length=1, max_length=50)


class Selection(PrivateModel):
    region_key: LocalKey
    candidate_id: UUID


class Resolution(Selection):
    rejected_candidate_ids: tuple[UUID, ...] = Field(min_length=1, max_length=49)
    reason: Reason


class SnapshotBody(PrivateModel):
    selections: tuple[Selection, ...] = Field(min_length=1, max_length=50)
    order: tuple[UUID, ...] = Field(min_length=1, max_length=50)
    resolutions: tuple[Resolution, ...] = Field(default=(), max_length=50)
    reason: Reason

    @model_validator(mode="after")
    def selection_order(self):
        ids = [s.candidate_id for s in self.selections]
        regions = [s.region_key for s in self.selections]
        if len(set(ids)) != len(ids) or len(set(regions)) != len(regions):
            raise ValueError("One candidate per region required")
        if len(set(self.order)) != len(self.order) or set(self.order) != set(ids):
            raise ValueError("Each selected candidate occurs once in order")
        resolution_regions = [r.region_key for r in self.resolutions]
        if len(set(resolution_regions)) != len(resolution_regions):
            raise ValueError("One resolution per conflicting region required")
        for resolution in self.resolutions:
            if (
                resolution.candidate_id not in ids
                or resolution.candidate_id in resolution.rejected_candidate_ids
            ):
                raise ValueError("Invalid conflict selection")
            if len(set(resolution.rejected_candidate_ids)) != len(resolution.rejected_candidate_ids):
                raise ValueError("Duplicate rejected candidate")
        return self


class SnapshotCommand(AssemblyEnvelope):
    action: Literal["record_snapshot"]
    snapshot_id: UUID
    run_id: UUID
    purpose: Reason
    expected_revision: int = Field(ge=0, le=31, strict=True)
    parent_hash: Hash | None = None
    payload: SnapshotBody

    @model_validator(mode="after")
    def parent(self):
        if bool(self.expected_revision) != (self.parent_hash is not None):
            raise ValueError("Exact prior snapshot hash required")
        return self


class AssemblyReconcileCommand(AssemblyEnvelope):
    action: Literal["reconcile_assembly"]
    after: UUID | None = None
    limit: int = Field(default=50, ge=1, le=100, strict=True)
    erase_due: bool = Field(default=False, strict=True)


AssemblyCommand = Annotated[
    SyntheticRunCommand | SnapshotCommand | AssemblyReconcileCommand | CanonicalCommand,
    Field(discriminator="action"),
]


class AssemblyResult(PrivateModel):
    command_id: UUID | None
    status: Literal[
        "applied", "forbidden", "conflict", "validation_failed", "capacity_exceeded", "unavailable"
    ]
    replayed: bool = False
    object_id: UUID | None = None
    revision: int | None = None
    output_hash: Hash | None = None
    scanned: int = Field(default=0, ge=0, le=100)
    held: int = Field(default=0, ge=0, le=12800)
    erasure_required: int = Field(default=0, ge=0, le=12800)
    erased: int = Field(default=0, ge=0, le=12800)
    next_after: UUID | None = None


class CandidateView(PrivateModel):
    run_id: UUID
    artifact_id: UUID
    collection_id: CollectionID
    assessment_id: UUID
    assessment_revision: int
    artifact_hash: Hash
    purpose: Reason
    retention_deadline: AwareDatetime
    payload_hash: Hash
    text_hash: Hash
    payload: CandidateInput


class CandidatePage(PrivateModel):
    items: tuple[CandidateView, ...] = ()
    next_after: UUID | None = None


class SnapshotView(PrivateModel):
    snapshot_id: UUID
    revision: int
    parent_hash: Hash | None
    run_id: UUID
    payload_hash: Hash
    snapshot_hash: Hash
    payload: SnapshotBody


class AssemblyContracts(PrivateModel):
    schema_version: Literal["assembly-proposal-2"] = "assembly-proposal-2"
    commands: tuple[AssemblyCommand, ...] = ()
    results: tuple[AssemblyResult, ...] = ()
    candidate_pages: tuple[CandidatePage, ...] = ()
    snapshots: tuple[SnapshotView, ...] = ()
    canonical_proposals: tuple[CanonicalView, ...] = ()
