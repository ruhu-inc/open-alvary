"""Original-synthetic private approval; no publication command or serving DTO."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from schema.identity import UUID4_SUFFIX, CollectionID, ExpressionID
from schema.models import Hash
from schema.operator import Reason

VersionID = Annotated[str, Field(pattern="^ver_" + UUID4_SUFFIX + "$")]
RepresentationID = Annotated[str, Field(pattern="^prp_" + UUID4_SUFFIX + "$")]


class PrivateModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ApprovalEnvelope(PrivateModel):
    command_id: UUID
    collection_id: CollectionID
    purpose: Reason
    reason: Reason


class SnapshotReviewCommand(ApprovalEnvelope):
    action: Literal["record_snapshot_review"]
    review_id: UUID
    binding_id: UUID
    binding_hash: Hash
    proposal_id: UUID
    snapshot_hash: Hash
    profile_hash: Hash
    content_hash: Hash
    verification_revision: int = Field(gt=0, strict=True)
    evidence_id: UUID
    reviewed_candidate_ids: tuple[UUID, ...] = Field(min_length=1, max_length=50)
    comparison_method: Literal["human_source_comparison"]
    review_scope: Literal["collection_policy"]
    requires_exception_review: Literal[False]


class ApproveVersionCommand(ApprovalEnvelope):
    action: Literal["approve_synthetic_version"]
    review_id: UUID
    review_hash: Hash
    rights_revision: int = Field(gt=0, strict=True)
    verification_revision: int = Field(gt=0, strict=True)
    version_id: VersionID
    representation_id: RepresentationID
    prior_version_id: VersionID | None = None
    mode: Literal["new_version", "representation_revision"]
    expected_rep_revision: int = Field(default=0, ge=0, le=32, strict=True)


ApprovalCommand = Annotated[SnapshotReviewCommand | ApproveVersionCommand, Field(discriminator="action")]


class ApprovalResult(PrivateModel):
    command_id: UUID | None
    status: Literal[
        "applied", "conflict", "forbidden", "validation_failed", "capacity_exceeded", "unavailable"
    ]
    replayed: bool = False
    review_hash: Hash | None = None
    version_id: VersionID | None = None
    representation_id: RepresentationID | None = None
    revision: int | None = None
    content_hash: Hash | None = None


class PrivateVersionView(PrivateModel):
    version_id: VersionID
    expression_id: ExpressionID
    current_rep_id: RepresentationID
    rep_revision: int
    snapshot_hash: Hash
    profile_hash: Hash
    content_hash: Hash
    serialization_profile_id: Literal["OA-text-1"]
    canonical_text: str
    record_set: dict
    publication_eligible: Literal[False]


class ApprovalContracts(PrivateModel):
    schema_version: Literal["synthetic-approval-1"] = "synthetic-approval-1"
    commands: tuple[ApprovalCommand, ...] = ()
    results: tuple[ApprovalResult, ...] = ()
    private_versions: tuple[PrivateVersionView, ...] = ()
