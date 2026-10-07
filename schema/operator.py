"""Private operator command contract; session actors are never input fields."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from schema.identity import CollectionID
from schema.models import Hash
from schema.policy import PermissionVector

Reason = Annotated[str, Field(min_length=1, max_length=2048)]
MATERIALS = frozenset(
    {"operative_tables", "amounts", "dates", "negations", "cross_references", "identity_citation"}
)


class CommandModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class EvidenceInput(CommandModel):
    id: UUID
    private_reference: Reason
    evidence_hash: Hash
    observed_at: AwareDatetime


class ReviewInput(CommandModel):
    evidence_id: UUID
    valid_from: AwareDatetime
    expires_at: AwareDatetime
    state: Literal["draft", "approved", "revoked"] = "draft"

    @model_validator(mode="after")
    def interval(self):
        if self.expires_at <= self.valid_from:
            raise ValueError("Invalid review interval")
        return self


class DecisionInput(ReviewInput, PermissionVector):
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
    privacy_reason: Reason
    privacy_review_id: UUID | None = None
    privacy_review_revision: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def privacy_pair(self):
        if (self.privacy_review_id is None) != (self.privacy_review_revision is None):
            raise ValueError("Incomplete privacy revision")
        if self.metadata_privacy_class != "no_personal_data" and self.privacy_review_id is None:
            raise ValueError("Privacy review required")
        return self


class MaterialInput(CommandModel):
    material_class: Literal[
        "operative_tables", "amounts", "dates", "negations", "cross_references", "identity_citation"
    ]
    method: Literal["human_source_comparison", "validated_native_sampling"]
    sample_numerator: int = Field(gt=0)
    sample_denominator: int = Field(gt=0)
    competence_evidence_id: UUID
    escalation_rule: Reason
    exclusion_rule: Reason

    @model_validator(mode="after")
    def sampling(self):
        if self.sample_numerator > self.sample_denominator:
            raise ValueError("Invalid sampling fraction")
        if self.material_class == "operative_tables" and self.method != "human_source_comparison":
            raise ValueError("Operative tables require source comparison")
        return self


class VerificationInput(ReviewInput):
    sample_numerator: int = Field(gt=0)
    sample_denominator: int = Field(gt=0)
    escalation_rule: Reason
    materials: tuple[MaterialInput, ...] = Field(min_length=6, max_length=6)

    @model_validator(mode="after")
    def complete(self):
        if self.sample_numerator > self.sample_denominator:
            raise ValueError("Invalid sampling fraction")
        if {material.material_class for material in self.materials} != MATERIALS:
            raise ValueError("Complete material review required")
        return self


class CommandEnvelope(CommandModel):
    command_id: UUID
    collection_id: CollectionID
    expected_revision: int = Field(ge=0)
    reason: Reason


class EvidenceCommand(CommandEnvelope):
    action: Literal["record_evidence"]
    expected_revision: Literal[0] = 0
    payload: EvidenceInput


class DecisionCommand(CommandEnvelope):
    action: Literal["record_collection_decision"]
    payload: DecisionInput


class VerificationCommand(CommandEnvelope):
    action: Literal["record_verification_review"]
    payload: VerificationInput


OperatorCommand = Annotated[
    EvidenceCommand | DecisionCommand | VerificationCommand, Field(discriminator="action")
]


class CommandResult(CommandModel):
    command_id: UUID | None
    status: Literal["applied", "conflict", "forbidden", "validation_failed", "unavailable"]
    revision: int | None = None
    replayed: bool = False


class OperatorContracts(CommandModel):
    schema_version: Literal["operator-command-1"] = "operator-command-1"
    commands: tuple[OperatorCommand, ...] = ()
    results: tuple[CommandResult, ...] = ()
