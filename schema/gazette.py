"""Private synthetic notice projections; issue versions own every byte and anchor."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from schema.approval import RepresentationID, VersionID
from schema.identity import UUID4_SUFFIX, CollectionID, ExpressionID, WorkID
from schema.models import Hash
from schema.operator import Reason

NodeID = Annotated[str, Field(pattern="^nod_" + UUID4_SUFFIX + "$")]
AnchorID = Annotated[str, Field(pattern="^anc_" + UUID4_SUFFIX + "$")]


class PrivateModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class NoticeRange(PrivateModel):
    start_byte: int = Field(ge=0, le=131072, strict=True)
    end_byte: int = Field(gt=0, le=131072, strict=True)
    span_hash: Hash


class Envelope(PrivateModel):
    command_id: UUID
    issue_collection_id: CollectionID
    item_collection_id: CollectionID
    binding_id: UUID
    purpose: Reason
    reason: Reason


class ItemReviewCommand(Envelope):
    action: Literal["record_gazette_item_review"]
    review_id: UUID
    binding_hash: Hash
    issue_version_id: VersionID
    representation_id: RepresentationID
    record_set_hash: Hash
    content_hash: Hash
    profile_hash: Hash
    ranges: tuple[NoticeRange, ...] = Field(min_length=1, max_length=1)
    item_rights_revision: int = Field(gt=0, strict=True)
    item_verification_revision: int = Field(gt=0, strict=True)
    expected_revision: int = Field(ge=0, le=31, strict=True)
    evidence_id: UUID
    identification_method: Literal["human_source_identification"]
    review_scope: Literal["collection_policy"]
    requires_exception_review: Literal[False]


class ItemApprovalCommand(Envelope):
    action: Literal["approve_gazette_item_correspondence"]
    review_id: UUID
    review_hash: Hash
    correspondence_id: UUID
    expected_revision: int = Field(ge=0, le=31, strict=True)


GazetteCommand = Annotated[ItemReviewCommand | ItemApprovalCommand, Field(discriminator="action")]


class GazetteResult(PrivateModel):
    command_id: UUID | None
    status: Literal[
        "applied", "conflict", "forbidden", "validation_failed", "capacity_exceeded", "unavailable"
    ]
    replayed: bool = False
    review_hash: Hash | None = None
    correspondence_id: UUID | None = None
    revision: int | None = None


class GazetteProjection(PrivateModel):
    correspondence_id: UUID
    revision: int
    item_work_id: WorkID
    item_expression_id: ExpressionID
    issue_version_id: VersionID
    representation_id: RepresentationID
    notice_node_id: NodeID
    anchor_id: AnchorID
    content_hash: Hash
    profile_hash: Hash
    ranges: tuple[NoticeRange, ...]
    projected_text: str
    publication_eligible: Literal[False]


class GazetteContracts(PrivateModel):
    schema_version: Literal["synthetic-gazette-1"] = "synthetic-gazette-1"
    commands: tuple[GazetteCommand, ...] = ()
    results: tuple[GazetteResult, ...] = ()
    private_projections: tuple[GazetteProjection, ...] = ()
