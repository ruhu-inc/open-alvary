"""Policy foundation DTOs; no raw contacts/evidence in public result contracts."""

from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, HttpUrl

from schema.identity import CollectionID, WorkID
from schema.models import ID, Hash, Nonempty

Permission = Literal["allow", "deny", "unknown"]


class PrivateRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ReviewedRecord(PrivateRecord):
    revision: int = Field(gt=0)
    collection_id: CollectionID
    actor_id: UUID
    evidence_id: UUID
    valid_from: AwareDatetime
    expires_at: AwareDatetime
    state: Literal["draft", "approved", "revoked"] = "draft"
    reason: Nonempty


class ControllerRecord(ReviewedRecord):
    id: UUID
    legal_name: Nonempty
    postal_address: Nonempty
    jurisdiction_id: ID
    privacy_contact: Nonempty
    accountable_role: Nonempty


class PrivacyReview(ReviewedRecord):
    id: UUID
    controller_id: UUID
    controller_revision: int = Field(gt=0)
    purpose: Nonempty
    lawful_basis: Nonempty
    assessment_reference: Nonempty
    clearance: Literal["pending", "cleared", "redacted", "rejected"]
    cleared_hash: Hash | None = None


class AcquisitionAssessment(ReviewedRecord):
    id: UUID
    discover: Permission = "unknown"
    acquire: Permission = "unknown"
    retain: Permission = "unknown"
    derive: Permission = "unknown"
    privacy_class: Literal["no_personal_data", "personal_data", "unknown"]
    classification_reason: Nonempty
    privacy_review_id: UUID | None = None
    privacy_review_revision: int | None = Field(default=None, gt=0)
    purpose: Nonempty
    retention_deadline: AwareDatetime


class PermissionVector(PrivateRecord):
    discover: Permission = "unknown"
    acquire: Permission = "unknown"
    retain: Permission = "unknown"
    external_process: Permission = "unknown"
    redistribute_metadata: Permission = "unknown"
    redistribute_text: Permission = "unknown"
    quote: Permission = "unknown"
    derive: Permission = "unknown"
    commercial_reuse: Permission = "unknown"
    publish_fixture: Permission = "unknown"


class CollectionDecision(ReviewedRecord, PermissionVector):
    basis_type: Literal[
        "open_licence",
        "direct_permission",
        "statutory_exclusion",
        "public_domain_expiry",
        "government_work_rule",
        "original_authorship",
        "statutory_exception",
        "unknown",
    ]
    conditions_satisfied: bool = False
    metadata_privacy_class: Literal["no_personal_data", "personal_data", "unknown"]
    privacy_reason: Nonempty
    privacy_review_id: UUID | None = None
    privacy_review_revision: int | None = Field(default=None, gt=0)


class VersionOverride(ReviewedRecord, PermissionVector):
    version_id: ID


class VerificationPolicy(ReviewedRecord):
    sample_numerator: int = Field(gt=0)
    sample_denominator: int = Field(gt=0)
    escalation_rule: Nonempty
    material_classes: tuple[Nonempty, ...]


class VerificationMaterial(PrivateRecord):
    collection_id: CollectionID
    revision: int = Field(gt=0)
    material_class: Literal[
        "operative_tables", "amounts", "dates", "negations", "cross_references", "identity_citation"
    ]
    method: Literal["human_source_comparison", "validated_native_sampling"]
    sample_numerator: int = Field(gt=0)
    sample_denominator: int = Field(gt=0)
    competence_evidence_id: UUID
    escalation_rule: Nonempty
    exclusion_rule: Nonempty


class PolicyResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    allowed: bool
    reason: Literal["permitted", "policy_denied", "runtime_capability_unavailable"]


class PublicMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    work_id: WorkID
    title: Nonempty
    citation: Nonempty
    reference_url: HttpUrl


class PolicyContracts(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["policy-foundation-1"] = "policy-foundation-1"
    results: tuple[PolicyResult, ...] = ()
    metadata: tuple[PublicMetadata, ...] = ()
    controllers: tuple[ControllerRecord, ...] = ()
    privacy_reviews: tuple[PrivacyReview, ...] = ()
    assessments: tuple[AcquisitionAssessment, ...] = ()
    decisions: tuple[CollectionDecision, ...] = ()
    overrides: tuple[VersionOverride, ...] = ()
    verification_policies: tuple[VerificationPolicy, ...] = ()
    verification_materials: tuple[VerificationMaterial, ...] = ()
