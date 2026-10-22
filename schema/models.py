"""Canonical contracts; all offsets are UTF-8 byte offsets into normalized text."""

from datetime import date
from enum import StrEnum
from hashlib import sha256
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, HttpUrl, StringConstraints, model_validator

from schema.record import EXPRESSION_AUTHORITY, LEGAL_EFFECTS

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
    # Optional only for the continent and the regional bodies (AKN "aa-…"), which are no country (DAT-02).
    country_code: Annotated[str, Field(pattern=r"^[A-Z]{2}$")] | None = None
    parent_id: ID | None = None
    legal_systems: list[str] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def coded(self):
        if self.country_code is None and not (self.id == "aa" or self.id.startswith("aa-")):
            raise ValueError(f"{self.id}: a country, or a place within one, has an ISO country code")
        return self


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


class StatedEvidence(Model):
    """Where a reviewed detail is stated (schema.record.Evidence): a page and the words of the document,
    the address of another instrument that states it, or the reviewer who recorded it."""

    source: Literal["document", "reviewer", "registry", "manifest", "external"]
    page: int | None = Field(default=None, ge=1)
    quote: Annotated[str, Field(min_length=1, max_length=400)] | None = None
    url: HttpUrl | None = None


class Edition(Model):
    """What one language edition of a work is worth as law, as its reviewed record states it (API-02,
    DAT-07), with where that is stated when recorded."""

    language: ID
    authority: Literal[*EXPRESSION_AUTHORITY]
    evidence: StatedEvidence | None = None


class LanguageRule(Model):
    """The work's own rule for divergence between its language versions (LNG-03), from its reviewed
    record: ``stated``, with the rule as stated, the language whose version prevails when it names one,
    where it is stated and the evidence; ``not_stated``, the reviewed copy states none;
    ``not_applicable``, as the reviewer recorded."""

    status: Literal["stated", "not_stated", "not_applicable"]
    rule: Nonempty | None = None
    prevails: ID | None = None
    provision: Nonempty | None = None
    evidence: StatedEvidence | None = None

    @model_validator(mode="after")
    def stated_with_evidence(self):
        if (self.status == "stated") != (self.rule is not None and self.evidence is not None):
            raise ValueError("a stated language rule has its words and evidence; no other has either")
        if self.status != "stated" and (self.prevails or self.provision):
            raise ValueError("only a stated language rule names a prevailing version or a provision")
        return self


class StatedDate(Model):
    """A reviewed date as its record gives it (schema.record.Dated): the date, how precise it is, and as
    printed when the record keeps that. A date with no normalised value is never compared."""

    value: date | None = None
    precision: Literal["day", "month", "year"] = "day"
    printed: Nonempty | None = None

    @model_validator(mode="after")
    def stated(self):
        if self.value is None and self.printed is None:
            raise ValueError("a date needs its value or its printed form")
        return self


class RegionalEffect(Model):
    """A regional instrument's legal effect, from its reviewed record (JUR-04, DAT-03): the organ that
    adopted it, its effect, the member states it binds (none listed: every member) and the date by which
    members must transpose it, when one is set."""

    organ: Nonempty
    legal_effect: Literal[*LEGAL_EFFECTS]
    member_states: list[ID] = Field(default_factory=list)
    transposition_deadline: date | None = None


class PartyStatus(Model):
    """One party to a treaty as its reviewed record lists it: its signature, ratification, accession and
    denunciation, each dated, and its reservations. Signature alone never binds (COR-01)."""

    party: Nonempty
    place: ID | None = None
    signed: StatedDate | None = None
    ratified: StatedDate | None = None
    acceded: StatedDate | None = None
    denounced: StatedDate | None = None
    reservations: Nonempty | None = None


class TreatyStatus(Model):
    """A treaty's status, from its reviewed record (JUR-04, DAT-03): who adopted it, its depositary, when it
    entered into force (null when the record states no single date), its authentic languages and its
    parties. No party listed means none is recorded, never that it has none. ``parties_complete_as_of`` is
    the date the party list is complete as of, with where that is shown (the depositary's status list);
    without it, a state missing from the list is unknown, never not a party."""

    adopted_by: Nonempty | None = None
    depositary: Nonempty | None = None
    entered_into_force: StatedDate | None = None
    authentic_languages: list[ID] = Field(default_factory=list)
    parties: list[PartyStatus] = Field(default_factory=list)
    # Left out of the row when not recorded, so rows stored before them are unchanged (DAT-08).
    parties_complete_as_of: date | None = Field(default=None, exclude_if=lambda value: value is None)
    parties_complete_evidence: StatedEvidence | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @model_validator(mode="after")
    def complete_with_evidence(self):
        if (self.parties_complete_as_of is None) != (self.parties_complete_evidence is None):
            raise ValueError("a party list complete as of a date says where that is shown, and only then")
        return self


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
    # Public reading aids: what the instrument is, in a sentence, and the year it was made. The
    # catalogue note above is a public caveat about the record, never an internal instruction.
    summary: Nonempty | None = None
    year: int | None = Field(default=None, ge=1800, le=2200)
    # What each language edition is worth as law, and the work's own rule where they differ, from its
    # reviewed record (API-02, DAT-07, LNG-03); an edition not listed is of unknown authority. Left out
    # of the row when not recorded, so rows stored before them are unchanged (DAT-08).
    editions: list[Edition] = Field(default_factory=list, exclude_if=lambda value: not value)
    language_rule: LanguageRule | None = Field(default=None, exclude_if=lambda value: value is None)
    # A regional instrument's legal effect and a treaty's status, from its reviewed record (JUR-04); left
    # out of the row for every other record, so rows stored before them are unchanged (DAT-08).
    regional: RegionalEffect | None = Field(default=None, exclude_if=lambda value: value is None)
    treaty: TreatyStatus | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def one_authority_per_edition(self):
        if len({e.language.casefold() for e in self.editions}) != len(self.editions):
            raise ValueError("one authority per language edition")
        return self


class SourceVersion(Model):
    id: ID
    source_id: ID
    canonical_url: HttpUrl
    retrieved_at: AwareDatetime
    raw_hash: Hash
    content_hash: Hash
    # Exactly as built: OA-text-1 texts end with one line feed, which stripping would remove (TXP-02).
    canonical_text: Annotated[str, StringConstraints(strip_whitespace=False)]
    parser: Nonempty
    language: ID = "en"
    effective_from: date | None = None
    effective_until: date | None = None
    verification_status: Literal["pending", "verified", "rejected"] = "pending"
    content_verifier: Nonempty | None = None
    content_verified_at: AwareDatetime | None = None
    # How the text was built from reviewed pages (TXP-02), and the version of the same work it
    # corrects or replaces (TXP-04); absent for texts published before either existed.
    serialization_profile: Nonempty | None = None
    prior_version_id: ID | None = None

    @model_validator(mode="after")
    def verify_hash(self):
        if sha256(self.canonical_text.encode()).hexdigest() != self.content_hash:
            raise ValueError("canonical text hash mismatch")
        if self.effective_from and self.effective_until and self.effective_until < self.effective_from:
            raise ValueError("invalid effective-date interval")
        return self


NodeKind = Literal[
    "document",
    "preamble",
    "contents",
    "contents_entry",
    "notice",
    "title",
    "part",
    "chapter",
    "division",
    "article",
    "section",
    "subsection",
    "definition",
    "paragraph",
    "subparagraph",
    "schedule",
    "annex",
    "table",
    "quotation",
    "opinion",
    "order",
]


class StructureNode(Model):
    id: ID
    source_id: ID
    version_id: ID
    parent_id: ID | None = None
    kind: NodeKind
    locator: Nonempty
    order: int = Field(ge=0)
    start_byte: int = Field(ge=0)
    end_byte: int = Field(ge=0)
    page: int | None = Field(default=None, ge=1)  # the page the node starts on
    source_hash: Hash  # the content_hash of the whole text the node is in
    # Reading and quotation aids (TXP-03, DAT-05/06): the printed number and heading, kept apart from
    # the locator; the last page the node reaches; SHA-256 of the node's own bytes [start, end); and,
    # for a contents entry, the node it lists.
    label: str | None = None
    heading: str | None = None
    page_end: int | None = Field(default=None, ge=1)
    span_hash: Hash | None = None
    target_node_id: ID | None = None

    @model_validator(mode="after")
    def ordered(self):
        if self.end_byte < self.start_byte:
            raise ValueError("end_byte precedes start_byte")
        if self.page and self.page_end and self.page_end < self.page:
            raise ValueError("page_end precedes page")
        return self


class Citation(Model):
    id: ID
    source_id: ID
    value: Nonempty
    version_id: ID | None = None
    node_id: ID | None = None


class Amendment(Model):
    """A link between two works, from the asserting (later) one to the one it concerns (EDT-01)."""

    id: ID
    source_id: ID
    target_source_id: ID
    kind: Literal[
        "amends",
        "repeals",
        "supersedes",
        "made_under",
        "cross_reference",
        "commences",
        "transposes",
        "domesticates",
        "implements",
        "ratifies",
        "cites",
    ]
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
            if v.prior_version_id:
                prior = require("versions", v.prior_version_id)
                if prior.id == v.id or prior.source_id != v.source_id:
                    raise ValueError("prior version must be another version of the same source")
        encoded: dict[str, bytes] = {}
        for n in self.structure:
            v = require("versions", n.version_id)
            if n.source_id != v.source_id or n.source_hash != v.content_hash:
                raise ValueError("structure provenance mismatch")
            raw = encoded.setdefault(v.id, v.canonical_text.encode())
            if n.end_byte > len(raw):
                raise ValueError("offset outside canonical text")
            raw[: n.start_byte].decode("utf-8")
            raw[: n.end_byte].decode("utf-8")
            if n.span_hash and sha256(raw[n.start_byte : n.end_byte]).hexdigest() != n.span_hash:
                raise ValueError("span hash mismatch")
            if n.target_node_id and require("structure", n.target_node_id).version_id != n.version_id:
                raise ValueError("a contents entry must point into its own version")
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
