"""Canonical contracts; all offsets are UTF-8 byte offsets into normalized text."""

from datetime import date
from enum import StrEnum
from hashlib import sha256
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, HttpUrl, model_validator

ID = Annotated[str, Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._:-]{0,159}$")]
Hash = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Nonempty = Annotated[str, Field(min_length=1)]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, validate_assignment=True)


class RightsStatus(StrEnum):
    OPEN = "OPEN"
    OPEN_WITH_CONDITIONS = "OPEN_WITH_CONDITIONS"
    LINK_ONLY = "LINK_ONLY"
    RESTRICTED = "RESTRICTED"
    RIGHTS_UNDER_REVIEW = "RIGHTS_UNDER_REVIEW"


class Permission(StrEnum):
    YES = "yes"
    NO = "no"
    CONDITIONAL = "conditional"
    UNKNOWN = "unknown"


class Jurisdiction(Model):
    id: ID
    name: Nonempty
    country_code: Annotated[str, Field(pattern=r"^[A-Z]{2}$")]
    parent_id: ID | None = None
    legal_systems: list[str] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=list)


class Authority(Model):
    id: ID
    jurisdiction_id: ID
    name: Nonempty
    kind: Literal["legislature", "court", "regulator", "government", "other"]
    official_url: HttpUrl


class Language(Model):
    id: ID
    name: Nonempty
    tag: Nonempty  # BCP 47, e.g. en, fr, sw, ha-Latn


class RightsRecord(Model):
    id: ID
    source_id: ID
    status: RightsStatus = RightsStatus.RIGHTS_UNDER_REVIEW
    rights_holder: str | None = None
    licence: str | None = None
    licence_url: HttpUrl | None = None
    redistribution_allowed: Permission = Permission.UNKNOWN
    commercial_reuse_allowed: Permission = Permission.UNKNOWN
    derivatives_allowed: Permission = Permission.UNKNOWN
    attribution_required: bool | None = None
    attribution_text: str | None = None
    database_rights_notes: str | None = None
    privacy_status: Literal["pending", "cleared", "redacted", "blocked"] = "pending"
    reviewer: Nonempty | None = None
    reviewed_at: AwareDatetime | None = None
    expires_at: AwareDatetime | None = None
    evidence: list[HttpUrl] = Field(default_factory=list)
    legal_note: str | None = None
    conditions: list[Nonempty] = Field(default_factory=list)
    conditions_satisfied: bool = False
    approved_content_hashes: list[Hash] = Field(default_factory=list)
    metadata_public: bool = False
    metadata_basis: Nonempty | None = None


class Source(Model):
    id: ID
    jurisdiction_id: ID
    authority_id: ID
    title: Nonempty
    document_type: Literal["constitution", "act", "regulation", "judgment", "gazette", "guidance", "other"]
    canonical_url: HttpUrl
    url_kind: Literal["document", "landing_page", "discovery"] = "landing_page"
    citation: Nonempty
    language: ID = "en"
    legal_status: Literal["unknown", "current", "historical", "repealed", "superseded"] = "unknown"
    status_evidence: HttpUrl | None = None
    catalogue_note: str | None = None


class SourceVersion(Model):
    id: ID
    source_id: ID
    canonical_url: HttpUrl
    retrieved_at: AwareDatetime
    raw_hash: Hash
    content_hash: Hash
    canonical_text: str
    parser: Nonempty
    language: ID = "en"
    effective_from: date | None = None
    effective_until: date | None = None
    verification_status: Literal["pending", "verified", "rejected"] = "pending"
    content_verifier: Nonempty | None = None
    content_verified_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def verify_hash(self):
        if sha256(self.canonical_text.encode()).hexdigest() != self.content_hash:
            raise ValueError("canonical text hash mismatch")
        if self.effective_from and self.effective_until and self.effective_until < self.effective_from:
            raise ValueError("invalid effective-date interval")
        return self


class StructureNode(Model):
    id: ID
    source_id: ID
    version_id: ID
    parent_id: ID | None = None
    kind: Literal["document", "part", "chapter", "section", "subsection", "paragraph", "schedule"]
    locator: Nonempty
    order: int = Field(ge=0)
    start_byte: int = Field(ge=0)
    end_byte: int = Field(ge=0)
    page: int | None = Field(default=None, ge=1)
    source_hash: Hash

    @model_validator(mode="after")
    def ordered(self):
        if self.end_byte < self.start_byte:
            raise ValueError("end_byte precedes start_byte")
        return self


class Citation(Model):
    id: ID
    source_id: ID
    value: Nonempty
    version_id: ID | None = None
    node_id: ID | None = None


class Amendment(Model):
    id: ID
    source_id: ID
    target_source_id: ID
    kind: Literal["amends", "repeals", "supersedes", "cross_reference"]
    effective_date: date | None = None
    evidence_url: HttpUrl
    verified: bool = False


class Translation(Model):
    id: ID
    source_version_id: ID
    translated_version_id: ID
    language: ID
    translator: Nonempty
    method: Literal["official", "human", "machine"]
    provenance_url: HttpUrl
    verified: bool = False


class Corpus(Model):
    jurisdictions: list[Jurisdiction] = Field(default_factory=list)
    authorities: list[Authority] = Field(default_factory=list)
    languages: list[Language] = Field(default_factory=list)
    sources: list[Source] = Field(default_factory=list)
    rights: list[RightsRecord] = Field(default_factory=list)
    versions: list[SourceVersion] = Field(default_factory=list)
    structure: list[StructureNode] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)
    amendments: list[Amendment] = Field(default_factory=list)
    translations: list[Translation] = Field(default_factory=list)

    @model_validator(mode="after")
    def integrity(self):
        indexes = {}
        for name in type(self).model_fields:
            rows = getattr(self, name)
            indexes[name] = {r.id: r for r in rows}
            if len(indexes[name]) != len(rows):
                raise ValueError(f"duplicate IDs in {name}")

        def require(table, key):
            if key not in indexes[table]:
                raise ValueError(f"missing {table}: {key}")
            return indexes[table][key]

        for j in self.jurisdictions:
            if j.parent_id:
                require("jurisdictions", j.parent_id)
            for language in j.languages:
                require("languages", language)
        for a in self.authorities:
            require("jurisdictions", a.jurisdiction_id)
        for s in self.sources:
            require("jurisdictions", s.jurisdiction_id)
            require("authorities", s.authority_id)
            require("languages", s.language)
        seen = set()
        for r in self.rights:
            require("sources", r.source_id)
            if r.source_id in seen:
                raise ValueError("only one active rights decision per source")
            seen.add(r.source_id)
        for v in self.versions:
            require("sources", v.source_id)
            require("languages", v.language)
        for n in self.structure:
            v = require("versions", n.version_id)
            if n.source_id != v.source_id or n.source_hash != v.content_hash:
                raise ValueError("structure provenance mismatch")
            raw = v.canonical_text.encode()
            if n.end_byte > len(raw):
                raise ValueError("offset outside canonical text")
            raw[: n.start_byte].decode("utf-8")
            raw[: n.end_byte].decode("utf-8")
            if n.parent_id:
                parent = require("structure", n.parent_id)
                if parent.version_id != n.version_id or not (
                    parent.start_byte <= n.start_byte <= n.end_byte <= parent.end_byte
                ):
                    raise ValueError("invalid structure parent")
                visited = {n.id}
                while parent:
                    if parent.id in visited:
                        raise ValueError("structure cycle")
                    visited.add(parent.id)
                    parent = require("structure", parent.parent_id) if parent.parent_id else None
        for c in self.citations:
            require("sources", c.source_id)
            if c.version_id and require("versions", c.version_id).source_id != c.source_id:
                raise ValueError("citation version mismatch")
            if c.node_id:
                n = require("structure", c.node_id)
                if n.source_id != c.source_id or n.version_id != c.version_id:
                    raise ValueError("citation anchor mismatch")
        for a in self.amendments:
            require("sources", a.source_id)
            require("sources", a.target_source_id)
        for t in self.translations:
            original = require("versions", t.source_version_id)
            translated = require("versions", t.translated_version_id)
            require("languages", t.language)
            if original.source_id != translated.source_id or translated.language != t.language:
                raise ValueError("translation provenance mismatch")
        return self
