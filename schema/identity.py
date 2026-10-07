"""Versioned identity slice; these DTOs never carry publication permission."""

from datetime import date
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, HttpUrl, model_validator

from schema.models import ID, Hash, Nonempty

UUID4_SUFFIX = r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
WorkID = Annotated[str, Field(pattern="^wrk_" + UUID4_SUFFIX + "$")]
ExpressionID = Annotated[str, Field(pattern="^exp_" + UUID4_SUFFIX + "$")]
ManifestationID = Annotated[str, Field(pattern="^man_" + UUID4_SUFFIX + "$")]
ObservationID = Annotated[str, Field(pattern="^obs_" + UUID4_SUFFIX + "$")]
CollectionID = Annotated[str, Field(pattern="^scp_" + UUID4_SUFFIX + "$")]
DocumentClass = Literal["constitution", "act", "regulation", "judgment", "gazette", "guidance", "other"]


def mint(prefix: Literal["wrk", "exp", "man", "obs", "scp"]) -> str:
    return f"{prefix}_{uuid4()}"


class IdentityModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Work(IdentityModel):
    id: WorkID
    jurisdiction_id: ID
    authority_id: ID
    document_class: DocumentClass
    title: Nonempty
    citation: Nonempty
    reference_url: HttpUrl
    reference_kind: Literal["document", "landing_page", "discovery"]
    catalogue_language_id: ID
    legacy_legal_status: Literal["unknown", "current", "historical", "repealed", "superseded"]
    status_evidence_url: HttpUrl | None
    catalogue_note: str | None
    identity_status: Literal["legacy_unreviewed", "identified"]
    collection_id: CollectionID | None
    collection_unknown_reason: Nonempty | None
    created_at: AwareDatetime
    updated_at: AwareDatetime

    @model_validator(mode="after")
    def collection_availability(self):
        if (self.collection_id is None) != (self.collection_unknown_reason is not None):
            raise ValueError("Unknown collection requires a reason")
        return self


class Expression(IdentityModel):
    id: ExpressionID
    work_id: WorkID
    language_id: ID
    edition_kind: Literal["original", "official_consolidation", "unofficial_consolidation", "translation"]
    edition_key: Annotated[str, Field(min_length=1, max_length=160)]
    edition_as_of: date | None
    edition_date_unknown_reason: Nonempty | None
    authenticity: Literal["unknown", "official", "unofficial"]
    authenticity_unknown_reason: Nonempty | None
    created_at: AwareDatetime

    @model_validator(mode="after")
    def unknowns(self):
        if (self.edition_as_of is None) != (self.edition_date_unknown_reason is not None):
            raise ValueError("Unknown edition date requires a reason")
        if (self.authenticity == "unknown") != (self.authenticity_unknown_reason is not None):
            raise ValueError("Unknown authenticity requires a reason")
        return self


class Manifestation(IdentityModel):
    id: ManifestationID
    raw_hash: Hash
    media_type: Annotated[str, Field(pattern=r"^[a-z0-9.+-]+/[a-z0-9.+-]+$")]
    size_bytes: int = Field(ge=0)
    provenance_reference: Nonempty
    created_at: AwareDatetime


class Observation(IdentityModel):
    id: ObservationID
    manifestation_id: ManifestationID
    provider_key: Annotated[str, Field(min_length=1, max_length=160)]
    retrieval_key: Annotated[str, Field(min_length=1, max_length=160)]
    requested_url: HttpUrl
    final_url: HttpUrl
    observed_at: AwareDatetime
    response_status: int = Field(ge=100, le=599)
    processing_config_hash: Hash
    acquisition_reference: Nonempty
    created_at: AwareDatetime


class Collection(IdentityModel):
    id: CollectionID
    jurisdiction_id: ID
    document_class: DocumentClass
    provider_key: Annotated[str, Field(min_length=1, max_length=160)]
    created_at: AwareDatetime


class PublicWorkIdentity(IdentityModel):
    """Only usable after current legacy metadata policy permits the source."""

    legacy_source_id: ID
    work_id: WorkID
    title: Nonempty
    citation: Nonempty
    reference_url: HttpUrl


class IdentityContracts(IdentityModel):
    schema_version: Literal["identity-1"] = "identity-1"
    works: tuple[Work, ...] = ()
    expressions: tuple[Expression, ...] = ()
    manifestations: tuple[Manifestation, ...] = ()
    observations: tuple[Observation, ...] = ()
    collections: tuple[Collection, ...] = ()
    public_identities: tuple[PublicWorkIdentity, ...] = ()
